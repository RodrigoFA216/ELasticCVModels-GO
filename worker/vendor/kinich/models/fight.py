"""FightDetector — port de FightDetector_Production.py al contrato Kinich.

Lógica de detección intacta respecto al original (arconte):
  YOLO track (bytetrack, solo personas) -> pares candidatos por heurística
  espacial (proximidad + IoU + movimiento) -> zonas de pelea por par de ids
  (máx 3, con sticky y estabilizador de encuadre) -> buffer de crops ->
  experto CLIP (KSM por actividad + mosaico temporal 3x2) -> EMA + fast-track
  + consecutivos para confirmar

Eliminado del original: display cv2 (overlays/banner/info bar), guardado de
alertas jpg / snapshot live / grabaciones, CLI, resumen final, worker de CLIP
y worker de imwrite (procesamiento inline en el thread del stream).
Cambios: CLIP vía open_clip (el repo `clip` de OpenAI está archivado) con
features de texto precomputadas en load(); el tracker bytetrack se instancia
por sesión (BYTETracker) con el YOLO compartido, porque `YOLO.track(persist=
True)` guarda el estado del tracker dentro del objeto YOLO y mezclaría IDs
entre streams; el contador de ids de zona es por sesión (en el original era
de clase, global al proceso).
"""
import logging
import os
from typing import Any

import cv2
import numpy as np
import open_clip
import torch
from PIL import Image
from ultralytics import YOLO

from kinich.contract import Capability, CVModel, Event, ModelInfo, StreamSession

log = logging.getLogger(__name__)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
WEIGHTS = os.environ.get("KINICH_FIGHT_WEIGHTS", "weights/yolo11x.pt")
# -quickgelu: los pesos openai usan QuickGELU; sin el sufijo open_clip
# construye GELU estándar y los scores divergen del script original.
# El original usaba ViT-L/14 por defecto ("ViT-L-14-quickgelu" en open_clip).
CLIP_MODEL_NAME = os.environ.get("KINICH_FIGHT_CLIP_MODEL", "ViT-B-32-quickgelu")
TRACKER_YAML = "bytetrack.yaml"  # config builtin de ultralytics

# =============================================================================
# CONFIG (constantes del original, sin cambios de valor)
# =============================================================================
CLASS_PERSON = 0

# ── Heurística espacial ──
F_DIST_MAX_LIMIT = 300
F_MIN_IOU_CRITICAL = 0.01
F_STICKY_FRAMES = 25
F_PAD_PIXELS = 100
F_PAD_CLOSE = 160

# ── Gate de movimiento ──
F_MIN_MOVEMENT_FOR_CLIP = 2.0

# ── CLIP buffer y umbrales ──
F_BUFFER_SIZE = 6
F_SET_SCORE_TH = 3.8
F_CONSEC_MIN = 3
F_SMOOTH_FACTOR = 0.70
F_FAST_TRACK_TH = 6.0

# ── KSM ──
KSM_SELECT = 6

# ── Mosaico temporal ──
CLIP_FRAME_SIZE = (128, 128)
MOSAIC_COLS = 3
MOSAIC_ROWS = 2

# ── Multitud ──
CROWD_PERSON_TH = 5

# ── Multi-ROI ──
ZONE_MAX = 3
ZONE_IDLE_MAX = 45
ZONE_MIN_PAIR_SCORE = 28

# ── Estabilizador de zona ──
STAB_EMA_ALPHA = 0.25
STAB_IOU_TH = 0.05
STAB_PENDING_MAX = 4

