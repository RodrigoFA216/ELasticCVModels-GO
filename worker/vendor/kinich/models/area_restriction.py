"""AreaRestriction — port de AreaRestriction_Debug.py al contrato Kinich.

Lógica de intrusión intacta respecto al original (ArconteDetection_DebugTools):
  por zona: crop del bounding box del polígono + CROP_PAD -> YOLO.track
  (ByteTrack, persist=True) -> dets mapeadas al frame con track_ids
  namespaceados por zona -> hit-test de 3 reglas (scale guard proporcional a
  la zona, consistencia de plano de suelo en Y, point-in-polygon con
  cv2.pointPolygonTest) -> máquina de estados OUTSIDE/INSIDE por
  (track_id, zone_id) con grace period anti-parpadeo para el EXIT.

Eliminado del original: editor interactivo de polígonos (RegionSelector +
GUI cv2), guardado/carga de zonas a disco (ahora config de sesión),
dashboard cv2, CLI, prompt interactivo de escena y prints (-> logging).
Los eventos ENTER/EXIT del script se publican como INTRUSION/INTRUSION_OFF.

Nota de pesos: el original carga UNA instancia YOLO por zona porque
ultralytics acopla el estado de ByteTrack a la instancia del modelo (es la
única forma de tener un tracker aislado por zona). Se conserva: cada sesión
crea sus instancias por zona en el primer frame; la instancia del modelo
padre solo valida/pre-descarga los pesos en load().
"""
import logging
import os
from collections import defaultdict
from typing import Any

import cv2
import numpy as np
from ultralytics import YOLO

from kinich.contract import Capability, CVModel, Detection, Event, ModelInfo, StreamSession

log = logging.getLogger(__name__)

WEIGHTS = os.environ.get("KINICH_AREAREST_WEIGHTS", "yolo11x.pt")

# =============================================================================
# CONFIG (constantes del original, sin cambios de valor)
# =============================================================================
TRACKER_YAML = "bytetrack.yaml"
DETECT_CONF = 0.10
DETECT_CLASSES = [0]     # 0 = persona (COCO)

# ── Punto de referencia del bbox para el hit-test ────────────────────────────
# "bottom_center" | "centroid" | "top_center"
HIT_POINT = "bottom_center"

# ── Perfiles de escena (el original preguntaba por CLI; default outdoor) ─────
#   indoor  -> cámara cercana, personas grandes; ratio estricto
#   outdoor -> cámara alta/lejana, personas pequeñas; ratio permisivo
SCENE_PROFILES: dict[str, dict] = {
    "indoor":  dict(min_bbox_zone_ratio=0.30, zone_y_tolerance=20),
    "outdoor": dict(min_bbox_zone_ratio=0.05, zone_y_tolerance=50),
}
DEFAULT_SCENE = "outdoor"

# ── Grace period (anti-parpadeo de track IDs) ────────────────────────────────
# Frames que espera _ZoneTracker antes de disparar EXIT cuando una persona
# deja de ser detectada; cubre oclusiones breves sin falsos EXIT/ENTER.
EXIT_GRACE_FRAMES = 20   # ~0.7 s a 30 fps (a target_fps=10 son ~2 s)

