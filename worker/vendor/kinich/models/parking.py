"""ParkingMonitor — port de parking_monitor.py (v2) al contrato Kinich.

Lógica intacta respecto al original (arconte/parking_monitor.py):
  ocupación:    YOLO vehículos sobre ROI de cajones -> máscara de bboxes ->
                fracción de solape por polígono >= iou_occ_thr
  verificación: crop del CARRO (bbox YOLO asignado al cajón) cada
                verify_every frames de ocupación continua ->
                  · similitud coseno ResNet50 vs features registradas
                  · color dominante HSV vs color registrado
                  · placa (YOLO placas + deskew + upscale + CLAHE + OCR)
                veredicto por mayoría sobre buffer de buffer_size muestras

Eliminado del original: display cv2 / grid de testigos / overlays, escritura
de video a disco, CLI, prints (-> logging) y el ThreadPoolExecutor de
verificación (el thread del stream ya es el worker; a target_fps bajo la
verificación inline no bloquea de forma relevante).

Cambios respecto al original:
  · cajones.json -> config de sesión (`spots`, ver CONFIG_SCHEMA).
  · Real-ESRGAN (deps basicsr/realesrgan, fuera de la lista permitida) se
    sustituye por el propio fallback bicúbico x4 del script.
  · EasyOCR no está en la lista de deps permitidas: se importa lazy; si no
    está instalado (o faltan los pesos del detector de placas) la
    verificación de placa queda deshabilitada (plate_ok=None) y el resto
    (features + color) sigue funcionando.
  · torchvision (ResNet50) llega como dependencia transitiva de ultralytics.

Sin `spots` en la config la sesión no monitorea nada: no emite eventos y
registra un warning una sola vez.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter, deque
from typing import Any

import cv2
import numpy as np
import torch
import torchvision.models as tv_models
import torchvision.transforms as T
from PIL import Image
from ultralytics import YOLO

from kinich.contract import Capability, CVModel, Event, ModelInfo, StreamSession

log = logging.getLogger(__name__)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
# yolo11x.pt es nombre oficial de ultralytics: se auto-descarga si no existe
WEIGHTS = os.environ.get("KINICH_PARKING_WEIGHTS", "yolo11x.pt")
PLATE_WEIGHTS = os.environ.get("KINICH_PARKING_PLATE_WEIGHTS", "license-plate-finetune-v1x.pt")

# =============================================================================
# CONFIG (constantes del original, sin cambios de valor)
# =============================================================================
VEHICLE_CLS = {2, 3, 5, 7}   # car, motorcycle, bus, truck (COCO)
PLATE_CONF = 0.25            # confianza mínima detector de placas
YOLO_CONF = 0.30
IOU_OCC_THR = 0.20
ROI_PAD = 20                 # padding del ROI global de cajones

BUFFER_SIZE = 10             # muestras a acumular antes de emitir veredicto
FEAT_SIM_THR = 0.75          # umbral similitud coseno ResNet50
COLOR_THR = 0.55             # fracción del buffer para "color correcto"
PLATE_THR = 0.40             # fracción del buffer para "placa correcta"
VERIFY_EVERY = 5             # extraer features cada N frames de ocupación continua
CAR_CROP_PAD = 0.08          # padding alrededor del bbox YOLO del carro

_COLOR_HSV = {
    "Blanco":      [((0,   0, 185), (180,  45, 255))],
    "Negro":       [((0,   0,   0), (180, 255,  45))],
    "Gris":        [((0,   0,  46), (180,  50, 184))],
    "Plateado":    [((0,   0, 145), (180,  40, 225))],
    "Rojo":        [((0,  100,  80), (8,   255, 255)),
                    ((172, 100,  80), (180, 255, 255))],
    "Azul":        [((100,  80,  50), (130, 255, 255))],
    "Verde":       [((40,   60,  50), (80,  255, 255))],
    "Amarillo":    [((18,   80, 100), (35,  255, 255))],
    "Naranja":     [((8,   100, 100), (18,  255, 255))],
    "Café/Marrón": [((8,    50,  30), (25,  200, 150))],
    "Morado":      [((130,  50,  50), (160, 255, 255))],
}

_PLATE_RE = re.compile(r"[^A-Z0-9]")   # solo letras y dígitos

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "spots": {
            "type": "array",
            "description": (
                "Cajones a monitorear (sin esto la sesión no emite eventos). "
                "Cada cajón: {id: int, name?: str, "
                "points: [[x, y], ...] — polígono de >=3 vértices en PÍXELES "
                "del frame del stream (o en [0,1] si spots_normalized=true), "
                "registration?: {features: [float x2048] (vector ResNet50 "
                "L2-normalizado del vehículo registrado), color?: str (uno de: "
                "Blanco, Negro, Gris, Plateado, Rojo, Azul, Verde, Amarillo, "
                "Naranja, Café/Marrón, Morado), plate?: str}}. "
                "Solo los cajones con registration.features se verifican."
            ),
            "items": {
                "type": "object",
                "required": ["id", "points"],
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "points": {
                        "type": "array", "minItems": 3,
                        "items": {"type": "array", "minItems": 2, "maxItems": 2,
                                  "items": {"type": "number"}},
                    },
                    "registration": {
                        "type": "object",
                        "properties": {
                            "features": {"type": "array", "items": {"type": "number"}},
                            "color": {"type": "string"},
                            "plate": {"type": "string"},
                        },
                    },
                },
            },
        },
        "spots_normalized": {
            "type": "boolean", "default": False,
            "description": "true → points en [0,1] relativos al frame; se escalan a píxeles en el primer frame",
        },
        "conf": {
            "type": "number", "default": YOLO_CONF, "minimum": 0.0, "maximum": 1.0,
            "description": "confianza mínima YOLO vehículos",
        },
        "iou_occ_thr": {
            "type": "number", "default": IOU_OCC_THR, "minimum": 0.0, "maximum": 1.0,
            "description": "fracción del polígono cubierta por bboxes para marcar OCUPADO",
        },
        "verify": {
            "type": "boolean", "default": True,
            "description": "habilitar verificación del vehículo registrado (features/color/placa)",
        },
        "feat_sim_thr": {
            "type": "number", "default": FEAT_SIM_THR, "minimum": 0.0, "maximum": 1.0,
            "description": "umbral de similitud coseno ResNet50 por muestra",
        },
        "buffer_size": {
            "type": "integer", "default": BUFFER_SIZE, "minimum": 1,
            "description": "muestras acumuladas antes de emitir veredicto por mayoría",
        },
        "verify_every": {
            "type": "integer", "default": VERIFY_EVERY, "minimum": 1,
            "description": "verificar cada N frames de ocupación continua",
        },
    },
}


# =============================================================================
# HELPERS (idénticos al original salvo lo documentado en el docstring)
# =============================================================================
def _norm_plate(text: str) -> str:
    """Limpia y normaliza texto de placa: mayúsculas, solo alfanumérico."""
    return _PLATE_RE.sub("", text.upper().strip())


def detect_vehicle_color(img_bgr: np.ndarray) -> str:
    """Retorna el nombre del color dominante del vehículo en el crop."""
    h, w = img_bgr.shape[:2]
    # Franja central para evitar cielo/suelo/sombras
    roi = img_bgr[h // 4: 3 * h // 4, w // 6: 5 * w // 6]
    if roi.size == 0:
        roi = img_bgr

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    area = hsv.shape[0] * hsv.shape[1]

    scores: dict[str, float] = {}
    for name, ranges in _COLOR_HSV.items():
        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for lo, hi in ranges:
            mask |= cv2.inRange(hsv, np.array(lo), np.array(hi))
        scores[name] = np.count_nonzero(mask) / max(area, 1)

    return max(scores, key=scores.get)


def _upscale_x4(bgr: np.ndarray) -> np.ndarray:
    """Upscale x4 bicúbico (fallback del script; Real-ESRGAN eliminado)."""
    if bgr is None or bgr.size == 0:
        return bgr
    ph, pw = bgr.shape[:2]
    return cv2.resize(bgr, (pw * 4, ph * 4), interpolation=cv2.INTER_CUBIC)


def _deskew_plate(bgr: np.ndarray) -> np.ndarray:
    """Endereza la placa detectando el ángulo dominante con HoughLinesP.
    Solo corrige si el ángulo es mayor a 0.5 grados."""
    if bgr is None or bgr.size == 0:
        return bgr
    ph, pw = bgr.shape[:2]
    scale = max(1.0, 120 / max(ph, 1))
    work = cv2.resize(bgr, (int(pw * scale), int(ph * scale)),
                      interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180,
                            threshold=30,
                            minLineLength=int(work.shape[1] * 0.2),
                            maxLineGap=10)
    angle = 0.0
    if lines is not None:
        angles = []
        for x1, y1, x2, y2 in lines[:, 0]:
            dx, dy = x2 - x1, y2 - y1
            if dx == 0:
                continue
            a = np.degrees(np.arctan2(dy, dx))
            if -30 < a < 30:
                angles.append(a)
        if angles:
            angle = float(np.median(angles))
    if abs(angle) < 0.5:
        return bgr
    h, w = bgr.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos_a = abs(M[0, 0])
    sin_a = abs(M[0, 1])
    nw = int(h * sin_a + w * cos_a)
    nh = int(h * cos_a + w * sin_a)
    M[0, 2] += (nw - w) / 2
    M[1, 2] += (nh - h) / 2
    return cv2.warpAffine(bgr, M, (nw, nh),
                          flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def build_masks(spots: list[dict], h: int, w: int) -> dict[int, np.ndarray]:
    blank = np.zeros((h, w), dtype=np.uint8)
    masks = {}
    for s in spots:
        m = blank.copy()
        pts = np.array(s["points"], dtype=np.int32)
        cv2.fillPoly(m, [pts], 255)
        masks[s["id"]] = m
    return masks


def build_roi(spots: list[dict], h: int, w: int) -> tuple[int, int, int, int]:
    all_xs = [x for s in spots for x, _ in s["points"]]
    all_ys = [y for s in spots for _, y in s["points"]]
    x1 = max(0, min(all_xs) - ROI_PAD)
    y1 = max(0, min(all_ys) - ROI_PAD)
    x2 = min(w, max(all_xs) + ROI_PAD)
    y2 = min(h, max(all_ys) + ROI_PAD)
    return int(x1), int(y1), int(x2), int(y2)


def car_crop(frame: np.ndarray, bbox: tuple, pad: float = CAR_CROP_PAD) -> np.ndarray:
    """Crop del CARRO a partir de su bbox YOLO + pequeño padding.
    El cajón solo sirve para saber si hay carro; el análisis usa este crop."""
    H, W = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    pw = int((x2 - x1) * pad)
    ph = int((y2 - y1) * pad)
    cx1 = max(0, x1 - pw)
    cy1 = max(0, y1 - ph)
    cx2 = min(W, x2 + pw)
    cy2 = min(H, y2 + ph)
    crop = frame[cy1:cy2, cx1:cx2]
    return crop.copy() if crop.size > 0 else frame.copy()


def detect(model: YOLO, frame: np.ndarray, roi: tuple, masks: dict,
           spots: list[dict], conf: float, iou_thr: float
           ) -> tuple[list, dict[int, bool], dict[int, float]]:
    """Detección YOLO sobre el ROI + ocupación por solape con cada polígono.
    Igual al original; además retorna la fracción de solape por cajón
    (dato que el original calculaba inline) para usarla como score."""
    rx1, ry1, rx2, ry2 = roi
    crop = frame[ry1:ry2, rx1:rx2]

    results = model.predict(crop, conf=conf, verbose=False)[0]
    boxes = []
    for box in results.boxes:
        if int(box.cls[0]) in VEHICLE_CLS:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            boxes.append((x1 + rx1, y1 + ry1, x2 + rx1, y2 + ry1))

    h, w = frame.shape[:2]
    det_mask = np.zeros((h, w), dtype=np.uint8)
    for x1, y1, x2, y2 in boxes:
        det_mask[y1:y2, x1:x2] = 255

    occ, ratios = {}, {}
    for s in spots:
        smask = masks.get(s["id"])
        if smask is None:
            continue
        area = np.count_nonzero(smask)
        inter = np.count_nonzero(det_mask & smask)
        ratio = inter / max(area, 1)
        ratios[s["id"]] = float(ratio)
        occ[s["id"]] = ratio >= iou_thr

    return boxes, occ, ratios


def assign_boxes_to_spots(boxes: list, masks: dict, spots: list[dict],
                          H: int, W: int, iou_thr: float) -> dict[int, tuple]:
    """Por cada cajón, encuentra el bbox YOLO del carro con mayor solapamiento.
    Retorna dict[spot_id -> (x1,y1,x2,y2)]. El bbox es del CARRO."""
    spot_boxes: dict[int, tuple] = {}
    for s in spots:
        sid = s["id"]
        smask = masks.get(sid)
        if smask is None:
            continue
        s_area = int(np.count_nonzero(smask))
        best_iou, best_box = 0.0, None
        for bbox in boxes:
            x1, y1, x2, y2 = bbox
            bm = np.zeros((H, W), dtype=np.uint8)
            bm[y1:y2, x1:x2] = 255
            inter = int(np.count_nonzero(bm & smask))
            iou = inter / max(s_area, 1)
            if iou > best_iou:
                best_iou, best_box = iou, bbox
        if best_box is not None and best_iou >= iou_thr:
            spot_boxes[sid] = best_box
    return spot_boxes


# =============================================================================
# SpotTracker — buffer de veredictos por cajón (sin lock: todo corre en el
# thread del stream, el ThreadPoolExecutor del original fue eliminado)
# =============================================================================
class SpotTracker:
    """Acumula similitudes de features, coincidencias de color y placa durante
    las últimas `buf` muestras en que el cajón estuvo ocupado.
    Emite un veredicto por mayoría (>50% de las muestras)."""

    def __init__(self, spot: dict, model: "ParkingMonitor",
                 buf: int = BUFFER_SIZE, feat_sim_thr: float = FEAT_SIM_THR):
        reg = spot.get("registration", {})
        feats = reg.get("features", [])

        self.model = model
        self.feat_sim_thr = feat_sim_thr
        self.sid = spot["id"]
        self.reg_feat = np.array(feats, dtype=np.float32) if feats else None
        self.reg_color = reg.get("color", "")
        self.reg_plate = _norm_plate(reg.get("plate", ""))   # normalizado
        self.has_reg = (self.reg_feat is not None and len(self.reg_feat) > 0)

        self._feat_buf: deque[float] = deque(maxlen=buf)
        self._color_buf: deque[bool] = deque(maxlen=buf)
        self._color_name_buf: deque[str] = deque(maxlen=buf)
        self._plate_buf: deque[bool] = deque(maxlen=buf)
        self._plate_text_buf: deque[str] = deque(maxlen=buf)

        # Estado público (leído por la sesión)
        self.feat_avg = 0.0
        self.color_ok: bool | None = None   # None=sin datos | True | False
        self.detected_color = ""            # último valor crudo
        self.color_majority = ""            # color más frecuente en el buffer
        self.plate_text = ""                # placa mayoritaria del buffer
        self.plate_ok: bool | None = None
        self.verdict = "empty"              # "empty"|"unknown"|"match"|"mismatch"
        self.occ_frames = 0                 # frames consecutivos ocupado

    # ── API pública ──────────────────────────────────────────────────────
    def reset(self) -> None:
        """Llamado cuando el cajón pasa a libre."""
        self._feat_buf.clear()
        self._color_buf.clear()
        self._color_name_buf.clear()
        self._plate_buf.clear()
        self._plate_text_buf.clear()
        self.feat_avg = 0.0
        self.color_ok = None
        self.detected_color = ""
        self.color_majority = ""
        self.plate_text = ""
        self.plate_ok = None
        self.verdict = "empty"
        self.occ_frames = 0

    def update(self, crop_bgr: np.ndarray) -> None:
        """Extrae features, color y placa del crop del carro."""
        if not self.has_reg:
            self.verdict = "unknown"
            return

        # Similitud de features
        feat = self.model.extract_features(crop_bgr)
        sim = float(np.dot(feat, self.reg_feat)) if feat is not None else None

        # Color
        color_name = detect_vehicle_color(crop_bgr)
        color_ok = (color_name == self.reg_color) if self.reg_color else None

        # Placa
        plate_norm = self.model.read_plate(crop_bgr)
        if self.reg_plate and plate_norm:
            plate_ok = (plate_norm == self.reg_plate)
        else:
            plate_ok = None

        if sim is not None:
            self._feat_buf.append(sim)
        if color_ok is not None:
            self._color_buf.append(color_ok)
        self._color_name_buf.append(color_name)
        self.detected_color = color_name
        if plate_norm:
            self._plate_text_buf.append(plate_norm)
        if plate_ok is not None:
            self._plate_buf.append(plate_ok)
        self._decide()

        log.debug(
            "[E%02d] sim=%s buf=%d/%d color=%s %s(reg=%s) placa=%s %s(reg=%s)",
            self.sid,
            f"{sim:.3f}" if sim is not None else "N/A",
            len(self._feat_buf), self._feat_buf.maxlen,
            color_name, "OK" if color_ok else "X", self.reg_color,
            plate_norm or "(no detectada)",
            "OK" if plate_ok else ("X" if plate_ok is False else "-"),
            self.reg_plate or "-",
        )

    # ── Privado ──────────────────────────────────────────────────────────
    def _decide(self) -> None:
        """Calcula veredicto por mayoría."""
        if not self._feat_buf:
            self.verdict = "unknown"
            return

        n = len(self._feat_buf)
        self.feat_avg = float(np.mean(self._feat_buf))
        feat_ok = (sum(1 for s in self._feat_buf if s >= self.feat_sim_thr) / n) >= 0.5

        if self._color_buf:
            self.color_ok = (sum(self._color_buf) / len(self._color_buf)) >= COLOR_THR
        else:
            self.color_ok = None

        # Color mayoritario del buffer (estable)
        if self._color_name_buf:
            self.color_majority = Counter(self._color_name_buf).most_common(1)[0][0]

        # Placa mayoritaria del buffer
        if self._plate_text_buf:
            self.plate_text = Counter(self._plate_text_buf).most_common(1)[0][0]
        if self._plate_buf:
            self.plate_ok = (sum(self._plate_buf) / len(self._plate_buf)) >= PLATE_THR
        else:
            self.plate_ok = None

        self.verdict = "match" if feat_ok else "mismatch"

    @property
    def alert(self) -> bool:
        return self.verdict == "mismatch"


# =============================================================================
# SESIÓN (port del loop de run(), sin display ni writer; verificación inline)
# =============================================================================
class _ParkingSession(StreamSession):
    def __init__(self, model: "ParkingMonitor", config: dict[str, Any]):
        self.model = model
        self.conf = float(config.get("conf", YOLO_CONF))
        self.iou_thr = float(config.get("iou_occ_thr", IOU_OCC_THR))
        self.verify = bool(config.get("verify", True))
        self.verify_every = max(1, int(config.get("verify_every", VERIFY_EVERY)))
        self.buffer_size = max(1, int(config.get("buffer_size", BUFFER_SIZE)))
        self.feat_sim_thr = float(config.get("feat_sim_thr", FEAT_SIM_THR))
        self.spots_normalized = bool(config.get("spots_normalized", False))

        # Validar cajones de la config (equivale a load_spots del original)
        self.spots: list[dict] = []
        for s in config.get("spots", []) or []:
            if "id" in s and len(s.get("points", [])) >= 3:
                self.spots.append(s)
            else:
                log.warning("parking: cajón inválido ignorado (requiere id y >=3 points): %r", s)
        if not self.spots:
            log.warning("parking: sesión sin cajones en config['spots']; no se emitirán eventos")

        # Máscaras y ROI se construyen en el primer frame (necesitan H, W)
        self.masks: dict[int, np.ndarray] | None = None
        self.roi: tuple[int, int, int, int] | None = None

        # Trackers de verificación: solo cajones con features registradas
        self.trackers: dict[int, SpotTracker] = {}
        if self.verify:
            for s in self.spots:
                if s.get("registration", {}).get("features"):
                    self.trackers[s["id"]] = SpotTracker(
                        s, model, buf=self.buffer_size, feat_sim_thr=self.feat_sim_thr)
            if self.trackers:
                log.info("parking: verificación activada — %d cajones con registro "
                         "(buffer=%d, umbral features=%.2f, muestra cada %d frames)",
                         len(self.trackers), self.buffer_size,
                         self.feat_sim_thr, self.verify_every)

        self.occ_prev: dict[int, bool] = {}
        self.verdict_prev: dict[int, str] = {t: "empty" for t in self.trackers}

    def _init_geometry(self, h: int, w: int) -> None:
        """Escala puntos normalizados a píxeles y construye máscaras + ROI."""
        if self.spots_normalized:
            for s in self.spots:
                s["points"] = [[int(round(x * w)), int(round(y * h))]
                               for x, y in s["points"]]
        self.masks = build_masks(self.spots, h, w)
        self.roi = build_roi(self.spots, h, w)

    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        events: list[Event] = []
        if not self.spots:
            return events

        h, w = frame.shape[:2]
        if self.masks is None:
            self._init_geometry(h, w)

        boxes, occ, ratios = detect(self.model.yolo, frame, self.roi,
                                    self.masks, self.spots, self.conf, self.iou_thr)
        # Mapear cada cajón -> bbox YOLO del carro (no del cajón)
        spot_boxes = assign_boxes_to_spots(boxes, self.masks, self.spots,
                                           h, w, self.iou_thr)

        # ── Verificación de vehículos (inline; el original usaba executor) ──
        if self.trackers:
            for s in self.spots:
                sid = s["id"]
                t = self.trackers.get(sid)
                if t is None:
                    continue
                is_occ = occ.get(sid, False)
                was_occ = self.occ_prev.get(sid, False)
                if not is_occ:
                    if was_occ:
                        t.reset()
                        self.verdict_prev[sid] = "empty"
                    continue
                t.occ_frames += 1
                if t.occ_frames % self.verify_every == 1 or self.verify_every == 1:
                    if sid not in spot_boxes:
                        continue   # cajón "occ" pero sin bbox YOLO clara
                    t.update(car_crop(frame, spot_boxes[sid]))

        # ── Transiciones de ocupación → eventos ─────────────────────────────
        for s in self.spots:
            sid = s["id"]
            name = s.get("name", f"E{sid}")
            is_occ = occ.get(sid, False)
            was_occ = self.occ_prev.get(sid, False)
            if is_occ and not was_occ:
                extra: dict[str, Any] = {"spot_id": sid, "spot_name": name}
                if sid in spot_boxes:
                    extra["bbox"] = [int(v) for v in spot_boxes[sid]]
                # score = fracción del polígono cubierta por bboxes de vehículos
                events.append(Event(type="OCUPADO", score=ratios.get(sid, 0.0),
                                    ts=ts, extra=extra))
            elif not is_occ and was_occ:
                events.append(Event(type="LIBRE", score=1.0 - ratios.get(sid, 0.0),
                                    ts=ts, extra={"spot_id": sid, "spot_name": name}))

        # ── Transiciones de veredicto → eventos de verificación ─────────────
        for sid, t in self.trackers.items():
            if not occ.get(sid, False):
                continue
            prev = self.verdict_prev.get(sid, "empty")
            if t.verdict in ("match", "mismatch") and t.verdict != prev:
                extra = {
                    "spot_id": sid,
                    "feat_avg": t.feat_avg,
                    "color_detectado": t.color_majority or t.detected_color,
                    "color_ok": t.color_ok,
                    "plate": t.plate_text,
                    "plate_ok": t.plate_ok,
                }
                etype = "VEHICULO_OK" if t.verdict == "match" else "ALERTA_VEHICULO"
                events.append(Event(type=etype, score=t.feat_avg, ts=ts, extra=extra))
            self.verdict_prev[sid] = t.verdict

        self.occ_prev = dict(occ)
        return events


# =============================================================================
# MODELO
# =============================================================================
class ParkingMonitor(CVModel):
    info = ModelInfo(
        name="parking-monitor",
        version="2.0",
        task="Ocupación de cajones de estacionamiento y verificación del "
             "vehículo registrado (YOLO + ResNet50 + color HSV + placa OCR)",
        capabilities={Capability.CLIP, Capability.STREAM},
        event_types=["OCUPADO", "LIBRE", "VEHICULO_OK", "ALERTA_VEHICULO"],
        config_schema=CONFIG_SCHEMA,
        target_fps=1.0,  # los cajones cambian despacio; 1 fps sobra
    )

    def __init__(self) -> None:
        self.yolo: YOLO | None = None
        self.feat_model = None
        self.feat_transform = None
        self.plate_model: YOLO | None = None
        self.ocr_reader = None

    def load(self) -> None:
        log.info("Cargando YOLO vehículos: %s", WEIGHTS)
        self.yolo = YOLO(WEIGHTS)

        log.info("Cargando ResNet50 (features de verificación) en %s", DEVICE)
        m = tv_models.resnet50(weights=tv_models.ResNet50_Weights.DEFAULT)
        m.fc = torch.nn.Identity()   # salida: 2048-dim
        m.eval()
        self.feat_model = m.to(DEVICE)
        self.feat_transform = T.Compose([
            T.Resize((224, 224)),
            T.ToTensor(),
            T.Normalize(mean=[0.485, 0.456, 0.406],
                        std=[0.229, 0.224, 0.225]),
        ])

        # Detector de placas + OCR: opcionales (igual que el original, que
        # desactivaba OCR si faltaba el peso; aquí además easyocr es opcional
        # porque no está en la lista de dependencias permitidas)
        if os.path.exists(PLATE_WEIGHTS):
            try:
                import easyocr  # dep opcional, fuera de la lista permitida
                self.plate_model = YOLO(PLATE_WEIGHTS)
                self.ocr_reader = easyocr.Reader(["en"], gpu=(DEVICE == "cuda"),
                                                 verbose=False)
                log.info("Detector de placas listo: %s", PLATE_WEIGHTS)
            except ImportError:
                log.warning("easyocr no instalado: verificación de placa deshabilitada")
            except Exception as e:
                log.warning("No se pudo cargar detector de placas: %s", e)
        else:
            log.warning("Pesos de placas no encontrados (%s): OCR deshabilitado",
                        PLATE_WEIGHTS)

    def warmup(self) -> None:
        session = self.create_session({
            "spots": [{"id": 0, "points": [[10, 10], [100, 10], [100, 100], [10, 100]]}],
        })
        session.process_frame(np.zeros((360, 640, 3), dtype=np.uint8), 0.0)
        session.close()

    def unload(self) -> None:
        self.yolo = self.feat_model = self.feat_transform = None
        self.plate_model = self.ocr_reader = None

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        if self.yolo is None:
            self.load()
        return _ParkingSession(self, config)

    # ── Servicios compartidos por las sesiones (pesos a nivel modelo) ───────
    def extract_features(self, img_bgr: np.ndarray) -> np.ndarray | None:
        """Feature vector ResNet50 L2-normalizado de un array BGR."""
        try:
            rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            x = self.feat_transform(Image.fromarray(rgb)).unsqueeze(0).to(DEVICE)
            with torch.no_grad():
                feat = self.feat_model(x).squeeze(0).cpu().numpy()
            norm = np.linalg.norm(feat)
            return feat / norm if norm > 0 else feat
        except Exception:
            return None

    def read_plate(self, car_bgr: np.ndarray) -> str:
        """Detecta y lee la placa dentro del crop del carro.
        Retorna el texto normalizado, o "" si no hay placa/OCR disponible."""
        if self.plate_model is None or self.ocr_reader is None:
            return ""
        if car_bgr is None or car_bgr.size == 0:
            return ""

        # 1. Detector YOLO de placas sobre el crop del carro
        results = self.plate_model.predict(car_bgr, conf=PLATE_CONF, verbose=False)[0]
        if not results.boxes or len(results.boxes) == 0:
            return ""
        best = max(results.boxes, key=lambda b: float(b.conf[0]))
        x1, y1, x2, y2 = map(int, best.xyxy[0])
        plate_crop = car_bgr[y1:y2, x1:x2]
        if plate_crop.size == 0:
            return ""

        # 2. Enderezar + upscale x4 (bicúbico) + contraste
        plate_crop = _deskew_plate(plate_crop)
        plate_crop = _upscale_x4(plate_crop)
        gray = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # 3. OCR
        ocr_out = self.ocr_reader.readtext(
            bw, detail=0, allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
        return _norm_plate(" ".join(ocr_out))
