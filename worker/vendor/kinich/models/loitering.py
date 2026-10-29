"""LoiteringDetector — port de loitering.py (paquete merodeo_version_2) al contrato Kinich.

Fuente canónica: loitering.py del paquete (LoiteringDetector + StableIdAssigner +
LoiteringClassifier). loitering_hybrid_baseline.py es un banco de pruebas "v7 geo
only" sin RF ni las reglas anti-falsos-positivos, y main.py es un stub; no se portan.

Lógica de detección intacta respecto al original:
  YOLO track (BoT-SORT, solo personas) -> StableIdAssigner (IDs estables por
  IoU/distancia) -> suavizado de bbox + guardas anti-cruce/anti-ID-switch ->
  trayectoria por track -> reglas espacio-temporales (deadzone, anclaje,
  sentado, patrón circular, stop-and-go, ida-y-vuelta, corredor recto) ->
  suavizado temporal + histéresis asimétrica (activar/desactivar) + cooldown
  de alerta. Detección de video congelado incluida.

Nota fiel al original: el RandomForest (pkl) se carga y queda disponible
(LoiteringClassifier con padding a max_len), pero la decisión en update() del
original es 100% por reglas — predict/predict_proba nunca se invocan en el
pipeline. Se preserva tal cual.

Eliminado del original: display cv2, escritura de videos/clips/JSON a disco,
CLI, auto-instalación de dependencias, prints (-> logging) y la extracción de
clips por evento (la agregación de eventos es del servidor).
Cambios: el tracker BoT-SORT se instancia por sesión (BOTSORT) con el YOLO
compartido, porque `YOLO.track(persist=True)` guarda el estado del tracker
dentro del objeto YOLO y mezclaría IDs entre streams; el yaml custom
`botsort_loitering.yaml` no viene en las fuentes, así que se usa el botsort.yaml
default de ultralytics (desviación documentada). Para streams 24/7 se acota la
trayectoria por track (LOITERING_MAX_TRACK_POINTS) y se purga el estado de
tracks no vistos (LOITERING_TRACK_TTL_FRAMES), emitiendo MERODEO_OFF si un
track desaparece en estado Loitering (necesario para la semántica on/off).
"""
import logging
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from ultralytics import YOLO
from ultralytics.trackers import BOTSORT
from ultralytics.utils import IterableSimpleNamespace
from ultralytics.utils.checks import check_yaml

try:  # ultralytics < 8.4
    from ultralytics.utils import yaml_load
except ImportError:  # ultralytics >= 8.4: yaml_load se movió a YAML.load
    from ultralytics.utils import YAML

    yaml_load = YAML.load

from kinich.contract import Capability, CVModel, Event, ModelInfo, StreamSession

log = logging.getLogger(__name__)

WEIGHTS = os.environ.get("KINICH_LOITERING_WEIGHTS", "weights/yolo11x.pt")
RF_PATH = os.environ.get("KINICH_LOITERING_RF", "weights/loitering_model_rf.pkl")
# Desviación: el original usa config/trackers/botsort_loitering.yaml (no incluido
# en las fuentes); se usa el botsort default builtin de ultralytics.
TRACKER_YAML = "botsort.yaml"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


# =============================================================================
# CONFIG (knobs por sesión; defaults del perfil "legacy" del original)
# =============================================================================
CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "detect_scale": {
            "type": "number", "default": 1.0, "minimum": 0.1, "maximum": 1.0,
            "description": "detección a fracción de la resolución (ojo: los umbrales en px del original están calibrados a resolución nativa)",
        },
        "profile": {
            "type": "string", "enum": ["legacy", "strict"], "default": "legacy",
            "description": "perfil de umbrales base del original (strict = más conservador)",
        },
        "loitering_threshold": {
            "type": "number", "default": 0.72,
            "description": "umbral de probabilidad suavizada para alerta (0.85 en strict)",
        },
        "activation_threshold": {
            "type": "number", "default": 0.62,
            "description": "umbral de probabilidad suavizada para activar el estado Loitering (0.75 en strict)",
        },
        "min_frames_activate": {
            "type": "integer", "default": 12,
            "description": "frames consecutivos para pasar Normal -> Loitering (20 en strict)",
        },
        "min_frames_deactivate": {
            "type": "integer", "default": 30,
            "description": "frames consecutivos para pasar Loitering -> Normal (50 en strict)",
        },
        "trajectory_points": {
            "type": "integer", "default": 120,
            "description": "ventana deslizante de puntos de trayectoria para el análisis (40 en strict)",
        },
        "stride_frames": {
            "type": "integer", "default": 2,
            "description": "predecir cada N frames (3 en strict)",
        },
        "min_walking_distance": {
            "type": "number", "default": 150.0,
            "description": "px recorridos en la ventana reciente para considerar que camina",
        },
        "anchor_radius": {
            "type": "number", "default": 100.0,
            "description": "radio (px) de la zona de anclaje: dentro de él no es merodeo",
        },
        "min_stationary_area": {
            "type": "number", "default": 5000.0,
            "description": "área de movimiento (px²) bajo la cual se considera estacionario",
        },
        "use_advanced_rules": {
            "type": "boolean", "default": True,
            "description": "reglas anti-falsos-positivos (estático, deadzone, corredor recto, circular)",
        },
        "yolo_conf": {"type": "number", "default": 0.35, "description": "confianza mínima YOLO"},
        "yolo_iou": {"type": "number", "default": 0.6, "description": "IoU NMS de YOLO"},
    },
}


# =============================================================================
# ASIGNADOR DE IDS ESTABLES (idéntico al original)
# =============================================================================
class StableIdAssigner:
    """Asignador simple de IDs estables por IoU/distancia sobre los IDs crudos
    del tracker, para amortiguar re-asignaciones tras oclusiones breves."""

    def __init__(self, iou_threshold: float = 0.3, dist_factor: float = 0.6, max_age: int = 30):
        self.iou_threshold = iou_threshold
        self.dist_factor = dist_factor
        self.max_age = max_age
        self.next_id = 1
        self.stable_tracks: dict[int, dict] = {}
        self.raw_to_stable: dict[int, int] = {}

    @staticmethod
    def _bbox_iou(a: list[float], b: list[float]) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        inter_x1 = max(ax1, bx1)
        inter_y1 = max(ay1, by1)
        inter_x2 = min(ax2, bx2)
        inter_y2 = min(ay2, by2)
        inter_w = max(0.0, inter_x2 - inter_x1)
        inter_h = max(0.0, inter_y2 - inter_y1)
        inter_area = inter_w * inter_h
        area_a = max(0.0, (ax2 - ax1)) * max(0.0, (ay2 - ay1))
        area_b = max(0.0, (bx2 - bx1)) * max(0.0, (by2 - by1))
        union = area_a + area_b - inter_area
        return inter_area / union if union > 0 else 0.0

    @staticmethod
    def _center(bbox: list[float]) -> tuple[float, float]:
        x1, y1, x2, y2 = bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def assign(self, detections: list[dict], frame_id: int) -> list[dict]:
        # Limpiar tracks viejos
        stale_ids = [sid for sid, st in self.stable_tracks.items()
                     if frame_id - st["last_frame"] > self.max_age]
        for sid in stale_ids:
            del self.stable_tracks[sid]

        for raw_id, sid in list(self.raw_to_stable.items()):
            if sid not in self.stable_tracks:
                del self.raw_to_stable[raw_id]

        used: set[int] = set()
        assigned = []

        for det in detections:
            raw_id = det["track_id"]
            bbox = det["bbox"]
            stable_id = None

            if raw_id in self.raw_to_stable:
                sid = self.raw_to_stable[raw_id]
                if sid in self.stable_tracks and sid not in used:
                    stable_id = sid

            if stable_id is None:
                best_iou = 0.0
                best_sid = None
                cx, cy = self._center(bbox)
                bw = max(1.0, bbox[2] - bbox[0])
                bh = max(1.0, bbox[3] - bbox[1])
                dist_limit = self.dist_factor * max(bw, bh)

                for sid, st in self.stable_tracks.items():
                    if sid in used:
                        continue
                    iou = self._bbox_iou(bbox, st["bbox"])
                    if iou >= self.iou_threshold and iou > best_iou:
                        best_iou = iou
                        best_sid = sid
                        continue

                    scx, scy = self._center(st["bbox"])
                    dist = np.hypot(cx - scx, cy - scy)
                    if dist <= dist_limit and iou > best_iou:
                        best_iou = iou
                        best_sid = sid

                stable_id = best_sid

            if stable_id is None:
                stable_id = self.next_id
                self.next_id += 1

            self.raw_to_stable[raw_id] = stable_id
            used.add(stable_id)
            self.stable_tracks[stable_id] = {"bbox": bbox, "last_frame": frame_id}
            det["track_id"] = stable_id
            assigned.append(det)

        return assigned


