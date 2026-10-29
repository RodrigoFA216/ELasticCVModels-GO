"""CrashDetector — port de CrashDetector_Production.py al contrato Kinich.

Lógica de detección intacta respecto al original (arconte):
  YOLO track (bytetrack) -> heurísticas de proximidad/movimiento entre
  vehículos (par móvil, closing-gate, pileup sobre autos ya chocados)
  -> lock + buffer de crops -> experto CLIP (KSM + KF + PC + TC)
  -> suavizado gaussiano + TTL de alerta

Eliminado del original: display cv2 (overlays/banner), guardado de alertas
jpg y grabaciones, CLI, colorama, worker de imwrite y los locks asociados
(el procesamiento es inline en el thread del stream).
Cambios: CLIP vía open_clip (el repo `clip` de OpenAI está archivado) con
features de texto precomputadas en load(); el tracker bytetrack se instancia
por sesión (BYTETracker) con el YOLO compartido, porque `YOLO.track(persist=
True)` guarda el estado del tracker dentro del objeto YOLO y mezclaría IDs
entre streams.
"""
import logging
import math
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

# ultralytics.trackers requiere `lap` (solver de asignación lineal), que no
# está entre las deps declaradas del proyecto — import perezoso al crear sesión
from kinich.contract import Capability, CVModel, Event, ModelInfo, StreamSession

log = logging.getLogger(__name__)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
WEIGHTS = os.environ.get("KINICH_CRASH_WEIGHTS", "weights/yolo11x.pt")
# -quickgelu: los pesos openai usan QuickGELU; sin el sufijo open_clip
# construye GELU estándar y los scores divergen del script original
CLIP_MODEL_NAME = os.environ.get("KINICH_CRASH_CLIP_MODEL", "ViT-B-32-quickgelu")
TRACKER_YAML = "bytetrack.yaml"  # config builtin de ultralytics

# =============================================================================
# CONFIG (constantes del original, sin cambios de valor)
# =============================================================================
_VEHICLE_CLASSES = [2, 5, 7]
_VEHICLE_MIN_CONF = 0.40
_ALL_TRACK_CLASSES = [0, 2, 3, 5, 7]

C_CAR_PAD_PX = 15
C_TRIGGER_FRAMES = 7
C_BUFFER_SIZE = 16
C_MOTION_THRESHOLD = 3
C_CAR_PAIR_IOU_THR = 0.10
C_STATIONARY_THR_FRAMES = 6
C_REAR_ANGLE_TOL = 35.0
C_REAR_DIST_MULT = 1.0
C_CRASH_TTL = 60
C_CRASH_MAX_FRAMES = 60
C_CLOSING_HISTORY = 10
C_CLOSING_DROP_RATIO = 0.30
C_MOTION_HISTORY_LEN = 5
C_CLOSING_MIN_FRAMES = 10

KEY_FRAMES_K = 4
GAMMA1 = 0.55
GAMMA2 = 0.30
GAMMA3 = 0.15
ALERT_THRESHOLD = 0.68
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

CRASH_POS_PROMPTS = [
    "a traffic accident with visibly damaged cars",
    "a wrecked car with a smashed front bumper",
    "crumpled car metal after a collision",
    "broken glass and car parts scattered on the road after a crash",
    "an accident scene with stopped cars and debris",
    "a car crash with airbags deployed",
    "a smashed car with a broken windshield after a crash",
    "two cars rear-end collision at an intersection",
    "a traffic accident with two damaged cars after a collision",
    "a serious vehicle collision with crumpled metal and damaged cars",
    "an accident scene with crashed vehicles blocking the road",
    "cars badly damaged after a traffic accident",
    "a car crash with airbags deployed and front-end damage",
    "two moving cars colliding at an intersection",
    "a side-impact collision between two driving cars",
    "a car crash where two vehicles hit each other while driving",
    "two cars crashing into each other on a city street",
    "a moving car crashing into another moving vehicle",
    "a high-speed collision between two cars on the road",
    "two vehicles colliding while driving through an intersection",
    "cars crashing together in the middle of traffic",
    "a moving car crashing into a parked car",
    "a car rear-ending a stopped car in traffic",
    "a car crashing into a tree beside the road",
    "a vehicle hitting a street pole or traffic light",
    "a car crashed into a wall with a damaged front",
    "a car with a smashed front end after a collision",
    "two cars about to collide at an intersection",
    "a car impact caught at the moment of collision",
]

