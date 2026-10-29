"""RobberyDetector — port de RobberyDetector_Production_V3.py al contrato Kinich.

Lógica de detección intacta respecto al original:
  YOLO tracker (personas, ByteTrack) -> _PersonEventDetector (proximidad
  adaptativa + closing + asimetría de velocidad + area spike) -> buffer de
  20 crops del par activo -> CLIP temporal V3 (KSM + KF + PC + TC)
  -> score suavizado > umbral -> consec/TTL -> alerta.

Parámetros optimizados por ablación 2D del original (sin cambios de valor):
  AREA_VARIATION_THR = 0.10, ALERT_THRESHOLD = 0.75.

Eliminado del original: display cv2 y overlays, imwrite de alertas y video,
CLI, workers propios (el thread del stream ya es el worker) y el quantizador
SmoothQuant/INT8. Cambios: CLIP vía open_clip (el repo `clip` de OpenAI está
archivado), features de texto precomputadas una vez en load(); el tracker
ByteTrack vive en el predictor de YOLO, así que cada sesión usa una copia
superficial del YOLO que comparte los pesos (nn.Module) del modelo padre.
"""
import copy
import logging
import os
from collections import deque
from typing import Any

import cv2
import numpy as np
import open_clip
import torch
from PIL import Image
from scipy.ndimage import gaussian_filter1d
from ultralytics import YOLO

from kinich.contract import Capability, CVModel, Event, ModelInfo, StreamSession

log = logging.getLogger(__name__)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
WEIGHTS = os.environ.get("KINICH_ROBBERY_WEIGHTS", "weights/yolov8n.pt")
# -quickgelu: los pesos openai usan QuickGELU; sin el sufijo open_clip
# construye GELU estándar y los scores divergen del script original.
# El original usaba ViT-L/14 por default (equivale a "ViT-L-14-quickgelu").
CLIP_MODEL_NAME = os.environ.get("KINICH_ROBBERY_CLIP_MODEL", "ViT-B-32-quickgelu")

# =============================================================================
# CONFIG (constantes del original, sin cambios de valor)
# =============================================================================
PERSON_CONF = 0.35
PAIR_CROP_SIZE = (640, 480)

# ── Temporal CLIP V3 ─────────────────────────────────────────────────────────
KEY_FRAMES_K = 4
GAMMA1 = 0.55
GAMMA2 = 0.30
GAMMA3 = 0.15
ALERT_THRESHOLD = 0.75
SMOOTH_SIGMA = 0.5
SMOOTH_HISTORY = 3
KF_SIGMOID_CENTER = 2.8
KF_SIGMOID_SCALE = 1.5
TC_SIM_LOW = 0.15
TC_SIM_HIGH = 0.35
IMG_SIZE = 240
WIN_SMALL = (48, 48)
WIN_MID = (80, 80)
WIN_LARGE = (120, 120)
BUFFER_SIZE = 20

# ── Heurísticas de robo ──────────────────────────────────────────────────────
AREA_VARIATION_THR = 0.10
AREA_HISTORY_LEN = 5
PROX_SCALE_FACTOR = 0.71
PROX_MIN_PX = 30.0
PROX_MAX_PX = 120.0
FRAME_PCT_CAP = 0.25
CLOSING_RATIO_REF = 0.30
CLOSING_RATIO_MIN = 0.15
CLOSING_RATIO_MAX = 0.60
CLOSING_HISTORY_LEN = 5
MOTION_THR_FRAC = 0.06
MOTION_THR_MIN = 2.0
MOTION_HISTORY_LEN = 5
ASYMMETRY_RATIO = 0.5
SCALE_REF = 80.0
EVENT_COOLDOWN = 15
CROP_PAD_PX = 10
ROBBERY_TTL = 30
ROBBERY_CONSEC_MIN = 1
SCORE_DECAY = 0.02