@dataclass
class TrackPoint:
    """Punto de trayectoria"""
    x: float
    y: float
    frame_id: int
    timestamp: float
    confidence: float
    bbox: list[float]


class SpatioTemporalFeatureExtractor:
    """Extractor de características espacio-temporales (ángulos de 3 puntos)"""

    def __init__(self, n_trajectory_points: int = 15):
        self.n_trajectory_points = n_trajectory_points

    def extract_features(self, trajectory: list[TrackPoint]) -> np.ndarray | None:
        """Extrae ángulos de trayectoria usando método de 3 puntos"""
        if len(trajectory) < 3:
            return None

        x = np.array([p.x for p in trajectory])
        y = np.array([p.y for p in trajectory])

        # Método de 3 puntos consecutivos
        p1_x, p1_y = x[:-2], y[:-2]
        p2_x, p2_y = x[1:-1], y[1:-1]
        p3_x, p3_y = x[2:], y[2:]

        deg1 = (360 + np.degrees(np.arctan2(p2_y - p1_y, p2_x - p1_x))) % 360
        deg2 = (360 + np.degrees(np.arctan2(p3_y - p2_y, p3_x - p2_x))) % 360

        angles = np.where(deg2 <= deg1, deg2 - deg1, 360 - (deg1 - deg2))
        return np.abs(angles) / 360.0


class LoiteringClassifier:
    """Clasificador de merodeo (Random Forest). En el original se carga pero la
    decisión de update() es por reglas; se conserva cargado y disponible."""

    def __init__(self, model_path: str):
        # Import perezoso: joblib/sklearn solo se necesitan al cargar el pkl
        # (extra `models` de pyproject), no al importar el módulo.
        import joblib

        if not Path(model_path).exists():
            raise FileNotFoundError(f"Modelo no encontrado: {model_path}")

        data = joblib.load(model_path)
        self.rf_model = data["rf_model"]
        self.max_len = data["max_len"]
        log.info("RandomForest de merodeo cargado: %s (max_len=%d)", model_path, self.max_len)

    def predict(self, X: list[np.ndarray]) -> np.ndarray:
        """Predice clase (-1 = loitering, 1 = normal)"""
        return self.rf_model.predict(self._pad_features(X))

    def predict_proba(self, X: list[np.ndarray]) -> np.ndarray:
        """Predice probabilidades de cada clase"""
        return self.rf_model.predict_proba(self._pad_features(X))

    def _pad_features(self, features: list[np.ndarray]) -> np.ndarray:
        """Rellena features a longitud fija"""
        padded = []
        for feat in features:
            if len(feat) < self.max_len:
                padded_feat = np.pad(feat, (0, self.max_len - len(feat)), mode="constant")
            else:
                padded_feat = feat[: self.max_len]
            padded.append(padded_feat)
        return np.array(padded)


