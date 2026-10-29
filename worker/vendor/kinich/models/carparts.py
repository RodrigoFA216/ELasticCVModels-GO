"""CarPartsDetector — port de CarPartsDetector_Production.py al contrato Kinich.

Lógica de detección intacta respecto al original (basado en CarPartsExpert_V4):
  YOLO track (persona + vehículos, ByteTrack) -> vehículo ancla -> candidatos
  persona-vehículo por proximidad/movimiento -> crop ROI congelado/estabilizado
  -> buffer de segmento (24 crops 224x224) -> experto CLIP (KSM + KF con
  early-exit + PC WinCLIP + TC grid temporal) -> suavizado gaussiano -> alerta
  por transición, con decaimiento de score cuando no hay crop activo.

Eliminado del original: display cv2 (overlays/banner/info bar), imwrite de
alertas y CP_PROD_LIVE.jpg, CLI, video writer, workers propios (el thread del
stream ya es el worker) y el resumen final por consola.
Cambios: CLIP vía open_clip (el repo `clip` de OpenAI está archivado),
features de texto precomputadas una vez en load(), y ByteTracker instanciado
por sesión (el `track(persist=True)` del original guarda estado en el objeto
YOLO, que aquí se comparte entre streams).
"""
import logging
import os
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
WEIGHTS = os.environ.get("KINICH_CARPARTS_WEIGHTS", "weights/yolo11x.pt")
# -quickgelu: los pesos openai usan QuickGELU; sin el sufijo open_clip
# construye GELU estándar y los scores divergen del script original.
# El original arrancaba con ViT-L/14 por CLI; aquí el default sigue la
# convención del repo y se puede subir a ViT-L-14-quickgelu por env var.
CLIP_MODEL_NAME = os.environ.get("KINICH_CARPARTS_CLIP_MODEL", "ViT-B-32-quickgelu")
TRACKER_YAML = "bytetrack.yaml"

# =============================================================================
# CONFIG (constantes del original, sin cambios de valor)
# =============================================================================
CLASS_PERSON = 0
CLASS_VEHICLES = [2, 5, 7]

REF_W = 1280
REF_H = 720

CP_PERIMETRO_PX_REF = 50
CP_PAD_SOLO_REF = 15
CP_PAD_GRUPO_REF = 40
CP_GRUPO_DIST_PX_REF = 200

SEGMENT_LEN = 24
KEY_FRAMES_K = 4
GAMMA1 = 0.60
GAMMA2 = 0.25
GAMMA3 = 0.15
ALERT_THRESHOLD = 0.50
SMOOTH_SIGMA = 2.0
SMOOTH_HISTORY = 12

TC_SIM_LOW = 0.15
TC_SIM_HIGH = 0.35

IMG_SIZE = 240
WIN_SMALL = (48, 48)
WIN_MID = (80, 80)
WIN_LARGE = (120, 120)

CROP_FREEZE_FRAMES = 60
CROP_CONFIRM_FRAMES = 5
CROP_GENEROUS_PAD_REF = 80
CROP_MOVE_TH_PX_REF = 45
CROP_MAX_W_FRAC = 0.65
CROP_MAX_H_FRAC = 0.80

ANCHOR_CONFIRM_FRAMES = 3
ANCHOR_MISS_MAX = 25

# Parámetros del sigmoide KF por familia de modelo CLIP (nombres open_clip)
_MODEL_PARAMS = {
    "B-32": {"kf_center": 2.8, "kf_scale": 1.5, "kf_exit_th": 0.25},
    "B-16": {"kf_center": 2.8, "kf_scale": 1.5, "kf_exit_th": 0.25},
    "L-14": {"kf_center": 1.2, "kf_scale": 1.0, "kf_exit_th": 0.10},
}
_DEFAULT_MODEL_PARAMS = _MODEL_PARAMS["B-32"]

KSM_QUERY = "a person stealing car headlights or parts from a parked vehicle"
POS_PROMPTS = [
    "a person prying out car headlights with tools",
    "hands pulling off a vehicle headlight",
    "someone dismantling the front lights of a car",
    "a thief stealing headlights from a vehicle",
]
NEG_PROMPTS = [
    "a person walking past a car",
    "owner opening a car door",
    "a parked car with lights intact",
    "street background",
]

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "alert_threshold": {
            "type": "number", "default": ALERT_THRESHOLD, "minimum": 0.0, "maximum": 1.0,
            "description": "umbral de score suavizado para confirmar ROBO",
        },
        "segment_len": {
            "type": "integer", "default": SEGMENT_LEN, "minimum": KEY_FRAMES_K,
            "description": "crops acumulados antes de correr el experto CLIP",
        },
    },
}


