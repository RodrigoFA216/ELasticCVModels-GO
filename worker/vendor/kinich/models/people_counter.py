"""PeopleCounter — port de PeopleCounter_Debug.py al contrato Kinich.

Lógica de conteo intacta respecto al original (ArconteDetection_DebugTools):
  frame -> grid de patches (2x4 con overlap) -> cada patch redimensionado a
  PATCH_IMGSZ y pasado a YOLO -> detecciones mapeadas a coords del frame
  -> NMS global entre patches -> conteo = dets post-NMS -> media móvil.

El patch-zoom existe para cámaras altas: personas de 15-60 px que YOLO
pierde a resolución normal ocupan más píxeles por patch y el recall mejora
sin subir imgsz global.

Eliminado del original: dashboard cv2 (imshow/putText), CLI, prompt
interactivo de escena (ahora knob de config) y prints (-> logging).
El YOLO es único y compartido entre sesiones: no usa tracking, así que no
arrastra estado entre streams.
"""
import logging
import os
from collections import deque
from typing import Any

import cv2
import numpy as np
from ultralytics import YOLO

from kinich.contract import Capability, CVModel, Detection, Event, ModelInfo, StreamSession

log = logging.getLogger(__name__)

WEIGHTS = os.environ.get("KINICH_PEOPLE_WEIGHTS", "yolo11x.pt")

# =============================================================================
# CONFIG (constantes del original, sin cambios de valor)
# =============================================================================
DETECT_CLASSES = [0]      # 0 = person (COCO)

# ── Grid de patches ──────────────────────────────────────────────────────────
GRID_ROWS = 2             # filas
GRID_COLS = 4             # columnas -> 2x4 = 8 patches
PATCH_OVERLAP = 0.15      # fracción de overlap por cada lado (evita pérdidas en bordes)
PATCH_IMGSZ = 640         # tamaño de inferencia por patch (px)

# ── Suavizado temporal ───────────────────────────────────────────────────────
SMOOTH_FRAMES = 15        # ventana de media móvil (frames)

# ── Perfiles de escena (el original preguntaba por CLI; default outdoor) ─────
SCENE_PROFILES: dict[str, dict] = {
    "indoor":  dict(conf=0.25, nms_iou=0.40),   # cámara cercana, personas grandes
    "outdoor": dict(conf=0.15, nms_iou=0.45),   # cámara alta, personas pequeñas
}
DEFAULT_SCENE = "outdoor"

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {
            "type": "string", "enum": ["indoor", "outdoor"], "default": "outdoor",
            "description": "perfil de escena: indoor (cámara cercana) fija conf=0.25/nms_iou=0.40; outdoor (cámara alta) conf=0.15/nms_iou=0.45",
        },
        "conf": {"type": "number", "description": "umbral de confianza YOLO (override del perfil de escena)"},
        "nms_iou": {"type": "number", "description": "IoU máximo del NMS global entre patches (override del perfil)"},
        "rows": {"type": "integer", "default": 2, "minimum": 1, "description": "filas del grid de patches"},
        "cols": {"type": "integer", "default": 4, "minimum": 1, "description": "columnas del grid de patches"},
        "overlap": {"type": "number", "default": 0.15, "minimum": 0.0, "maximum": 0.5,
                    "description": "fracción de overlap por lado entre patches"},
        "smooth_frames": {"type": "integer", "default": 15, "minimum": 1,
                          "description": "ventana (frames) de la media móvil del conteo"},
    },
}


# =============================================================================
# PATCH HELPERS (idénticos al original)
# =============================================================================
def compute_patches(frame_h: int, frame_w: int, rows: int, cols: int,
                    overlap: float) -> list[tuple[int, int, int, int]]:
    """Retorna lista de (x1, y1, x2, y2) en coords del frame original,
    una por cada celda del grid rows x cols con el overlap pedido."""
    base_h = frame_h / rows
    base_w = frame_w / cols
    pad_h = int(base_h * overlap)
    pad_w = int(base_w * overlap)

    patches = []
    for r in range(rows):
        for c in range(cols):
            x1 = max(0, int(c * base_w) - pad_w)
            y1 = max(0, int(r * base_h) - pad_h)
            x2 = min(frame_w, int((c + 1) * base_w) + pad_w)
            y2 = min(frame_h, int((r + 1) * base_h) + pad_h)
            patches.append((x1, y1, x2, y2))
    return patches