# ── Crop para inferencia ─────────────────────────────────────────────────────
# Padding alrededor del bounding box de la zona antes de recortar el frame.
CROP_PAD = 40            # px

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "zones": {
            "type": "array",
            "items": {
                "type": "array", "minItems": 3,
                "items": {"type": "array", "items": {"type": "number"},
                          "minItems": 2, "maxItems": 2},
            },
            "description": "lista de polígonos (zonas restringidas); cada zona es una lista de puntos [x, y] en PÍXELES del frame original (el script usaba 4 puntos; se acepta >= 3). Sin zonas ni polygon, la zona es el frame completo",
        },
        "polygon": {
            "type": "array", "minItems": 3,
            "items": {"type": "array", "items": {"type": "number"},
                      "minItems": 2, "maxItems": 2},
            "description": "atajo para una sola zona: lista de [x, y] en píxeles del frame original (equivale a zones=[polygon])",
        },
        "scene": {
            "type": "string", "enum": ["indoor", "outdoor"], "default": "outdoor",
            "description": "perfil de escena: indoor fija min_bbox_zone_ratio=0.30/y_tol=20px; outdoor 0.05/50px",
        },
        "min_bbox_zone_ratio": {"type": "number", "description": "alto mínimo del bbox como fracción del alto de la zona (override del perfil)"},
        "zone_y_tolerance": {"type": "integer", "description": "tolerancia en px del guard de plano de suelo (override del perfil)"},
        "conf": {"type": "number", "default": 0.10, "description": "confianza mínima post-tracking para aceptar una detección"},
        "hit_point": {"type": "string", "enum": ["bottom_center", "centroid", "top_center"],
                      "default": "bottom_center", "description": "punto de referencia del bbox para el hit-test"},
        "exit_grace_frames": {"type": "integer", "default": 20, "minimum": 0,
                              "description": "frames de gracia antes de emitir INTRUSION_OFF al perder el track"},
        "crop_pad": {"type": "integer", "default": 40, "minimum": 0,
                     "description": "padding (px) alrededor del bbox de la zona para el crop de inferencia"},
    },
}


# =============================================================================
# HIT-TEST (idéntico al original)
# =============================================================================
def ref_point(bbox: tuple[int, int, int, int], hit_point: str) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2.0
    if hit_point == "bottom_center":
        h = y2 - y1
        return (cx, y2 - 0.20 * h)   # pies subidos 20% de la altura del bbox
    if hit_point == "top_center":
        return (cx, float(y1))
    return (cx, (y1 + y2) / 2.0)     # centroid


def detection_in_zone(bbox: tuple[int, int, int, int], zone: list[list[int]],
                      min_bbox_zone_ratio: float, zone_y_tolerance: float,
                      hit_point: str) -> bool:
    """Tres reglas en orden; las tres deben cumplirse para INSIDE = True.

    Regla 1 — Scale guard (proxy de profundidad): el alto del bbox debe superar
      una fracción del alto de la zona; bbox pequeño = persona lejos.
    Regla 2 — Y-floor consistency (plano de suelo): si los pies proyectados
      quedan por encima del borde superior del polígono, el objeto está
      "detrás" de la zona en 3D (falso positivo por perspectiva).
    Regla 3 — Point-in-polygon: hit-test 2D clásico con cv2.pointPolygonTest.
    """
    x1, y1, x2, y2 = bbox
    h_bbox = y2 - y1
    pt = ref_point(bbox, hit_point)
    zone_ys = [p[1] for p in zone]
    zone_min_y = min(zone_ys)
    zone_max_y = max(zone_ys)
    zone_h = max(zone_max_y - zone_min_y, 1)
    min_h = zone_h * min_bbox_zone_ratio

    # Regla 1: scale guard proporcional a la zona
    if h_bbox < min_h:
        log.debug("zone RECHAZADO R1 bbox_h=%.0f < min=%.0f (zone_h=%s) bbox=%s",
                  h_bbox, min_h, zone_h, bbox)
        return False

    # Regla 2: Y-floor consistency
    if pt[1] < zone_min_y - zone_y_tolerance:
        log.debug("zone RECHAZADO R2 ref_y=%.0f < zone_min_y-tol=%.0f bbox=%s",
                  pt[1], zone_min_y - zone_y_tolerance, bbox)
        return False

    # Regla 3: point-in-polygon
    poly = np.array(zone, dtype=np.int32)
    result = cv2.pointPolygonTest(poly, pt, False) >= 0
    log.debug("zone R3=%s ref_pt=(%.0f,%.0f) bbox=%s",
              "DENTRO" if result else "FUERA", pt[0], pt[1], bbox)
    return result


