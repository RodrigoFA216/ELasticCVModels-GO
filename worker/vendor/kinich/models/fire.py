"""FireDetector — port de FireDetector_Production_V2.py al contrato Kinich.

Lógica de detección intacta respecto al original (ArconteDetection_DebugTools):
  fuego: HSV + motion diff -> candidatos -> YOLO -> flicker + saturación
         -> buffer de crops -> experto CLIP (KSM + KF + PC + TC) -> suavizado
  humo:  HSV + MOG2 -> grid de patches -> buffer de crops -> experto CLIP top-k

Eliminado del original: display cv2, imwrite de alertas, CLI, workers propios
(el thread del stream ya es el worker) y el quantizador SmoothQuant/INT8.
Cambios: un solo YOLO compartido (el original cargaba dos copias), CLIP vía
open_clip (el repo `clip` de OpenAI está archivado), features de texto
precomputadas una vez en load().
"""
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
WEIGHTS = os.environ.get("KINICH_FIRE_WEIGHTS", "weights/fire_best_large.pt")
# -quickgelu: los pesos openai usan QuickGELU; sin el sufijo open_clip
# construye GELU estándar y los scores divergen del script original
CLIP_MODEL_NAME = os.environ.get("KINICH_FIRE_CLIP_MODEL", "ViT-B-32-quickgelu")

# =============================================================================
# CONFIG (constantes del original, sin cambios de valor)
# =============================================================================
CLIP_CROP_SIZE = (640, 480)

FIRE_REF_W = 1280

FIRE_HISTORY_LEN = 18
FIRE_PRE_CROP_CONF = 0.05
FIRE_ZOOM_CONF = 0.10
FIRE_CROP_PAD_REF = 200
FIRE_MASK_MIN_AREA_REF = 40
FIRE_MOTION_THR = 8
FIRE_MIN_BBOX_AREA_REF = 1200
FIRE_FLICKER_FRAMES = 8
FIRE_FLICKER_MIN_STD = 7.0
FIRE_FLICKER_SIZE = (32, 32)
FIRE_FLICKER_RESET_DIST_REF = 80
FIRE_SAT_PATCH_SIZE = (64, 64)
FIRE_SAT_STD_MIN = 15.0

FIRE_CLIP_BUFFER_SIZE = 20
FIRE_KEY_FRAMES_K = 6
FIRE_CONSEC_MIN = 4
FIRE_TTL = 90
FIRE_ALERT_THRESHOLD = 0.54
FIRE_GAMMA1 = 0.55
FIRE_GAMMA2 = 0.30
FIRE_GAMMA3 = 0.15
FIRE_KF_SIGMOID_CENTER = 3.1
FIRE_KF_SIGMOID_SCALE = 1.5
FIRE_TC_SIM_LOW = 0.18
FIRE_TC_SIM_HIGH = 0.38
FIRE_SMOOTH_SIGMA = 2.0
FIRE_SMOOTH_HISTORY = 12
FIRE_IMG_SIZE = 240
FIRE_WIN_SMALL = (48, 48)
FIRE_WIN_MID = (80, 80)
FIRE_WIN_LARGE = (120, 120)

PATCH_GRID_COLS = 4
PATCH_GRID_ROWS = 4
PATCH_PX_RATIO_TH = 0.08
PATCH_PAD_RATIO = 0.15
PATCH_MOG2_HISTORY = 200
PATCH_MOG2_THRESH = 40

SMOKE_HSV_DARK_LO = np.array([0, 0, 20])
SMOKE_HSV_DARK_HI = np.array([179, 50, 160])
SMOKE_HSV_LIGHT_LO = np.array([0, 0, 140])
SMOKE_HSV_LIGHT_HI = np.array([179, 35, 230])

SMOKE_HISTORY_LEN = 30
SMOKE_CLIP_BUFFER_SIZE = 20
SMOKE_CLIP_STRIDE = 2
SMOKE_CLIP_DIFF_TH = 1.5
SMOKE_CLIP_MIN_HITS = 3
SMOKE_CLIP_SET_SCORE_TH = 2.0
SMOKE_CLIP_TOPK = 4
SMOKE_CONSEC_MIN = 2
SMOKE_TTL = 120