FIGHT_POS_PROMPTS = [
    "two people throwing punches and hitting each other hard",
    "violent street brawl with people kicking and punching aggressively",
    "two men fighting violently on the street throwing fists",
    "person striking another person with a punch or kick attack",
    "people wrestling aggressively on the ground fighting",
    "violent physical assault with hitting kicking and striking",
    "street fight with people grabbing and hitting each other",
    "two people in a violent altercation throwing blows at each other",
]
FIGHT_NEG_PROMPTS = [
    "two people shaking hands and greeting each other calmly",
    "friends having a calm conversation standing close together",
    "two people hugging each other warmly and peacefully",
    "people standing still and talking face to face quietly",
    "two persons walking slowly side by side on the street",
    "pedestrians standing near each other waiting calmly",
    "people posing together for a photo smiling",
    "two friends meeting and greeting with a handshake or hug",
    "crowd of people walking together in a busy public area",
    "group of people moving through a crowded street normally",
    "people passing each other in a busy pedestrian zone",
    "bystanders standing around in a public space doing nothing",
    "people walking in different directions in a crowd",
    "two people standing close together in a group of people",
    "large crowd of pedestrians moving normally through a plaza",
    "many people walking and going about their daily routine outdoors",
    "busy street scene with lots of people going in different directions",
    "people accidentally bumping into each other while walking in a crowd",
    "dense crowd at a market or public space walking around normally",
    "people standing close together waiting in a line or queue patiently",
    "group of thirty people moving through a busy public area calmly",
    "crowd scene with many pedestrians walking side by side peacefully",
]

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "fight_set_score_th": {
            "type": "number", "default": F_SET_SCORE_TH,
            "description": "umbral del score EMA para contar positivo",
        },
        "fight_consec_min": {
            "type": "integer", "default": F_CONSEC_MIN,
            "description": "batches CLIP positivos consecutivos para confirmar pelea",
        },
        "zone_max": {
            "type": "integer", "default": ZONE_MAX, "minimum": 1,
            "description": "zonas de pelea simultáneas máximas",
        },
        "fight_min_movement": {
            "type": "number", "default": F_MIN_MOVEMENT_FOR_CLIP,
            "description": "movimiento mínimo del par para alimentar el buffer CLIP",
        },
    },
}


# =============================================================================
# CROP ZONE STABILIZER (idéntico al original, sin el crop de display)
# =============================================================================
class CropZoneStabilizer:
    """EMA del bbox de zona: mientras el encuadre salta encola los frames y
    los libera en bloque cuando se estabiliza (o al llenarse la cola)."""

    def __init__(self) -> None:
        self._smooth: np.ndarray | None = None
        self._state = "stable"
        self._queue: list[np.ndarray] = []

    @staticmethod
    def _iou(a, b) -> float:
        ax1, ay1, ax2, ay2 = float(a[0]), float(a[1]), float(a[2]), float(a[3])
        bx1, by1, bx2, by2 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        a1 = (ax2 - ax1) * (ay2 - ay1)
        a2 = (bx2 - bx1) * (by2 - by1)
        return inter / (a1 + a2 - inter + 1e-6)

    def update(self, bbox_new, clip_frame: np.ndarray) -> list[np.ndarray]:
        """Devuelve los clip-frames a volcar al buffer (puede ser vacío)."""
        arr = np.array(bbox_new, dtype=float)
        if self._smooth is None:
            self._smooth = arr.copy()
            return [clip_frame]
        iou = self._iou(arr, self._smooth)
        if self._state == "stable":
            if iou >= STAB_IOU_TH:
                self._smooth = STAB_EMA_ALPHA * arr + (1.0 - STAB_EMA_ALPHA) * self._smooth
                return [clip_frame]
            self._state = "pending"
            self._queue = [clip_frame]
            return []
        else:
            if self._iou(arr, self._smooth) >= STAB_IOU_TH:
                flush = self._queue + [clip_frame]
                self._smooth = STAB_EMA_ALPHA * arr + (1.0 - STAB_EMA_ALPHA) * self._smooth
                self._state = "stable"
                self._queue = []
                return flush
            self._queue.append(clip_frame)
            if len(self._queue) >= STAB_PENDING_MAX:
                # igual que el original: el frame actual ya está en la cola y
                # se agrega de nuevo al flush
                flush = self._queue + [clip_frame]
                self._smooth = arr.copy()
                self._state = "stable"
                self._queue = []
                return flush
            return []

    def reset(self) -> None:
        self._smooth = None
        self._state = "stable"
        self._queue = []