CRASH_NEG_PROMPTS = [
    "a normal street scene with buildings and cars",
    "an ordinary urban scene with no emergency",
    "cars driving normally on a road with no accident",
    "normal traffic flowing on a city street",
    "a road scene with cars and no collision",
    "a parking lot with cars parked normally",
    "normal traffic moving through an intersection",
    "vehicles driving safely on a city street",
    "a calm urban street with normal traffic",
    "a highway scene with vehicles moving normally",
    "cars waiting at a traffic light with no collision",
    "a normal road scene with vehicles and no accident",
    "a car slowly parking into a parking spot next to another car",
    "a car parallel parking carefully near other vehicles",
    "a vehicle reversing slowly into a parking space",
    "two cars parked very close together with no damage",
    "a car slowly approaching another car in heavy traffic without collision",
    "a vehicle inching forward in stop-and-go traffic",
]

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "detect_scale": {
            "type": "number", "default": 1.0, "minimum": 0.1, "maximum": 1.0,
            "description": "detección a fracción de la resolución (el original corre a resolución nativa)",
        },
        "crash_alert_threshold": {
            "type": "number", "default": ALERT_THRESHOLD,
            "description": "umbral de score suavizado para confirmar choque",
        },
        "trigger_frames": {
            "type": "integer", "default": C_TRIGGER_FRAMES,
            "description": "frames consecutivos de candidato para lockear el ROI",
        },
        "vehicle_min_conf": {
            "type": "number", "default": _VEHICLE_MIN_CONF,
            "description": "confianza mínima YOLO para considerar un vehículo",
        },
    },
}