def _params_for(model_name: str) -> dict:
    for key, params in _MODEL_PARAMS.items():
        if key in model_name:
            return params
    return _DEFAULT_MODEL_PARAMS


# =============================================================================
# HELPERS — escala de umbrales (idéntico al original)
# =============================================================================
def _scale_thresholds(frame_w: int, frame_h: int) -> dict:
    _ = frame_h
    s = frame_w / REF_W
    return {
        "perimetro": max(10, int(CP_PERIMETRO_PX_REF * s)),
        "pad_solo": max(4, int(CP_PAD_SOLO_REF * s)),
        "pad_grupo": max(8, int(CP_PAD_GRUPO_REF * s)),
        "grupo_dist": max(40, int(CP_GRUPO_DIST_PX_REF * s)),
        "generous_pad": max(10, int(CROP_GENEROUS_PAD_REF * s)),
        "move_th": max(8, int(CROP_MOVE_TH_PX_REF * s)),
        "scale": s,
    }


# =============================================================================
# HELPERS — geometría (idénticos al original)
# =============================================================================
def _dist_persona_vehiculo(box_p, box_v) -> float:
    cx = (box_p[0] + box_p[2]) / 2.0
    cy = box_p[3]
    dx = max(box_v[0] - cx, 0.0, cx - box_v[2])
    dy = max(box_v[1] - cy, 0.0, cy - box_v[3])
    return float(np.sqrt(dx * dx + dy * dy))


def _score_candidato(box_p, box_v, prev_box_p) -> float:
    cx = (box_p[0] + box_p[2]) / 2.0
    cy = box_p[3]
    dx = max(box_v[0] - cx, 0.0, cx - box_v[2])
    dy = max(box_v[1] - cy, 0.0, cy - box_v[3])
    dist = float(np.sqrt(dx * dx + dy * dy)) + 1e-3
    prox_score = 100.0 / dist
    mov_score = 0.0
    if prev_box_p is not None:
        xA = max(box_p[0], prev_box_p[0])
        yA = max(box_p[1], prev_box_p[1])
        xB = min(box_p[2], prev_box_p[2])
        yB = min(box_p[3], prev_box_p[3])
        inter = max(0.0, xB - xA) * max(0.0, yB - yA)
        a1 = (box_p[2] - box_p[0]) * (box_p[3] - box_p[1])
        a2 = (prev_box_p[2] - prev_box_p[0]) * (prev_box_p[3] - prev_box_p[1])
        iou = inter / (a1 + a2 - inter + 1e-6)
        mov_score = (1.0 - iou) * 50.0
    return prox_score + mov_score


def _compute_crop_bbox(frame, box_p, box_v, pad) -> tuple[int, int, int, int] | None:
    H, W = frame.shape[:2]
    cx_p = (box_p[0] + box_p[2]) / 2.0
    vx_near = box_v[0] if cx_p < (box_v[0] + box_v[2]) / 2 else box_v[2]
    vy_near = box_v[1]
    x1 = int(max(0, min(box_p[0], vx_near) - pad))
    y1 = int(max(0, min(box_p[1], vy_near) - pad))
    x2 = int(min(W, max(box_p[2], vx_near) + pad))
    y2 = int(min(H, max(box_p[3], vy_near) + pad))
    if x2 <= x1 or y2 <= y1:
        return None
    if (x2 - x1) > W * CROP_MAX_W_FRAC or (y2 - y1) > H * CROP_MAX_H_FRAC:
        return None
    return (x1, y1, x2, y2)


def _bbox_center(bbox) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _crop_from_bbox(frame, bbox) -> np.ndarray | None:
    x1, y1, x2, y2 = bbox
    crop = frame[y1:y2, x1:x2]
    return crop if (crop is not None and crop.size > 0) else None


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