# =============================================================================
# FIGHT ZONE
# =============================================================================
class FightZone:
    """Zona candidata: par de track-ids + buffer de crops + score EMA."""

    def __init__(self, zone_id: int, pair_ids: tuple[int, int],
                 set_score_th: float, consec_min: int):
        self.zone_id = zone_id
        self.pair_ids = tuple(sorted(pair_ids))
        self.set_score_th = set_score_th
        self.consec_min = consec_min

        self.buffer: list[np.ndarray] = []
        self.stabilizer = CropZoneStabilizer()
        self.smoothed_score = 0.0
        self.consec_pos = 0.0
        self.is_confirmed = False
        self.clips_analyzed = 0
        self.last_raw = 0.0

        self.idle_frames = 0
        self.last_bbox: tuple[int, int, int, int] | None = None
        self.created_frame = 0
        self.last_fed_frame = 0

    def matches(self, p1: int, p2: int) -> bool:
        return tuple(sorted((p1, p2))) == self.pair_ids

    def update_score(self, raw: float) -> None:
        self.last_raw = raw
        self.clips_analyzed += 1
        if raw >= F_FAST_TRACK_TH:
            self.smoothed_score = raw
            self.consec_pos = 10
        else:
            self.smoothed_score = (F_SMOOTH_FACTOR * raw +
                                   (1.0 - F_SMOOTH_FACTOR) * self.smoothed_score)
            if self.smoothed_score > self.set_score_th:
                self.consec_pos = min(6, self.consec_pos + 1)
            else:
                self.consec_pos = max(0, self.consec_pos - 1)
        self.is_confirmed = self.consec_pos >= self.consec_min

    @property
    def should_discard(self) -> bool:
        if self.is_confirmed and self.smoothed_score > 1.0:
            return False
        return self.idle_frames > ZONE_IDLE_MAX


# =============================================================================
# EXPERTO CLIP (features de texto precomputadas por el modelo; stateless)
# =============================================================================
class _FightTextFeats:
    def __init__(self, clip_model, tokenizer):
        with torch.no_grad():
            pf = clip_model.encode_text(tokenizer(FIGHT_POS_PROMPTS).to(DEVICE)).float()
            nf = clip_model.encode_text(tokenizer(FIGHT_NEG_PROMPTS).to(DEVICE)).float()
            # igual que el original: media de embeddings normalizados,
            # sin renormalizar la media
            self.pos = (pf / pf.norm(dim=-1, keepdim=True)).mean(0, keepdim=True)
            self.neg = (nf / nf.norm(dim=-1, keepdim=True)).mean(0, keepdim=True)
        self.logit_scale = float(clip_model.logit_scale.exp().item())


def _ksm_select(frames: list[np.ndarray]) -> list[np.ndarray]:
    """Selecciona los KSM_SELECT frames de mayor actividad (diff de grises)."""
    valid = [(i, f) for i, f in enumerate(frames) if f is not None and f.size > 0]
    if len(valid) <= KSM_SELECT:
        return [f for _, f in valid]
    activity = []
    for k, (i, f) in enumerate(valid):
        if k == 0:
            activity.append(0.0)
        else:
            prev = cv2.cvtColor(valid[k - 1][1], cv2.COLOR_BGR2GRAY).astype(np.float32)
            curr = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
            activity.append(float(np.mean(np.abs(curr - prev))))
    if len(activity) > 1:
        activity[0] = activity[1]
    top = sorted(sorted(range(len(activity)), key=lambda k: activity[k], reverse=True)[:KSM_SELECT])
    return [valid[k][1] for k in top]