# =============================================================================
# NÚCLEO (port de LoiteringDetector del original; estado por sesión)
# =============================================================================
class _LoiteringEngine:
    """Máquina de estados de merodeo por track. Port fiel de LoiteringDetector
    del original; en lugar de imprimir alertas, acumula transiciones on/off que
    la sesión convierte en Events."""

    def __init__(self, classifier: LoiteringClassifier | None, config: dict[str, Any]):
        profile = str(config.get("profile", os.environ.get("LOITERING_PROFILE", "legacy"))).strip().lower()
        if profile in ("strict", "default"):
            base_traj_points = 40
            base_stride_frames = 3
            base_loitering_threshold = 0.85
            base_activation_threshold = 0.75
            base_temp_window = 10
            base_min_frames_activate = 20
            base_min_frames_deactivate = 50
        else:
            base_traj_points = 120
            base_stride_frames = 2
            base_loitering_threshold = 0.72
            base_activation_threshold = 0.62
            base_temp_window = 6
            base_min_frames_activate = 12
            base_min_frames_deactivate = 30

        self.n_trajectory_points = int(config.get(
            "trajectory_points", _env_int("LOITERING_TRAJ_POINTS", base_traj_points)))
        self.stride_frames = max(1, int(config.get(
            "stride_frames", _env_int("LOITERING_STRIDE_FRAMES", base_stride_frames))))

        # Parámetros de detección
        self.loitering_threshold = float(config.get(
            "loitering_threshold", _env_float("LOITERING_ALERT_THRESHOLD", base_loitering_threshold)))
        self.alert_cooldown_seconds = 5.0

        # Umbrales de movimiento configurables (para multitudes)
        self.min_walking_distance = float(config.get(
            "min_walking_distance", _env_float("LOITERING_MIN_WALKING_DISTANCE", 150.0)))
        self.anchor_radius = float(config.get(
            "anchor_radius", _env_float("LOITERING_ANCHOR_RADIUS", 100.0)))
        self.min_stationary_area = float(config.get(
            "min_stationary_area", _env_float("LOITERING_MIN_STATIONARY_AREA", 5000.0)))

        # Reglas anti-falsos positivos
        self.use_advanced_rules = bool(config.get(
            "use_advanced_rules", _env_bool("LOITERING_ADVANCED_RULES", "true")))
        self.static_displacement_threshold = _env_float("LOITERING_STATIC_THRESHOLD", 120.0)  # px
        self.straight_walk_efficiency = _env_float("LOITERING_STRAIGHT_EFFICIENCY", 0.65)  # ratio 0-1
        self.circular_efficiency_threshold = _env_float("LOITERING_CIRCULAR_EFFICIENCY", 0.25)  # ratio 0-1
        self.circular_min_distance = _env_float("LOITERING_CIRCULAR_MIN_DIST", 200.0)  # px

        # Deadzone para personas quietas: micro-movimientos por debajo del umbral
        # no acumulan distancia recorrida
        self.movement_deadzone = _env_float("LOITERING_MOVEMENT_DEADZONE", 12.0)  # px por frame
        self.jitter_deadzone = _env_float("LOITERING_JITTER_DEADZONE_PX", self.movement_deadzone)
        self.crowd_jitter_multiplier = _env_float("LOITERING_CROWD_JITTER_MULTIPLIER", 2.5)

        # Detección de saltos/teleports por errores de tracking (ID swaps)
        self.max_jump_distance = _env_float("LOITERING_MAX_JUMP_DISTANCE", 80.0)  # px por frame
        self.id_switch_jump_distance = _env_float(
            "LOITERING_ID_SWITCH_JUMP_DISTANCE",
            max(120.0, self.max_jump_distance * 1.5),
        )

        # Suavizado temporal
        self.temporal_window_size = _env_int("LOITERING_TEMPORAL_WINDOW", base_temp_window)
        self.prediction_history = defaultdict(lambda: deque(maxlen=self.temporal_window_size))

        # Umbrales ASIMÉTRICOS: estricto para activar, difícil para desactivar.
        # Como se predice cada N frames, se convierten a predicciones consecutivas.
        self.min_frames_to_activate = int(config.get(
            "min_frames_activate", _env_int("LOITERING_MIN_FRAMES_ACTIVATE", base_min_frames_activate)))
        self.min_frames_to_deactivate = int(config.get(
            "min_frames_deactivate", _env_int("LOITERING_MIN_FRAMES_DEACTIVATE", base_min_frames_deactivate)))
        self.min_predictions_to_activate = max(1, int(np.ceil(self.min_frames_to_activate / self.stride_frames)))
        self.min_predictions_to_deactivate = max(1, int(np.ceil(self.min_frames_to_deactivate / self.stride_frames)))
        self.consecutive_counter = defaultdict(int)

        self.loitering_activation_threshold = float(config.get(
            "activation_threshold", _env_float("LOITERING_ACTIVATION_THRESHOLD", base_activation_threshold)))
        self.current_state: dict[int, bool] = {}  # True=Loitering, False=Normal

        self.extractor = SpatioTemporalFeatureExtractor(self.n_trajectory_points)
        self.classifier = classifier  # RF compartido del modelo padre (ver docstring)

        # Trayectoria por track. El original la guarda completa (para dibujar);
        # aquí se acota para streams largos (desviación documentada).
        self.max_track_points = _env_int("LOITERING_MAX_TRACK_POINTS", 2400)
        self.tracks: dict[int, list[TrackPoint]] = defaultdict(list)
        self.last_result: dict[int, dict] = {}  # el original acumulaba todo results_by_track; solo se usa el último
        self.last_alert_time: dict[int, float] = {}

        # Patrones de comportamiento sospechoso
        self.stop_go_counter = defaultdict(int)
        self.last_movement_state: dict[int, bool] = {}
        self.visited_positions = defaultdict(list)  # últimas 100 posiciones
        self.direction_changes = defaultdict(int)

        # Detección de frames congelados (evita falsos positivos en streams caídos)
        self.frame_hashes = deque(maxlen=10)
        self.frozen_frame_count = 0
        self.is_video_frozen = False
        self.frozen_min_frames = _env_int("LOITERING_FROZEN_MIN_FRAMES", 5)
        self.frozen_diff_threshold = _env_float("LOITERING_FROZEN_DIFF_THRESHOLD", 0.5)
        self.frozen_motion_threshold = _env_float("LOITERING_FROZEN_MOTION_PX", 2.0)
        self.prev_small_gray: np.ndarray | None = None
        self.prev_detection_centers: dict[int, tuple[float, float]] = {}
        self.prev_detection_count: int | None = None

        # Suavizado de bbox y gating anti-ID-switch
        self.bbox_ema_alpha = 0.6
        self.bbox_max_scale_change = 0.5   # máx ±50% por frame
        self.bbox_max_aspect_change = 1.2  # cambio máximo de aspecto por frame
        self.smoothed_bbox: dict[int, tuple] = {}
        self.last_raw_bbox: dict[int, list[float]] = {}
        self.bbox_anomaly_counter = defaultdict(int)
        self.bbox_size_history = defaultdict(lambda: deque(maxlen=15))
        self.bbox_size_baseline: dict[int, tuple] = {}
        self.bbox_size_lock = defaultdict(int)

        # Suavizado más estable en multitudes
        self.crowd_bbox_scale_up = 0.2
        self.crowd_bbox_scale_down = 0.3
        self.bbox_center_alpha = 0.6
        self.bbox_size_alpha = 0.25
        self.crowd_inflation_ratio = 1.2
        self.crowd_size_cap = 1.05
        self.crowd_lock_frames = 8
        self.crowd_center_jump = 0.35
        self.crowd_iou_guard = 0.3
        self.crowd_proximity_scale = 0.9
        self.crossing_iou_guard = _env_float("LOITERING_CROSSING_IOU_GUARD", 0.15)
        self.crossing_center_guard = _env_float("LOITERING_CROSSING_CENTER_GUARD", 0.5)
        self.crossing_hold_frames = max(1, _env_int("LOITERING_CROSSING_HOLD_FRAMES", 1))
        self.crossing_hold = defaultdict(int)
        self.crossing_active = defaultdict(bool)
        self.straight_corridor_px = _env_float("LOITERING_STRAIGHT_CORRIDOR_PX", 60.0)
        self.straight_corridor_scale = _env_float("LOITERING_STRAIGHT_CORRIDOR_SCALE", 0.4)
        self.straight_deviation_ratio = _env_float("LOITERING_STRAIGHT_DEVIATION_RATIO", 0.35)
        self.progress_min_span_px = _env_float("LOITERING_PROGRESS_MIN_SPAN_PX", 250.0)
        self.progress_aspect_ratio = _env_float("LOITERING_PROGRESS_ASPECT_RATIO", 2.5)
        self.progress_displacement_ratio = _env_float("LOITERING_PROGRESS_DISPLACEMENT_RATIO", 0.6)
        self.backforth_min_distance = _env_float("LOITERING_BACKFORTH_MIN_DISTANCE", 200.0)
        self.backforth_max_span_px = _env_float("LOITERING_BACKFORTH_MAX_SPAN_PX", 300.0)
        self.backforth_span_scale = _env_float("LOITERING_BACKFORTH_SPAN_SCALE", 3.0)
        self.backforth_max_efficiency = _env_float("LOITERING_BACKFORTH_MAX_EFF", 0.6)

        # Purga de tracks no vistos (nuevo: acota memoria en streams 24/7)
        self.track_ttl_frames = _env_int("LOITERING_TRACK_TTL_FRAMES", 900)
        self.last_seen_frame: dict[int, int] = {}

        # Transiciones on/off pendientes de emitir como eventos
        self.transitions: list[dict] = []

    # ── helpers de bbox (idénticos al original) ─────────────────────────────
    def _bbox_state_from_xyxy(self, bbox: list[float]) -> tuple:
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        w = max(1.0, x2 - x1)
        h = max(1.0, y2 - y1)
        return (cx, cy, w, h)

    def _smooth_bbox(self, track_id: int, bbox: list[float], is_crowded: bool = False) -> list[float]:
        cx, cy, w, h = self._bbox_state_from_xyxy(bbox)

        prev_state = self.smoothed_bbox.get(track_id)

        # Historial de tamaños base (solo cuando NO hay multitud)
        if not is_crowded:
            self.bbox_size_history[track_id].append((w, h))
            if len(self.bbox_size_history[track_id]) >= 3:
                sizes = self.bbox_size_history[track_id]
                median_w = float(np.median([s[0] for s in sizes]))
                median_h = float(np.median([s[1] for s in sizes]))
                self.bbox_size_baseline[track_id] = (median_w, median_h)
        elif track_id not in self.bbox_size_baseline:
            # En multitud sin baseline, fijarlo desde el estado previo para evitar crecimiento
            if prev_state:
                self.bbox_size_baseline[track_id] = (prev_state[2], prev_state[3])

        baseline = self.bbox_size_baseline.get(track_id)
        if is_crowded and baseline:
            base_w, base_h = baseline
            w = np.clip(w, base_w * (1 - self.crowd_bbox_scale_down), base_w * self.crowd_size_cap)
            h = np.clip(h, base_h * (1 - self.crowd_bbox_scale_down), base_h * self.crowd_size_cap)

        if prev_state:
            prev_cx, prev_cy, prev_w, prev_h = prev_state
            max_scale = self.bbox_max_scale_change
            w = np.clip(w, prev_w * (1 - max_scale), prev_w * (1 + max_scale))
            h = np.clip(h, prev_h * (1 - max_scale), prev_h * (1 + max_scale))

            if is_crowded:
                prev_bbox = [prev_cx - prev_w / 2.0, prev_cy - prev_h / 2.0,
                             prev_cx + prev_w / 2.0, prev_cy + prev_h / 2.0]
                iou = self._bbox_iou(bbox, prev_bbox)
                prev_area = prev_w * prev_h
                curr_area = w * h
                if iou < self.crowd_iou_guard or (prev_area > 0 and curr_area / prev_area > self.crowd_inflation_ratio):
                    # Oclusión/mezcla probable: mantener bbox previo por varios frames
                    if self.bbox_size_lock[track_id] == 0:
                        self.bbox_size_lock[track_id] = self.crowd_lock_frames

                if self.bbox_size_lock[track_id] > 0:
                    self.bbox_size_lock[track_id] -= 1
                    w, h = prev_w, prev_h

                    center_shift = np.hypot(cx - prev_cx, cy - prev_cy)
                    center_limit = self.crowd_center_jump * max(prev_w, prev_h)
                    if center_shift > center_limit:
                        scale = center_limit / center_shift if center_shift > 0 else 0.0
                        cx = prev_cx + (cx - prev_cx) * scale
                        cy = prev_cy + (cy - prev_cy) * scale

            cx = self.bbox_center_alpha * cx + (1 - self.bbox_center_alpha) * prev_cx
            cy = self.bbox_center_alpha * cy + (1 - self.bbox_center_alpha) * prev_cy
            w = self.bbox_size_alpha * w + (1 - self.bbox_size_alpha) * prev_w
            h = self.bbox_size_alpha * h + (1 - self.bbox_size_alpha) * prev_h

        self.smoothed_bbox[track_id] = (cx, cy, w, h)
        return [cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0]

    def _bbox_iou(self, a: list[float], b: list[float]) -> float:
        return StableIdAssigner._bbox_iou(a, b)

    def _compute_crowded_ids(self, detections: list[dict]) -> set:
        crowded: set[int] = set()
        if len(detections) < 2:
            return crowded

        for i in range(len(detections)):
            for j in range(i + 1, len(detections)):
                a = detections[i]["bbox"]
                b = detections[j]["bbox"]
                iou = self._bbox_iou(a, b)

                acx, acy, aw, ah = self._bbox_state_from_xyxy(a)
                bcx, bcy, bw, bh = self._bbox_state_from_xyxy(b)
                dist = np.hypot(acx - bcx, acy - bcy)
                proximity = self.crowd_proximity_scale * (max(aw, ah) + max(bw, bh))

                if iou > 0.02 or dist < proximity:
                    crowded.add(detections[i]["track_id"])
                    crowded.add(detections[j]["track_id"])

        return crowded

    def _is_crossing_event(self, track_id: int, bbox: list[float], detections: list[dict]) -> bool:
        if len(detections) < 2:
            return False

        cx, cy, w, h = self._bbox_state_from_xyxy(bbox)
        for det in detections:
            if det["track_id"] == track_id:
                continue
            other_bbox = det["bbox"]
            iou = self._bbox_iou(bbox, other_bbox)
            if iou >= self.crossing_iou_guard:
                return True

            ocx, ocy, ow, oh = self._bbox_state_from_xyxy(other_bbox)
            dist = np.hypot(cx - ocx, cy - ocy)
            avg_scale = max(1.0, (max(w, h) + max(ow, oh)) / 2.0)
            if dist <= self.crossing_center_guard * avg_scale:
                return True

        return False

    def _bbox_change_excessive(self, track_id: int, bbox: list[float]) -> bool:
        prev_bbox = self.last_raw_bbox.get(track_id)
        self.last_raw_bbox[track_id] = bbox

        if not prev_bbox:
            return False

        prev_cx, prev_cy, prev_w, prev_h = self._bbox_state_from_xyxy(prev_bbox)
        curr_cx, curr_cy, curr_w, curr_h = self._bbox_state_from_xyxy(bbox)

        scale_w = curr_w / prev_w if prev_w > 0 else 1.0
        scale_h = curr_h / prev_h if prev_h > 0 else 1.0
        size_change = max(scale_w, scale_h, 1.0 / scale_w, 1.0 / scale_h)

        prev_ratio = prev_h / prev_w if prev_w > 0 else 1.0
        curr_ratio = curr_h / curr_w if curr_w > 0 else 1.0
        aspect_change = abs(curr_ratio - prev_ratio)

        return size_change > (1 + self.bbox_max_scale_change) or aspect_change > self.bbox_max_aspect_change

    # ── frames congelados (idéntico al original, prints -> logging) ─────────
    def _detect_frozen_frame(self, frame: np.ndarray, motion_override: bool = False) -> bool:
        small_frame = cv2.resize(frame, (64, 64))
        gray = cv2.cvtColor(small_frame, cv2.COLOR_BGR2GRAY)
        frame_hash = hash(gray.tobytes())

        # Comparar con frame anterior usando diferencia promedio (tolerante a compresión)
        if self.prev_small_gray is not None:
            mean_diff = float(np.mean(cv2.absdiff(gray, self.prev_small_gray)))
            if mean_diff <= self.frozen_diff_threshold:
                self.frozen_frame_count += 1
            else:
                self.frozen_frame_count = 0
        else:
            self.frozen_frame_count = 0

        self.frame_hashes.append(frame_hash)
        self.prev_small_gray = gray

        if motion_override:
            self.frozen_frame_count = 0
            self.is_video_frozen = False
            return False

        # Video congelado si hay N frames casi idénticos consecutivos
        if self.frozen_frame_count >= self.frozen_min_frames:
            if not self.is_video_frozen:
                log.warning("Video congelado detectado - pausando detección de merodeo")
            self.is_video_frozen = True
            return True
        else:
            if self.is_video_frozen:
                log.info("Video reactivado - reanudando detección de merodeo")
            self.is_video_frozen = False
            return False

    def _detections_show_motion(self, detections: list[dict]) -> bool:
        if self.prev_detection_count is None:
            return True
        if len(detections) != self.prev_detection_count:
            return True
        for det in detections:
            track_id = det["track_id"]
            prev_center = self.prev_detection_centers.get(track_id)
            if prev_center is None:
                return True
            cx, cy, _, _ = self._bbox_state_from_xyxy(det["bbox"])
            shift = np.hypot(cx - prev_center[0], cy - prev_center[1])
            if shift >= self.frozen_motion_threshold:
                return True
        return False

    def _store_detection_centers(self, detections: list[dict]) -> None:
        centers = {}
        for det in detections:
            track_id = det["track_id"]
            cx, cy, _, _ = self._bbox_state_from_xyxy(det["bbox"])
            centers[track_id] = (cx, cy)
        self.prev_detection_centers = centers
        self.prev_detection_count = len(detections)

    # ── purga de estado por track (nuevo: memoria acotada + OFF al perderse) ─
    def _purge_stale_tracks(self, frame_id: int, timestamp: float) -> None:
        stale = [tid for tid, last in self.last_seen_frame.items()
                 if frame_id - last > self.track_ttl_frames]
        for tid in stale:
            if self.current_state.get(tid):
                self.transitions.append({
                    "on": False, "track_id": tid, "score": 0.0, "ts": timestamp,
                    "bbox": None, "extra": {"reason": "track_lost"},
                })
            for container in (self.tracks, self.current_state, self.consecutive_counter,
                              self.prediction_history, self.stop_go_counter,
                              self.last_movement_state, self.visited_positions,
                              self.direction_changes, self.smoothed_bbox,
                              self.last_raw_bbox, self.bbox_anomaly_counter,
                              self.bbox_size_history, self.bbox_size_baseline,
                              self.bbox_size_lock, self.crossing_hold,
                              self.crossing_active, self.last_alert_time,
                              self.last_result, self.last_seen_frame):
                container.pop(tid, None)

    def pop_transitions(self) -> list[dict]:
        out = self.transitions
        self.transitions = []
        return out

    # ── update (port fiel del original) ─────────────────────────────────────
    def update(self, detections: list[dict], frame_id: int, timestamp: float,
               frame: np.ndarray | None = None) -> dict[int, dict]:
        """Actualiza el detector con nuevas detecciones. Devuelve resultados
        por track_id y acumula transiciones on/off en self.transitions."""
        results: dict[int, dict] = {}

        self._purge_stale_tracks(frame_id, timestamp)
        for det in detections:
            self.last_seen_frame[det["track_id"]] = frame_id

        # CRÍTICO: si el video está congelado NO predecir (evita falsos positivos)
        if frame is not None:
            motion_override = self._detections_show_motion(detections)
            self._store_detection_centers(detections)
            is_frozen = self._detect_frozen_frame(frame, motion_override=motion_override)
            if is_frozen:
                # Video congelado: retornar resultados previos sin actualizar
                for det in detections:
                    track_id = det["track_id"]
                    if track_id in self.current_state:
                        prediction = "Loitering" if self.current_state[track_id] else "Normal"
                        last = self.last_result.get(track_id)
                        last_conf = last["confidence"] if last else 0.5
                        results[track_id] = {
                            "frame_id": frame_id,
                            "timestamp": timestamp,
                            "prediction": prediction,
                            "confidence": last_conf,
                            "is_alert": False,
                        }
                return results

        # Agregar puntos a trayectoria en CADA frame; predecir cada N frames
        should_predict = (frame_id % self.stride_frames == 0)

        crowded_ids = self._compute_crowded_ids(detections)

        for det in detections:
            track_id = det["track_id"]
            raw_bbox = det["bbox"]
            crossing_event = self._is_crossing_event(track_id, raw_bbox, detections)
            if crossing_event and not self.crossing_active[track_id] and self.tracks[track_id]:
                self.crossing_hold[track_id] = max(self.crossing_hold[track_id], self.crossing_hold_frames)
            self.crossing_active[track_id] = crossing_event

            is_crowded = track_id in crowded_ids
            bbox_change_excessive = self._bbox_change_excessive(track_id, raw_bbox)
            if not is_crowded and self.crossing_hold[track_id] == 0:
                if bbox_change_excessive:
                    self.bbox_anomaly_counter[track_id] += 1
                else:
                    self.bbox_anomaly_counter[track_id] = 0
            else:
                self.bbox_anomaly_counter[track_id] = 0
            prev_state = self.smoothed_bbox.get(track_id)
            bbox = self._smooth_bbox(track_id, raw_bbox, is_crowded=is_crowded)
            if prev_state:
                prev_cx, prev_cy, prev_w, prev_h = prev_state
                cx, cy, w, h = self._bbox_state_from_xyxy(bbox)
                center_shift = np.hypot(cx - prev_cx, cy - prev_cy)
                unstable_bbox = is_crowded or self.bbox_anomaly_counter[track_id] > 0
                jitter_threshold = self.jitter_deadzone * (self.crowd_jitter_multiplier if unstable_bbox else 1.0)
                if unstable_bbox and center_shift < jitter_threshold:
                    bbox = [prev_cx - prev_w / 2.0, prev_cy - prev_h / 2.0,
                            prev_cx + prev_w / 2.0, prev_cy + prev_h / 2.0]
                    self.smoothed_bbox[track_id] = (prev_cx, prev_cy, prev_w, prev_h)
            x1, y1, x2, y2 = bbox

            if self.crossing_hold[track_id] > 0:
                self.crossing_hold[track_id] -= 1
                if track_id in self.current_state:
                    prediction = "Loitering" if self.current_state[track_id] else "Normal"
                    last = self.last_result.get(track_id)
                    last_conf = last["confidence"] if last else 0.5
                    results[track_id] = {
                        "frame_id": frame_id,
                        "timestamp": timestamp,
                        "prediction": prediction,
                        "confidence": last_conf,
                        "is_alert": False,
                    }
                continue

            # Crear punto de trayectoria
            track_point = TrackPoint(
                x=float((x1 + x2) / 2),
                y=float((y1 + y2) / 2),
                frame_id=frame_id,
                timestamp=timestamp,
                confidence=float(det.get("confidence", 1.0)),
                bbox=[float(v) for v in bbox],
            )

            self.tracks[track_id].append(track_point)
            # Acotar trayectoria (nuevo: streams 24/7; el original guardaba todo)
            if len(self.tracks[track_id]) > self.max_track_points:
                self.tracks[track_id] = self.tracks[track_id][-self.max_track_points:]

            # DETECCIÓN DE ID SWITCH: saltos anormales en la trayectoria
            # (ocurre cuando el tracker confunde IDs al cruzarse personas)
            id_switch_detected = False
            if len(self.tracks[track_id]) >= 2:
                prev_point = self.tracks[track_id][-2]
                curr_point = self.tracks[track_id][-1]

                jump_distance = np.sqrt(
                    (curr_point.x - prev_point.x) ** 2 +
                    (curr_point.y - prev_point.y) ** 2
                )

                bbox_anomaly_persistent = self.bbox_anomaly_counter[track_id] >= 3
                jump_threshold = max(self.id_switch_jump_distance, self.max_jump_distance)
                if jump_distance > jump_threshold or bbox_anomaly_persistent:
                    id_switch_detected = True
                    was_loitering = self.current_state.get(track_id, False)
                    # LIMPIAR TODO EL TRACK - empezar de cero
                    self.tracks[track_id] = [track_point]
                    self.stop_go_counter[track_id] = 0
                    self.direction_changes[track_id] = 0
                    self.visited_positions[track_id] = []
                    self.prediction_history[track_id].clear()
                    self.current_state[track_id] = False  # Forzar a Normal
                    self.consecutive_counter[track_id] = 0
                    self.smoothed_bbox[track_id] = self._bbox_state_from_xyxy(bbox)
                    self.last_raw_bbox[track_id] = raw_bbox
                    if track_id in self.bbox_size_history:
                        self.bbox_size_history[track_id].clear()
                    if track_id in self.bbox_size_baseline:
                        del self.bbox_size_baseline[track_id]
                    self.bbox_anomaly_counter[track_id] = 0
                    log.debug("ID switch detectado en track %d (salto: %.1fpx) - track reiniciado",
                              track_id, jump_distance)
                    if was_loitering:
                        # El reset fuerza Normal: emitir OFF para consistencia on/off
                        self.transitions.append({
                            "on": False, "track_id": track_id, "score": 0.0, "ts": timestamp,
                            "bbox": [float(v) for v in bbox], "extra": {"reason": "id_switch"},
                        })

            # Solo predecir cada N frames; en frames intermedios usar último resultado
            if not should_predict:
                if track_id in self.current_state:
                    prediction = "Loitering" if self.current_state[track_id] else "Normal"
                    last = self.last_result.get(track_id)
                    last_conf = last["confidence"] if last else 0.5
                    results[track_id] = {
                        "frame_id": frame_id,
                        "timestamp": timestamp,
                        "prediction": prediction,
                        "confidence": last_conf,
                        "is_alert": False,
                    }
                continue

            # Si se detectó ID switch, saltarse análisis de este frame
            if id_switch_detected:
                continue

            # Analizar si tenemos suficientes puntos
            if len(self.tracks[track_id]) >= self.n_trajectory_points:
                # Ventana deslizante de los ÚLTIMOS N puntos (comportamiento ACTUAL)
                recent_trajectory = self.tracks[track_id][-self.n_trajectory_points:]

                # FILTRO DE VELOCIDAD: distancia total recorrida en la ventana reciente
                total_distance = 0.0
                for i in range(1, len(recent_trajectory)):
                    dx = recent_trajectory[i].x - recent_trajectory[i - 1].x
                    dy = recent_trajectory[i].y - recent_trajectory[i - 1].y
                    total_distance += np.sqrt(dx * dx + dy * dy)

                is_moving = total_distance > self.min_walking_distance

                # ── reglas anti-falsos positivos (requieren historia mínima) ──
                full_trajectory = self.tracks[track_id]
                min_points_for_analysis = 80  # ~4-6 segundos de historia
                if len(full_trajectory) >= min_points_for_analysis:
                    # LIMPIEZA DE TRAYECTORIA: dividir en segmentos continuos
                    # (sin saltos por ID swaps) y usar el más largo
                    cleaned_trajectory = []
                    current_segment = [full_trajectory[0]]

                    for i in range(1, len(full_trajectory)):
                        prev_point = full_trajectory[i - 1]
                        curr_point = full_trajectory[i]

                        dx = curr_point.x - prev_point.x
                        dy = curr_point.y - prev_point.y
                        step_distance = np.sqrt(dx * dx + dy * dy)

                        if step_distance > self.max_jump_distance:
                            if len(current_segment) > 10:
                                cleaned_trajectory.append(current_segment)
                            current_segment = [curr_point]
                        else:
                            current_segment.append(curr_point)

                    if len(current_segment) > 10:
                        cleaned_trajectory.append(current_segment)

                    original_length = len(full_trajectory)
                    if cleaned_trajectory:
                        full_trajectory = max(cleaned_trajectory, key=len)
                        num_segments = len(cleaned_trajectory)
                        trajectory_cleaned = True
                    else:
                        full_trajectory = self.tracks[track_id]
                        num_segments = 0
                        trajectory_cleaned = False

                    # Si la trayectoria limpia es < 30% de la original, muy corrupta
                    cleaned_ratio = len(full_trajectory) / max(1, original_length)
                    is_heavily_corrupted = trajectory_cleaned and cleaned_ratio < 0.30

                    first_point = full_trajectory[0]
                    last_point = full_trajectory[-1]
                    net_displacement = np.sqrt((last_point.x - first_point.x) ** 2 +
                                               (last_point.y - first_point.y) ** 2)

                    # Eficiencia del camino (lineal vs circular) con deadzone y filtro de saltos
                    total_path_distance = 0.0
                    total_path_distance_with_deadzone = 0.0
                    num_jumps_detected = 0

                    for i in range(1, len(full_trajectory)):
                        dx = full_trajectory[i].x - full_trajectory[i - 1].x
                        dy = full_trajectory[i].y - full_trajectory[i - 1].y
                        step_distance = np.sqrt(dx * dx + dy * dy)

                        is_jump = step_distance > self.max_jump_distance
                        if is_jump:
                            num_jumps_detected += 1
                            continue  # error de tracking: no suma distancia

                        total_path_distance += step_distance
                        if step_distance > self.movement_deadzone:
                            total_path_distance_with_deadzone += step_distance

                    path_efficiency = net_displacement / total_path_distance if total_path_distance > 0 else 1.0
                    max_lateral_deviation = 0.0
                    line_len = np.hypot(last_point.x - first_point.x, last_point.y - first_point.y)
                    if line_len > 1.0:
                        dx = last_point.x - first_point.x
                        dy = last_point.y - first_point.y
                        for p in full_trajectory:
                            distance = abs(dy * p.x - dx * p.y + last_point.x * first_point.y
                                           - last_point.y * first_point.x) / line_len
                            if distance > max_lateral_deviation:
                                max_lateral_deviation = distance

                    # Persona REALMENTE estática (bbox fluctúa pero no se mueve)
                    is_truly_static = net_displacement < self.static_displacement_threshold

                    # Quieta con micro-movimientos (deadzone aplicado)
                    is_static_with_micro_movements = (
                        total_path_distance_with_deadzone < (self.static_displacement_threshold * 1.5))

                    # Caminata lineal con propósito
                    is_walking_straight = path_efficiency > self.straight_walk_efficiency

                    # Patrón circular sospechoso (usa distancia CON deadzone)
                    is_circular_pattern = (path_efficiency < self.circular_efficiency_threshold and
                                           total_path_distance_with_deadzone > self.circular_min_distance)

                    # Tracking poco confiable (muchos saltos/ID swaps)
                    jump_ratio = num_jumps_detected / max(1, len(full_trajectory) - 1)
                    is_unreliable_tracking = jump_ratio > 0.15 or is_heavily_corrupted

                else:
                    is_truly_static = False
                    is_static_with_micro_movements = False
                    is_walking_straight = False
                    is_circular_pattern = False
                    path_efficiency = 1.0
                    net_displacement = 0.0
                    total_path_distance_with_deadzone = 0.0
                    max_lateral_deviation = 0.0
                    num_jumps_detected = 0
                    is_unreliable_tracking = False
                    is_heavily_corrupted = False
                    trajectory_cleaned = False
                    cleaned_ratio = 1.0
                    num_segments = 0

                # Área total de movimiento en la ventana reciente
                all_x = [p.x for p in recent_trajectory]
                all_y = [p.y for p in recent_trajectory]
                movement_area_width = max(all_x) - min(all_x)
                movement_area_height = max(all_y) - min(all_y)
                movement_area = movement_area_width * movement_area_height
                movement_span = max(movement_area_width, movement_area_height)
                movement_thickness = max(1.0, min(movement_area_width, movement_area_height))
                movement_aspect = movement_span / movement_thickness
                deviation_ratio = max_lateral_deviation / max(1.0, movement_span)

                is_stationary = movement_area < self.min_stationary_area

                # Zona de anclaje: movimientos pequeños alrededor de un punto
                center_x = np.mean(all_x)
                center_y = np.mean(all_y)

                max_distance_from_center = 0.0
                for x, y in zip(all_x, all_y):
                    distance = np.sqrt((x - center_x) ** 2 + (y - center_y) ** 2)
                    if distance > max_distance_from_center:
                        max_distance_from_center = distance

                is_anchored = max_distance_from_center < self.anchor_radius

                # SENTADO vs PARADO por aspect ratio del bbox
                bbox_pt = recent_trajectory[-1].bbox
                bbox_width = bbox_pt[2] - bbox_pt[0]
                bbox_height = bbox_pt[3] - bbox_pt[1]
                aspect_ratio = bbox_height / bbox_width if bbox_width > 0 else 2.0

                is_sitting = aspect_ratio < 1.2
                corridor_px = max(self.straight_corridor_px,
                                  self.straight_corridor_scale * max(bbox_width, bbox_height))
                is_straight_within_corridor = is_walking_straight and max_lateral_deviation <= corridor_px
                progress_span_threshold = max(self.progress_min_span_px, 2.5 * max(bbox_width, bbox_height))
                is_progressive_motion = (
                    movement_span >= progress_span_threshold
                    and movement_aspect >= self.progress_aspect_ratio
                    and net_displacement >= self.progress_displacement_ratio * movement_span
                )
                # Protección extra: sentado con movimiento pequeño no es merodeo
                SITTING_ANCHOR_RADIUS = 80
                SITTING_AREA_THRESHOLD = 12000
                is_sitting_static = is_sitting and (
                    is_anchored
                    or movement_area < SITTING_AREA_THRESHOLD
                    or max_distance_from_center < SITTING_ANCHOR_RADIUS
                )

                # ── detección de patrones sospechosos ────────────────────────
                suspicious_behavior_detected = False
                behavior_reason = ""

                # REGLA CRÍTICA: tracking poco confiable -> no confiar
                if self.use_advanced_rules and is_unreliable_tracking:
                    suspicious_behavior_detected = False
                    if is_heavily_corrupted:
                        behavior_reason = (f"heavily corrupted trajectory "
                                           f"(cleaned: {cleaned_ratio:.0%}, segments: {num_segments})")
                    else:
                        behavior_reason = (f"unreliable tracking "
                                           f"(jumps: {num_jumps_detected}, ratio: {jump_ratio:.2%})")
                    self.stop_go_counter[track_id] = 0

                elif self.use_advanced_rules and is_truly_static:
                    suspicious_behavior_detected = False
                    behavior_reason = "truly static - no net displacement (not suspicious)"
                    self.stop_go_counter[track_id] = 0

                # REGLA DEADZONE: quieta con micro-movimientos (balanceo/gestos)
                elif self.use_advanced_rules and is_static_with_micro_movements:
                    suspicious_behavior_detected = False
                    behavior_reason = (f"static with micro-movements (deadzone filtered, "
                                       f"distance: {total_path_distance_with_deadzone:.1f}px)")
                    self.stop_go_counter[track_id] = 0

                # Caminata con progreso claro A -> B
                elif self.use_advanced_rules and is_progressive_motion:
                    suspicious_behavior_detected = False
                    behavior_reason = (
                        f"progressive walk (span: {movement_span:.1f}px, aspect: {movement_aspect:.2f}, "
                        f"disp: {net_displacement:.1f}px)"
                    )
                    self.stop_go_counter[track_id] = 0

                elif self.use_advanced_rules and is_straight_within_corridor:
                    suspicious_behavior_detected = False
                    behavior_reason = (f"walking straight (efficiency: {path_efficiency:.2f}, "
                                       f"deviation: {max_lateral_deviation:.1f}px, not suspicious)")
                    self.stop_go_counter[track_id] = 0
                elif self.use_advanced_rules and is_walking_straight and not is_straight_within_corridor:
                    limited_progress = net_displacement < (self.progress_displacement_ratio * movement_span)
                    if deviation_ratio >= self.straight_deviation_ratio and limited_progress:
                        suspicious_behavior_detected = True
                        behavior_reason = (
                            f"straight path with large deviation (ratio: {deviation_ratio:.2f}, "
                            f"dev: {max_lateral_deviation:.1f}px)"
                        )
                    else:
                        suspicious_behavior_detected = False
                        behavior_reason = (
                            f"straight path with tolerable deviation (ratio: {deviation_ratio:.2f}, "
                            f"dev: {max_lateral_deviation:.1f}px)"
                        )
                        self.stop_go_counter[track_id] = 0

                # Patrón circular claro: dando vueltas en área pequeña
                elif self.use_advanced_rules and is_circular_pattern:
                    suspicious_behavior_detected = True
                    behavior_reason = f"circular pattern detected (efficiency: {path_efficiency:.2f})"

                elif is_sitting_static:
                    suspicious_behavior_detected = False
                    behavior_reason = "sitting/static (not suspicious)"
                    self.stop_go_counter[track_id] = 0
                else:
                    # FILTRO PRIMARIO: anclado -> no es loitering
                    if is_anchored:
                        suspicious_behavior_detected = False
                        behavior_reason = "anchored in place (not suspicious)"

                    if is_stationary:
                        suspicious_behavior_detected = False

                    if is_sitting and is_anchored:
                        suspicious_behavior_detected = False
                        behavior_reason = "sitting and anchored (not suspicious)"

                # Multiplicadores de sensibilidad por contexto
                sitting_multiplier = 0.3 if is_sitting else 1.0
                anchor_multiplier = 0.1 if is_anchored else 1.0

                if not is_sitting_static and not is_straight_within_corridor and not is_progressive_motion:
                    # 1. Patrón STOP-AND-GO: cambios frecuentes quieto<->movimiento
                    if not is_sitting:
                        if track_id in self.last_movement_state:
                            if self.last_movement_state[track_id] != is_moving:
                                self.stop_go_counter[track_id] += 1
                                if self.stop_go_counter[track_id] >= 4:
                                    suspicious_behavior_detected = True
                                    behavior_reason = "stop-and-go pattern (4+ transitions)"

                        self.last_movement_state[track_id] = is_moving

                    # 2. Patrón CIRCULAR: regresa a posiciones anteriores
                    # (solo si NO está anclado, para no penalizar personas estáticas)
                    if not is_anchored and not is_sitting:
                        current_pos = (recent_trajectory[-1].x, recent_trajectory[-1].y)
                        self.visited_positions[track_id].append(current_pos)
                        if len(self.visited_positions[track_id]) > 100:
                            self.visited_positions[track_id] = self.visited_positions[track_id][-100:]

                        circular_radius_threshold = max(self.anchor_radius + 10,
                                                        0.35 * max(bbox_width, bbox_height))
                        if (len(self.visited_positions[track_id]) > 20
                                and max_distance_from_center > circular_radius_threshold):
                            for old_pos in self.visited_positions[track_id][:-20]:
                                dist_to_old = np.sqrt((current_pos[0] - old_pos[0]) ** 2 +
                                                      (current_pos[1] - old_pos[1]) ** 2)
                                if dist_to_old < 50:
                                    suspicious_behavior_detected = True
                                    behavior_reason = "circular pattern (returned to previous area)"
                                    break

                    # 3. Patrón IDA Y VUELTA: cambios bruscos de dirección (>130°)
                    backforth_span_limit = max(self.backforth_max_span_px,
                                               self.backforth_span_scale * max(bbox_width, bbox_height))
                    if (
                        not is_sitting
                        and not is_anchored
                        and total_path_distance_with_deadzone > self.backforth_min_distance
                        and movement_span <= backforth_span_limit
                        and path_efficiency <= self.backforth_max_efficiency
                        and len(recent_trajectory) >= 5
                    ):
                        direction_reversals = 0
                        for i in range(len(recent_trajectory) - 3):
                            dx1 = recent_trajectory[i + 1].x - recent_trajectory[i].x
                            dy1 = recent_trajectory[i + 1].y - recent_trajectory[i].y
                            dx2 = recent_trajectory[i + 3].x - recent_trajectory[i + 2].x
                            dy2 = recent_trajectory[i + 3].y - recent_trajectory[i + 2].y

                            if (dx1 * dx1 + dy1 * dy1) > 20 and (dx2 * dx2 + dy2 * dy2) > 20:
                                angle1 = np.arctan2(dy1, dx1)
                                angle2 = np.arctan2(dy2, dx2)
                                angle_diff = abs(np.degrees(angle2 - angle1))
                                if angle_diff > 180:
                                    angle_diff = 360 - angle_diff
                                if angle_diff > 130:
                                    direction_reversals += 1

                        if direction_reversals >= 3:
                            suspicious_behavior_detected = True
                            behavior_reason = f"back-and-forth pattern ({direction_reversals} reversals)"

                # SOLO marcar loitering si hay patrón sospechoso; la quietud
                # prolongada por sí sola no basta (podría ser espera legítima)
                if is_sitting_static:
                    loitering_proba = 0.0
                elif suspicious_behavior_detected:
                    if is_anchored and not is_sitting:
                        # Anclado pero NO sentado + patrón detectado: confiar en el patrón
                        loitering_proba = 0.95 * sitting_multiplier
                    else:
                        loitering_proba = 0.95 * sitting_multiplier * anchor_multiplier
                else:
                    loitering_proba = 0.0

                # Suavizado temporal (promedio de últimas N predicciones)
                self.prediction_history[track_id].append(loitering_proba)
                smoothed_loitering_proba = np.mean(list(self.prediction_history[track_id]))

                desired_state = smoothed_loitering_proba >= self.loitering_activation_threshold

                # Histéresis asimétrica anti-parpadeo
                prev_track_state = self.current_state.get(track_id)
                if track_id not in self.current_state:
                    self.current_state[track_id] = desired_state
                    self.consecutive_counter[track_id] = 0
                else:
                    current_state = self.current_state[track_id]

                    if desired_state != current_state:
                        self.consecutive_counter[track_id] += 1

                        if desired_state:  # Normal -> Loitering (más estricto)
                            required_frames = self.min_predictions_to_activate
                        else:  # Loitering -> Normal (difícil salir)
                            required_frames = self.min_predictions_to_deactivate

                        if self.consecutive_counter[track_id] >= required_frames:
                            self.current_state[track_id] = desired_state
                            self.consecutive_counter[track_id] = 0
                            log.debug("Track %d: estado cambiado a %s (confirmado por %d predicciones)",
                                      track_id, "Loitering" if desired_state else "Normal", required_frames)
                    else:
                        self.consecutive_counter[track_id] = 0

                prediction = "Loitering" if self.current_state[track_id] else "Normal"
                confidence = (smoothed_loitering_proba if self.current_state[track_id]
                              else (1.0 - smoothed_loitering_proba))

                if log.isEnabledFor(logging.DEBUG):
                    log.debug(
                        "Track %d @ frame %d: %s (smooth: %.2f, raw: %.2f, dist: %.1fpx, "
                        "sitting=%s, anchored=%s, radius=%.1fpx%s)",
                        track_id, frame_id, prediction, smoothed_loitering_proba, loitering_proba,
                        total_distance, is_sitting, is_anchored, max_distance_from_center,
                        f", {behavior_reason}" if behavior_reason else "",
                    )

                result = {
                    "frame_id": frame_id,
                    "timestamp": timestamp,
                    "prediction": prediction,
                    "confidence": confidence,
                    "is_alert": False,
                }

                self.last_result[track_id] = result

                # Alerta si es merodeo con alta confianza (con cooldown anti-spam)
                if self.current_state[track_id] and smoothed_loitering_proba > self.loitering_threshold:
                    should_alert = True

                    if track_id in self.last_alert_time:
                        time_since_last = timestamp - self.last_alert_time[track_id]
                        if time_since_last < self.alert_cooldown_seconds:
                            should_alert = False

                    if should_alert:
                        self.last_alert_time[track_id] = timestamp
                        result["is_alert"] = True
                        log.info("Alerta de merodeo - track %d @ frame %d (smooth: %.2f)",
                                 track_id, frame_id, smoothed_loitering_proba)

                # ── transición on/off -> evento ──────────────────────────────
                new_track_state = self.current_state[track_id]
                changed = (new_track_state if prev_track_state is None
                           else new_track_state != prev_track_state)
                if changed:
                    self.transitions.append({
                        "on": new_track_state,
                        "track_id": track_id,
                        "score": float(smoothed_loitering_proba),
                        "ts": timestamp,
                        "bbox": [float(v) for v in bbox],
                        "extra": {
                            "raw_proba": float(loitering_proba),
                            "behavior_reason": behavior_reason,
                            "is_alert": bool(result["is_alert"]),
                            "total_distance": float(total_distance),
                            "net_displacement": float(net_displacement),
                            "path_efficiency": float(path_efficiency),
                            "max_distance_from_center": float(max_distance_from_center),
                            "is_sitting": bool(is_sitting),
                            "is_anchored": bool(is_anchored),
                            "is_stationary": bool(is_stationary),
                            "aspect_ratio": float(aspect_ratio),
                        },
                    })

                results[track_id] = result

        return results