# =============================================================================
# NMS GLOBAL (idéntico al original)
# =============================================================================
def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix1 = max(a[0], b[0]); iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2]); iy2 = min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def nms(dets: list[dict], iou_thr: float) -> list[dict]:
    """NMS clásico sobre lista de dets con campo 'conf'. Retorna lista filtrada.

    POR QUÉ NO ES EL BUCLE EN PYTHON QUE ERA

    Este detector es el más caro de los diez (67 ms/fotograma medidos) y el
    único que NO gana nada al paralelizarse: medido en la partición, cuatro
    sesiones en hilos dan 15,3 fotogramas/s contra 14,9 con una sola, o sea
    0,99x. `fire-detector`, en el mismo banco, escala 1,99x.

    La diferencia es quién suelta el GIL. `fire` pasa su tiempo en kernels de
    CUDA y en OpenCV, que lo sueltan; este NMS era un doble bucle en Python
    puro, O(n^2) sobre las detecciones. Con 8 parches sobre una escena de
    multitud son cientos de cajas y decenas de miles de llamadas a `iou()`
    seguidas, con el intérprete tomado todo el rato: los otros hilos no pueden
    avanzar ni aunque la GPU esté libre.

    `torchvision.ops.nms` es EL MISMO algoritmo --supresión codiciosa por IoU
    tras ordenar por confianza-- en una sola llamada que suelta el GIL. No es
    una aproximación ni un algoritmo distinto.

    Se conserva el camino en Python como respaldo, no por gusto: si torchvision
    faltara, es preferible un detector lento a un detector caído.
    """
    if not dets:
        return []
    try:
        import torch
        from torchvision.ops import nms as _nms_tv

        cajas = torch.tensor([d["bbox"] for d in dets], dtype=torch.float32)
        puntajes = torch.tensor([d["conf"] for d in dets], dtype=torch.float32)
        quedan = _nms_tv(cajas, puntajes, iou_thr).tolist()
        return [dets[i] for i in quedan]
    except Exception:  # noqa: BLE001
        log.warning("NMS de torchvision no disponible; se usa el bucle en Python",
                    exc_info=True)

    dets = sorted(dets, key=lambda d: d["conf"], reverse=True)
    keep = []
    suppressed = [False] * len(dets)
    for i, d in enumerate(dets):
        if suppressed[i]:
            continue
        keep.append(d)
        for j in range(i + 1, len(dets)):
            if not suppressed[j] and iou(d["bbox"], dets[j]["bbox"]) > iou_thr:
                suppressed[j] = True
    return keep


# =============================================================================
# INFERENCIA POR PATCH (idéntica al original, YOLO compartido del modelo padre)
# =============================================================================
def run_patches(model: YOLO, frame: np.ndarray,
                patches: list[tuple[int, int, int, int]],
                conf: float, nms_iou: float) -> tuple[list[dict], list[list[dict]]]:
    """Ejecuta YOLO en cada patch. Retorna:
      - dets_global : todas las detecciones post-NMS en coords del frame
      - dets_per_patch : lista de dets locales (coords del frame) por patch
    """
    all_dets: list[dict] = []
    dets_per_patch: list[list[dict]] = []

    for pi, (px1, py1, px2, py2) in enumerate(patches):
        crop = frame[py1:py2, px1:px2]
        ph, pw = crop.shape[:2]
        if ph == 0 or pw == 0:
            dets_per_patch.append([])
            continue

        # escalar al tamaño de inferencia
        scale_x = pw / PATCH_IMGSZ
        scale_y = ph / PATCH_IMGSZ
        resized = cv2.resize(crop, (PATCH_IMGSZ, PATCH_IMGSZ), interpolation=cv2.INTER_LINEAR)

        res = model(resized, classes=DETECT_CLASSES, conf=conf, verbose=False)[0]

        local_dets: list[dict] = []
        if res.boxes is not None and len(res.boxes):
            b = res.boxes
            all_xyxy = b.xyxy.cpu().numpy().astype(np.float32)
            all_conf = b.conf.cpu().numpy().astype(np.float32)

            for xyxy, c in zip(all_xyxy, all_conf):
                # patch-scaled -> patch-original -> frame
                lx1, ly1, lx2, ly2 = xyxy
                fx1 = int(lx1 * scale_x) + px1
                fy1 = int(ly1 * scale_y) + py1
                fx2 = int(lx2 * scale_x) + px1
                fy2 = int(ly2 * scale_y) + py1
                local_dets.append(dict(bbox=(fx1, fy1, fx2, fy2), conf=float(c), patch=pi))

        dets_per_patch.append(local_dets)
        all_dets.extend(local_dets)

    dets_global = nms(all_dets, nms_iou)
    return dets_global, dets_per_patch