ROBBERY_POS_PROMPTS = [
    "a person violently grabbing another person on the street",
    "a robbery in progress with an attacker and victim",
    "a person being attacked and robbed on a sidewalk",
    "an aggressor forcefully taking belongings from a person",
    "a violent assault on a pedestrian in public",
    "a thief attacking a pedestrian aggressively",
    "one person pushing and grabbing another in an aggressive manner",
    "a physical confrontation and mugging on a street",
    "a person overpowering a victim to steal their belongings",
    "a violent street crime with physical contact between two people",
]
ROBBERY_NEG_PROMPTS = [
    "two people talking normally on a sidewalk",
    "people walking calmly on a street",
    "two friends greeting each other on the street",
    "people standing next to each other peacefully",
    "a person helping another person on the street",
    "people passing by without confrontation",
    "a normal street scene with pedestrians",
    "people hugging or embracing as friends",
    "a couple walking together on a sidewalk",
    "people waiting at a bus stop or crosswalk",
]
ROBBERY_KSM_QUERY = (
    "a person aggressively approaching and grabbing another person on a street"
)

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "detect_scale": {
            "type": "number", "default": 1.0, "minimum": 0.1, "maximum": 1.0,
            "description": "detección a fracción de la resolución (tracker y heurísticas)",
        },
        "robbery_alert_threshold": {
            "type": "number", "default": ALERT_THRESHOLD,
            "description": "umbral de score CLIP suavizado (optimizado por ablación)",
        },
        "robbery_consec_min": {
            "type": "integer", "default": ROBBERY_CONSEC_MIN,
            "description": "batches CLIP positivos consecutivos para confirmar",
        },
        "area_variation_thr": {
            "type": "number", "default": AREA_VARIATION_THR,
            "description": "variación relativa de área de bbox para el gate area_spike",
        },
    },
}


# =============================================================================
# HEURÍSTICAS — funciones adaptativas (idénticas al original)
# =============================================================================
def _estimate_person_scale(persons: list[dict]) -> float:
    if not persons:
        return SCALE_REF
    diags = [np.hypot(p["bbox_xyxy"][2] - p["bbox_xyxy"][0],
                      p["bbox_xyxy"][3] - p["bbox_xyxy"][1]) for p in persons]
    return float(np.median(diags))


def _adaptive_prox_threshold(person_scale: float, frame_w: int = 320) -> float:
    eff_max = min(PROX_MAX_PX, FRAME_PCT_CAP * frame_w)
    return float(np.clip(PROX_SCALE_FACTOR * person_scale, PROX_MIN_PX, eff_max))


def _adaptive_closing_ratio(person_scale: float) -> float:
    return float(np.clip(CLOSING_RATIO_REF * (person_scale / SCALE_REF),
                         CLOSING_RATIO_MIN, CLOSING_RATIO_MAX))


def _adaptive_motion_thr(person_scale: float) -> float:
    return float(max(MOTION_THR_MIN, MOTION_THR_FRAC * person_scale))


def _crop_pair(frame: np.ndarray, b1, b2, pad_px: int) -> np.ndarray | None:
    H, W = frame.shape[:2]
    x1 = max(0, int(min(float(b1[0]), float(b2[0]))) - pad_px)
    y1 = max(0, int(min(float(b1[1]), float(b2[1]))) - pad_px)
    x2 = min(W, int(max(float(b1[2]), float(b2[2]))) + pad_px)
    y2 = min(H, int(max(float(b1[3]), float(b2[3]))) + pad_px)
    if x2 <= x1 or y2 <= y1:
        return None
    crop = frame[y1:y2, x1:x2]
    return cv2.resize(crop, PAIR_CROP_SIZE) if crop.size > 0 else None