# =============================================================================
# TRACKER (YOLO compartido del modelo padre; estado BoT-SORT por sesión)
# =============================================================================
class _SessionTracker:
    """Equivalente a YOLO.track(persist=True) del original, pero con el BOTSORT
    instanciado por sesión para no mezclar IDs entre streams."""

    def __init__(self, yolo: YOLO, yolo_conf: float, yolo_iou: float):
        self._yolo = yolo
        self.conf = yolo_conf
        self.iou = yolo_iou
        # min_det_conf: filtro post-tracker del original (default = yolo_conf)
        self.min_det_conf = _env_float("LOITERING_MIN_DET_CONF", yolo_conf)
        self._imgsz: int | None = None
        cfg = IterableSimpleNamespace(**yaml_load(check_yaml(TRACKER_YAML)))
        try:
            # frame_rate=30: mismo valor que usa Model.track internamente
            self._tracker = BOTSORT(args=cfg, frame_rate=30)
        except TypeError:
            # ultralytics >= 8.4 eliminó frame_rate del constructor
            self._tracker = BOTSORT(args=cfg)

    def process(self, frame: np.ndarray) -> list[dict]:
        if self._imgsz is None:
            # imgsz adaptativo del original: 960 para >=1080p, 640 en otro caso
            max_dim = max(frame.shape[:2])
            self._imgsz = _env_int("YOLO_IMGSZ", 960 if max_dim >= 1080 else 640)

        res = self._yolo.predict(frame, classes=[0], conf=self.conf, iou=self.iou,
                                 max_det=500, imgsz=self._imgsz, verbose=False)[0]
        if res.boxes is None or len(res.boxes) == 0:
            return []
        tracks = self._tracker.update(res.boxes.cpu().numpy(), frame)
        if len(tracks) == 0:
            return []
        # columnas: x1, y1, x2, y2, track_id, conf, cls, idx
        detections = []
        for t in tracks:
            conf = float(t[5])
            if conf < self.min_det_conf:
                continue
            detections.append({
                "track_id": int(t[4]),
                "bbox": [float(v) for v in t[:4]],
                "confidence": conf,
            })
        return detections