# =============================================================================
# SESIÓN (port del loop de process(), sin dashboard)
# =============================================================================
class _PeopleSession(StreamSession):
    def __init__(self, model: "PeopleCounter", config: dict[str, Any]):
        self.model = model

        scene = str(config.get("scene", DEFAULT_SCENE))
        profile = SCENE_PROFILES.get(scene, SCENE_PROFILES[DEFAULT_SCENE])
        self.conf = float(config.get("conf", profile["conf"]))
        self.nms_iou = float(config.get("nms_iou", profile["nms_iou"]))

        self.rows = max(1, int(config.get("rows", GRID_ROWS)))
        self.cols = max(1, int(config.get("cols", GRID_COLS)))
        self.overlap = max(0.0, min(0.5, float(config.get("overlap", PATCH_OVERLAP))))
        smooth = max(1, int(config.get("smooth_frames", SMOOTH_FRAMES)))

        # el grid se calcula con el primer frame (igual que el original)
        self.patches: list[tuple[int, int, int, int]] = []
        self.count_buf: deque[int] = deque(maxlen=smooth)
        self._last_count: int | None = None  # último conteo suavizado emitido

    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        h, w = frame.shape[:2]
        if not self.patches:
            self.patches = compute_patches(h, w, self.rows, self.cols, self.overlap)
            log.info("grid %dx%d = %d patches, frame %dx%d, overlap=%.0f%%",
                     self.rows, self.cols, len(self.patches), w, h, self.overlap * 100)

        dets_global, _ = run_patches(self.model.yolo, frame, self.patches,
                                     self.conf, self.nms_iou)

        self.count_buf.append(len(dets_global))
        count_smooth = sum(self.count_buf) / len(self.count_buf)
        count = int(round(count_smooth))

        # evento CONTEO cuando cambia el número (suavizado) de personas
        if count == self._last_count:
            return []
        self._last_count = count
        mean_conf = float(np.mean([d["conf"] for d in dets_global])) if dets_global else 0.0
        return [Event(
            type="CONTEO", score=mean_conf, ts=ts,
            detections=[Detection(label="person", score=d["conf"], bbox=d["bbox"])
                        for d in dets_global],
            extra={"count": count, "raw": len(dets_global),
                   "smooth": round(count_smooth, 2), "smooth_frames": self.count_buf.maxlen},
        )]


# =============================================================================
# MODELO
# =============================================================================
class PeopleCounter(CVModel):
    info = ModelInfo(
        name="people-counter",
        version="1.0",
        task="Conteo de personas por patch-zoom (grid + YOLO por patch + NMS global)",
        capabilities={Capability.CLIP, Capability.STREAM},
        event_types=["CONTEO"],
        config_schema=CONFIG_SCHEMA,
        target_fps=5.0,  # 8 pasadas YOLO por frame: 5 fps mantiene la GPU razonable
    )

    def __init__(self) -> None:
        self.yolo: YOLO | None = None

    def load(self) -> None:
        log.info("Cargando YOLO people: %s", WEIGHTS)
        self.yolo = YOLO(WEIGHTS)

    def warmup(self) -> None:
        session = self.create_session({})
        session.process_frame(np.zeros((360, 640, 3), dtype=np.uint8), 0.0)
        session.close()

    def unload(self) -> None:
        self.yolo = None

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        if self.yolo is None:
            self.load()
        return _PeopleSession(self, config)