# =============================================================================
# ZONE STATE TRACKER (idéntico al original; retorna eventos en vez de imprimir)
# =============================================================================
class _ZoneTracker:
    """Máquina de estados OUTSIDE / INSIDE por (track_id, zone_id)
    con grace period para EXIT.

    Flujo EXIT con grace:
      INSIDE -> persona desaparece -> countdown de gracia comienza
        · si reaparece INSIDE antes de llegar a 0 -> sigue INSIDE, sin EXIT
        · si el contador llega a 0 sin reaparecer -> EXIT
    """

    def __init__(self, exit_grace_frames: int):
        self._exit_grace = exit_grace_frames
        self._inside: dict[int, dict[int, bool]] = defaultdict(lambda: defaultdict(bool))
        self._grace: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))

    def update(self, track_id: int, zone_id: int, inside: bool) -> str | None:
        was_inside = self._inside[track_id][zone_id]
        grace = self._grace[track_id][zone_id]

        if inside:
            # resetear grace y marcar inside
            self._grace[track_id][zone_id] = 0
            self._inside[track_id][zone_id] = True
            if not was_inside and grace == 0:
                # transición real OUTSIDE -> INSIDE
                return "ENTER"
        else:
            if was_inside:
                if grace == 0:
                    # primera vez que no se ve: iniciar grace
                    self._grace[track_id][zone_id] = self._exit_grace
                else:
                    self._grace[track_id][zone_id] -= 1
                    if self._grace[track_id][zone_id] <= 0:
                        # grace agotado -> EXIT real
                        self._inside[track_id][zone_id] = False
                        self._grace[track_id][zone_id] = 0
                        return "EXIT"
            # si ya era OUTSIDE, no hay nada que hacer
        return None

    def tick_missing(self, track_id: int, n_zones: int) -> list[tuple[int, int]]:
        """Llamar cuando track_id NO aparece en el frame actual.
        Avanza el grace counter de todas sus zonas; retorna (tid, zid) de los EXIT."""
        exits: list[tuple[int, int]] = []
        for zone_id in range(n_zones):
            if self._inside[track_id][zone_id]:
                g = self._grace[track_id][zone_id]
                if g == 0:
                    self._grace[track_id][zone_id] = self._exit_grace
                else:
                    self._grace[track_id][zone_id] -= 1
                    if self._grace[track_id][zone_id] <= 0:
                        self._inside[track_id][zone_id] = False
                        self._grace[track_id][zone_id] = 0
                        exits.append((track_id, zone_id))
        return exits

    def purge(self, active_ids: list[int], n_zones: int) -> tuple[list[tuple[int, int]], list[int]]:
        """Para IDs que ya no están en el tracker: avanzar grace y limpiar los
        que agotaron su gracia. Retorna (EXITs emitidos, tids eliminados)."""
        exits: list[tuple[int, int]] = []
        removed: list[int] = []
        missing = [tid for tid in list(self._inside) if tid not in active_ids]
        for tid in missing:
            exits.extend(self.tick_missing(tid, n_zones))
            # limpiar si ya no tiene ninguna zona activa ni grace pendiente
            if not any(self._inside[tid][z] or self._grace[tid][z] > 0
                       for z in range(n_zones)):
                del self._inside[tid]
                del self._grace[tid]
                removed.append(tid)
        return exits, removed


# =============================================================================
# INFERENCIA POR ZONA (idéntica al original)
# =============================================================================
def _crop_zone(frame: np.ndarray, region: list[list[int]],
               pad: int) -> tuple[np.ndarray, int, int]:
    """Recorta el área de UNA zona + pad. Retorna (crop, offset_x, offset_y).
    Si el crop es inválido retorna el frame completo con offset (0, 0)."""
    h, w = frame.shape[:2]
    pts = np.array(region, dtype=np.int32)
    x1 = max(0, int(pts[:, 0].min()) - pad)
    y1 = max(0, int(pts[:, 1].min()) - pad)
    x2 = min(w - 1, int(pts[:, 0].max()) + pad)
    y2 = min(h - 1, int(pts[:, 1].max()) + pad)
    if x2 <= x1 or y2 <= y1:
        return frame, 0, 0
    return frame[y1:y2, x1:x2].copy(), x1, y1