def _build_mosaic(frames: list[np.ndarray]) -> np.ndarray:
    """Mosaico temporal 3x2 con los key-frames (celdas negras si faltan)."""
    cw, ch = CLIP_FRAME_SIZE
    canvas = np.zeros((ch * MOSAIC_ROWS, cw * MOSAIC_COLS, 3), dtype=np.uint8)
    for idx in range(MOSAIC_ROWS * MOSAIC_COLS):
        r, c = idx // MOSAIC_COLS, idx % MOSAIC_COLS
        if idx < len(frames) and frames[idx] is not None and frames[idx].size > 0:
            cell = cv2.resize(frames[idx], (cw, ch))
        else:
            cell = np.zeros((ch, cw, 3), dtype=np.uint8)
        canvas[r * ch:(r + 1) * ch, c * cw:(c + 1) * cw] = cell
    return canvas


class CLIPFightExpert:
    def __init__(self, clip_model, preprocess, feats: _FightTextFeats):
        self.model = clip_model
        self.preprocess = preprocess
        self.feats = feats

    @torch.no_grad()
    def predict(self, frames: list[np.ndarray]) -> float:
        if not frames:
            return 0.0
        key_frames = _ksm_select(frames)
        mosaic = _build_mosaic(key_frames)
        pil = Image.fromarray(cv2.cvtColor(mosaic, cv2.COLOR_BGR2RGB))
        img = self.preprocess(pil).unsqueeze(0).to(DEVICE)
        feat = self.model.encode_image(img).float()
        feat /= feat.norm(dim=-1, keepdim=True)
        return float(self.feats.logit_scale *
                     (feat @ self.feats.pos.T - feat @ self.feats.neg.T).item())