class _PersonEventDetector:
    """Evalúa pares de personas y emite eventos cuando se supera la puerta de
    proximidad + al menos una señal (cierre, asimetría o area_spike)."""

    def __init__(self, area_thr: float = AREA_VARIATION_THR):
        self.area_thr = area_thr
        self._speed_history: dict[int, deque] = {}
        self._prev_center: dict[int, np.ndarray] = {}
        self._dist_history: dict[tuple[int, int], deque] = {}
        self._last_event_frame: dict[tuple[int, int], int] = {}
        self._area_history: dict[int, deque] = {}
        self._frame_w: int = 320

    def _update_speed(self, tid: int, center: np.ndarray) -> float:
        prev = self._prev_center.get(tid)
        self._prev_center[tid] = center.copy()
        inst = 0.0 if prev is None else float(np.linalg.norm(center - prev))
        if tid not in self._speed_history:
            self._speed_history[tid] = deque(maxlen=MOTION_HISTORY_LEN)
        self._speed_history[tid].append(inst)
        return inst

    def _avg_speed(self, tid: int) -> float:
        hist = self._speed_history.get(tid)
        return float(np.mean(hist)) if hist else 0.0

    def _update_area(self, tid: int, bbox) -> None:
        x1, y1, x2, y2 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
        if tid not in self._area_history:
            self._area_history[tid] = deque(maxlen=AREA_HISTORY_LEN)
        self._area_history[tid].append((x2 - x1) * (y2 - y1))

    def _area_spike(self, tid: int) -> bool:
        hist = self._area_history.get(tid)
        if not hist or len(hist) < 2:
            return False
        prev_mean = float(np.mean(list(hist)[:-1]))
        if prev_mean < 1.0:
            return False
        return abs(hist[-1] - prev_mean) / prev_mean >= self.area_thr

    def _is_closing(self, key: tuple[int, int], current_dist: float, ratio: float) -> bool:
        if key not in self._dist_history:
            self._dist_history[key] = deque(maxlen=CLOSING_HISTORY_LEN)
        hist = self._dist_history[key]
        hist.append(current_dist)
        if len(hist) < 2:
            return True
        oldest = hist[0]
        if oldest < 1e-6:
            return True
        return (oldest - current_dist) / oldest >= ratio

    def process_frame(self, frame_idx: int, persons: list[dict],
                      person_scale: float = SCALE_REF) -> list[dict]:
        prox_thr = _adaptive_prox_threshold(person_scale, self._frame_w)
        closing_ratio = _adaptive_closing_ratio(person_scale)
        motion_thr = _adaptive_motion_thr(person_scale)

        for p in persons:
            self._update_speed(p["track_id"], p["center"])
            self._update_area(p["track_id"], p["bbox_xyxy"])

        events: list[dict] = []
        n = len(persons)
        for i in range(n):
            for j in range(i + 1, n):
                pa, pb = persons[i], persons[j]
                id_a, id_b = pa["track_id"], pb["track_id"]
                key = (min(id_a, id_b), max(id_a, id_b))

                if (frame_idx - self._last_event_frame.get(key, -9999)) < EVENT_COOLDOWN:
                    continue

                dist = float(np.linalg.norm(pa["center"] - pb["center"]))
                if dist > prox_thr:
                    self._is_closing(key, dist, closing_ratio)
                    continue

                closing = self._is_closing(key, dist, closing_ratio)
                sp_a = self._avg_speed(id_a)
                sp_b = self._avg_speed(id_b)
                fast, slow = max(sp_a, sp_b), min(sp_a, sp_b)
                asymmetric = (fast > motion_thr and
                              (fast - slow) / (fast + 1e-6) >= ASYMMETRY_RATIO)
                area_spike = self._area_spike(id_a) or self._area_spike(id_b)

                if not closing and not asymmetric and not area_spike:
                    continue

                relation = (
                    "contact" if dist < 60 else
                    "area_spike" if area_spike and not closing and not asymmetric else
                    "asymmetric" if asymmetric and not closing else
                    "closing"
                )
                prox = max(0.0, 1.0 - dist / max(prox_thr, 1.0))
                sig = (0.5 if closing else 0.0) + (0.5 if asymmetric else 0.0)
                score = round(prox * 0.4 + sig * 0.6, 4)

                events.append({
                    "frame_idx": frame_idx, "pair_key": key,
                    "relation": relation, "score": score,
                    "b1": pa["bbox_xyxy"], "b2": pb["bbox_xyxy"],
                })
                self._last_event_frame[key] = frame_idx
        return events


# =============================================================================
# CLIP helpers — KSM / PC / TC (idénticos al original)
# =============================================================================
from . import _preproceso  # noqa: E402