# =============================================================================
# KSM / PC / TC (idénticos al original, encode compartido)
# =============================================================================
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
    """WinCLIP multi-escala: score + bbox caliente normalizado dentro del crop."""
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
        th_val = float(np.percentile(M_norm, 90))
        rows_h, cols_h = np.where(M_norm >= th_val)
    hot_bbox_norm = None
    if len(rows_h) > 0:
        hot_bbox_norm = (float(cols_h.min()) / IMG_SIZE,
                         float(rows_h.min()) / IMG_SIZE,
                         float(cols_h.max() + 1) / IMG_SIZE,
                         float(rows_h.max() + 1) / IMG_SIZE)
    return score_pc, hot_bbox_norm


def compute_tc(key_frames_bgr, text_feat, clip_model, preprocess):
    """Grid 2x2 temporal con los 4 key frames, multi-escala."""
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
                grid_bgr = np.vstack([
                    np.hstack([cv2.resize(p, (cell, cell)) for p in patches[:2]]),
                    np.hstack([cv2.resize(p, (cell, cell)) for p in patches[2:]]),
                ])
                grids_bgr.append(grid_bgr)
    if not grids_bgr:
        return 0.0
    feats = _encode_image_feats(clip_model, preprocess, grids_bgr)
    tf = text_feat / text_feat.norm(dim=-1, keepdim=True)
    sims = (tf @ feats.T).cpu().squeeze(0).numpy()
    raw_sim = float(sims[int(np.argmax(sims))])
    return float(np.clip((raw_sim - TC_SIM_LOW) / (TC_SIM_HIGH - TC_SIM_LOW + 1e-8), 0.0, 1.0))


# =============================================================================
# EXPERTO CLIP (features de texto precomputadas por el modelo; estado por sesión)
# =============================================================================
class _CarPartsTextFeats:
    def __init__(self, clip_model, tokenizer) -> None:
        with torch.no_grad():
            tf = clip_model.encode_text(tokenizer([KSM_QUERY]).to(DEVICE)).float()
            tf /= tf.norm(dim=-1, keepdim=True)
            pf = clip_model.encode_text(tokenizer(POS_PROMPTS).to(DEVICE)).float()
            nf = clip_model.encode_text(tokenizer(NEG_PROMPTS).to(DEVICE)).float()
            # ojo: el original NO re-normaliza el promedio pos/neg — se respeta
            self.pos = (pf / pf.norm(dim=-1, keepdim=True)).mean(0, keepdim=True)
            self.neg = (nf / nf.norm(dim=-1, keepdim=True)).mean(0, keepdim=True)
        self.ksm = tf
        self.logit_scale = float(clip_model.logit_scale.exp().item())


class CLIPAnomalyDetector:
    """KSM + KF (con early-exit) + PC + TC + suavizado gaussiano."""

    def __init__(self, clip_model, preprocess, feats: _CarPartsTextFeats,
                 params: dict, alert_threshold: float) -> None:
        self.model = clip_model
        self.preprocess = preprocess
        self.feats = feats
        self.alert_threshold = alert_threshold
        self._kf_center = params["kf_center"]
        self._kf_scale = params["kf_scale"]
        self._kf_exit_th = params["kf_exit_th"]

        self.score_history: list[float] = []
        self.smoothed_score = 0.0
        self.last_score_kf = 0.0
        self.last_score_pc = 0.0
        self.last_score_tc = 0.0
        self.last_ascore = 0.0
        self.last_hot_bbox_norm: tuple[float, float, float, float] | None = None

    @torch.no_grad()
    def _score_kf(self, frame_bgr: np.ndarray) -> float:
        feat = _encode_image_feats(self.model, self.preprocess, [frame_bgr])
        diff = float(self.feats.logit_scale * (feat @ self.feats.pos.T - feat @ self.feats.neg.T).item())
        return float(1.0 / (1.0 + np.exp(-(diff - self._kf_center) / self._kf_scale)))

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

    def process_segment(self, frames_bgr: list[np.ndarray]) -> dict:
        N = len(frames_bgr)
        if N < KEY_FRAMES_K:
            return {"smoothed": self.smoothed_score,
                    "detected": self.smoothed_score > self.alert_threshold, "early_exit": False}

        hat_k_idx, other_idx = ksm_select_key_frames(frames_bgr, self.feats.ksm, self.model, self.preprocess)
        key_frames = [frames_bgr[i] for i in [hat_k_idx] + list(other_idx)]
        hat_k = frames_bgr[hat_k_idx]

        score_kf = self._score_kf(hat_k)
        self.last_score_kf = score_kf

        if score_kf < self._kf_exit_th:
            ascore = GAMMA1 * score_kf
            smoothed = self._update_smooth(ascore)
            self.last_ascore = ascore
            self.last_score_pc = 0.0
            self.last_score_tc = 0.0
            log.debug("[CP] EARLY_EXIT kf=%.3f<%.2f ascore=%.3f smooth=%.3f",
                      score_kf, self._kf_exit_th, ascore, smoothed)
            return {"smoothed": smoothed,
                    "detected": smoothed > self.alert_threshold, "early_exit": True}

        score_pc, hot_bbox_norm = compute_pc(hat_k, self.feats.ksm, self.model, self.preprocess)
        self.last_hot_bbox_norm = hot_bbox_norm
        score_tc = compute_tc(key_frames, self.feats.ksm, self.model, self.preprocess)

        ascore = GAMMA1 * score_kf + GAMMA2 * score_pc + GAMMA3 * score_tc
        smoothed = self._update_smooth(ascore)
        self.last_ascore = ascore
        self.last_score_pc = score_pc
        self.last_score_tc = score_tc
        detected = smoothed > self.alert_threshold
        log.debug("[CP] ascore=%.3f smooth=%.3f kf=%.3f pc=%.3f tc=%.3f %s",
                  ascore, smoothed, score_kf, score_pc, score_tc, "ALERTA" if detected else "normal")
        return {"smoothed": smoothed, "detected": detected, "early_exit": False}


