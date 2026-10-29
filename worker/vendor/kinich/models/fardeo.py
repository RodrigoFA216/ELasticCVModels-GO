"""FardeoDetector — port de FardeoDetector_Production_V2.py al contrato Kinich.

Lógica de detección intacta respecto al original (pipeline event-triggered,
NO continuo como el de fuego):
  YOLO-pose (ByteTrack) → heurística mano-cerca-de-ROI (anaquel) → al SALIR la
  mano dispara un evento → se colectan 3 ventanas del MISMO track:
      W1 = evento        (24 frames pre-salida + 24 post-salida)
      W2 = ~1-3 s después (48 frames)
      W3 = ~3-5 s después (48 frames)
  → X-CLIP puntúa cada ventana (max(pos)-mean(neg)), combina con pesos
    (0.3, 0.4, 0.3), sigmoide + suavizado gaussiano + umbral → SOSPECHOSO.

Eliminado del original: display cv2 + overlay, selector gráfico de ROIs
(ahora llegan por config de sesión), imwrite de alertas + su worker, CLI,
prints (→ logging) y el worker asíncrono de X-CLIP (inline en el thread del
stream, que ya es el worker).

Desviaciones:
  - `transformers` (XCLIPModel/XCLIPProcessor) no está en las deps base de
    Kinich → import perezoso dentro de load().
  - Sin worker/queue, el gate `if not self._processing` y los descartes por
    queue.Full desaparecen: todo evento completado se puntúa (inline nada se
    pierde ni se solapa).
  - No se fuerza TRANSFORMERS_OFFLINE/HF_HUB_OFFLINE (rompería la primera
    descarga del modelo en el API; el operador puede exportarlas en edge).
  - ByteTrack vive DENTRO del objeto YOLO de ultralytics → cada sesión crea
    su propia instancia YOLO (pesos nano ~6 MB); el X-CLIP pesado sí se
    comparte a nivel modelo.
"""
from __future__ import annotations

import logging
import os
from collections import deque
from typing import Any

import cv2
import numpy as np
import torch
from PIL import Image
from scipy.ndimage import gaussian_filter1d
from ultralytics import YOLO

from kinich.contract import Capability, CVModel, Event, ModelInfo, StreamSession

log = logging.getLogger(__name__)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
WEIGHTS = os.environ.get("KINICH_FARDEO_WEIGHTS", "weights/yolov8n-pose.pt")
XCLIP_MODEL_ID = os.environ.get(
    "KINICH_FARDEO_XCLIP_MODEL", "microsoft/xclip-base-patch16-16-frames"
)

# =============================================================================
# CONFIG (constantes del original, sin cambios de valor)
# =============================================================================
XCLIP_NUM_FRAMES = 16

# ── Ventanas ─────────────────────────────────────────────────────────────────
SEG_XCLIP = 48                       # ventana W1 (24 pre-salida + 24 post-salida)
HALF = SEG_XCLIP // 2
WINDOW_POST = 48                     # frames por ventana W2 y W3
WINDOW_WEIGHTS = (0.3, 0.4, 0.3)     # (W1, W2, W3) — re-optimizados con prompts depurados
WINDOW_TIMEOUT = 48                  # si el track desaparece, finalizar con lo colectado
MIN_PRE_FRAMES = max(1, SEG_XCLIP // 4)   # presencia mínima para evento válido
MAX_PRE_FRAMES = SEG_XCLIP * 3            # tope antes de dispatch forzado

# ── Heurística mano-ROI ──────────────────────────────────────────────────────
PROXIMITY_PX = 35
CROP_PAD = 10
KP_CONF = 0.30

# ── Scoring X-CLIP ───────────────────────────────────────────────────────────
KF_SIGMOID_CENTER = 1.5
KF_SIGMOID_SCALE = 1.0
ALERT_THRESHOLD = 0.35
SMOOTH_SIGMA = 2.0
SMOOTH_HISTORY = 12

# ── Detección / alertas ──────────────────────────────────────────────────────
FARDEO_TTL = 90                      # frames que se mantiene la alerta tras dispararse

# ── Prompts depurados (validados con k-fold anidado) ─────────────────────────
POS_PROMPTS = [
    "person tucking a product under their hoodie or sweatshirt",
    "person putting an item in their pants pockets",
    "person placing store merchandise into a backpack",
    "person lifting their clothing to conceal a product against their body",
    "person inserting a retail item under their jacket near the waist",
]
NEG_PROMPTS = [
    "person reading a product label at store shelves",
    "person browsing retail products with hands visible",
    "person putting a product back on the shelf",
    "person reaching for an item on a store shelf",
]

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "regions": {
            "type": "array",
            "default": [],
            "description": (
                "polígonos ROI (anaqueles) en píxeles del frame: "
                "[[[x,y], [x,y], ...], ...]; sin ROIs la sesión no genera eventos"
            ),
            "items": {
                "type": "array",
                "minItems": 3,
                "items": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": 2,
                },
            },
        },
        "fardeo_alert_threshold": {
            "type": "number", "default": ALERT_THRESHOLD,
            "description": "umbral del score suavizado para marcar SOSPECHOSO",
        },
        "proximity_px": {
            "type": "integer", "default": PROXIMITY_PX,
            "description": "dilatación de la ROI en px para la heurística mano-anaquel",
        },
    },
}