FIRE_POS_PROMPTS = [
    "a building on fire with visible flames",
    "a vehicle on fire with flames coming out",
    "a fire burning on the street outdoors",
    "bright orange flames burning intensely",
    "flames coming from a window in a building",
    "a trash fire with visible flames and smoke",
    "a small fire on the ground with flames",
    "a fire with flames and thick smoke rising",
    "an active fire with visible flames in an urban scene",
]
FIRE_NEG_PROMPTS = [
    "a normal city street at night with lights",
    "car headlights and tail lights on a road",
    "a sunset or sunrise sky with orange colors",
    "reflections of lights on a wet street",
    "a bright lamp or spotlight with no fire",
    "a construction site light with no flames",
    "a red car parked on a street with no fire",
    "a yellow taxi cab driving on a road",
    "orange painted vehicle with no flames",
    "an indoor warm light scene with no flames",
    "a yellow bus or van driving on a street",
    "a bright yellow car with patterned markings",
    "a yellow vehicle with logo or text on the side",
    "yellow painted metal surface with no fire",
    "a yellow truck or heavy vehicle with no flames",
]
SMOKE_POS_PROMPTS = [
    "thick smoke rising from a fire outdoors",
    "black smoke coming from a burning vehicle",
    "gray smoke plume rising into the sky",
    "smoke billowing from a building",
    "dense smoke cloud over a street",
    "smoke rising from debris after a fire",
    "smoke coming out of a window",
    "a street scene with visible smoke haze from a fire",
    "a smoke plume from an accident or fire scene",
    "heavy smoke with low visibility",
]
SMOKE_NEG_PROMPTS = [
    "foggy street scene with fog not smoke",
    "clouds in the sky on a normal day",
    "steam rising from a manhole or vent",
    "dust cloud from construction with no fire",
    "car exhaust smoke from a tailpipe",
    "a normal street scene with no smoke",
    "haze from sunlight or camera exposure",
    "blur or compression artifacts in a video",
    "mist near water or a fountain",
    "smoke-like lighting glare but no smoke",
]

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "detect_scale": {
            "type": "number", "default": 1.0, "minimum": 0.1, "maximum": 1.0,
            "description": "detección a fracción de la resolución (0.5 → 4x más rápido HSV/MOG2)",
        },
        "fire_alert_threshold": {"type": "number", "description": "umbral de score suavizado (default según modelo CLIP)"},
        "fire_consec_min": {"type": "integer", "description": "batches CLIP positivos consecutivos para confirmar"},
    },
}


# =============================================================================
# FIRE helpers (idénticos al original)
# =============================================================================
def _scale_fire_thresholds(frame_w: int) -> dict:
    s = frame_w / FIRE_REF_W
    return {
        "crop_pad": max(20, int(FIRE_CROP_PAD_REF * s)),
        "mask_min_area": max(4, int(FIRE_MASK_MIN_AREA_REF * s * s)),
        "min_bbox_area": max(100, int(FIRE_MIN_BBOX_AREA_REF * s * s)),
        "flicker_reset_dist": max(20, int(FIRE_FLICKER_RESET_DIST_REF * s)),
    }


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