# =============================================================================
# SESIÓN (port del loop de run(), inline, sin display ni disco)
# =============================================================================
def _make_tracker():
    # BYTETracker por sesión (el .track(persist=True) del original guarda estado
    # en el objeto YOLO; aquí uno por stream para no mezclar IDs).
    from kinich.models._tracking import make_bytetracker

    return make_bytetracker(TRACKER_YAML)


class _CarPartsSession(StreamSession):
    def __init__(self, model: "CarPartsDetector", config: dict[str, Any]):
        self.model = model
        self.alert_threshold = float(config.get("alert_threshold", ALERT_THRESHOLD))
        self.segment_len = max(KEY_FRAMES_K, int(config.get("segment_len", SEGMENT_LEN)))

        self.tracker = _make_tracker()
        self.detector = CLIPAnomalyDetector(
            model.clip_model, model.clip_preprocess, model.text_feats,
            model.clip_params, self.alert_threshold)

        self._th: dict | None = None  # umbrales escalados al primer frame

        # Estado heurística (mismos nombres que el loop original)
        self.seg_buffer: list[np.ndarray] = []
        self.cp_prev_boxes: dict[int, np.ndarray] = {}

        self.anchor_v_id: int | None = None
        self.anchor_box_v: np.ndarray | None = None
        self.anchor_miss = 0
        self.anchor_idle = 0
        self.anchor_cand_id: int | None = None
        self.anchor_cand_count = 0

        self.frozen_bbox: tuple[int, int, int, int] | None = None
        self.frozen_center: tuple[float, float] | None = None
        self.frozen_timer = 0
        self.pending_bbox: tuple[int, int, int, int] | None = None
        self.pending_center: tuple[float, float] | None = None
        self.pending_count = 0
        self.crop_state = "inactivo"

        self._is_detected = False
        self._last_smoothed = 0.0
        self.prev_detected = False

    # ── YOLO + ByteTrack (equivalente a gen_model.track(persist=True)) ──────
    def _track(self, frame: np.ndarray):
        res = self.model.yolo.predict(
            frame, classes=CLASS_VEHICLES + [CLASS_PERSON], verbose=False)[0]
        det = res.boxes.cpu().numpy()
        if len(det) == 0:
            return res
        tracks = self.tracker.update(det, frame)
        if len(tracks) == 0:
            return res
        idx = tracks[:, -1].astype(int)
        res = res[idx]
        res.update(boxes=torch.as_tensor(tracks[:, :-1]))
        return res

    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        H_f, W_f = frame.shape[:2]
        if self._th is None:
            self._th = _scale_thresholds(W_f, H_f)
        th = self._th

        # ── YOLO track en resolución nativa ─────────────────────────────────
        res = self._track(frame)

        person_xyxy = np.zeros((0, 4), dtype=np.float32)
        person_ids = np.zeros((0,), dtype=np.int32)
        vehicle_xyxy = np.zeros((0, 4), dtype=np.float32)
        vehicle_ids = np.zeros((0,), dtype=np.int32)

        if res.boxes is not None and len(res.boxes) > 0:
            b = res.boxes
            all_xyxy = b.xyxy.cpu().numpy().astype(np.float32)
            all_ids = (b.id.cpu().numpy().astype(np.int32)
                       if b.id is not None
                       else np.full(len(all_xyxy), -1, dtype=np.int32))
            all_cls = b.cls.cpu().numpy().astype(np.int32)
            p_mask = (all_cls == CLASS_PERSON)
            v_mask = np.isin(all_cls, CLASS_VEHICLES)
            person_xyxy = all_xyxy[p_mask]
            person_ids = all_ids[p_mask]
            vehicle_xyxy = all_xyxy[v_mask]
            vehicle_ids = all_ids[v_mask]

        # ── Vehículo ancla ──────────────────────────────────────────────────
        if self.anchor_v_id is not None:
            anchor_found = False
            for j, v_id in enumerate(vehicle_ids):
                if int(v_id) == self.anchor_v_id:
                    self.anchor_box_v = vehicle_xyxy[j].copy()
                    self.anchor_miss = 0
                    anchor_found = True
                    break
            if not anchor_found:
                self.anchor_miss += 1
                if self.anchor_miss > ANCHOR_MISS_MAX:
                    self.anchor_v_id = self.anchor_box_v = None
                    self.anchor_miss = self.anchor_idle = 0
                    self.anchor_cand_id = None
                    self.anchor_cand_count = 0

        eval_vehicle_xyxy = np.array([self.anchor_box_v]) if self.anchor_v_id is not None else vehicle_xyxy
        eval_vehicle_ids = np.array([self.anchor_v_id]) if self.anchor_v_id is not None else vehicle_ids

        # ── Candidatos persona-vehículo ─────────────────────────────────────
        candidatos = []
        for i, box_p in enumerate(person_xyxy):
            p_id = int(person_ids[i]) if i < len(person_ids) else -1
            for j, box_v in enumerate(eval_vehicle_xyxy):
                dist = _dist_persona_vehiculo(box_p, box_v)
                if dist < th["perimetro"]:
                    v_id = int(eval_vehicle_ids[j]) if j < len(eval_vehicle_ids) else -1
                    score = _score_candidato(box_p, box_v, self.cp_prev_boxes.get(p_id))
                    candidatos.append((score, dist, box_p.copy(), box_v.copy(), p_id, v_id))

        ids_visibles = set()
        for i, box_p in enumerate(person_xyxy):
            p_id = int(person_ids[i]) if i < len(person_ids) else -1
            self.cp_prev_boxes[p_id] = box_p.copy()
            ids_visibles.add(p_id)
        self.cp_prev_boxes = {k: v for k, v in self.cp_prev_boxes.items() if k in ids_visibles}

        best_box_p = best_box_v = None
        pad = th["pad_solo"]

        if candidatos:
            candidatos.sort(key=lambda x: -x[0])
            _, _, best_box_p, best_box_v, _, best_v_id = candidatos[0]

            if self.anchor_v_id is None:
                if best_v_id == self.anchor_cand_id:
                    self.anchor_cand_count += 1
                else:
                    self.anchor_cand_id = best_v_id
                    self.anchor_cand_count = 1
                if self.anchor_cand_count >= ANCHOR_CONFIRM_FRAMES:
                    self.anchor_v_id = self.anchor_cand_id
                    self.anchor_box_v = best_box_v.copy()
                    self.anchor_miss = 0
                    self.anchor_cand_id = None
                    self.anchor_cand_count = 0

            if len(candidatos) >= 2:
                box_p2 = candidatos[1][2]
                cx1 = (best_box_p[0] + best_box_p[2]) / 2
                cy1 = (best_box_p[1] + best_box_p[3]) / 2
                cx2 = (box_p2[0] + box_p2[2]) / 2
                cy2 = (box_p2[1] + box_p2[3]) / 2
                if np.sqrt((cx1 - cx2) ** 2 + (cy1 - cy2) ** 2) < th["grupo_dist"]:
                    best_box_p = np.array([
                        min(best_box_p[0], box_p2[0]), min(best_box_p[1], box_p2[1]),
                        max(best_box_p[2], box_p2[2]), max(best_box_p[3], box_p2[3]),
                    ], dtype=np.float32)
                    pad = th["pad_grupo"]
        else:
            self.anchor_cand_id = None
            self.anchor_cand_count = 0
            if self.anchor_v_id is not None:
                self.anchor_idle += 1
                if self.anchor_idle > ANCHOR_MISS_MAX:
                    self.anchor_v_id = self.anchor_box_v = None
                    self.anchor_miss = self.anchor_idle = 0

        if best_box_p is not None:
            self.anchor_idle = 0

        # ── Crop estabilizado (congelado / expandido / pendiente) ───────────
        if best_box_p is not None:
            self.frozen_timer = CROP_FREEZE_FRAMES
            if self.frozen_bbox is None:
                generous_bbox = _compute_crop_bbox(frame, best_box_p, best_box_v, th["generous_pad"])
                if generous_bbox is not None:
                    self.frozen_bbox = generous_bbox
                    self.frozen_center = _bbox_center(self.frozen_bbox)
                    self.pending_bbox = None
                    self.pending_count = 0
                    self.crop_state = "congelado"
            else:
                fx1, fy1, fx2, fy2 = self.frozen_bbox
                needs_expand = False
                ex1, ey1, ex2, ey2 = fx1, fy1, fx2, fy2
                for _, _, cp_box_p, _, _, _ in candidatos:
                    pcx = (cp_box_p[0] + cp_box_p[2]) / 2
                    pcy = (cp_box_p[1] + cp_box_p[3]) / 2
                    if pcx < fx1 or pcx > fx2 or pcy < fy1 or pcy > fy2:
                        needs_expand = True
                        ep = th["pad_solo"]
                        ex1 = min(ex1, int(max(0, cp_box_p[0] - ep)))
                        ey1 = min(ey1, int(max(0, cp_box_p[1] - ep)))
                        ex2 = max(ex2, int(min(W_f, cp_box_p[2] + ep)))
                        ey2 = max(ey2, int(min(H_f, cp_box_p[3] + ep)))
                if needs_expand:
                    ew, eh = ex2 - ex1, ey2 - ey1
                    if ew <= W_f * CROP_MAX_W_FRAC and eh <= H_f * CROP_MAX_H_FRAC:
                        self.frozen_bbox = (ex1, ey1, ex2, ey2)
                        self.frozen_center = _bbox_center(self.frozen_bbox)
                        self.crop_state = "expandido"
                        self.pending_bbox = None
                        self.pending_count = 0

                raw_bbox = _compute_crop_bbox(frame, best_box_p, best_box_v, pad)
                if raw_bbox is not None and self.crop_state != "expandido":
                    raw_center = _bbox_center(raw_bbox)
                    dist_to_frozen = np.sqrt(
                        (raw_center[0] - self.frozen_center[0]) ** 2 +
                        (raw_center[1] - self.frozen_center[1]) ** 2)
                    if dist_to_frozen < th["move_th"]:
                        self.pending_bbox = None
                        self.pending_count = 0
                        self.crop_state = "congelado"
                    else:
                        if self.pending_bbox is None:
                            self.pending_bbox = _compute_crop_bbox(frame, best_box_p, best_box_v, th["generous_pad"])
                            self.pending_center = raw_center
                            self.pending_count = 1
                        else:
                            if np.sqrt((raw_center[0] - self.pending_center[0]) ** 2 +
                                       (raw_center[1] - self.pending_center[1]) ** 2) < th["move_th"]:
                                self.pending_count += 1
                            else:
                                self.pending_bbox = _compute_crop_bbox(frame, best_box_p, best_box_v, th["generous_pad"])
                                self.pending_center = raw_center
                                self.pending_count = 1
                        self.crop_state = "pendiente"
                        if self.pending_count >= CROP_CONFIRM_FRAMES and self.pending_bbox is not None:
                            self.frozen_bbox = self.pending_bbox
                            self.frozen_center = self.pending_center
                            self.pending_bbox = None
                            self.pending_count = 0
                            self.crop_state = "congelado"
                elif self.crop_state != "expandido":
                    self.pending_bbox = None
                    self.pending_count = 0
                    self.crop_state = "congelado"
        else:
            self.frozen_timer = max(0, self.frozen_timer - 1)
            self.pending_bbox = None
            self.pending_count = 0
            if self.frozen_timer == 0:
                self.frozen_bbox = None
                self.frozen_center = None
                self.crop_state = "inactivo"
            else:
                self.crop_state = "congelado"

        current_crop = _crop_from_bbox(frame, self.frozen_bbox) if self.frozen_bbox is not None else None

        # ── Buffer segmento → experto CLIP ──────────────────────────────────
        if current_crop is not None:
            self.seg_buffer.append(cv2.resize(current_crop, (224, 224)))
            if len(self.seg_buffer) >= self.segment_len:
                # ponytail: scoring CLIP inline en el thread del stream (el
                # original usaba worker y descartaba segmentos si estaba
                # ocupado); si bloquea demasiado, mover a worker propio
                seg_res = self.detector.process_segment(self.seg_buffer)
                self._last_smoothed = seg_res["smoothed"]
                self._is_detected = seg_res["detected"]
                self.seg_buffer = []
        else:
            self.seg_buffer = []
            self.detector.smoothed_score = max(0.0, self.detector.smoothed_score - 0.02)
            self._last_smoothed = self.detector.smoothed_score
            self._is_detected = self._last_smoothed > self.alert_threshold

        # ── Transición de alerta → eventos ──────────────────────────────────
        just_triggered = self._is_detected and not self.prev_detected
        just_cleared = not self._is_detected and self.prev_detected
        self.prev_detected = self._is_detected

        events: list[Event] = []
        if just_triggered:
            d = self.detector
            hot_bbox = None
            if self.frozen_bbox is not None and d.last_hot_bbox_norm is not None:
                # proyección del hot area WinCLIP (normalizada al crop) al frame
                fx1, fy1, fx2, fy2 = self.frozen_bbox
                cw, ch = fx2 - fx1, fy2 - fy1
                nx1, ny1, nx2, ny2 = d.last_hot_bbox_norm
                hot_bbox = [int(fx1 + nx1 * cw), int(fy1 + ny1 * ch),
                            int(fx1 + nx2 * cw), int(fy1 + ny2 * ch)]
            events.append(Event(
                type="ROBO", score=self._last_smoothed, ts=ts,
                extra={
                    "kf": d.last_score_kf, "pc": d.last_score_pc, "tc": d.last_score_tc,
                    "anchor_id": self.anchor_v_id,
                    "anchor_bbox": ([int(v) for v in self.anchor_box_v]
                                    if self.anchor_box_v is not None else None),
                    "roi_bbox": list(self.frozen_bbox) if self.frozen_bbox is not None else None,
                    "hot_bbox": hot_bbox,
                },
            ))
        elif just_cleared:
            events.append(Event(type="ROBO_OFF", score=self._last_smoothed, ts=ts))

        return events