# =============================================================================
# TRACKER (YOLO compartido del modelo padre; estado bytetrack por sesión)
# =============================================================================
class _SessionTracker:
    """Equivalente a YOLO.track(persist=True, tracker="bytetrack.yaml") del
    original, pero con el BYTETracker instanciado por sesión para no mezclar
    IDs entre streams."""

    def __init__(self, yolo: YOLO):
        from kinich.models._tracking import make_bytetracker

        self._yolo = yolo
        self._tracker = make_bytetracker(TRACKER_YAML)

    def process(self, frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Devuelve (person_xyxy Nx4, person_ids N) del frame."""
        empty = (np.zeros((0, 4), dtype=np.float32), np.zeros((0,), dtype=np.int32))
        # conf=0.1: mismo default que usa Model.track (bytetrack necesita
        # detecciones de baja confianza para la asociación de segunda etapa)
        res = self._yolo.predict(frame, classes=[CLASS_PERSON], conf=0.1, verbose=False)[0]
        if res.boxes is None or len(res.boxes) == 0:
            return empty
        tracks = self._tracker.update(res.boxes.cpu().numpy(), frame)
        if len(tracks) == 0:
            return empty
        # columnas: x1, y1, x2, y2, track_id, conf, cls, idx
        return tracks[:, :4].astype(np.float32), tracks[:, 4].astype(np.int32)


# =============================================================================
# SESIÓN (port del loop de run() + _process_fight, inline y sin display)
# =============================================================================
class _FightSession(StreamSession):
    def __init__(self, model: "FightDetector", config: dict[str, Any]):
        self.model = model
        self.set_score_th = float(config.get("fight_set_score_th", F_SET_SCORE_TH))
        self.consec_min = int(config.get("fight_consec_min", F_CONSEC_MIN))
        self.zone_max = int(config.get("zone_max", ZONE_MAX))
        self.min_movement = float(config.get("fight_min_movement", F_MIN_MOVEMENT_FOR_CLIP))

        self.tracker = _SessionTracker(model.yolo)
        self.expert = CLIPFightExpert(model.clip_model, model.clip_preprocess, model.fight_feats)

        self.fight_zones: list[FightZone] = []
        self.f_prev_boxes: dict[int, np.ndarray] = {}
        self.zone_sticky: dict[int, int] = {}
        self.zone_last_valid: dict[int, tuple] = {}
        self._zone_seq = 0
        self.f_idx = 0

        # transiciones confirmed on/off por zona (para eventos)
        self._prev_confirmed: set[int] = set()
        self._confirmed_scores: dict[int, float] = {}

    # ── helpers espaciales (idénticos al original) ───────────────────────────
    @staticmethod
    def _iou(b1, b2) -> float:
        xA, yA = max(b1[0], b2[0]), max(b1[1], b2[1])
        xB, yB = min(b1[2], b2[2]), min(b1[3], b2[3])
        inter = max(0.0, xB - xA) * max(0.0, yB - yA)
        a1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
        a2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
        return inter / (a1 + a2 - inter + 1e-6)

    @staticmethod
    def _center_dist(b1, b2) -> float:
        cx1 = (b1[0] + b1[2]) / 2.0
        cy1 = (b1[1] + b1[3]) / 2.0
        cx2 = (b2[0] + b2[2]) / 2.0
        cy2 = (b2[1] + b2[3]) / 2.0
        return float(np.sqrt((cx1 - cx2) ** 2 + (cy1 - cy2) ** 2))

    def _movement_score(self, p_id: int, box_p) -> float:
        prev = self.f_prev_boxes.get(p_id)
        if prev is None:
            return 0.0
        return (1.0 - self._iou(box_p, prev)) * 50.0

    def _score_pair(self, b1, b2, p1: int, p2: int) -> float:
        iou = self._iou(b1, b2)
        dist = self._center_dist(b1, b2)
        prox = max(0.0, 1.0 - dist / F_DIST_MAX_LIMIT) * 40.0
        mov = (self._movement_score(p1, b1) + self._movement_score(p2, b2)) / 2.0
        return prox + iou * 100.0 + mov

    @staticmethod
    def _union_bbox(b1, b2, pad: int, W: int, H: int) -> tuple[int, int, int, int]:
        x1 = int(max(0, min(b1[0], b2[0]) - pad))
        y1 = int(max(0, min(b1[1], b2[1]) - pad))
        x2 = int(min(W, max(b1[2], b2[2]) + pad))
        y2 = int(min(H, max(b1[3], b2[3]) + pad))
        return x1, y1, x2, y2

    # ── gestión de zonas ─────────────────────────────────────────────────────
    def _find_or_create_zone(self, p1: int, p2: int, frame_idx: int) -> FightZone | None:
        for z in self.fight_zones:
            if z.matches(p1, p2):
                return z
        if len(self.fight_zones) >= self.zone_max:
            return None
        self._zone_seq += 1
        z = FightZone(self._zone_seq, (p1, p2), self.set_score_th, self.consec_min)
        z.created_frame = frame_idx
        self.fight_zones.append(z)
        log.debug("nueva zona z%d ids=(%d,%d) total=%d",
                  z.zone_id, p1, p2, len(self.fight_zones))
        return z

    def _prune_zones(self) -> None:
        self.fight_zones = [z for z in self.fight_zones if not z.should_discard]

    def _run_zone_batch(self, zone: FightZone) -> None:
        # ponytail: scoring CLIP inline en el thread del stream (un mosaico
        # 384x256 por batch de 6 crops); si bloquea demasiado, mover a worker
        raw = self.expert.predict(list(zone.buffer))
        zone.update_score(raw)
        log.debug("[z%d %s] ema=%.2f raw=%.2f consec=%.1f/%d clips=%d",
                  zone.zone_id, zone.pair_ids, zone.smoothed_score, raw,
                  zone.consec_pos, zone.consec_min, zone.clips_analyzed)

    def _feed_zone(self, zone: FightZone, bbox, raw_crop: np.ndarray) -> None:
        """Estabiliza el encuadre y vuelca los frames liberados al buffer;
        dispara el batch CLIP al llenarse (inline, sin worker)."""
        clip_frame = cv2.resize(raw_crop, CLIP_FRAME_SIZE)
        for cf in zone.stabilizer.update(bbox, clip_frame):
            zone.buffer.append(cf)
            if len(zone.buffer) >= F_BUFFER_SIZE:
                self._run_zone_batch(zone)
                zone.buffer = []

    # ── pipeline principal (idéntico al original, sin display/imwrite) ───────
    def _process_fight(self, frame: np.ndarray, person_xyxy, person_ids,
                       frame_idx: int) -> None:
        n = len(person_xyxy)
        H, W = frame.shape[:2]

        candidatos = []
        for i in range(n):
            for j in range(i + 1, n):
                b1, b2 = person_xyxy[i], person_xyxy[j]
                p1 = int(person_ids[i]) if i < len(person_ids) else -1
                p2 = int(person_ids[j]) if j < len(person_ids) else -1
                iou = self._iou(b1, b2)
                dist = self._center_dist(b1, b2)
                if dist < F_DIST_MAX_LIMIT or iou > F_MIN_IOU_CRITICAL:
                    sc = self._score_pair(b1, b2, p1, p2)
                    candidatos.append((sc, iou, dist, i, j, p1, p2))
        candidatos.sort(key=lambda x: x[0], reverse=True)

        # igual que el original: f_prev_boxes se actualiza al frame actual
        # ANTES del loop de candidatos (el gate de movimiento de abajo compara
        # contra el mismo frame)
        ids_visibles = set()
        for i, box_p in enumerate(person_xyxy):
            p_id = int(person_ids[i]) if i < len(person_ids) else -1
            self.f_prev_boxes[p_id] = box_p.copy()
            ids_visibles.add(p_id)
        self.f_prev_boxes = {k: v for k, v in self.f_prev_boxes.items() if k in ids_visibles}

        active_pair_keys = set()

        for cand in candidatos:
            sc, iou, dist, i, j, p1, p2 = cand
            b1, b2 = person_xyxy[i], person_xyxy[j]
            if p1 == -1 or p2 == -1:
                continue
            if sc < ZONE_MIN_PAIR_SCORE:
                continue
            zone = self._find_or_create_zone(p1, p2, frame_idx)
            if zone is None:
                continue

            active_pair_keys.add(zone.zone_id)
            zone.idle_frames = 0
            zone.last_fed_frame = frame_idx

            pad = F_PAD_CLOSE if iou > 0.05 else F_PAD_PIXELS
            x1, y1, x2, y2 = self._union_bbox(b1, b2, pad, W, H)
            raw_crop = frame[y1:y2, x1:x2]
            if raw_crop is None or raw_crop.size == 0:
                continue

            zone.last_bbox = (x1, y1, x2, y2)

            mov1 = self._movement_score(p1, b1)
            mov2 = self._movement_score(p2, b2)
            if (mov1 + mov2) / 2.0 < self.min_movement:
                zone.buffer = []
                self.zone_sticky[zone.zone_id] = F_STICKY_FRAMES
                self.zone_last_valid[zone.zone_id] = (b1.copy(), b2.copy(), pad)
                continue

            self.zone_sticky[zone.zone_id] = F_STICKY_FRAMES
            self.zone_last_valid[zone.zone_id] = (b1.copy(), b2.copy(), pad)
            self._feed_zone(zone, (x1, y1, x2, y2), raw_crop)

        # zonas sin par activo: retención por multitud, sticky o decaimiento
        is_crowd = n > CROWD_PERSON_TH
        for z in self.fight_zones:
            if z.zone_id in active_pair_keys:
                continue
            if is_crowd and z.last_bbox is not None:
                zx1, zy1, zx2, zy2 = z.last_bbox
                persons_in_zone = sum(
                    1 for box in person_xyxy
                    if zx1 <= (box[0] + box[2]) / 2 <= zx2
                    and zy1 <= (box[1] + box[3]) / 2 <= zy2
                )
                if persons_in_zone >= 2:
                    z.idle_frames = 0
                    continue
            sticky = self.zone_sticky.get(z.zone_id, 0)
            if sticky > 0 and z.zone_id in self.zone_last_valid:
                b1, b2, pad = self.zone_last_valid[z.zone_id]
                x1, y1, x2, y2 = self._union_bbox(b1, b2, pad, W, H)
                raw_crop = frame[y1:y2, x1:x2]
                if raw_crop is not None and raw_crop.size > 0:
                    self._feed_zone(z, (x1, y1, x2, y2), raw_crop)
                self.zone_sticky[z.zone_id] = sticky - 1
                active_pair_keys.add(z.zone_id)
            else:
                z.idle_frames += 1
                if z.idle_frames > 5:
                    z.consec_pos = max(0, z.consec_pos - 0.1)
                    z.is_confirmed = z.consec_pos >= z.consec_min

        self._prune_zones()

    # ── frame -> eventos ─────────────────────────────────────────────────────
    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        events: list[Event] = []
        person_xyxy, person_ids = self.tracker.process(frame)
        self._process_fight(frame, person_xyxy, person_ids, self.f_idx)
        self.f_idx += 1

        cur_confirmed = {z.zone_id: z for z in self.fight_zones if z.is_confirmed}
        for zid, z in cur_confirmed.items():
            self._confirmed_scores[zid] = z.smoothed_score

        for zid in sorted(set(cur_confirmed) - self._prev_confirmed):
            z = cur_confirmed[zid]
            events.append(Event(
                type="FIGHT", score=z.smoothed_score, ts=ts,
                extra={"zone_id": zid, "pair_ids": list(z.pair_ids),
                       "raw": z.last_raw, "consec": z.consec_pos,
                       "clips": z.clips_analyzed,
                       "bbox": list(z.last_bbox) if z.last_bbox else None},
            ))
        for zid in sorted(self._prev_confirmed - set(cur_confirmed)):
            events.append(Event(
                type="FIGHT_OFF", score=self._confirmed_scores.pop(zid, 0.0),
                ts=ts, extra={"zone_id": zid},
            ))
        self._prev_confirmed = set(cur_confirmed)
        return events


# =============================================================================
# MODELO
# =============================================================================
class FightDetector(CVModel):
    info = ModelInfo(
        name="fight-detector",
        version="1.0",
        task="Detección de peleas en CCTV (YOLO track + zonas por par + CLIP mosaico temporal)",
        capabilities={Capability.CLIP, Capability.STREAM},
        event_types=["FIGHT", "FIGHT_OFF"],
        config_schema=CONFIG_SCHEMA,
        target_fps=20.0,  # frame_skip=0 en el original: todos los frames (~20 fps CCTV)
    )

    def __init__(self) -> None:
        self.yolo: YOLO | None = None
        self.clip_model = None
        self.clip_preprocess = None
        self.fight_feats: _FightTextFeats | None = None

    def load(self) -> None:
        log.info("Cargando YOLO fight: %s", WEIGHTS)
        self.yolo = YOLO(WEIGHTS)
        log.info("Cargando CLIP %s (openai) en %s", CLIP_MODEL_NAME, DEVICE)
        self.clip_model, _, self.clip_preprocess = open_clip.create_model_and_transforms(
            CLIP_MODEL_NAME, pretrained="openai", device=DEVICE)
        self.clip_model.eval()
        tokenizer = open_clip.get_tokenizer(CLIP_MODEL_NAME)
        self.fight_feats = _FightTextFeats(self.clip_model, tokenizer)

    def warmup(self) -> None:
        session = self.create_session({})
        session.process_frame(np.zeros((360, 640, 3), dtype=np.uint8), 0.0)
        session.close()

    def unload(self) -> None:
        self.yolo = self.clip_model = self.clip_preprocess = None
        self.fight_feats = None

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        if self.yolo is None:
            self.load()
        return _FightSession(self, config)