def ksm_select_key_frames_fire(frames_bgr, text_feat, clip_model, preprocess):
    N = len(frames_bgr)
    feats = _encode_image_feats(clip_model, preprocess, frames_bgr, cuadrar=True)
    tf = text_feat / text_feat.norm(dim=-1, keepdim=True)
    sims = (tf @ feats.T).cpu().squeeze(0).numpy()
    hat_k_idx = int(np.argmax(sims))
    offset = hat_k_idx % max(1, N // FIRE_KEY_FRAMES_K)
    other_idx = [min((i * N // FIRE_KEY_FRAMES_K) + offset, N - 1) for i in range(1, FIRE_KEY_FRAMES_K)]
    return hat_k_idx, other_idx


def compute_pc_fire(key_frame_bgr, text_feat, clip_model, preprocess):
    img = cv2.resize(key_frame_bgr, (FIRE_IMG_SIZE, FIRE_IMG_SIZE))
    all_maps = []
    for wh, ww in [FIRE_WIN_SMALL, FIRE_WIN_MID, FIRE_WIN_LARGE]:
        n_rows = FIRE_IMG_SIZE // wh
        n_cols = FIRE_IMG_SIZE // ww
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
            cv2.resize((sim_map - mu) / sigma, (FIRE_IMG_SIZE, FIRE_IMG_SIZE), interpolation=cv2.INTER_LINEAR)
        )
    if not all_maps:
        return 0.0
    m_norm = np.mean(all_maps, axis=0).astype(np.float32)
    m_norm = (m_norm - m_norm.min()) / (m_norm.max() - m_norm.min() + 1e-8)
    flat = m_norm.flatten()
    return float(np.mean(np.sort(flat)[len(flat) // 2:]))


def compute_tc_fire_6(key_frames_bgr, text_feat, clip_model, preprocess):
    if len(key_frames_bgr) < FIRE_KEY_FRAMES_K:
        return 0.0
    frames = [cv2.resize(f, (FIRE_IMG_SIZE, FIRE_IMG_SIZE)) for f in key_frames_bgr[:6]]
    cell = 112
    grids_bgr = []
    for wh, ww in [FIRE_WIN_SMALL, FIRE_WIN_MID, FIRE_WIN_LARGE]:
        n_rows = FIRE_IMG_SIZE // wh
        n_cols = FIRE_IMG_SIZE // ww
        for r in range(n_rows):
            for c in range(n_cols):
                patches = [f[r * wh:(r + 1) * wh, c * ww:(c + 1) * ww] for f in frames]
                row0 = np.hstack([cv2.resize(p, (cell, cell)) for p in patches[:3]])
                row1 = np.hstack([cv2.resize(p, (cell, cell)) for p in patches[3:]])
                grid_bgr = cv2.resize(np.vstack([row0, row1]), (224, 224))
                grids_bgr.append(grid_bgr)
    if not grids_bgr:
        return 0.0
    feats = _encode_image_feats(clip_model, preprocess, grids_bgr)
    tf = text_feat / text_feat.norm(dim=-1, keepdim=True)
    sims = (tf @ feats.T).cpu().squeeze(0).numpy()
    raw_sim = float(sims[int(np.argmax(sims))])
    return float(np.clip((raw_sim - FIRE_TC_SIM_LOW) / (FIRE_TC_SIM_HIGH - FIRE_TC_SIM_LOW + 1e-8), 0, 1))


# =============================================================================
# EXPERTOS CLIP (features de texto precomputadas por el modelo; estado por sesión)
# =============================================================================
class _FireTextFeats:
    def __init__(self, clip_model, tokenizer):
        with torch.no_grad():
            tf = clip_model.encode_text(tokenizer(["a fire with visible flames burning"]).to(DEVICE)).float()
            tf /= tf.norm(dim=-1, keepdim=True)
            pf = clip_model.encode_text(tokenizer(FIRE_POS_PROMPTS).to(DEVICE)).float()
            nf = clip_model.encode_text(tokenizer(FIRE_NEG_PROMPTS).to(DEVICE)).float()
            pf /= pf.norm(dim=-1, keepdim=True)
            nf /= nf.norm(dim=-1, keepdim=True)
            self.pos = pf.mean(0, keepdim=True)
            self.neg = nf.mean(0, keepdim=True)
            self.pos /= self.pos.norm(dim=-1, keepdim=True)
            self.neg /= self.neg.norm(dim=-1, keepdim=True)
        self.ksm = tf
        self.logit_scale = float(clip_model.logit_scale.exp().item())


class _SmokeTextFeats:
    def __init__(self, clip_model, tokenizer):
        with torch.no_grad():
            pf = clip_model.encode_text(tokenizer(SMOKE_POS_PROMPTS).to(DEVICE)).float()
            nf = clip_model.encode_text(tokenizer(SMOKE_NEG_PROMPTS).to(DEVICE)).float()
            pf /= pf.norm(dim=-1, keepdim=True)
            nf /= nf.norm(dim=-1, keepdim=True)
            self.pos = pf.mean(0, keepdim=True)
            self.neg = nf.mean(0, keepdim=True)
            self.pos /= self.pos.norm(dim=-1, keepdim=True)
            self.neg /= self.neg.norm(dim=-1, keepdim=True)


class CLIPFireExpert:
    def __init__(self, clip_model, preprocess, feats: _FireTextFeats, alert_threshold: float):
        self.model = clip_model
        self.preprocess = preprocess
        self.feats = feats
        self.alert_threshold = alert_threshold
        self.smoothed_score = 0.0
        self.score_history: list[float] = []
        self.last_score_kf = self.last_score_pc = self.last_score_tc = 0.0

    @torch.no_grad()
    def _score_kf(self, frame_bgr):
        feat = _encode_image_feats(self.model, self.preprocess, [frame_bgr])
        diff = float(self.feats.logit_scale * (feat @ self.feats.pos.T - feat @ self.feats.neg.T).item())
        return float(1.0 / (1.0 + np.exp(-(diff - FIRE_KF_SIGMOID_CENTER) / FIRE_KF_SIGMOID_SCALE)))

    def process_segment(self, frames_bgr) -> dict:
        if len(frames_bgr) < FIRE_KEY_FRAMES_K:
            return {"smoothed": self.smoothed_score,
                    "detected": self.smoothed_score > self.alert_threshold, "skipped": True}
        hat_k_idx, other_idx = ksm_select_key_frames_fire(frames_bgr, self.feats.ksm, self.model, self.preprocess)
        key_frames = [frames_bgr[i] for i in [hat_k_idx] + list(other_idx)]
        hat_k = frames_bgr[hat_k_idx]
        score_kf = self._score_kf(hat_k)
        score_pc = compute_pc_fire(hat_k, self.feats.ksm, self.model, self.preprocess)
        score_tc = compute_tc_fire_6(key_frames, self.feats.ksm, self.model, self.preprocess)
        ascore = FIRE_GAMMA1 * score_kf + FIRE_GAMMA2 * score_pc + FIRE_GAMMA3 * score_tc
        self.score_history.append(ascore)
        if len(self.score_history) > FIRE_SMOOTH_HISTORY:
            self.score_history.pop(0)
        self.smoothed_score = (
            float(gaussian_filter1d(np.array(self.score_history, dtype=np.float32), sigma=FIRE_SMOOTH_SIGMA)[-1])
            if len(self.score_history) >= 3 else ascore
        )
        self.last_score_kf, self.last_score_pc, self.last_score_tc = score_kf, score_pc, score_tc
        return {"smoothed": self.smoothed_score,
                "detected": self.smoothed_score > self.alert_threshold, "skipped": False}


class CLIPSmokeExpert:
    def __init__(self, clip_model, preprocess, feats: _SmokeTextFeats):
        self.model = clip_model
        self.preprocess = preprocess
        self.feats = feats

    @torch.no_grad()
    def predict(self, frames) -> dict:
        scale = float(self.model.logit_scale.exp().item())
        diffs, hits = [], 0
        for fr in frames[::SMOKE_CLIP_STRIDE]:
            if fr is None or fr.size == 0:
                continue
            feat = _encode_image_feats(self.model, self.preprocess, [fr])
            diff = float(scale * (feat @ self.feats.pos.T - feat @ self.feats.neg.T).item())
            diffs.append(diff)
            if diff > SMOKE_CLIP_DIFF_TH:
                hits += 1
        if not diffs:
            return {"detected": False, "score": 0.0, "hits": 0}
        da = np.array(diffs, dtype=np.float32)
        raw_score = float(np.sort(da)[-min(SMOKE_CLIP_TOPK, len(da)):].mean())
        return {"detected": (hits >= SMOKE_CLIP_MIN_HITS) and (raw_score > SMOKE_CLIP_SET_SCORE_TH),
                "score": raw_score, "hits": hits}


# =============================================================================
# DETECTORES CLÁSICOS (estado por sesión; YOLO compartido del modelo padre)
# =============================================================================
class _FireCandidates:
    def __init__(self, yolo: YOLO):
        self._model = yolo
        self._prev_gray = None
        self._last_crop: np.ndarray | None = None
        self._last_bboxes: list = []
        self.history = deque(maxlen=FIRE_HISTORY_LEN)
        self._th: dict | None = None
        self._flicker_buf = deque(maxlen=FIRE_FLICKER_FRAMES)
        self._flicker_center = None
        self._flicker_score = 0.0
        self._sat_std = 0.0

    def process(self, frame) -> dict:
        H, W = frame.shape[:2]
        if self._th is None:
            self._th = _scale_fire_thresholds(W)
        crop_pad, mask_min, min_bbox = self._th["crop_pad"], self._th["mask_min_area"], self._th["min_bbox_area"]

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        m1 = cv2.inRange(hsv, np.array([0, 60, 120]), np.array([40, 255, 255]))
        m2 = cv2.inRange(hsv, np.array([170, 60, 120]), np.array([179, 255, 255]))
        m_color = cv2.morphologyEx(cv2.bitwise_or(m1, m2), cv2.MORPH_OPEN,
                                   cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if self._prev_gray is not None:
            _, md = cv2.threshold(cv2.absdiff(gray, self._prev_gray), FIRE_MOTION_THR, 255, cv2.THRESH_BINARY)
            m_final = cv2.bitwise_and(m_color, md)
        else:
            m_final = m_color
        self._prev_gray = gray

        cnts, _ = cv2.findContours(m_final, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for c in cnts:
            if cv2.contourArea(c) > mask_min:
                x, y, w, h = cv2.boundingRect(c)
                candidates.append(((x, y, w, h),
                                   (max(0, x - crop_pad), max(0, y - crop_pad),
                                    min(W, x + w + crop_pad), min(H, y + h + crop_pad))))

        f_active, crop, bboxes = False, None, []
        for tight, cb in candidates[:5]:
            patch = frame[cb[1]:cb[3], cb[0]:cb[2]]
            if patch.size == 0:
                continue
            results = self._model.predict(patch, conf=FIRE_PRE_CROP_CONF, verbose=False)[0]
            for rb in results.boxes:
                if int(rb.cls[0]) == 0 and float(rb.conf[0]) >= FIRE_ZOOM_CONF:
                    tx, ty, tw, th = tight
                    if tw * th < min_bbox:
                        continue
                    f_active = True
                    bboxes.append(tight)
            if f_active:
                crop = cv2.resize(patch, CLIP_CROP_SIZE)
                self._last_crop = crop

        if f_active and crop is not None:
            tx, ty, tw, th_ = max(bboxes, key=lambda b: b[2] * b[3])
            cx, cy = tx + tw // 2, ty + th_ // 2
            if self._flicker_center is not None:
                if np.hypot(cx - self._flicker_center[0], cy - self._flicker_center[1]) > self._th["flicker_reset_dist"]:
                    self._flicker_buf.clear()
            self._flicker_center = (cx, cy)
            gp = cv2.cvtColor(cv2.resize(crop, FIRE_FLICKER_SIZE), cv2.COLOR_BGR2GRAY).astype(np.float32)
            self._flicker_buf.append(gp)
            if len(self._flicker_buf) >= 3:
                self._flicker_score = float(np.std(np.array(list(self._flicker_buf)), axis=0).mean())
                if self._flicker_score < FIRE_FLICKER_MIN_STD:
                    f_active = False
            else:
                self._flicker_score = 0.0
        else:
            self._flicker_score = 0.0

        self._sat_std = 0.0
        if f_active and crop is not None:
            hsv_p = cv2.cvtColor(cv2.resize(crop, FIRE_SAT_PATCH_SIZE), cv2.COLOR_BGR2HSV).astype(np.float32)
            self._sat_std = float(np.std(hsv_p[:, :, 1]))
            if self._sat_std < FIRE_SAT_STD_MIN:
                f_active = False

        self.history.append(f_active)
        hits_f = sum(self.history)
        if bboxes:
            self._last_bboxes = bboxes
        if hits_f == 0:
            self._last_bboxes = []
        return {"hits_f": hits_f, "bboxes": self._last_bboxes, "crop": crop}


class _SmokeCandidates:
    def __init__(self):
        self._last_crop: np.ndarray | None = None
        self.history = deque(maxlen=SMOKE_HISTORY_LEN)
        self._mog2 = cv2.createBackgroundSubtractorMOG2(
            history=PATCH_MOG2_HISTORY, varThreshold=PATCH_MOG2_THRESH, detectShadows=False)
        self._focused_rc = None
        self.roi_locked = False

    def _patch_coords(self, r, c, H, W):
        ph, pw = H / PATCH_GRID_ROWS, W / PATCH_GRID_COLS
        py, px = int(ph * PATCH_PAD_RATIO), int(pw * PATCH_PAD_RATIO)
        return (max(0, int(r * ph) - py), min(H, int((r + 1) * ph) + py),
                max(0, int(c * pw) - px), min(W, int((c + 1) * pw) + px))

    def process(self, frame) -> dict:
        H, W = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        m1 = cv2.inRange(hsv, SMOKE_HSV_DARK_LO, SMOKE_HSV_DARK_HI)
        m2 = cv2.inRange(hsv, SMOKE_HSV_LIGHT_LO, SMOKE_HSV_LIGHT_HI)
        m_c = cv2.morphologyEx(cv2.bitwise_or(m1, m2), cv2.MORPH_OPEN,
                               cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)), iterations=2)
        m_mog2 = self._mog2.apply(frame)
        m_fin = cv2.bitwise_and(m_c, m_mog2)

        scan, ratios = [], {}
        if self._focused_rc is not None:
            fr, fc = self._focused_rc
            y0, y1, x0, x1 = self._patch_coords(fr, fc, H, W)
            region = m_fin[y0:y1, x0:x1]
            ratio = np.count_nonzero(region) / max(region.size, 1)
            ratios[(fr, fc)] = ratio
            if ratio >= PATCH_PX_RATIO_TH or self.roi_locked:
                scan = [(fr, fc)]
            else:
                self._focused_rc = None

        if not scan and not self.roi_locked:
            for r in range(PATCH_GRID_ROWS):
                for c in range(PATCH_GRID_COLS):
                    y0, y1, x0, x1 = self._patch_coords(r, c, H, W)
                    region = m_fin[y0:y1, x0:x1]
                    ratio = np.count_nonzero(region) / max(region.size, 1)
                    ratios[(r, c)] = ratio
                    if ratio >= PATCH_PX_RATIO_TH:
                        scan.append((r, c))

        s_active, crop = False, None
        px_total = int(np.count_nonzero(m_fin))
        max_sus = (PATCH_GRID_ROWS * PATCH_GRID_COLS) // 2

        if scan and len(scan) <= max_sus and px_total >= 200:
            r_b, c_b = max(scan, key=lambda rc: ratios.get(rc, 0.0))
            y0, y1, x0, x1 = self._patch_coords(r_b, c_b, H, W)
            patch = frame[y0:y1, x0:x1]
            if patch.size > 0:
                s_active = True
                crop = cv2.resize(patch, CLIP_CROP_SIZE)
                self._last_crop = crop
                self._focused_rc = (r_b, c_b)

        self.history.append(s_active)
        return {"hits_s": sum(self.history), "crop": crop}


# =============================================================================
# SESIÓN (port del loop de run() + workers, inline y sin display)
# =============================================================================
class _FireSession(StreamSession):
    def __init__(self, model: "FireSmokeDetector", config: dict[str, Any]):
        self.model = model
        self.detect_scale = max(0.1, min(1.0, float(config.get("detect_scale", 1.0))))
        alert_th = float(config.get("fire_alert_threshold", model.default_alert_threshold))
        self.fire_consec_min = int(config.get("fire_consec_min", model.default_consec_min))

        self.fire_cand = _FireCandidates(model.yolo)
        self.smoke_cand = _SmokeCandidates()
        self.fire_expert = CLIPFireExpert(model.clip_model, model.clip_preprocess, model.fire_feats, alert_th)
        self.smoke_expert = CLIPSmokeExpert(model.clip_model, model.clip_preprocess, model.smoke_feats)

        self.fire_buffer: list = []
        self.fire_is_detected = False
        self.fire_consec_pos = 0.0
        self.fire_last_score = 0.0
        self.fire_ttl = 0

        self.smoke_buffer: list = []
        self.smoke_is_detected = False
        self.smoke_consec_pos = 0.0
        self.smoke_last_score = 0.0
        self.smoke_last_hits = 0
        self.smoke_ttl = 0

    def _fire_batch(self) -> None:
        # ponytail: scoring CLIP inline en el thread del stream (~100-300ms por
        # batch cada 20 frames); si bloquea demasiado, mover a worker propio
        res = self.fire_expert.process_segment(self.fire_buffer)
        self.fire_last_score = res["smoothed"]
        if not res.get("skipped"):
            if res["detected"]:
                self.fire_consec_pos = min(6.0, self.fire_consec_pos + 1)
            else:
                self.fire_consec_pos = max(0.0, self.fire_consec_pos - 1)
            self.fire_is_detected = self.fire_consec_pos >= self.fire_consec_min
            if self.fire_is_detected:
                self.fire_ttl = FIRE_TTL

    def _smoke_batch(self) -> None:
        res = self.smoke_expert.predict(self.smoke_buffer)
        self.smoke_last_score = res["score"]
        self.smoke_last_hits = res["hits"]
        if res["detected"]:
            self.smoke_consec_pos = min(6.0, self.smoke_consec_pos + 1)
        else:
            self.smoke_consec_pos = max(0.0, self.smoke_consec_pos - 1)
        self.smoke_is_detected = self.smoke_consec_pos >= SMOKE_CONSEC_MIN
        if self.smoke_is_detected:
            self.smoke_ttl = SMOKE_TTL

    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        events: list[Event] = []
        fire_was_on, smoke_was_on = self.fire_is_detected, self.smoke_is_detected

        if self.detect_scale < 1.0:
            H, W = frame.shape[:2]
            frame_det = cv2.resize(frame, (int(W * self.detect_scale), int(H * self.detect_scale)))
        else:
            frame_det = frame

        det_fire = self.fire_cand.process(frame_det)
        det_smoke = self.smoke_cand.process(frame_det)

        # ── buffer fuego → experto CLIP ─────────────────────────────────────
        if det_fire["hits_f"] > 0:
            crop = det_fire["crop"] if det_fire["crop"] is not None else self.fire_cand._last_crop
            if crop is not None:
                self.fire_buffer.append(crop)
        else:
            self.fire_buffer = []
        if len(self.fire_buffer) >= FIRE_CLIP_BUFFER_SIZE:
            self._fire_batch()
            self.fire_buffer = []
        if self.fire_is_detected:
            self.fire_ttl -= 1
            if self.fire_ttl <= 0:
                self.fire_is_detected = False
                self.fire_consec_pos = 0.0

        # ── buffer humo → experto CLIP ──────────────────────────────────────
        self.smoke_cand.roi_locked = self.smoke_is_detected
        _sc = det_smoke["crop"]
        smoke_crop = _sc if _sc is not None else (self.smoke_cand._last_crop if det_smoke["hits_s"] > 0 else None)
        if smoke_crop is not None:
            self.smoke_buffer.append(smoke_crop)
        else:
            self.smoke_buffer = []
        if len(self.smoke_buffer) >= SMOKE_CLIP_BUFFER_SIZE:
            self._smoke_batch()
            self.smoke_buffer = []
        if self.smoke_is_detected:
            self.smoke_ttl -= 1
            if self.smoke_ttl <= 0:
                self.smoke_is_detected = False
                self.smoke_consec_pos = 0.0

        # ── transiciones → eventos ──────────────────────────────────────────
        scale_up = 1.0 / self.detect_scale
        if self.fire_is_detected and not fire_was_on:
            events.append(Event(
                type="FUEGO", score=self.fire_last_score, ts=ts,
                extra={"kf": self.fire_expert.last_score_kf, "pc": self.fire_expert.last_score_pc,
                       "tc": self.fire_expert.last_score_tc, "consec": self.fire_consec_pos,
                       "bboxes": [[int(v * scale_up) for v in b] for b in det_fire["bboxes"]]},
            ))
        elif not self.fire_is_detected and fire_was_on:
            events.append(Event(type="FUEGO_OFF", score=self.fire_last_score, ts=ts))

        if self.smoke_is_detected and not smoke_was_on:
            events.append(Event(type="HUMO", score=self.smoke_last_score, ts=ts,
                                extra={"hits": self.smoke_last_hits, "consec": self.smoke_consec_pos}))
        elif not self.smoke_is_detected and smoke_was_on:
            events.append(Event(type="HUMO_OFF", score=self.smoke_last_score, ts=ts))

        return events


# =============================================================================
# MODELO
# =============================================================================
class FireSmokeDetector(CVModel):
    info = ModelInfo(
        name="fire-detector",
        version="2.0",
        task="Detección de fuego y humo en CCTV (YOLO + CV clásico + CLIP temporal)",
        capabilities={Capability.CLIP, Capability.STREAM},
        event_types=["FUEGO", "FUEGO_OFF", "HUMO", "HUMO_OFF"],
        config_schema=CONFIG_SCHEMA,
        target_fps=10.0,  # equivale al --frame-skip 2 del original en cámaras de 20 fps
    )

    def __init__(self) -> None:
        self.yolo: YOLO | None = None
        self.clip_model = None
        self.clip_preprocess = None
        self.fire_feats: _FireTextFeats | None = None
        self.smoke_feats: _SmokeTextFeats | None = None
        # thresholds adaptativos según modelo CLIP (igual que el original)
        if any(s in CLIP_MODEL_NAME for s in ("B-32", "B-16")):
            self.default_alert_threshold, self.default_consec_min = 0.50, 2
        else:
            self.default_alert_threshold, self.default_consec_min = FIRE_ALERT_THRESHOLD, FIRE_CONSEC_MIN

    def load(self) -> None:
        log.info("Cargando YOLO fire: %s", WEIGHTS)
        self.yolo = YOLO(WEIGHTS)
        log.info("Cargando CLIP %s (openai) en %s", CLIP_MODEL_NAME, DEVICE)
        self.clip_model, _, self.clip_preprocess = open_clip.create_model_and_transforms(
            CLIP_MODEL_NAME, pretrained="openai", device=DEVICE)
        self.clip_model.eval()
        tokenizer = open_clip.get_tokenizer(CLIP_MODEL_NAME)
        self.fire_feats = _FireTextFeats(self.clip_model, tokenizer)
        self.smoke_feats = _SmokeTextFeats(self.clip_model, tokenizer)

    def warmup(self) -> None:
        session = self.create_session({})
        session.process_frame(np.zeros((360, 640, 3), dtype=np.uint8), 0.0)
        session.close()

    def unload(self) -> None:
        self.yolo = self.clip_model = self.clip_preprocess = None
        self.fire_feats = self.smoke_feats = None

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        if self.yolo is None:
            self.load()
        return _FireSession(self, config)