def _encode_image_feats(clip_model, preprocess, imagenes, cuadrar=False):
    """Codifica un lote de imagenes con CLIP.

    Acepta fotogramas BGR de OpenCV -- camino rapido, el lote entero se
    redimensiona y normaliza en la GPU de una vez -- o imagenes PIL, que es el
    camino de siempre y sigue aqui por si algun llamador todavia las construye.
    El porque del cambio esta en `_preproceso.py`.
    """
    if imagenes and isinstance(imagenes[0], np.ndarray):
        gpu = _preproceso.obtener(preprocess, DEVICE)
        if gpu.disponible:
            prep = gpu.desde_bgr(imagenes, cuadrar=cuadrar)
        else:
            prep = torch.stack([
                preprocess(Image.fromarray(cv2.cvtColor(
                    cv2.resize(im, (224, 224)) if cuadrar else im, cv2.COLOR_BGR2RGB)))
                for im in imagenes
            ]).to(DEVICE)
    else:
        prep = torch.stack([preprocess(im) for im in imagenes]).to(DEVICE)
    with torch.no_grad():
        feats = clip_model.encode_image(prep).float()
        feats /= feats.norm(dim=-1, keepdim=True)
    return feats


def ksm_select_key_frames(frames_bgr, text_feat, clip_model, preprocess):
    N = len(frames_bgr)
    feats = _encode_image_feats(clip_model, preprocess, frames_bgr, cuadrar=True)
    tf = text_feat / text_feat.norm(dim=-1, keepdim=True)
    sims = (tf @ feats.T).cpu().squeeze(0).numpy()
    hat_k = int(np.argmax(sims))
    offset = hat_k % max(1, N // KEY_FRAMES_K)
    other = [min((i * N // KEY_FRAMES_K) + offset, N - 1) for i in range(1, KEY_FRAMES_K)]
    return hat_k, other


def compute_pc(key_frame_bgr, text_feat, clip_model, preprocess) -> float:
    img = cv2.resize(key_frame_bgr, (IMG_SIZE, IMG_SIZE))
    all_maps = []
    for wh, ww in [WIN_SMALL, WIN_MID, WIN_LARGE]:
        n_rows, n_cols = IMG_SIZE // wh, IMG_SIZE // ww
        patches = [
            img[r * wh:(r + 1) * wh, c * ww:(c + 1) * ww]
            for r in range(n_rows) for c in range(n_cols)
        ]
        if not patches:
            continue
        feats = _encode_image_feats(clip_model, preprocess, patches)
        tf = text_feat / text_feat.norm(dim=-1, keepdim=True)
        sims = (tf @ feats.T).cpu().squeeze(0).numpy().astype(np.float32)
        sim_map = sims.reshape(n_rows, n_cols)
        mu, sigma = sim_map.mean(), sim_map.std() + 1e-8
        all_maps.append(
            cv2.resize((sim_map - mu) / sigma, (IMG_SIZE, IMG_SIZE), interpolation=cv2.INTER_LINEAR)
        )
    if not all_maps:
        return 0.0
    m_norm = np.mean(all_maps, axis=0).astype(np.float32)
    m_norm = (m_norm - m_norm.min()) / (m_norm.max() - m_norm.min() + 1e-8)
    flat = m_norm.flatten()
    return float(np.mean(np.sort(flat)[len(flat) // 2:]))


def compute_tc(key_frames_bgr, text_feat, clip_model, preprocess) -> float:
    if len(key_frames_bgr) < 4:
        return 0.0
    frames = [cv2.resize(f, (IMG_SIZE, IMG_SIZE)) for f in key_frames_bgr[:4]]
    cell = 112
    grids_bgr = []
    for wh, ww in [WIN_SMALL, WIN_MID, WIN_LARGE]:
        for r in range(IMG_SIZE // wh):
            for c in range(IMG_SIZE // ww):
                patches = [f[r * wh:(r + 1) * wh, c * ww:(c + 1) * ww] for f in frames]
                row0 = np.hstack([cv2.resize(p, (cell, cell)) for p in patches[:2]])
                row1 = np.hstack([cv2.resize(p, (cell, cell)) for p in patches[2:]])
                grid_bgr = np.vstack([row0, row1])
                grids_bgr.append(grid_bgr)
    if not grids_bgr:
        return 0.0
    feats = _encode_image_feats(clip_model, preprocess, grids_bgr)
    tf = text_feat / text_feat.norm(dim=-1, keepdim=True)
    sims = (tf @ feats.T).cpu().squeeze(0).numpy()
    raw = float(sims[int(np.argmax(sims))])
    return float(np.clip((raw - TC_SIM_LOW) / (TC_SIM_HIGH - TC_SIM_LOW + 1e-8), 0, 1))


# =============================================================================
# EXPERTO CLIP (features de texto precomputadas por el modelo; estado por sesión)
# =============================================================================
class _RobberyTextFeats:
    def __init__(self, clip_model, tokenizer):
        with torch.no_grad():
            tf = clip_model.encode_text(tokenizer([ROBBERY_KSM_QUERY]).to(DEVICE)).float()
            tf /= tf.norm(dim=-1, keepdim=True)
            pf = clip_model.encode_text(tokenizer(ROBBERY_POS_PROMPTS).to(DEVICE)).float()
            nf = clip_model.encode_text(tokenizer(ROBBERY_NEG_PROMPTS).to(DEVICE)).float()
            pf /= pf.norm(dim=-1, keepdim=True)
            nf /= nf.norm(dim=-1, keepdim=True)
            self.pos = pf.mean(0, keepdim=True)
            self.neg = nf.mean(0, keepdim=True)
            self.pos /= self.pos.norm(dim=-1, keepdim=True)
            self.neg /= self.neg.norm(dim=-1, keepdim=True)
        self.ksm = tf
        self.logit_scale = float(clip_model.logit_scale.exp().item())


class CLIPRobberyExpert:
    def __init__(self, clip_model, preprocess, feats: _RobberyTextFeats, alert_threshold: float):
        self.model = clip_model
        self.preprocess = preprocess
        self.feats = feats
        self.alert_threshold = alert_threshold
        self.smoothed_score = 0.0
        self.score_history: list[float] = []
        self.last_ascore = self.last_score_kf = self.last_score_pc = self.last_score_tc = 0.0

    @torch.no_grad()
    def _score_kf(self, frame_bgr: np.ndarray) -> float:
        feat = _encode_image_feats(self.model, self.preprocess, [frame_bgr])
        diff = float(self.feats.logit_scale * (feat @ self.feats.pos.T - feat @ self.feats.neg.T).item())
        return float(1.0 / (1.0 + np.exp(-(diff - KF_SIGMOID_CENTER) / KF_SIGMOID_SCALE)))

    def process_segment(self, frames_bgr: list[np.ndarray]) -> dict:
        if len(frames_bgr) < KEY_FRAMES_K:
            return {"ascore": self.smoothed_score, "smoothed": self.smoothed_score,
                    "detected": self.smoothed_score > self.alert_threshold, "skipped": True}
        hat_k_idx, other_idx = ksm_select_key_frames(
            frames_bgr, self.feats.ksm, self.model, self.preprocess)
        key_frames = [frames_bgr[hat_k_idx]] + [frames_bgr[i] for i in other_idx]
        hat_k = frames_bgr[hat_k_idx]
        score_kf = self._score_kf(hat_k)
        score_pc = compute_pc(hat_k, self.feats.ksm, self.model, self.preprocess)
        score_tc = compute_tc(key_frames, self.feats.ksm, self.model, self.preprocess)
        ascore = GAMMA1 * score_kf + GAMMA2 * score_pc + GAMMA3 * score_tc
        self.score_history.append(ascore)
        if len(self.score_history) > SMOOTH_HISTORY:
            self.score_history.pop(0)
        self.smoothed_score = (
            float(gaussian_filter1d(np.array(self.score_history, dtype=np.float32), sigma=SMOOTH_SIGMA)[-1])
            if len(self.score_history) >= 3 else ascore
        )
        self.last_ascore = ascore
        self.last_score_kf, self.last_score_pc, self.last_score_tc = score_kf, score_pc, score_tc
        detected = self.smoothed_score > self.alert_threshold
        log.debug("CLIP_V3 ascore=%.3f smooth=%.3f kf=%.3f pc=%.3f tc=%.3f %s",
                  ascore, self.smoothed_score, score_kf, score_pc, score_tc,
                  "ROBO" if detected else "normal")
        return {"ascore": ascore, "smoothed": self.smoothed_score,
                "detected": detected, "skipped": False}


# =============================================================================
# SESIÓN (port del loop de run() + _process_frame + worker CLIP, inline)
# =============================================================================
class _RobberySession(StreamSession):
    def __init__(self, model: "RobberyDetector", config: dict[str, Any]):
        self.model = model
        self.detect_scale = max(0.1, min(1.0, float(config.get("detect_scale", 1.0))))
        alert_th = float(config.get("robbery_alert_threshold", ALERT_THRESHOLD))
        self.consec_min = int(config.get("robbery_consec_min", ROBBERY_CONSEC_MIN))
        area_thr = float(config.get("area_variation_thr", AREA_VARIATION_THR))

        # ByteTrack guarda estado en el predictor de YOLO: copia superficial
        # que comparte los pesos (self.model del YOLO) pero con predictor y
        # trackers propios por sesión.
        self.yolo = copy.copy(model.yolo)
        self.yolo.predictor = None

        self.event_detector = _PersonEventDetector(area_thr)
        self.expert = CLIPRobberyExpert(
            model.clip_model, model.clip_preprocess, model.robbery_feats, alert_th)

        self.robbery_buffer: list[np.ndarray] = []
        self.is_detected = False
        self.consec_pos = 0.0
        self.last_score = 0.0
        self.robbery_ttl = 0
        self.frame_idx = 0

        self._active_pair: dict | None = None
        self._last_pair: dict | None = None
        self._last_crop: np.ndarray | None = None
        self._person_scale_ema = SCALE_REF

    def _clip_batch(self) -> None:
        # ponytail: scoring CLIP inline en el thread del stream (~100-300ms por
        # batch cada 20 crops); si bloquea demasiado, mover a worker propio
        res = self.expert.process_segment(self.robbery_buffer)
        self.last_score = res["smoothed"]
        if not res.get("skipped"):
            if res["detected"]:
                self.consec_pos = min(6.0, self.consec_pos + 1)
            else:
                self.consec_pos = max(0.0, self.consec_pos - 1)
            self.is_detected = self.consec_pos >= self.consec_min
            if self.is_detected:
                self.robbery_ttl = ROBBERY_TTL

    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        events: list[Event] = []
        was_on = self.is_detected

        if self.detect_scale < 1.0:
            H, W = frame.shape[:2]
            frame_det = cv2.resize(frame, (int(W * self.detect_scale), int(H * self.detect_scale)))
        else:
            frame_det = frame

        # ── tracker de personas ─────────────────────────────────────────────
        results = self.yolo.track(frame_det, persist=True, classes=[0],
                                  conf=PERSON_CONF, verbose=False,
                                  tracker="bytetrack.yaml")
        persons: list[dict] = []
        if results[0].boxes.id is not None:
            xyxy_arr = results[0].boxes.xyxy.cpu().numpy()
            ids_arr = results[0].boxes.id.cpu().numpy().astype(int)
            for xyxy, tid in zip(xyxy_arr, ids_arr):
                persons.append({"track_id": int(tid), "bbox_xyxy": xyxy,
                                "center": np.array([(xyxy[0] + xyxy[2]) / 2,
                                                    (xyxy[1] + xyxy[3]) / 2])})

        self.event_detector._frame_w = frame_det.shape[1]
        raw_scale = _estimate_person_scale(persons)
        self._person_scale_ema = 0.85 * self._person_scale_ema + 0.15 * raw_scale

        # ── heurísticas de par ──────────────────────────────────────────────
        heur_events = self.event_detector.process_frame(
            self.frame_idx, persons, self._person_scale_ema)
        for ev in heur_events:
            id_a, id_b = ev["pair_key"]
            crop = _crop_pair(frame_det, ev["b1"], ev["b2"], CROP_PAD_PX)
            if crop is not None:
                self._last_crop = crop
                self._active_pair = {"id_a": id_a, "id_b": id_b, "b1": ev["b1"], "b2": ev["b2"]}
            log.debug("HEURISTIC pair=(%d,%d) relation=%s score=%.3f",
                      id_a, id_b, ev["relation"], ev["score"])

        # ── buffer de crops mientras el par activo siga presente ────────────
        if self._last_crop is not None and self._active_pair is not None:
            bbox_map = {p["track_id"]: p["bbox_xyxy"] for p in persons}
            b1 = bbox_map.get(self._active_pair["id_a"])
            b2 = bbox_map.get(self._active_pair["id_b"])
            if b1 is not None and b2 is not None:
                refreshed = _crop_pair(frame_det, b1, b2, CROP_PAD_PX)
                if refreshed is not None:
                    self._last_crop = refreshed
                self._active_pair["b1"], self._active_pair["b2"] = b1, b2
            self.robbery_buffer.append(self._last_crop.copy())

        # ── batch CLIP cuando el buffer se llena ────────────────────────────
        if len(self.robbery_buffer) >= BUFFER_SIZE:
            self._last_pair = self._active_pair
            self._clip_batch()
            self.robbery_buffer = []
            self._active_pair = None

        # ── TTL / decaimiento de score ──────────────────────────────────────
        if self.is_detected:
            self.robbery_ttl -= 1
            if self.robbery_ttl <= 0:
                self.is_detected = False
                self.consec_pos = 0.0
        if not self.is_detected:
            self.expert.smoothed_score = max(0.0, self.expert.smoothed_score - SCORE_DECAY)
            self.last_score = self.expert.smoothed_score

        # ── transiciones → eventos ──────────────────────────────────────────
        if self.is_detected and not was_on:
            scale_up = 1.0 / self.detect_scale
            extra: dict[str, Any] = {
                "ascore": self.expert.last_ascore,
                "kf": self.expert.last_score_kf, "pc": self.expert.last_score_pc,
                "tc": self.expert.last_score_tc, "consec": self.consec_pos,
            }
            if self._last_pair is not None:
                extra["pair"] = [self._last_pair["id_a"], self._last_pair["id_b"]]
                extra["bboxes"] = [
                    [int(float(v) * scale_up) for v in self._last_pair["b1"]],
                    [int(float(v) * scale_up) for v in self._last_pair["b2"]],
                ]
            events.append(Event(type="ROBO", score=self.last_score, ts=ts, extra=extra))
        elif not self.is_detected and was_on:
            events.append(Event(type="ROBO_OFF", score=self.last_score, ts=ts))

        self.frame_idx += 1
        return events

    def close(self) -> None:
        self.yolo = None
        self.robbery_buffer = []


# =============================================================================
# MODELO
# =============================================================================
class RobberyDetector(CVModel):
    info = ModelInfo(
        name="robbery-detector",
        version="3.0",
        task="Detección de robo con violencia en CCTV (tracker YOLO + heurísticas de par + CLIP temporal V3)",
        capabilities={Capability.CLIP, Capability.STREAM},
        event_types=["ROBO", "ROBO_OFF"],
        config_schema=CONFIG_SCHEMA,
        target_fps=10.0,  # equivale al --frame-skip 2 recomendado del original en cámaras de 20 fps
    )

    def __init__(self) -> None:
        self.yolo: YOLO | None = None
        self.clip_model = None
        self.clip_preprocess = None
        self.robbery_feats: _RobberyTextFeats | None = None

    def load(self) -> None:
        log.info("Cargando YOLO personas: %s", WEIGHTS)
        self.yolo = YOLO(WEIGHTS)
        log.info("Cargando CLIP %s (openai) en %s", CLIP_MODEL_NAME, DEVICE)
        self.clip_model, _, self.clip_preprocess = open_clip.create_model_and_transforms(
            CLIP_MODEL_NAME, pretrained="openai", device=DEVICE)
        self.clip_model.eval()
        tokenizer = open_clip.get_tokenizer(CLIP_MODEL_NAME)
        self.robbery_feats = _RobberyTextFeats(self.clip_model, tokenizer)

    def warmup(self) -> None:
        session = self.create_session({})
        session.process_frame(np.zeros((360, 640, 3), dtype=np.uint8), 0.0)
        session.close()

    def unload(self) -> None:
        self.yolo = self.clip_model = self.clip_preprocess = None
        self.robbery_feats = None

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        if self.yolo is None:
            self.load()
        return _RobberySession(self, config)