# =============================================================================
# HEURÍSTICA MANO-ROI (idéntica al original, portada de fardeo_expert.py)
# =============================================================================
def _single_person_hand_mask(
    kps_xy: np.ndarray, kps_cf: np.ndarray, person_idx: int, H: int, W: int
) -> np.ndarray:
    """Máscara de brazos (cápsula codo→muñeca) de una persona."""
    mask = np.zeros((H, W), dtype=np.uint8)
    if person_idx >= len(kps_xy):
        return mask
    for elbow_idx, wrist_idx in ((7, 9), (8, 10)):   # COCO-17
        if float(kps_cf[person_idx, wrist_idx]) < KP_CONF:
            continue
        wrist = kps_xy[person_idx, wrist_idx].astype(np.float32)
        elbow = kps_xy[person_idx, elbow_idx].astype(np.float32)
        length = float(np.linalg.norm(wrist - elbow))
        radius = max(int(length * 0.25), 8)
        pt1 = tuple(elbow.astype(int).tolist())
        pt2 = tuple(wrist.astype(int).tolist())
        cv2.line(mask, pt1, pt2, 255, thickness=radius * 2, lineType=cv2.LINE_8)
        cv2.circle(mask, pt1, radius, 255, -1)
        cv2.circle(mask, pt2, radius, 255, -1)
    return mask


def _build_region_masks(
    regions: list, H: int, W: int, proximity_px: int = PROXIMITY_PX
) -> list[np.ndarray | None]:
    """Pre-computa máscaras dilatadas de cada ROI (una sola vez)."""
    out: list[np.ndarray | None] = []
    k = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * proximity_px + 1, 2 * proximity_px + 1)
    )
    for poly in regions:
        if len(poly) < 3:
            out.append(None)
            continue
        m = np.zeros((H, W), dtype=np.uint8)
        cv2.fillPoly(m, [np.array(poly, np.int32).reshape((-1, 1, 2))], 255)
        out.append(cv2.dilate(m, k) if proximity_px > 0 else m)
    return out


def _triggered_track_ids(
    kps_xy: np.ndarray, kps_cf: np.ndarray, tids: list[int],
    region_masks: list[np.ndarray | None], H: int, W: int,
) -> set[int]:
    """track_ids cuya mano (cápsula de brazo) solapa alguna ROI dilatada."""
    trig: set[int] = set()
    for i, tid in enumerate(tids):
        hm = _single_person_hand_mask(kps_xy, kps_cf, i, H, W)
        if hm.sum() == 0:
            continue
        for rm in region_masks:
            if rm is not None and cv2.bitwise_and(hm, rm).any():
                trig.add(tid)
                break
    return trig


def _union_crop(frame: np.ndarray, box, pad: int) -> np.ndarray | None:
    """Crop 224×224 del bbox de la persona expandido 5% + pad."""
    H, W = frame.shape[:2]
    px1, py1, px2, py2 = [float(v) for v in box]
    pw, ph = px2 - px1, py2 - py1
    x1 = max(0, int(px1 - pw * 0.05) - pad)
    y1 = max(0, int(py1 - ph * 0.05) - pad)
    x2 = min(W - 1, int(px2 + pw * 0.05) + pad)
    y2 = min(H - 1, int(py2 + ph * 0.05) + pad)
    if x2 <= x1 or y2 <= y1:
        return None
    roi = frame[y1:y2, x1:x2]
    return cv2.resize(roi, (224, 224)) if roi.size else None


def _sample_uniform(items: list, n: int) -> list:
    if not items:
        return []
    if len(items) >= n:
        return [items[i] for i in np.linspace(0, len(items) - 1, n, dtype=int)]
    out = list(items)
    while len(out) < n:
        out.append(out[-1])
    return out