def _parse_results(res, ox: int, oy: int, zone_idx: int, conf_min: float) -> list[dict]:
    """Extrae detecciones de un resultado YOLO, mapea al frame original y
    namespacea los track_ids con el índice de zona para evitar colisiones."""
    if res.boxes is None or len(res.boxes) == 0:
        return []

    b = res.boxes
    all_xyxy = b.xyxy.cpu().numpy().astype(np.float32)
    all_conf = b.conf.cpu().numpy().astype(np.float32)
    all_cls = b.cls.cpu().numpy().astype(np.int32)
    all_ids = (b.id.cpu().numpy().astype(np.int32)
               if b.id is not None
               else np.full(len(all_xyxy), -1, dtype=np.int32))

    dets = []
    for xyxy, conf, cls, tid in zip(all_xyxy, all_conf, all_cls, all_ids):
        if conf < conf_min:
            continue
        x1, y1, x2, y2 = map(int, xyxy)
        # namespace: zona 0 -> IDs 0-9999, zona 1 -> IDs 10000-19999, ...
        ns_tid = int(tid) + zone_idx * 10_000 if tid >= 0 else -1
        dets.append(dict(
            bbox=(x1 + ox, y1 + oy, x2 + ox, y2 + oy),
            conf=float(conf),
            class_id=int(cls),
            track_id=ns_tid,
            zone_src=zone_idx,   # zona que generó esta detección
        ))
    return dets