# =============================================================================
# SESIÓN (port del loop de run_loitering_detection, inline y sin display)
# =============================================================================
class _LoiteringSession(StreamSession):
    def __init__(self, model: "LoiteringDetector", config: dict[str, Any]):
        self.model = model
        self.detect_scale = max(0.1, min(1.0, float(config.get("detect_scale", 1.0))))
        yolo_conf = float(config.get("yolo_conf", _env_float("YOLO_CONF", 0.35)))
        yolo_iou = float(config.get("yolo_iou", _env_float("YOLO_IOU", 0.6)))

        self.tracker = _SessionTracker(model.yolo, yolo_conf, yolo_iou)
        self.id_assigner = StableIdAssigner() if _env_bool("LOITERING_STABLE_IDS", "true") else None
        self.engine = _LoiteringEngine(model.classifier, config)
        self.frame_id = 0

    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        events: list[Event] = []

        if self.detect_scale < 1.0:
            H, W = frame.shape[:2]
            frame_det = cv2.resize(frame, (int(W * self.detect_scale), int(H * self.detect_scale)))
        else:
            frame_det = frame

        detections = self.tracker.process(frame_det)
        if detections:
            if self.id_assigner is not None:
                detections = self.id_assigner.assign(detections, self.frame_id)
            # frame_det para la detección de video congelado (mismo espacio de coords)
            self.engine.update(detections, self.frame_id, ts, frame_det)

        # ── transiciones -> eventos ──────────────────────────────────────────
        scale_up = 1.0 / self.detect_scale
        for tr in self.engine.pop_transitions():
            extra: dict[str, Any] = {"track_id": tr["track_id"], **tr["extra"]}
            if tr["bbox"] is not None:
                extra["bbox"] = [int(v * scale_up) for v in tr["bbox"]]
            events.append(Event(
                type="MERODEO" if tr["on"] else "MERODEO_OFF",
                score=tr["score"], ts=ts, extra=extra,
            ))

        self.frame_id += 1
        return events