def _parse_pose(res) -> tuple[list[int], np.ndarray | None, np.ndarray | None, np.ndarray | None]:
    """Extrae (track_ids, boxes, keypoints_xy, keypoints_conf) de un result YOLO-pose."""
    if res.boxes is None or res.boxes.xyxy is None or len(res.boxes.xyxy) == 0:
        return [], None, None, None
    boxes = res.boxes.xyxy.cpu().numpy().astype(np.float32)
    kxy = res.keypoints.xy.cpu().numpy().astype(np.float32) if res.keypoints is not None \
        else np.zeros((len(boxes), 17, 2), np.float32)
    kcf = res.keypoints.conf.cpu().numpy().astype(np.float32) \
        if (res.keypoints is not None and res.keypoints.conf is not None) \
        else np.ones((len(boxes), 17), np.float32)
    ids = res.boxes.id
    tids = [int(x) for x in ids.cpu().numpy()] if ids is not None else list(range(len(boxes)))
    return tids, boxes, kxy, kcf


# =============================================================================
# EXPERTO X-CLIP windowed (pesos/tokens en el modelo padre; scoring por sesión)
# =============================================================================
class XCLIPFardeoExpert:
    """Estado de scoring por sesión: historial de scores + suavizado gaussiano.
    El modelo X-CLIP y los tokens de texto precomputados viven en el modelo padre."""

    def __init__(self, model: "FardeoDetector", alert_threshold: float):
        self.m = model
        self.alert_threshold = alert_threshold
        self.score_history: list[float] = []
        self.smoothed_score = 0.0
        self.last_diff = 0.0
        self.last_per_window: list[float | None] = [None, None, None]

    @torch.no_grad()
    def _diff_window(self, frames_bgr: list[np.ndarray]) -> float | None:
        """diff = max(pos_logits) - mean(neg_logits) de una ventana."""
        if not frames_bgr or len(frames_bgr) < 4:
            return None
        pil = _sample_uniform(
            [Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) for f in frames_bgr],
            XCLIP_NUM_FRAMES,
        )
        pv = self.m.xclip_processor(videos=[pil], return_tensors="pt").pixel_values.to(DEVICE)
        out = self.m.xclip(
            input_ids=self.m.text_ids, attention_mask=self.m.text_mask, pixel_values=pv
        )
        logits = out.logits_per_video[0]
        pos = float(logits[: self.m.n_pos].max())
        neg = float(logits[self.m.n_pos:].mean())
        return pos - neg

    def predict_windowed(self, windows: list[list[np.ndarray]]) -> dict:
        """Combina el diff de W1/W2/W3 con WINDOW_WEIGHTS (renormaliza si faltan)."""
        diffs: list[float] = []
        wts: list[float] = []
        per: list[float | None] = []
        for frames, w in zip(windows, WINDOW_WEIGHTS):
            d = self._diff_window(frames)
            per.append(d)
            if d is not None:
                diffs.append(d)
                wts.append(w)
        if not diffs:
            return {"detected": False, "score": self.smoothed_score,
                    "diff": 0.0, "per_window": per}
        diff = sum(a * b for a, b in zip(wts, diffs)) / sum(wts)
        score = float(1.0 / (1.0 + np.exp(-(diff - KF_SIGMOID_CENTER) / KF_SIGMOID_SCALE)))
        self.score_history.append(score)
        if len(self.score_history) > SMOOTH_HISTORY:
            self.score_history.pop(0)
        self.smoothed_score = float(gaussian_filter1d(
            np.array(self.score_history, np.float32), SMOOTH_SIGMA)[-1]) \
            if len(self.score_history) >= 3 else score
        self.last_diff = diff
        self.last_per_window = per
        return {"detected": self.smoothed_score > self.alert_threshold,
                "score": self.smoothed_score, "diff": diff, "per_window": per}


# =============================================================================
# COLECTOR DE EVENTO/VENTANAS POR TRACK
# =============================================================================
class _TrackCollector:
    """Estado por track: rolling pre-buffer → al salir la mano colecta W1/W2/W3."""
    __slots__ = ("rolling", "in_roi", "pre_count", "stage",
                 "w1", "w2", "w3", "post_need", "missing", "box")

    def __init__(self) -> None:
        self.rolling: deque = deque(maxlen=SEG_XCLIP)   # crops mientras la mano está en ROI
        self.in_roi = False
        self.pre_count = 0
        self.stage = "idle"          # idle | w1post | w2 | w3
        self.w1: list = []
        self.w2: list = []
        self.w3: list = []
        self.post_need = 0
        self.missing = 0
        self.box: tuple[int, int, int, int] | None = None