# =============================================================================
# KSM / PC / TC (idénticos al original)
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
    hat_k_idx = int(np.argmax(sims))
    offset = hat_k_idx % max(1, N // KEY_FRAMES_K)
    other_idx = [min((i * N // KEY_FRAMES_K) + offset, N - 1) for i in range(1, KEY_FRAMES_K)]
    return hat_k_idx, other_idx


def compute_pc(key_frame_bgr, text_feat, clip_model, preprocess):
    img = cv2.resize(key_frame_bgr, (IMG_SIZE, IMG_SIZE))
    all_maps = []
    for wh, ww in [WIN_SMALL, WIN_MID, WIN_LARGE]:
        n_rows = IMG_SIZE // wh
        n_cols = IMG_SIZE // ww
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
        return 0.0, None
    M = np.mean(all_maps, axis=0).astype(np.float32)
    M_norm = (M - M.min()) / (M.max() - M.min() + 1e-8)
    flat = M_norm.flatten()
    score_pc = float(np.mean(np.sort(flat)[len(flat) // 2:]))
    hot_th = 0.65
    rows_h, cols_h = np.where(M_norm >= hot_th)
    if len(rows_h) == 0:
        rows_h, cols_h = np.where(M_norm >= float(np.percentile(M_norm, 90)))
    hot_bbox_norm: tuple[float, float, float, float] | None = None
    if len(rows_h) > 0:
        hot_bbox_norm = (
            float(cols_h.min()) / IMG_SIZE, float(rows_h.min()) / IMG_SIZE,
            float(cols_h.max() + 1) / IMG_SIZE, float(rows_h.max() + 1) / IMG_SIZE,
        )
    return score_pc, hot_bbox_norm


def compute_tc(key_frames_bgr, text_feat, clip_model, preprocess):
    if len(key_frames_bgr) < 4:
        return 0.0
    frames = [cv2.resize(f, (IMG_SIZE, IMG_SIZE)) for f in key_frames_bgr[:4]]
    cell = 112
    grids_bgr = []
    for wh, ww in [WIN_SMALL, WIN_MID, WIN_LARGE]:
        n_rows = IMG_SIZE // wh
        n_cols = IMG_SIZE // ww
        for r in range(n_rows):
            for c in range(n_cols):
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
    raw_sim = float(sims[int(np.argmax(sims))])
    return float(np.clip((raw_sim - TC_SIM_LOW) / (TC_SIM_HIGH - TC_SIM_LOW + 1e-8), 0.0, 1.0))


# =============================================================================
# FEATURES DE TEXTO (precomputadas una vez por el modelo)
# =============================================================================
class _CrashTextFeats:
    def __init__(self, clip_model, tokenizer):
        with torch.no_grad():
            tf = clip_model.encode_text(
                tokenizer(["two cars colliding or making violent contact on a road"]).to(DEVICE)).float()
            tf /= tf.norm(dim=-1, keepdim=True)
            pf = clip_model.encode_text(tokenizer(CRASH_POS_PROMPTS).to(DEVICE)).float()
            nf = clip_model.encode_text(tokenizer(CRASH_NEG_PROMPTS).to(DEVICE)).float()
            pf /= pf.norm(dim=-1, keepdim=True)
            nf /= nf.norm(dim=-1, keepdim=True)
            self.pos = pf.mean(0, keepdim=True)
            self.neg = nf.mean(0, keepdim=True)
            self.pos /= self.pos.norm(dim=-1, keepdim=True)
            self.neg /= self.neg.norm(dim=-1, keepdim=True)
        self.ksm = tf
        self.logit_scale = float(clip_model.logit_scale.exp().item())


# =============================================================================
# EXPERTO CRASH (heurísticas + CLIP; lógica idéntica al original, inline)
# =============================================================================
class CrashExpert:
    _VIEW_SIZE = (640, 480)

    def __init__(self, clip_model, preprocess, feats: _CrashTextFeats,
                 alert_threshold: float, trigger_frames: int):
        self._model = clip_model
        self._preprocess = preprocess
        self.feats = feats
        self.alert_threshold = alert_threshold
        self.trigger_frames = trigger_frames

        self.crash_counter = 0.0
        self.crash_locked = False
        self.crash_buffer: list = []
        self.crash_is_detected = False
        self.crash_consec_pos = 0
        self.crash_last_crop = None
        self.crash_last_crop_bbox: tuple[int, int, int, int] | None = None
        self.crash_car_prev_center: dict = {}
        self.crash_interaction_type = None
        self.last_interaction_type = None  # tipo del último dispatch (para el evento)
        self.active_car_pair = None
        self.crashed_stationary_cars: list = []
        self.active_pileup = None
        self.crash_active_frames = 0
        self.crash_ttl_counter = 0
        self._cycle_id = 0
        self._cycle_dispatch_seq = 0
        self._motion_history: dict = {}
        self._vel_ema: dict[int, float] = {}
        self._vel_ema_prev: dict[int, float] = {}
        self._pair_dist_history: dict[tuple, deque] = {}

        self.score_history: list[float] = []
        self.smoothed_score: float = 0.0
        self._clip_last_set_score: float | None = None
        self._last_hot_bbox_norm: tuple[float, float, float, float] | None = None
        self.last_score_kf: float = 0.0
        self.last_score_pc: float = 0.0
        self.last_score_tc: float = 0.0

    def process_heuristics(self, frame, tracker_data, frame_id=0, frame_w=0, frame_h=0):
        car_xyxy = tracker_data["vehicles_xyxy"]
        car_ids = tracker_data["vehicles_ids"]
        person_xyxy = tracker_data["persons_xyxy"]
        _fw = frame_w if frame_w > 0 else frame.shape[1]
        _fh = frame_h if frame_h > 0 else frame.shape[0]
        if self.crash_is_detected:
            self.crash_active_frames += 1
            if self.crash_active_frames > C_CRASH_MAX_FRAMES:
                self.crash_is_detected = False
                self.crash_active_frames = 0
                self.crash_consec_pos = 0
        else:
            self.crash_active_frames = 0
        self._process_crash(frame, car_xyxy, car_ids, person_xyxy,
                            frame_id=frame_id, frame_w=_fw, frame_h=_fh)

    @torch.no_grad()
    def predict(self, frames: list[np.ndarray], dispatch_id_str: str = "") -> dict:
        N = len(frames)
        if N < KEY_FRAMES_K:
            return {"detected": False, "set_score": 0.0, "smoothed": 0.0,
                    "hits": 0, "score_kf": 0.0, "score_pc": 0.0, "score_tc": 0.0}

        hat_k_idx, other_idx = ksm_select_key_frames(
            frames, self.feats.ksm, self._model, self._preprocess)
        key_frames = [frames[hat_k_idx]] + [frames[i] for i in other_idx]
        hat_k = frames[hat_k_idx]

        feat = _encode_image_feats(self._model, self._preprocess, [hat_k])
        diff = float(self.feats.logit_scale *
                     (feat @ self.feats.pos.T - feat @ self.feats.neg.T).item())
        score_kf = float(1.0 / (1.0 + np.exp(-(diff - KF_SIGMOID_CENTER) / KF_SIGMOID_SCALE)))
        self.last_score_kf = score_kf

        score_pc, hot_bbox_norm = compute_pc(hat_k, self.feats.ksm, self._model, self._preprocess)
        self._last_hot_bbox_norm = hot_bbox_norm

        score_tc = compute_tc(key_frames, self.feats.ksm, self._model, self._preprocess)

        ascore = GAMMA1 * score_kf + GAMMA2 * score_pc + GAMMA3 * score_tc
        smoothed = self._update_smooth(ascore)

        self.last_score_pc = score_pc
        self.last_score_tc = score_tc

        # umbral reducido durante el arranque del historial (igual que el original)
        if len(self.score_history) < 3:
            effective_threshold = self.alert_threshold * 0.78
        else:
            effective_threshold = self.alert_threshold
        detected = smoothed > effective_threshold

        log.debug("CLIP crash dispatch=%s ascore=%.3f smooth=%.3f kf=%.3f pc=%.3f tc=%.3f %s",
                  dispatch_id_str, ascore, smoothed, score_kf, score_pc, score_tc,
                  "CHOQUE" if detected else "normal")
        return {"detected": detected, "set_score": round(ascore, 3),
                "smoothed": round(smoothed, 3), "hits": 1 if detected else 0,
                "score_kf": round(score_kf, 3), "score_pc": round(score_pc, 3),
                "score_tc": round(score_tc, 3)}

    def _update_smooth(self, ascore: float) -> float:
        self.score_history.append(ascore)
        if len(self.score_history) > SMOOTH_HISTORY:
            self.score_history.pop(0)
        if len(self.score_history) >= 3:
            arr = gaussian_filter1d(np.array(self.score_history, dtype=np.float32), sigma=SMOOTH_SIGMA)
            self.smoothed_score = float(arr[-1])
        else:
            self.smoothed_score = ascore
        return self.smoothed_score

    def _run_clip_result(self, res: dict, dispatch_id_str: str) -> None:
        smoothed = res.get("smoothed", 0.0)
        set_score = res.get("set_score", 0.0)
        confirmed = smoothed > self.alert_threshold
        prev = self.crash_is_detected
        if confirmed:
            self.crash_is_detected = True
            self.crash_consec_pos = res.get("hits", 0)
            self.crash_ttl_counter = 0
        else:
            if self.crash_is_detected:
                self.crash_ttl_counter += 1
                if self.crash_ttl_counter > C_CRASH_TTL:
                    self.crash_is_detected = False
                    self.crash_consec_pos = 0
            else:
                self.crash_consec_pos = res.get("hits", 0)
        if not prev and self.crash_is_detected:
            log.info("CHOQUE_CONFIRMED dispatch=%s set_score=%.3f", dispatch_id_str, set_score)
        self._clip_last_set_score = set_score

    # ── heuristic helpers ─────────────────────────────────────────────────
    @staticmethod
    def _norm_box(box, w, h):
        return [box[0] / w, box[1] / h, box[2] / w, box[3] / h]

    @staticmethod
    def _denorm_box(nb, w, h):
        return [nb[0] * w, nb[1] * h, nb[2] * w, nb[3] * h]

    @staticmethod
    def _xyxy_center(b):
        return np.array([(b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0], dtype=np.float32)

    @staticmethod
    def _iou_xyxy(bA, bB) -> float:
        xA1, yA1, xA2, yA2 = float(bA[0]), float(bA[1]), float(bA[2]), float(bA[3])
        xB1, yB1, xB2, yB2 = float(bB[0]), float(bB[1]), float(bB[2]), float(bB[3])
        inter = max(0., min(xA2, xB2) - max(xA1, xB1)) * max(0., min(yA2, yB2) - max(yA1, yB1))
        aA = max(0., xA2 - xA1) * max(0., yA2 - yA1)
        aB = max(0., xB2 - xB1) * max(0., yB2 - yB1)
        u = aA + aB - inter
        return float(inter / u) if u > 0 else 0.0

    def _match_box_by_iou(self, prev, boxes, thr):
        best, biou = None, 0.0
        for b in boxes:
            iou = self._iou_xyxy(prev, b)
            if iou > biou:
                biou, best = iou, b
        return (best, biou) if (best is not None and biou >= thr) else (None, biou)

    @staticmethod
    def _crop_raw(frame, x1, y1, x2, y2, pad):
        H, W = frame.shape[:2]
        x1 = max(0, int(x1 - pad)); y1 = max(0, int(y1 - pad))
        x2 = min(W - 1, int(x2 + pad)); y2 = min(H - 1, int(y2 + pad))
        if x2 <= x1 or y2 <= y1:
            return None
        c = frame[y1:y2 + 1, x1:x2 + 1]
        return (c, (x1, y1, x2, y2)) if c.size > 0 else None

    def _crop_union_with_padding(self, frame, b1, b2, pad):
        x1 = min(float(b1[0]), float(b2[0]))
        y1 = min(float(b1[1]), float(b2[1]))
        x2 = max(float(b1[2]), float(b2[2]))
        y2 = max(float(b1[3]), float(b2[3]))
        result = self._crop_raw(frame, x1, y1, x2, y2, pad)
        if result is None:
            return None, None
        crop, bbox = result
        return cv2.resize(crop, self._VIEW_SIZE), bbox

    def _crop_multi_car_union(self, frame, boxes, pad):
        if not boxes:
            return None, None
        x1 = min(float(b[0]) for b in boxes)
        y1 = min(float(b[1]) for b in boxes)
        x2 = max(float(b[2]) for b in boxes)
        y2 = max(float(b[3]) for b in boxes)
        result = self._crop_raw(frame, x1, y1, x2, y2, pad)
        if result is None:
            return None, None
        crop, bbox = result
        return cv2.resize(crop, self._VIEW_SIZE), bbox

    @staticmethod
    def _short_side_segments(box):
        x1, y1, x2, y2 = float(box[0]), float(box[1]), float(box[2]), float(box[3])
        w, h = x2 - x1, y2 - y1
        if w <= h:
            return [(np.array([x1, y1], dtype=float), np.array([x1, y2], dtype=float)),
                    (np.array([x2, y1], dtype=float), np.array([x2, y2], dtype=float))]
        return [(np.array([x1, y1], dtype=float), np.array([x2, y1], dtype=float)),
                (np.array([x1, y2], dtype=float), np.array([x2, y2], dtype=float))]

    @staticmethod
    def _pt_seg_dist(p, a, b) -> float:
        p, a, b = np.asarray(p, dtype=float), np.asarray(a, dtype=float), np.asarray(b, dtype=float)
        ab = b - a; ab2 = float(np.dot(ab, ab))
        if ab2 <= 1e-12:
            return float(np.linalg.norm(p - a))
        t = float(np.clip(np.dot(p - a, ab) / ab2, 0., 1.))
        return float(np.linalg.norm(p - (a + t * ab)))

    @staticmethod
    def _adaptive_thr(nb1, nb2, frame_w=1., frame_h=1.,
                      base_px=85., k=0.35, min_px=30., max_px=75.) -> float:
        diag = math.hypot(frame_w, frame_h) if (frame_w > 1 and frame_h > 1) else 1.
        d1 = math.hypot(nb1[2] - nb1[0], nb1[3] - nb1[1])
        d2 = math.hypot(nb2[2] - nb2[0], nb2[3] - nb2[1])
        return float(np.clip(base_px / diag + k * 0.5 * (d1 + d2), min_px / diag, max_px / diag))

    def _car_is_moving(self, car_id, center, diag=1.0) -> bool:
        prev = self.crash_car_prev_center.get(car_id)
        self.crash_car_prev_center[car_id] = center
        inst = 0.0 if prev is None else float(np.linalg.norm(center - prev))
        moved = inst > C_MOTION_THRESHOLD / diag
        if car_id not in self._motion_history:
            self._motion_history[car_id] = deque(maxlen=C_MOTION_HISTORY_LEN)
        self._motion_history[car_id].append(moved)
        self._vel_ema[car_id] = 0.3 * inst + 0.7 * self._vel_ema.get(car_id, 0)
        prev_ema = self._vel_ema_prev.get(car_id, self._vel_ema[car_id])
        dec = (prev_ema - self._vel_ema[car_id]) / (prev_ema + 1e-6)
        self._vel_ema_prev[car_id] = prev_ema
        return self._vel_ema[car_id] > 2.0 / diag or dec > 0.60

    def _is_closing_fast(self, id1, id2, dist, dist_deque=None) -> bool:
        key = (min(id1, id2), max(id1, id2))
        hist = dist_deque if dist_deque is not None else self._pair_dist_history.setdefault(
            key, deque(maxlen=C_CLOSING_HISTORY))
        if dist_deque is None:
            hist.append(dist)
        if len(hist) < 2:
            return True
        oldest = hist[0]
        return False if oldest < 1e-6 else ((oldest - dist) / oldest) >= C_CLOSING_DROP_RATIO

    def _pair_is_converging(self, id1, id2) -> bool:
        key = (min(id1, id2), max(id1, id2))
        hist = list(self._pair_dist_history.get(key, []))
        if len(hist) < C_CLOSING_MIN_FRAMES:
            return False
        oldest = hist[0]
        if oldest < 1e-9 or (oldest - hist[-1]) / oldest < 0.15:
            return False
        dec = sum(1 for a, b in zip(hist, hist[1:]) if b < a)
        return dec / max(len(hist) - 1, 1) >= 0.70

    def _is_rear_collision(self, mc, sc) -> bool:
        angle = np.abs(np.degrees(np.arctan2(
            (mc[1] + mc[3]) / 2 - (sc[1] + sc[3]) / 2,
            (mc[0] + mc[2]) / 2 - (sc[0] + sc[2]) / 2,
        )))
        return (angle < C_REAR_ANGLE_TOL or angle > 180 - C_REAR_ANGLE_TOL or
                (90 - C_REAR_ANGLE_TOL) < angle < (90 + C_REAR_ANGLE_TOL))

    def _update_crashed_cars(self, car_boxes):
        updated = []
        for cc in self.crashed_stationary_cars:
            m, iou = self._match_box_by_iou(cc["box"], car_boxes, 0.4)
            if m is not None and iou > 0.7:
                updated.append({"box": np.array(m[:4], dtype=float),
                                "frames_stationary": cc["frames_stationary"] + 1})
        self.crashed_stationary_cars = updated

    def _detect_pileup_collision(self, frame, car_boxes, frame_w=1, frame_h=1):
        if not self.crashed_stationary_cars:
            return False, None, None, None
        targets = [c for c in self.crashed_stationary_cars
                   if c["frames_stationary"] >= C_STATIONARY_THR_FRAMES]
        if not targets:
            return False, None, None, None
        for car in car_boxes:
            nc = self._norm_box(car, frame_w, frame_h)
            cc = self._xyxy_center(nc)
            for tgt in targets:
                ntg = self._norm_box(tgt["box"], frame_w, frame_h)
                tc = self._xyxy_center(ntg)
                if self._iou_xyxy(car, tgt["box"]) > 0.5:
                    continue
                s1, s2 = self._short_side_segments(nc)
                s3, s4 = self._short_side_segments(ntg)
                dist = min(self._pt_seg_dist(tc, s1[0], s1[1]), self._pt_seg_dist(tc, s2[0], s2[1]),
                           self._pt_seg_dist(cc, s3[0], s3[1]), self._pt_seg_dist(cc, s4[0], s4[1]))
                thr = self._adaptive_thr(nc, ntg, frame_w, frame_h) * C_REAR_DIST_MULT
                if dist < thr and self._is_rear_collision(car, tgt["box"]):
                    inv_norm = [ntg, nc]
                    for other in targets:
                        if np.array_equal(other["box"], tgt["box"]):
                            continue
                        no = self._norm_box(other["box"], frame_w, frame_h)
                        if np.linalg.norm(tc - self._xyxy_center(no)) < (
                            math.hypot(ntg[2] - ntg[0], ntg[3] - ntg[1]) +
                            math.hypot(no[2] - no[0], no[3] - no[1])
                        ) / 2 * 2.5:
                            inv_norm.append(no)
                    inv_px = [self._denorm_box(b, frame_w, frame_h) for b in inv_norm]
                    crop, bbox = self._crop_multi_car_union(frame, inv_px, C_CAR_PAD_PX)
                    return True, crop, len(inv_norm), bbox
        return False, None, None, None

    def _get_car_car_crop(self, frame, car_xyxy, car_ids, frame_w=1, frame_h=1):
        diag = math.hypot(frame_w, frame_h)
        for i in range(len(car_xyxy)):
            for j in range(i + 1, len(car_xyxy)):
                nb1 = self._norm_box(car_xyxy[i], frame_w, frame_h)
                nb2 = self._norm_box(car_xyxy[j], frame_w, frame_h)
                c1 = self._xyxy_center(nb1)
                c2 = self._xyxy_center(nb2)
                id1 = int(car_ids[i]) if car_ids[i] >= 0 else i
                id2 = int(car_ids[j]) if car_ids[j] >= 0 else j
                moving_a = self._car_is_moving(id1, c1, diag)
                moving_b = self._car_is_moving(id2, c2, diag)
                motion_gate = moving_a or moving_b or self._pair_is_converging(id1, id2)
                s1, s2 = self._short_side_segments(nb1)
                s3, s4 = self._short_side_segments(nb2)
                dist = min(self._pt_seg_dist(c2, s1[0], s1[1]), self._pt_seg_dist(c2, s2[0], s2[1]),
                           self._pt_seg_dist(c1, s3[0], s3[1]), self._pt_seg_dist(c1, s4[0], s4[1]))
                thr = self._adaptive_thr(nb1, nb2, frame_w, frame_h)
                within = dist < thr
                key = (min(id1, id2), max(id1, id2))
                self._pair_dist_history.setdefault(key, deque(maxlen=16)).append(dist)
                closing_gate = False
                if motion_gate and within:
                    closing_gate = self._is_closing_fast(id1, id2, dist,
                                                         dist_deque=self._pair_dist_history.get(key))
                if not motion_gate or not within or not closing_gate:
                    continue
                db1 = self._denorm_box(nb1, frame_w, frame_h)
                db2 = self._denorm_box(nb2, frame_w, frame_h)
                crop, bbox = self._crop_union_with_padding(frame, db1, db2, C_CAR_PAD_PX)
                if crop is not None:
                    return True, crop, (car_xyxy[i], car_xyxy[j]), bbox
        return False, None, None, None

    def _process_crash(self, frame, car_xyxy, car_ids, person_xyxy,
                       frame_id=0, frame_w=1., frame_h=1.):
        if len(car_xyxy) > 0:
            self._update_crashed_cars(car_xyxy)

        if not self.crash_locked:
            pu_found, pu_crop, _pu_n, pu_bbox = self._detect_pileup_collision(
                frame, car_xyxy, frame_w, frame_h)
            if pu_found and pu_crop is not None:
                self.crash_last_crop = pu_crop
                self.crash_last_crop_bbox = pu_bbox
                self.crash_counter += 1
                if self.crash_counter >= self.trigger_frames:
                    self.crash_locked = True
                    self._cycle_id += 1; self._cycle_dispatch_seq = 0
                    self.crash_interaction_type = "multi-car"
                    self.active_pileup = {"cars": [np.array(b[:4], dtype=float) for b in car_xyxy]}
                    self.active_car_pair = None
                    self.crash_buffer.append(pu_crop)
            else:
                cc_found, cc_crop, cc_pair, cc_bbox = self._get_car_car_crop(
                    frame, car_xyxy, car_ids, frame_w, frame_h)
                if cc_found and cc_crop is not None:
                    self.crash_last_crop = cc_crop
                    self.crash_last_crop_bbox = cc_bbox
                    self.crash_counter += 1
                    if self.crash_counter >= self.trigger_frames:
                        self.crash_locked = True
                        self._cycle_id += 1; self._cycle_dispatch_seq = 0
                        self.crash_interaction_type = "car-car"
                        self.active_pileup = None
                        if cc_pair is not None:
                            b1, b2 = cc_pair
                            self.active_car_pair = {
                                "b1": np.array(b1[:4], dtype=float),
                                "b2": np.array(b2[:4], dtype=float),
                            }
                            self.crashed_stationary_cars += [
                                {"box": np.array(b1[:4], dtype=float), "frames_stationary": 0},
                                {"box": np.array(b2[:4], dtype=float), "frames_stationary": 0},
                            ]
                        self.crash_buffer.append(cc_crop)
                else:
                    self.crash_counter = max(0., self.crash_counter - 0.5)
                    if self.crash_counter <= 0:
                        self.crash_buffer = []
                        self.active_car_pair = self.active_pileup = None
                        self.crash_interaction_type = None
        else:
            sample = self.crash_last_crop
            if self.crash_interaction_type == "multi-car" and self.active_pileup and len(car_xyxy) > 0:
                matched = [np.array(m[:4], dtype=float)
                           for pc in self.active_pileup["cars"]
                           for m, _ in [self._match_box_by_iou(pc, car_xyxy, 0.3)]
                           if m is not None]
                if matched:
                    self.active_pileup["cars"] = matched
                    crop, bbox = self._crop_multi_car_union(frame, matched, C_CAR_PAD_PX)
                    if crop is not None:
                        sample = crop
                        self.crash_last_crop = crop
                        self.crash_last_crop_bbox = bbox
            elif self.crash_interaction_type == "car-car" and self.active_car_pair and len(car_xyxy) > 0:
                m1, _ = self._match_box_by_iou(self.active_car_pair["b1"], car_xyxy, C_CAR_PAIR_IOU_THR)
                rem = [b for b in car_xyxy
                       if not (m1 is not None and np.allclose(np.array(b[:4], dtype=float),
                                                              np.array(m1[:4], dtype=float)))]
                m2, _ = self._match_box_by_iou(self.active_car_pair["b2"], rem, C_CAR_PAIR_IOU_THR)
                if m1 is not None and m2 is not None:
                    self.active_car_pair = {"b1": np.array(m1[:4], dtype=float),
                                            "b2": np.array(m2[:4], dtype=float)}
                    crop, bbox = self._crop_union_with_padding(frame, m1, m2, C_CAR_PAD_PX)
                    if crop is not None:
                        sample = crop
                        self.crash_last_crop = crop
                        self.crash_last_crop_bbox = bbox

            if sample is not None:
                self.crash_buffer.append(sample)

            if len(self.crash_buffer) >= C_BUFFER_SIZE:
                # ponytail: scoring CLIP inline en el thread del stream (~100-400ms
                # por batch de 16 crops); si bloquea demasiado, mover a worker propio
                self._cycle_dispatch_seq += 1
                dispatch_id_str = f"C{self._cycle_id}.D{self._cycle_dispatch_seq}"
                self.last_interaction_type = self.crash_interaction_type
                try:
                    res = self.predict(list(self.crash_buffer), dispatch_id_str=dispatch_id_str)
                    self._run_clip_result(res, dispatch_id_str)
                except Exception:
                    log.exception("CLIP_ERROR crash dispatch=%s", dispatch_id_str)
                self.crash_locked = False
                self.crash_counter = 0.0
                self.crash_buffer = []
                self.active_car_pair = self.active_pileup = None
                self.crash_interaction_type = None


# =============================================================================
# TRACKER (YOLO compartido del modelo padre; estado bytetrack por sesión)
# =============================================================================
class _SessionTracker:
    """Equivalente a YOLO.track(persist=True) del original, pero con el
    BYTETracker instanciado por sesión para no mezclar IDs entre streams."""

    def __init__(self, yolo: YOLO, vehicle_min_conf: float):
        from kinich.models._tracking import make_bytetracker

        self._yolo = yolo
        self.vehicle_min_conf = vehicle_min_conf
        self._tracker = make_bytetracker(TRACKER_YAML)

    @staticmethod
    def _empty() -> dict:
        z4 = np.zeros((0, 4), dtype=np.float32)
        z1 = np.zeros((0,), dtype=np.int32)
        return {"persons_xyxy": z4, "persons_ids": z1,
                "vehicles_xyxy": z4, "vehicles_ids": z1,
                "vehicles_confs": np.zeros((0,), dtype=np.float32)}

    def process(self, frame: np.ndarray) -> dict:
        # conf=0.1: mismo default que usa Model.track (bytetrack necesita
        # detecciones de baja confianza para la asociación de segunda etapa)
        res = self._yolo.predict(frame, classes=_ALL_TRACK_CLASSES, conf=0.1, verbose=False)[0]
        if res.boxes is None or len(res.boxes) == 0:
            return self._empty()
        tracks = self._tracker.update(res.boxes.cpu().numpy(), frame)
        if len(tracks) == 0:
            return self._empty()
        # columnas: x1, y1, x2, y2, track_id, conf, cls, idx
        xyxy = tracks[:, :4].astype(np.float32)
        ids = tracks[:, 4].astype(np.int32)
        conf = tracks[:, 5].astype(np.float32)
        cls = tracks[:, 6].astype(np.int32)
        pm = cls == 0
        vm = np.isin(cls, _VEHICLE_CLASSES) & (conf >= self.vehicle_min_conf)
        return {"persons_xyxy": xyxy[pm], "persons_ids": ids[pm],
                "vehicles_xyxy": xyxy[vm], "vehicles_ids": ids[vm],
                "vehicles_confs": conf[vm]}


# =============================================================================
# SESIÓN (port del loop de run(), inline y sin display)
# =============================================================================
class _CrashSession(StreamSession):
    def __init__(self, model: "CrashDetector", config: dict[str, Any]):
        self.model = model
        self.detect_scale = max(0.1, min(1.0, float(config.get("detect_scale", 1.0))))
        alert_th = float(config.get("crash_alert_threshold", ALERT_THRESHOLD))
        trigger = int(config.get("trigger_frames", C_TRIGGER_FRAMES))
        vehicle_conf = float(config.get("vehicle_min_conf", _VEHICLE_MIN_CONF))

        self.tracker = _SessionTracker(model.yolo, vehicle_conf)
        self.expert = CrashExpert(model.clip_model, model.clip_preprocess,
                                  model.crash_feats, alert_th, trigger)
        self.f_idx = 0
        self.prev_detected = False

    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        events: list[Event] = []

        if self.detect_scale < 1.0:
            H, W = frame.shape[:2]
            frame_det = cv2.resize(frame, (int(W * self.detect_scale), int(H * self.detect_scale)))
        else:
            frame_det = frame
        fh, fw = frame_det.shape[:2]

        tracker_data = self.tracker.process(frame_det)
        self.expert.process_heuristics(frame_det, tracker_data, frame_id=self.f_idx,
                                       frame_w=fw, frame_h=fh)

        # decay suave cuando no detecta (igual que el loop original)
        if not self.expert.crash_is_detected:
            self.expert.smoothed_score = max(0.0, self.expert.smoothed_score - 0.02)

        # ── transiciones → eventos ──────────────────────────────────────────
        cur = self.expert.crash_is_detected
        scale_up = 1.0 / self.detect_scale
        if cur and not self.prev_detected:
            roi = self.expert.crash_last_crop_bbox
            events.append(Event(
                type="CHOQUE", score=self.expert.smoothed_score, ts=ts,
                extra={"kf": self.expert.last_score_kf, "pc": self.expert.last_score_pc,
                       "tc": self.expert.last_score_tc,
                       "set_score": self.expert._clip_last_set_score,
                       "interaction_type": self.expert.last_interaction_type,
                       "roi_bbox": [int(v * scale_up) for v in roi] if roi is not None else None,
                       "hot_bbox_norm": self.expert._last_hot_bbox_norm},
            ))
        elif not cur and self.prev_detected:
            events.append(Event(type="CHOQUE_OFF", score=self.expert.smoothed_score, ts=ts))
        self.prev_detected = cur

        self.f_idx += 1
        return events


# =============================================================================
# MODELO
# =============================================================================
class CrashDetector(CVModel):
    info = ModelInfo(
        name="crash-detector",
        version="1.0",
        task="Detección de choques vehiculares en CCTV (YOLO track + heurísticas de proximidad + CLIP temporal)",
        capabilities={Capability.CLIP, Capability.STREAM},
        event_types=["CHOQUE", "CHOQUE_OFF"],
        config_schema=CONFIG_SCHEMA,
        target_fps=10.0,  # equivale al --frame-skip 1 de los ejemplos del original en cámaras de 20 fps
    )

    def __init__(self) -> None:
        self.yolo: YOLO | None = None
        self.clip_model = None
        self.clip_preprocess = None
        self.crash_feats: _CrashTextFeats | None = None

    def load(self) -> None:
        log.info("Cargando YOLO crash: %s", WEIGHTS)
        self.yolo = YOLO(WEIGHTS)
        log.info("Cargando CLIP %s (openai) en %s", CLIP_MODEL_NAME, DEVICE)
        self.clip_model, _, self.clip_preprocess = open_clip.create_model_and_transforms(
            CLIP_MODEL_NAME, pretrained="openai", device=DEVICE)
        self.clip_model.eval()
        tokenizer = open_clip.get_tokenizer(CLIP_MODEL_NAME)
        self.crash_feats = _CrashTextFeats(self.clip_model, tokenizer)

    def warmup(self) -> None:
        session = self.create_session({})
        session.process_frame(np.zeros((360, 640, 3), dtype=np.uint8), 0.0)
        session.close()

    def unload(self) -> None:
        self.yolo = self.clip_model = self.clip_preprocess = None
        self.crash_feats = None

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        if self.yolo is None:
            self.load()
        return _CrashSession(self, config)