# =============================================================================
# MODELO
# =============================================================================
class LoiteringDetector(CVModel):
    info = ModelInfo(
        name="loitering-detector",
        version="2.0",  # merodeo_version_2
        task="Detección de merodeo en CCTV (YOLO track + reglas espacio-temporales por trayectoria + RF)",
        capabilities={Capability.CLIP, Capability.STREAM},
        event_types=["MERODEO", "MERODEO_OFF"],
        config_schema=CONFIG_SCHEMA,
        target_fps=15.0,  # el original consume el fps nativo de la cámara (~14 fps en sus comentarios)
    )

    def __init__(self) -> None:
        self.yolo: YOLO | None = None
        self.classifier: LoiteringClassifier | None = None

    def load(self) -> None:
        log.info("Cargando YOLO loitering: %s", WEIGHTS)
        self.yolo = YOLO(WEIGHTS)
        # RF opcional: el original lo carga pero update() nunca lo invoca
        # (la decisión es 100% reglas espacio-temporales). Si el pkl no está,
        # se sigue sin él en vez de tumbar el modelo.
        if os.path.exists(RF_PATH):
            log.info("Cargando RandomForest de merodeo: %s", RF_PATH)
            self.classifier = LoiteringClassifier(RF_PATH)
        else:
            log.warning("RF de merodeo ausente (%s); se continúa sin él (no se usa en la decisión)", RF_PATH)
            self.classifier = None

    def warmup(self) -> None:
        session = self.create_session({})
        session.process_frame(np.zeros((360, 640, 3), dtype=np.uint8), 0.0)
        session.close()

    def unload(self) -> None:
        self.yolo = None
        self.classifier = None

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        if self.yolo is None:
            self.load()
        return _LoiteringSession(self, config)