# =============================================================================
# SESIÓN (port del loop de process_frame() + worker X-CLIP, inline y sin display)
# =============================================================================
class _FardeoSession(StreamSession):
    def __init__(self, model: "FardeoDetector", config: dict[str, Any]):
        self.model = model
        self.regions: list = list(config.get("regions", []))
        if not any(len(p) >= 3 for p in self.regions):
            log.warning("fardeo: sesión sin ROIs válidas — no se generarán eventos")
        self.proximity_px = int(config.get("proximity_px", PROXIMITY_PX))
        alert_th = float(config.get("fardeo_alert_threshold", ALERT_THRESHOLD))

        # ByteTrack vive dentro del objeto YOLO de ultralytics: instancia propia
        # por sesión para no mezclar track_ids entre streams (pesos nano ~6 MB;
        # el X-CLIP pesado sí se comparte desde el modelo padre)
        self.yolo = YOLO(WEIGHTS)
        self.expert = XCLIPFardeoExpert(model, alert_th)

        self.region_masks: list[np.ndarray | None] | None = None  # se construyen al saber H,W
        self.collectors: dict[int, _TrackCollector] = {}
        self.is_detected = False
        self.ttl = 0
        self.last_score = 0.0
        self.last_diff = 0.0
        self.last_per_window: list[float | None] = [None, None, None]
        self.last_event_tid: int | None = None
        self.last_event_box: tuple[int, int, int, int] | None = None
        self.n_events = 0
        self._f_idx = 0

    # ── scoring por evento (inline; en el original era un worker en thread) ──
    def _dispatch(self, c: _TrackCollector, tid: int) -> None:
        # ponytail: scoring X-CLIP inline en el thread del stream (~0.5-2 s por
        # evento: 3 ventanas de 16 frames); si bloquea demasiado el consumo del
        # stream, mover a worker propio como en el original
        res = self.expert.predict_windowed([list(c.w1), list(c.w2), list(c.w3)])
        self.n_events += 1
        self.last_score = res["score"]
        self.last_diff = res["diff"]
        self.last_per_window = res["per_window"]
        self.last_event_tid = tid
        self.last_event_box = c.box
        if res["detected"]:
            self.is_detected = True
            self.ttl = FARDEO_TTL
        pw_s = "/".join(f"{d:+.2f}" if d is not None else "--" for d in res["per_window"])
        log.info("fardeo: evento tid=%s %s diff=%+.3f score=%.3f W1/W2/W3=%s",
                 tid, "SOSPECHOSO" if res["detected"] else "normal",
                 res["diff"], res["score"], pw_s)

    def _start_post(self, c: _TrackCollector, forced: bool) -> None:
        """Congela W1 pre (rolling) e inicia colección post-salida."""
        c.w1 = list(c.rolling)[-HALF:]      # 24 frames pre-salida
        c.w2 = []
        c.w3 = []
        c.in_roi = False
        c.pre_count = 0
        if forced:
            c.stage = "w2"                   # sin post de W1
        else:
            c.stage = "w1post"
            c.post_need = HALF               # 24 frames post-salida

    def _feed_post(self, c: _TrackCollector, crop: np.ndarray, tid: int) -> None:
        if c.stage == "w2":
            c.w2.append(crop)
            if len(c.w2) >= WINDOW_POST:
                c.stage = "w3"
        elif c.stage == "w3":
            c.w3.append(crop)
            if len(c.w3) >= WINDOW_POST:
                self._dispatch(c, tid)
                c.stage = "idle"
                c.w1 = []
                c.w2 = []
                c.w3 = []

    # ── lógica por frame ──────────────────────────────────────────────────────
    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        events: list[Event] = []
        was_on = self.is_detected
        H, W = frame.shape[:2]
        if self.region_masks is None:
            self.region_masks = _build_region_masks(self.regions, H, W, self.proximity_px)

        res = self.yolo.track(frame, persist=(self._f_idx > 0), conf=0.3, iou=0.7,
                              verbose=False, tracker="bytetrack.yaml", imgsz=640,
                              device=DEVICE)[0]
        self._f_idx += 1

        tids, boxes, kxy, kcf = _parse_pose(res)
        triggered = _triggered_track_ids(kxy, kcf, tids, self.region_masks, H, W) if tids else set()
        present = set(tids)

        # Actualizar colector de cada track presente
        for i, tid in enumerate(tids):
            c = self.collectors.get(tid)
            if c is None:
                c = self.collectors[tid] = _TrackCollector()
            crop = _union_crop(frame, boxes[i], CROP_PAD)
            c.box = tuple(int(v) for v in boxes[i])
            c.missing = 0
            trig = tid in triggered

            if c.stage in ("w2", "w3"):
                # colectando ventanas post-evento del mismo track
                if crop is not None:
                    self._feed_post(c, crop, tid)
                continue

            # estados idle / pre / w1post
            if crop is not None:
                c.rolling.append(crop)
            if c.stage == "w1post":
                if crop is not None:
                    c.w1.append(crop)
                    c.post_need -= 1
                if c.post_need <= 0:
                    c.stage = "w2"
                continue

            # idle / collecting-pre
            if trig:
                c.in_roi = True
                c.pre_count += 1
                if c.pre_count >= MAX_PRE_FRAMES:        # dispatch forzado
                    self._start_post(c, forced=True)
            elif c.in_roi:                                # mano SALIÓ → evento
                if c.pre_count >= MIN_PRE_FRAMES:
                    self._start_post(c, forced=False)
                else:
                    c.in_roi = False                      # entrada muy breve → descartar
                    c.pre_count = 0

        # Tracks ausentes: timeout de ventanas en curso
        for tid in list(self.collectors.keys()):
            if tid in present:
                continue
            c = self.collectors[tid]
            c.missing += 1
            if c.stage in ("w1post", "w2", "w3") and c.missing > WINDOW_TIMEOUT:
                self._dispatch(c, tid)
                del self.collectors[tid]
            elif c.missing > WINDOW_TIMEOUT * 2:
                del self.collectors[tid]

        # TTL de la alerta
        if self.is_detected:
            self.ttl -= 1
            if self.ttl <= 0:
                self.is_detected = False

        # ── transiciones → eventos ────────────────────────────────────────────
        if self.is_detected and not was_on:
            events.append(Event(
                type="FARDEO", score=self.last_score, ts=ts,
                extra={"diff": self.last_diff,
                       "per_window": self.last_per_window,
                       "track_id": self.last_event_tid,
                       "bbox": list(self.last_event_box) if self.last_event_box else None,
                       "n_events": self.n_events},
            ))
        elif not self.is_detected and was_on:
            events.append(Event(type="FARDEO_OFF", score=self.last_score, ts=ts))

        return events

    def close(self) -> None:
        self.collectors.clear()
        self.yolo = None