# =============================================================================
# SESIÓN (port del loop de process(), sin GUI ni disco)
# =============================================================================
class _AreaSession(StreamSession):
    def __init__(self, model: "AreaRestrictionDetector", config: dict[str, Any]):
        self.model = model

        # ── zonas: config de sesión en vez de GUI/JSON en disco ─────────────
        zones = config.get("zones")
        if zones is None and config.get("polygon") is not None:
            zones = [config["polygon"]]
        self.zones: list[list[list[int]]] | None = None
        if zones is not None:
            for zi, z in enumerate(zones):
                if len(z) < 3:
                    raise ValueError(f"Zona {zi}: necesita >= 3 puntos, tiene {len(z)}")
            self.zones = [[[int(p[0]), int(p[1])] for p in z] for z in zones]
        # sin zonas -> frame completo; se resuelve con el primer frame

        scene = str(config.get("scene", DEFAULT_SCENE))
        profile = SCENE_PROFILES.get(scene, SCENE_PROFILES[DEFAULT_SCENE])
        self.min_bbox_zone_ratio = float(config.get("min_bbox_zone_ratio",
                                                    profile["min_bbox_zone_ratio"]))
        self.zone_y_tolerance = float(config.get("zone_y_tolerance",
                                                 profile["zone_y_tolerance"]))
        self.conf = float(config.get("conf", DETECT_CONF))
        self.hit_point = str(config.get("hit_point", HIT_POINT))
        self.crop_pad = max(0, int(config.get("crop_pad", CROP_PAD)))

        self.tracker = _ZoneTracker(max(0, int(config.get("exit_grace_frames",
                                                          EXIT_GRACE_FRAMES))))
        # una instancia YOLO por zona = un ByteTrack aislado por zona (como el
        # original); se crean con el primer frame, cuando las zonas ya existen
        self.zone_models: list[YOLO] | None = None
        # último bbox/conf visto por track (para el extra de INTRUSION_OFF)
        self._last_det: dict[int, tuple[tuple[int, int, int, int], float]] = {}

    def _ensure_ready(self, frame: np.ndarray) -> None:
        if self.zones is None:
            h, w = frame.shape[:2]
            self.zones = [[[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]]]
            log.info("Sin zonas en config: usando el frame completo (%dx%d)", w, h)
        if self.zone_models is None:
            self.zone_models = []
            for zi in range(len(self.zones)):
                self.zone_models.append(YOLO(self.model.weights_path))
                log.info("zona %d: %s | tracker: %s", zi + 1, self.model.weights_path, TRACKER_YAML)

    def _run_model(self, frame: np.ndarray) -> list[dict]:
        """Corre un YOLO independiente por zona sobre el crop de esa zona.
        IDs namespaceados (zona_idx * 10000 + id_local) -> sin colisiones."""
        all_dets: list[dict] = []
        for zi, (zmodel, region) in enumerate(zip(self.zone_models, self.zones)):
            crop, ox, oy = _crop_zone(frame, region, self.crop_pad)
            res = zmodel.track(
                crop,
                classes=DETECT_CLASSES,
                persist=True,
                verbose=False,
                tracker=TRACKER_YAML,
            )[0]
            all_dets.extend(_parse_results(res, ox, oy, zi, self.conf))
        return all_dets

    def _make_event(self, etype: str, ts: float, tid: int, zid: int,
                    bbox: tuple[int, int, int, int] | None, conf: float) -> Event:
        det = [Detection(label="person", score=conf, bbox=bbox)] if bbox else []
        extra: dict[str, Any] = {"track_id": tid, "zone_id": zid}
        if bbox:
            extra["bbox"] = list(bbox)
        return Event(type=etype, score=conf, ts=ts, detections=det, extra=extra)

    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        self._ensure_ready(frame)
        events: list[Event] = []

        dets = self._run_model(frame)
        active_ids: list[int] = []

        for d in dets:
            tid = d["track_id"]
            if tid < 0:
                continue
            active_ids.append(tid)
            self._last_det[tid] = (d["bbox"], d["conf"])
            for zi, zone in enumerate(self.zones):
                inside = detection_in_zone(d["bbox"], zone, self.min_bbox_zone_ratio,
                                           self.zone_y_tolerance, self.hit_point)
                ev = self.tracker.update(tid, zi, inside)
                if ev == "ENTER":
                    events.append(self._make_event("INTRUSION", ts, tid, zi,
                                                   d["bbox"], d["conf"]))
                elif ev == "EXIT":
                    events.append(self._make_event("INTRUSION_OFF", ts, tid, zi,
                                                   d["bbox"], d["conf"]))

        exits, removed = self.tracker.purge(active_ids, len(self.zones))
        for tid, zid in exits:
            bbox, conf = self._last_det.get(tid, (None, 0.0))
            events.append(self._make_event("INTRUSION_OFF", ts, tid, zid, bbox, conf))
        for tid in removed:
            self._last_det.pop(tid, None)

        return events

    def close(self) -> None:
        # libera las instancias por zona (estado de ByteTrack incluido);
        # los pesos del modelo padre no se tocan
        self.zone_models = None
        self._last_det.clear()


# =============================================================================
# MODELO
# =============================================================================
class AreaRestrictionDetector(CVModel):
    info = ModelInfo(
        name="area-restriction",
        version="1.0",
        task="Intrusión en zonas restringidas (YOLO + ByteTrack por zona + hit-test 3 reglas)",
        capabilities={Capability.CLIP, Capability.STREAM},
        event_types=["INTRUSION", "INTRUSION_OFF"],
        config_schema=CONFIG_SCHEMA,
        target_fps=10.0,  # el tracking necesita continuidad temporal
    )

    def __init__(self) -> None:
        self.weights_path = WEIGHTS
        self.yolo: YOLO | None = None

    def load(self) -> None:
        # valida y pre-descarga los pesos; las sesiones crean sus propias
        # instancias por zona (ultralytics acopla ByteTrack al objeto YOLO)
        log.info("Cargando YOLO area-restriction: %s", self.weights_path)
        self.yolo = YOLO(self.weights_path)

    def warmup(self) -> None:
        session = self.create_session({})
        session.process_frame(np.zeros((360, 640, 3), dtype=np.uint8), 0.0)
        session.close()

    def unload(self) -> None:
        self.yolo = None

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        if self.yolo is None:
            self.load()
        return _AreaSession(self, config)