# =============================================================================
# MODELO
# =============================================================================
class CarPartsDetector(CVModel):
    info = ModelInfo(
        name="carparts-detector",
        version="4.0",
        task="Detección de robo de autopartes en CCTV (YOLO track + ancla + CLIP anomaly)",
        capabilities={Capability.CLIP, Capability.STREAM},
        event_types=["ROBO", "ROBO_OFF"],
        config_schema=CONFIG_SCHEMA,
        target_fps=20.0,  # el original procesaba todos los frames (frame_skip=0) en cámaras ~20 fps
    )

    def __init__(self) -> None:
        self.yolo: YOLO | None = None
        self.clip_model = None
        self.clip_preprocess = None
        self.text_feats: _CarPartsTextFeats | None = None
        # sigmoide KF adaptativo según modelo CLIP (igual que el original)
        self.clip_params = _params_for(CLIP_MODEL_NAME)

    def load(self) -> None:
        log.info("Cargando YOLO carparts: %s", WEIGHTS)
        self.yolo = YOLO(WEIGHTS)
        log.info("Cargando CLIP %s (openai) en %s", CLIP_MODEL_NAME, DEVICE)
        self.clip_model, _, self.clip_preprocess = open_clip.create_model_and_transforms(
            CLIP_MODEL_NAME, pretrained="openai", device=DEVICE)
        self.clip_model.eval()
        tokenizer = open_clip.get_tokenizer(CLIP_MODEL_NAME)
        self.text_feats = _CarPartsTextFeats(self.clip_model, tokenizer)

    def warmup(self) -> None:
        session = self.create_session({})
        session.process_frame(np.zeros((360, 640, 3), dtype=np.uint8), 0.0)
        session.close()

    def unload(self) -> None:
        self.yolo = self.clip_model = self.clip_preprocess = None
        self.text_feats = None

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        if self.yolo is None:
            self.load()
        return _CarPartsSession(self, config)