# =============================================================================
# MODELO
# =============================================================================
class FardeoDetector(CVModel):
    info = ModelInfo(
        name="fardeo-detector",
        version="2.0",
        task="Detección de fardeo (hurto en retail) — YOLO-pose + heurística mano-ROI + X-CLIP windowed",
        capabilities={Capability.CLIP, Capability.STREAM},
        event_types=["FARDEO", "FARDEO_OFF"],
        config_schema=CONFIG_SCHEMA,
        # las ventanas del original (48 frames ≈ 2 s: W2 = "~1-3 s después")
        # están calibradas a ~24 fps con frame_skip=1
        target_fps=24.0,
    )

    def __init__(self) -> None:
        self.xclip = None
        self.xclip_processor = None
        self.text_ids = None
        self.text_mask = None
        self.n_pos = len(POS_PROMPTS)

    def load(self) -> None:
        # import perezoso: transformers no está en las deps base de Kinich
        from transformers import XCLIPModel, XCLIPProcessor

        log.info("Cargando X-CLIP %s en %s", XCLIP_MODEL_ID, DEVICE)
        self.xclip = XCLIPModel.from_pretrained(XCLIP_MODEL_ID).to(DEVICE).eval()
        self.xclip_processor = XCLIPProcessor.from_pretrained(XCLIP_MODEL_ID)
        # tokens de texto precomputados una vez (pos + neg concatenados)
        tok = self.xclip_processor(text=POS_PROMPTS + NEG_PROMPTS,
                                   return_tensors="pt", padding=True)
        self.text_ids = tok.input_ids.to(DEVICE)
        self.text_mask = tok.attention_mask.to(DEVICE)
        # fail-fast de los pesos YOLO-pose (cada sesión instancia el suyo)
        log.info("Verificando YOLO-pose fardeo: %s", WEIGHTS)
        YOLO(WEIGHTS)

    def warmup(self) -> None:
        session = self.create_session({})
        session.process_frame(np.zeros((360, 640, 3), dtype=np.uint8), 0.0)
        session.close()

    def unload(self) -> None:
        self.xclip = self.xclip_processor = None
        self.text_ids = self.text_mask = None

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        if self.xclip is None:
            self.load()
        return _FardeoSession(self, config)
