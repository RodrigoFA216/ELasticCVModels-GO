# -*- coding: utf-8 -*-
"""ppe_detector.py — modelo `ppe-detector` (PPE Detector) v1.1 para Kinich.

Deteccion de EPP (casco/chaleco/botas/guantes x color) con SAM 3.1 INT4.

Version 1.1 (2026-08-31): + preferencia por la mascara que cumple el color, regla de
pertenencia (>=60% de la mascara dentro de la caja propia), banda naranja realista
H [3,20) y zoom de deteccion por zona (config `zone_zoom`). Antes v1.0. Adaptador AUTOCONTENIDO (un solo archivo) del detector
PPE_SAM3 de PPE_Detection/platform (congelado alli como V5) al contrato `kinich.contract.CVModel`. El modelo nunca ve HTTP ni Redis: frames numpy ->
eventos Pydantic, como exige el contrato.

Pipeline por sesion (stream):
  1. YOLO11s (person) + BYTETracker -> cajas con ID (cada frame que Kinich
     entrega a target_fps).
  2. TransReID -> identidad anonima persistente P<n> (opcional, `reid: true`):
     votos y estado sobreviven a oclusiones y re-entradas.
  3. SAM 3.1 INT4 sobre el RECORTE de la persona (+20 % margen): un pase del
     ViT + un decodificador por EPP exigido -> mascaras. Solo cuando la persona
     lo necesita (nueva / >refresh_s sin revisar / cambio de tamaño), maximo
     una persona por frame (acota la latencia por frame a ~50+25*EPP ms).
  4. Verificador: la mascara debe caer en la region del cuerpo del EPP; el
     COLOR se decide por pixeles HSV de la mascara (>= color_min_frac del tono
     entre los pixeles con color).
  5. Veredicto por persona = mayoria de sus evaluaciones en `vote_window_s`.

Eventos POR FLANCO (nada de un evento por frame):
  EPP_FALTA      alerta: una persona sostiene faltas durante `alert_after_s`
                 (detections = caja de la persona + cajas de su EPP visto;
                  extra = {persona, faltas, votos, similitud_reid})
  EPP_FALTA_OFF  la persona recupero el EPP o salio de escena
  CUMPLIMIENTO   info periodica (`stats_every_s`): {en_zona, con_epp, sin_epp,
                 pct_sin_epp, tolerancia, excede}
  TOLERANCIA     alerta: el % sin EPP supera `tol_pct` sostenido; y _OFF al bajar

Capabilities: IMAGE (foto -> detecciones por persona), STREAM, CLIP (default).

Pesos y rutas (env):
  KINICH_PPE_COMFY      raiz de ComfyUI (codigo + models/checkpoints con el ckpt SAM3)
  KINICH_PPE_SAM3_CKPT  nombre del checkpoint (default sam3.1_multiplex_w4a4_..._fp32source.safetensors)
  KINICH_PPE_YOLO       weights/yolo11s.pt
  KINICH_PPE_REID       weights/transreid_msmt.onnx  (vacio o ausente = sin identidad persistente)

Limitaciones (ver README): distancia manda en botas/guantes; umbrales por escena;
identidad anonima con EPP uniforme; el rostro/nominal NO se porto a Kinich.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import deque
from typing import Any

import cv2
import numpy as np

from kinich.contract import Capability, CVModel, Detection, Event, ModelInfo, StreamSession

log = logging.getLogger(__name__)

_AQUI = os.path.dirname(os.path.abspath(__file__))
COMFY_DIR = os.environ.get("KINICH_PPE_COMFY", os.path.join(os.path.dirname(_AQUI), "ComfyUI"))
SAM3_CKPT = os.environ.get("KINICH_PPE_SAM3_CKPT",
                           "sam3.1_multiplex_w4a4_convrot_selective_fp16clip_fp32source.safetensors")
YOLO_WEIGHTS = os.environ.get("KINICH_PPE_YOLO", "weights/yolo11s.pt")
REID_WEIGHTS = os.environ.get("KINICH_PPE_REID", "weights/transreid_msmt.onnx")

# --------------------------------------------------------------------------- #
#  Catalogo de EPP (identico a platform/ppe_detector.py V5)
# --------------------------------------------------------------------------- #
CATALOGO = {
    "casco":   dict(nombre="CASCO",   prompt="hard hat",     region=(-0.05, 0.35), borde="arriba"),
    "chaleco": dict(nombre="CHALECO", prompt="safety vest",  region=(0.15, 0.80),  borde=None),
    "botas":   dict(nombre="BOTAS",   prompt="safety boots", region=(0.75, 1.05),  borde="abajo"),
    "guantes": dict(nombre="GUANTES", prompt="gloves",       region=(0.35, 0.90),  borde=None),
}
COLORES = {
    "cualquiera": None,
    "amarillo": [(18, 50)], "naranja": [(3, 20)], "rojo": [(0, 5), (170, 180)],
    "verde": [(50, 90)], "azul": [(90, 135)], "blanco": "blanco", "negro": "negro",
}

_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "require": {"type": "boolean"},
        "color": {"type": "string", "enum": list(COLORES)},
        "conf": {"type": "number", "minimum": 0.05, "maximum": 0.95},
    },
}
CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        **{k: {**_ITEM_SCHEMA, "description": f"exigencia de {k}"} for k in CATALOGO},
        "conf_person": {"type": "number", "default": 0.25, "description": "confianza YOLO persona"},
        "zonas": {"type": "array", "items": {"type": "array", "items": {"type": "array"}},
                  "description": "poligonos [[x,y],...] normalizados 0-1 o en pixeles; vacio = todo el frame"},
        "zone_zoom": {"type": "boolean", "default": False,
                      "description": "YOLO adicional sobre el recorte de cada zona (max 3): detecta personas pequeñas en zonas lejanas"},
        "reid": {"type": "boolean", "default": True, "description": "identidad anonima persistente P<n>"},
        "reid_match_th": {"type": "number", "default": 0.65},
        "vote_window_s": {"type": "number", "default": 60},
        "refresh_s": {"type": "number", "default": 5, "description": "re-evaluar cada N s por persona"},
        "alert_after_s": {"type": "number", "default": 3},
        "tol_pct": {"type": "number", "default": 20, "description": "% sin EPP tolerado (evento TOLERANCIA)"},
        "min_personas_pct": {"type": "integer", "default": 2},
        "stats_every_s": {"type": "number", "default": 10},
        "color_min_frac": {"type": "number", "default": 70},
        "min_colored_frac": {"type": "number", "default": 25},
        "sat_min": {"type": "integer", "default": 35},
        "val_min": {"type": "integer", "default": 60},
    },
}
_DEFAULT_ITEMS = {"casco": {"require": False, "color": "cualquiera", "conf": 0.45},
                  "chaleco": {"require": True, "color": "amarillo", "conf": 0.50},
                  "botas": {"require": False, "color": "cualquiera", "conf": 0.40},
                  "guantes": {"require": False, "color": "cualquiera", "conf": 0.40}}


def _centro(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _etiqueta(faltan):
    sin = [f for f in faltan if " NO " not in f]
    otros = [f for f in faltan if " NO " in f]
    return " / ".join((["SIN " + "+".join(sin)] if sin else []) + otros)



# --------------------------------------------------------------------------- #
#  Identidad anonima persistente (TransReID) — version minima embebida.
#  Motivo: ByteTrack pierde a la gente (medido: vida mediana de un ID 2.7 s,
#  ~9 IDs por persona/min en oclusiones); sin re-identificacion, los votos de
#  EPP se reinician y las alertas se repiten. Extraido de la plataforma
#  PPE_Detection (identity.py) SIN las capas de rostro/nominal, que este
#  adaptador no usa. Modo 'zona_crudo' validado en Queue-Cashier: coseno crudo
#  sobre el vector BNNeck 768-d; umbral 0.65 (impostores p99 = 0.64).
# --------------------------------------------------------------------------- #
_REID_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_REID_STD = np.array([0.229, 0.224, 0.225], np.float32)
_GALLERY_SIZE = 5


class _ReIDEngine:
    """TransReID ONNX compartido por proceso (una sesion CUDA, ~5 ms/embedding)."""
    _shared = None
    _shared_lock = threading.Lock()

    @classmethod
    def get(cls, weights: str) -> "_ReIDEngine":
        with cls._shared_lock:
            if cls._shared is None:
                cls._shared = cls(weights)
            return cls._shared

    def __init__(self, weights: str):
        self.ok = False
        self._lock = threading.Lock()
        if not os.path.exists(weights):
            log.warning("PPE Detector: sin TransReID (%s)", weights)
            return
        import torch  # noqa: F401, PLC0415  (carga las libs CUDA que onnxruntime usa)
        import onnxruntime as ort  # noqa: PLC0415
        self.sess = ort.InferenceSession(weights, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        self.ok = "CUDAExecutionProvider" in self.sess.get_providers()
        log.info("PPE Detector: TransReID listo | %s", self.sess.get_providers())

    def embed(self, crop_bgr: np.ndarray):
        """crop BGR -> vector 768-d L2 (o None si es ilegible/chico)."""
        if not self.ok or crop_bgr is None or crop_bgr.size == 0:
            return None
        h, w = crop_bgr.shape[:2]
        if w < 24 or h < 48:
            return None
        if w < 64 or h < 128:
            crop_bgr = cv2.resize(crop_bgr, (64, 128), interpolation=cv2.INTER_CUBIC)
        rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (128, 256), interpolation=cv2.INTER_LINEAR)
        x = ((rgb.astype(np.float32) / 255.0 - _REID_MEAN) / _REID_STD).transpose(2, 0, 1)[None]
        with self._lock:
            e = self.sess.run(None, {"image": x})[0][0].astype(np.float64)
        v = e[:768]
        n = np.linalg.norm(v)
        return v / n if np.isfinite(n) and n > 1e-9 else None

    @staticmethod
    def score(buffer: list, gallery: list) -> float:
        if not buffer or not gallery:
            return -1.0
        S = np.stack(buffer) @ np.stack(gallery).T
        S = S[np.isfinite(S)]
        return float(S.mean()) if S.size else -1.0

    @staticmethod
    def gallery_add(gallery: list, e) -> None:
        """<= _GALLERY_SIZE vistas maximizando diversidad."""
        if len(gallery) < _GALLERY_SIZE:
            gallery.append(e)
            return
        gallery[int(np.argmax(np.stack(gallery) @ e))] = e


class _Persona:
    __slots__ = ("pid", "gallery", "votes", "faltan", "bad_since", "last_eval",
                 "first_seen", "last_seen", "n_tracks")

    def __init__(self, pid: int, now: float):
        self.pid = pid
        self.gallery: list = []
        self.votes: deque = deque(maxlen=64)   # (ts, frozenset de faltas)
        self.faltan = None
        self.bad_since = None
        self.last_eval = None
        self.first_seen = self.last_seen = now
        self.n_tracks = 1

    @property
    def nombre(self) -> str:
        return f"P{self.pid}"


class _Registro:
    """Asocia tracks de ByteTrack a personas P<n>. Un track nuevo acumula 3
    vistas y se compara contra las personas SIN track vivo; similitud media
    >= match_th -> se re-liga (conserva votos/estado), si no nace una nueva."""

    def __init__(self, engine: _ReIDEngine, match_th: float = 0.65, olvidar_s: float = 600.0):
        self.reid = engine
        self.match_th = match_th
        self.olvidar_s = olvidar_s
        self.personas: dict[int, _Persona] = {}
        self.track2pid: dict[int, int] = {}
        self.track_buf: dict[int, deque] = {}
        self.track_last_emb: dict[int, float] = {}
        self._next = 1

    def actualizar(self, frame: np.ndarray, tracks: list, now: float,
                   emb_every_s: float = 1.0) -> dict[int, _Persona]:
        H, W = frame.shape[:2]
        vivos = {tid for tid, _ in tracks}
        ocupadas = {self.track2pid[t] for t in vivos if t in self.track2pid}
        out: dict[int, _Persona] = {}
        for tid, box in tracks:
            pid = self.track2pid.get(tid)
            emb = None
            if self.reid.ok and now - self.track_last_emb.get(tid, -1e9) >= (emb_every_s if pid else 0.25):
                x1, y1, x2, y2 = [int(v) for v in box]
                x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
                if x2 > x1 and y2 > y1:
                    emb = self.reid.embed(frame[y1:y2, x1:x2])
                    self.track_last_emb[tid] = now
            if pid is None:
                buf = self.track_buf.setdefault(tid, deque(maxlen=6))
                if emb is not None:
                    buf.append(emb)
                if len(buf) >= 3 or not self.reid.ok:
                    pid = self._ligar(tid, list(buf), ocupadas, now)
                    ocupadas.add(pid)
                    self.track_buf.pop(tid, None)
            if pid is not None:
                p = self.personas[pid]
                p.last_seen = now
                if emb is not None:
                    _ReIDEngine.gallery_add(p.gallery, emb)
                out[tid] = p
        for tid in [t for t in self.track2pid if t not in vivos]:
            del self.track2pid[tid]
        for tid in [t for t in self.track_buf if t not in vivos]:
            self.track_buf.pop(tid, None)
            self.track_last_emb.pop(tid, None)
        for pid in [k for k, p in self.personas.items() if now - p.last_seen > self.olvidar_s]:
            del self.personas[pid]
        return out

    def _ligar(self, tid: int, buf: list, ocupadas: set, now: float) -> int:
        best_pid, best = None, -1.0
        for pid, p in self.personas.items():
            if pid in ocupadas or not p.gallery:
                continue
            sc = _ReIDEngine.score(buf, p.gallery)
            if sc > best:
                best_pid, best = pid, sc
        if best_pid is not None and best >= self.match_th:
            p = self.personas[best_pid]
            p.n_tracks += 1
        else:
            p = _Persona(self._next, now)
            self._next += 1
            self.personas[p.pid] = p
        for e in buf:
            _ReIDEngine.gallery_add(p.gallery, e)
        self.track2pid[tid] = p.pid
        return p.pid


# --------------------------------------------------------------------------- #
#  Motor SAM3 (ComfyUI in-process, un pase del ViT + un decoder por prompt)
# --------------------------------------------------------------------------- #
class _Sam3Engine:
    def __init__(self, comfy_dir: str, ckpt: str, prompts: dict[str, str]):
        if comfy_dir not in sys.path:
            sys.path.insert(0, comfy_dir)
        import nodes  # noqa: PLC0415
        import comfy.model_management as mm  # noqa: PLC0415
        import torch  # noqa: PLC0415
        self.torch, self.mm = torch, mm
        model, clip, _ = nodes.CheckpointLoaderSimple().load_checkpoint(ckpt)
        self.conds = {}
        for k, text in prompts.items():
            c = nodes.CLIPTextEncode().encode(clip, text)[0]
            self.conds[k] = (c[0][0], c[0][1].get("attention_mask"))
        mm.unload_all_models()
        self.model = model
        self._lock = threading.Lock()
        log.info("PPE Detector: SAM3 %s cargado (%d prompts)", ckpt, len(prompts))

    def detect(self, crop_bgr: np.ndarray, items: list[str], confs: dict[str, float],
               max_det: int = 5) -> dict[str, list]:
        """-> {item: [(box_xyxy_crop, score, mask_bool_HxW)]}. Un pase del ViT."""
        torch = self.torch
        import comfy.utils  # noqa: PLC0415
        H, W = crop_bgr.shape[:2]
        rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        t = torch.from_numpy(rgb.astype(np.float32) / 255.0)[None]
        img = comfy.utils.common_upscale(t.movedim(-1, 1), 1008, 1008, "bilinear", crop="disabled")
        out: dict[str, list] = {}
        with self._lock:
            self.mm.load_model_gpu(self.model)
            device = self.mm.get_torch_device()
            dtype = self.model.model.get_dtype()
            det = self.model.model.diffusion_model.detector
            with torch.no_grad():
                frame = img.to(device=device, dtype=dtype)
                feats, pos, _, _ = det._get_backbone_features(frame)
                resizer = det.backbone["language_backbone"]["resizer"]
                scale = torch.tensor([W, H, W, H], device=device)
                for k in items:
                    cond, amask = self.conds[k]
                    emb = cond.to(device=device, dtype=dtype)
                    mask = (amask.to(device) if amask is not None
                            else torch.ones(emb.shape[0], emb.shape[1], dtype=torch.int64, device=device))
                    boxes, scores, masks, _ = det._detect(feats, pos, resizer(emb), mask.bool(), None, None)
                    probs = scores[0].sigmoid()
                    keep = probs > confs[k]
                    dets = []
                    if keep.any():
                        bx = (boxes[0][keep] * scale.to(boxes.dtype)).float().cpu()
                        sc = probs[keep].float().cpu()
                        orden = sc.argsort(descending=True)[:max_det]
                        m = masks[0][keep][orden.to(masks.device)].unsqueeze(1).float()
                        m = torch.nn.functional.interpolate(m, size=(H, W), mode="bilinear", align_corners=False)
                        m = (m[:, 0] > 0).cpu().numpy()
                        for j, i in enumerate(orden):
                            dets.append((bx[i].tolist(), float(sc[i]), m[j]))
                    out[k] = dets
        return out


# --------------------------------------------------------------------------- #
#  Sesion por stream
# --------------------------------------------------------------------------- #
class _PPESession(StreamSession):
    def __init__(self, model: "PPEDetectorModel", config: dict[str, Any]):
        self.m = model
        cfg = dict(config or {})
        self.items_cfg = {}
        for k in CATALOGO:
            d = dict(_DEFAULT_ITEMS[k]); d.update(cfg.get(k) or {})
            self.items_cfg[k] = d
        self.items = [k for k, d in self.items_cfg.items() if d["require"]]
        g = lambda k, d: cfg.get(k, d)  # noqa: E731
        self.conf_person = float(g("conf_person", 0.25))
        self.refresh_s = float(g("refresh_s", 5.0))
        self.vote_window_s = float(g("vote_window_s", 60.0))
        self.alert_after_s = float(g("alert_after_s", 3.0))
        self.tol_pct = float(g("tol_pct", 20.0))
        self.min_personas = int(g("min_personas_pct", 2))
        self.stats_every_s = float(g("stats_every_s", 10.0))
        self.color_min_frac = float(g("color_min_frac", 70.0))
        self.min_colored_frac = float(g("min_colored_frac", 25.0))
        self.sat_min = int(g("sat_min", 35))
        self.val_min = int(g("val_min", 60))
        self.zonas_cfg = g("zonas", []) or []
        self.zone_zoom = bool(g("zone_zoom", False))
        self._contornos: list | None = None      # se materializa con el primer frame
        self.reid_on = bool(g("reid", True)) and self.m.registro_factory is not None
        self._registro = self.m.registro_factory(float(g("reid_match_th", 0.65))) if self.reid_on else None
        self._tracker = self.m.new_tracker()
        self._tracks: dict[int, dict] = {}       # tid -> estado
        self._last_stats = 0.0
        self._tol_excedida_desde: float | None = None
        self._tol_alertada = False
        self._alertadas: dict[Any, dict] = {}    # sujeto_id -> {faltas, persona}

    # -- zonas ------------------------------------------------------------
    def _zonas(self, frame) -> list:
        if self._contornos is None:
            H, W = frame.shape[:2]
            cs = []
            for poly in self.zonas_cfg:
                pts = np.array(poly, dtype=np.float64)
                if pts.size and pts.max() <= 1.5:      # normalizado 0-1
                    pts[:, 0] *= W
                    pts[:, 1] *= H
                cs.append(pts.astype(np.int32).reshape(-1, 1, 2))
            self._contornos = cs
        return self._contornos

    def _zone_rects(self, frame):
        """Rectangulos (x1,y1,x2,y2) de las zonas +15% margen, para el zoom V5."""
        H, W = frame.shape[:2]
        rects = []
        for c in (self._zonas(frame) or [])[:3]:
            x, y, w, h = cv2.boundingRect(c)
            mx, my = int(0.15 * w), int(0.15 * h)
            x1, y1 = max(0, x - mx), max(0, y - my)
            x2, y2 = min(W, x + w + mx), min(H, y + h + my)
            if (x2 - x1) * (y2 - y1) <= 0.7 * W * H and x2 - x1 >= 32 and y2 - y1 >= 32:
                rects.append((x1, y1, x2, y2))
        return rects

    def _en_zona(self, punto, contornos) -> bool:
        if not contornos:
            return True
        return any(cv2.pointPolygonTest(c, punto, False) >= 0 for c in contornos)

    # -- color ------------------------------------------------------------
    def _cumple_color(self, crop, mask, color) -> tuple[bool, float]:
        if color == "cualquiera":
            return True, 100.0
        px = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[mask.astype(bool)]
        if len(px) == 0:
            return False, 0.0
        h, s, v = px[:, 0].astype(int), px[:, 1].astype(int), px[:, 2].astype(int)
        if color == "blanco":
            pct = float(np.mean((s < 60) & (v >= 170))) * 100.0
            return pct >= self.color_min_frac, pct
        if color == "negro":
            pct = float(np.mean(v < 60)) * 100.0
            return pct >= self.color_min_frac, pct
        vivo = (s >= self.sat_min) & (v >= self.val_min)
        nv = int(vivo.sum())
        if nv / len(px) * 100.0 < self.min_colored_frac:
            return False, 0.0
        sel = np.zeros(len(px), bool)
        for lo, hi in COLORES[color]:
            sel |= (h >= lo) & (h < hi)
        pct = float((vivo & sel).sum()) / max(1, nv) * 100.0
        return pct >= self.color_min_frac, pct

    # -- evaluacion SAM3 de UNA persona ------------------------------------
    def _evaluar(self, frame, box) -> tuple[frozenset, list[Detection]]:
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = box
        bw, bh = x2 - x1, y2 - y1
        cx1, cy1 = int(max(0, x1 - 0.2 * bw)), int(max(0, y1 - 0.2 * bh))
        cx2, cy2 = int(min(W, x2 + 0.2 * bw)), int(min(H, y2 + 0.2 * bh))
        visibles = [k for k in self.items
                    if not (CATALOGO[k]["borde"] == "abajo" and y2 >= H - 3)
                    and not (CATALOGO[k]["borde"] == "arriba" and y1 <= 3)]
        if cx2 - cx1 < 8 or cy2 - cy1 < 8 or not visibles:
            return frozenset(), []
        crop = frame[cy1:cy2, cx1:cx2]
        confs = {k: self.items_cfg[k]["conf"] for k in visibles}
        dets = self.m.sam3.detect(crop, visibles, confs)
        faltas, epp_dets = set(), []
        for k in visibles:
            r0, r1 = CATALOGO[k]["region"]
            ry0, ry1 = (y1 + r0 * bh) - cy1, (y1 + r1 * bh) - cy1
            rx0, rx1 = x1 - cx1 - 0.15 * bw, x2 - cx1 + 0.15 * bw
            # ademas de caer en la region, >=50% de la mascara debe estar DENTRO de
            # la caja de ESTA persona (evita adoptar el EPP de un vecino solapado)
            px1, py1, px2, py2 = x1 - cx1, y1 - cy1, x2 - cx1, y2 - cy1

            def _propia(mk):
                ys, xs = np.nonzero(mk)
                return float(np.mean((xs >= px1) & (xs <= px2) & (ys >= py1) & (ys <= py2))) if len(xs) else 0.0

            hits = [(bb, sc, mk) for bb, sc, mk in dets.get(k, [])
                    if rx0 <= _centro(bb)[0] <= rx1 and ry0 <= _centro(bb)[1] <= ry1
                    and _propia(mk) >= 0.6]   # propio: 95-100%; fuga de vecino solapado: 8-47%
            if not hits:
                faltas.add(CATALOGO[k]["nombre"])
                continue
            # SAM3 devuelve mascaras solapadas del mismo EPP: si se exige color,
            # preferir la que lo cumple (la de mayor score no siempre es la mas limpia)
            hit = None
            ok_color, pct = False, 0.0
            if self.items_cfg[k]["color"] != "cualquiera":
                for bb2, sc2, mk2 in hits:
                    ok2, p2 = self._cumple_color(crop, mk2, self.items_cfg[k]["color"])
                    if ok2:
                        hit, ok_color, pct = (bb2, sc2, mk2), True, p2
                        break
            if hit is None:
                hit = hits[0]
                ok_color, pct = self._cumple_color(crop, hit[2], self.items_cfg[k]["color"])
            bb, sc, mk = hit
            if not ok_color:
                faltas.add(f"{CATALOGO[k]['nombre']} NO {self.items_cfg[k]['color'].upper()}")
            epp_dets.append(Detection(
                label=f"{k}:{self.items_cfg[k]['color']}:{pct:.0f}%" if self.items_cfg[k]["color"] != "cualquiera" else k,
                score=sc, bbox=(int(bb[0] + cx1), int(bb[1] + cy1), int(bb[2] + cx1), int(bb[3] + cy1))))
        return frozenset(faltas), epp_dets

    # -- veredicto (mayoria en ventana) -------------------------------------
    @staticmethod
    def _mayoria(votos, now, ventana):
        rec = [v for t, v in votos if now - t <= ventana]
        need = (len(rec) + 1) // 2
        conteo: dict[str, int] = {}
        for v in rec:
            for k in v:
                conteo[k] = conteo.get(k, 0) + 1
        return sorted(k for k, c in conteo.items() if c >= need), len(rec)

    # -- bucle -------------------------------------------------------------
    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        # TODO el tiempo de la sesion corre sobre el ts DEL MEDIO (contrato Kinich):
        # asi predict_clip (video a velocidad de proceso) y streams se comportan igual.
        now = ts
        eventos: list[Event] = []
        contornos = self._zonas(frame)
        vistos = self.m.track(self._tracker, frame, self.conf_person,
                              zone_rects=self._zone_rects(frame) if self.zone_zoom else None)

        vivos = set()
        personas = {}
        if self._registro is not None:
            personas = self._registro.actualizar(frame, [(tid, box) for tid, box, _ in vistos], now)
        for tid, box, _c in vistos:
            st = self._tracks.get(tid)
            if st is None:
                st = self._tracks[tid] = {"box": box, "votes": deque(maxlen=64), "faltan": None,
                                          "eval_t": None, "bad_since": None, "epp": [], "persona": None}
            elif _iou(st["box"], box) < 0.2:      # salto de caja: re-evaluar
                st.update(votes=deque(maxlen=64), faltan=None, eval_t=None, bad_since=None, epp=[])
            st["box"] = box
            st["last_seen"] = now
            st["en_zona"] = self._en_zona(((box[0] + box[2]) / 2.0, box[3]), contornos)
            p = personas.get(tid)
            if p is not None:
                st["persona"] = p
            vivos.add(tid)

        # evaluar como maximo UNA persona por frame (la mas urgente)
        pend = []
        for tid in vivos:
            st = self._tracks[tid]
            if not st["en_zona"] or not self.items:
                continue
            p = st["persona"]
            le = (p.last_eval if (p is not None and p.last_eval) else st["eval_t"]) if st["eval_t"] is not None else None
            if le is None:
                pend.append((0, 0.0, tid))
            elif now - le >= self.refresh_s:
                pend.append((1, le, tid))
        if pend:
            pend.sort()
            tid = pend[0][2]
            st = self._tracks[tid]
            voto, epp_dets = self._evaluar(frame, st["box"])
            st["epp"] = epp_dets
            st["eval_t"] = now
            p = st["persona"]
            if p is not None:
                p.votes.append((now, voto))
                p.last_eval = now
                st["faltan"], st["n_votos"] = self._mayoria(p.votes, now, self.vote_window_s)
                p.faltan = st["faltan"]
            else:
                st["votes"].append((now, voto))
                st["faltan"], st["n_votos"] = self._mayoria(st["votes"], now, self.vote_window_s)

        # limpiar tracks muertos
        for tid in [t for t, s in self._tracks.items() if t not in vivos and now - s.get("last_seen", 0) > 10]:
            del self._tracks[tid]

        # ---- flancos por sujeto (persona ReID o track) ----
        n_ok = n_bad = 0
        activos_alerta = set()
        for tid in vivos:
            st = self._tracks[tid]
            if not st["en_zona"] or st["faltan"] is None:
                continue
            suj = st["persona"] if st["persona"] is not None else st
            sid = f"P{st['persona'].pid}" if st["persona"] is not None else f"T{tid}"
            nombre = st["persona"].nombre if st["persona"] is not None else f"#{tid}"
            if st["faltan"]:
                n_bad += 1
                bs = suj.bad_since if st["persona"] is not None else st["bad_since"]
                if bs is None:
                    bs = now
                    if st["persona"] is not None:
                        suj.bad_since = bs
                    else:
                        st["bad_since"] = bs
                if now - bs >= self.alert_after_s:
                    activos_alerta.add(sid)
                    if sid not in self._alertadas or self._alertadas[sid]["faltas"] != st["faltan"]:
                        x1, y1, x2, y2 = [int(v) for v in st["box"]]
                        dets = [Detection(label=f"persona {nombre}", score=1.0, bbox=(x1, y1, x2, y2))] + st["epp"]
                        eventos.append(Event(type="EPP_FALTA", score=1.0, ts=ts, detections=dets,
                                             extra={"persona": nombre, "faltas": st["faltan"],
                                                    "votos": st.get("n_votos", 0)}))
                        self._alertadas[sid] = {"faltas": st["faltan"], "nombre": nombre}
            else:
                n_ok += 1
                if st["persona"] is not None:
                    suj.bad_since = None
                else:
                    st["bad_since"] = None
        # OFF: sujetos que estaban alertados y ya no (recuperaron o se fueron)
        for sid in [s for s in self._alertadas if s not in activos_alerta]:
            info = self._alertadas.pop(sid)
            eventos.append(Event(type="EPP_FALTA_OFF", score=0.0, ts=ts,
                                 extra={"persona": info["nombre"], "faltas_previas": info["faltas"]}))

        # ---- tolerancia colectiva ----
        n_eval = n_ok + n_bad
        pct = 100.0 * n_bad / n_eval if n_eval else 0.0
        excede = n_eval >= self.min_personas and pct > self.tol_pct
        if excede:
            self._tol_excedida_desde = self._tol_excedida_desde or now
            if not self._tol_alertada and now - self._tol_excedida_desde >= self.alert_after_s:
                self._tol_alertada = True
                eventos.append(Event(type="TOLERANCIA", score=round(pct / 100.0, 3), ts=ts,
                                     extra={"sin_epp": n_bad, "en_zona": n_eval,
                                            "pct_sin_epp": round(pct, 1), "tolerancia": self.tol_pct}))
        else:
            self._tol_excedida_desde = None
            if self._tol_alertada:
                self._tol_alertada = False
                eventos.append(Event(type="TOLERANCIA_OFF", score=round(pct / 100.0, 3), ts=ts,
                                     extra={"pct_sin_epp": round(pct, 1)}))

        # ---- cumplimiento periodico ----
        if n_eval and now - self._last_stats >= self.stats_every_s:
            self._last_stats = now
            eventos.append(Event(type="CUMPLIMIENTO", score=round(1.0 - pct / 100.0, 3), ts=ts,
                                 extra={"en_zona": n_eval, "con_epp": n_ok, "sin_epp": n_bad,
                                        "pct_sin_epp": round(pct, 1), "tolerancia": self.tol_pct,
                                        "exige": self.items}))
        return eventos

    def close(self) -> None:
        self._tracks.clear()


# --------------------------------------------------------------------------- #
#  Modelo
# --------------------------------------------------------------------------- #
class PPEDetectorModel(CVModel):
    info = ModelInfo(
        name="ppe-detector",
        version="1.1",
        task="PPE Detector — EPP por persona (casco/chaleco/botas/guantes x color), zero-shot",
        capabilities={Capability.IMAGE, Capability.STREAM, Capability.CLIP},
        event_types=["EPP_FALTA", "EPP_FALTA_OFF", "TOLERANCIA", "TOLERANCIA_OFF", "CUMPLIMIENTO"],
        config_schema=CONFIG_SCHEMA,
        target_fps=10.0,
    )

    def __init__(self) -> None:
        self.yolo = None
        self.sam3: _Sam3Engine | None = None
        self.registro_factory = None
        self._yolo_lock = threading.Lock()

    # -- carga -------------------------------------------------------------
    def load(self) -> None:
        from ultralytics import YOLO  # noqa: PLC0415
        self.yolo = YOLO(YOLO_WEIGHTS)
        self.sam3 = _Sam3Engine(COMFY_DIR, SAM3_CKPT, {k: c["prompt"] for k, c in CATALOGO.items()})
        engine = _ReIDEngine.get(REID_WEIGHTS) if REID_WEIGHTS else None
        if engine is not None and engine.ok:
            self.registro_factory = lambda th: _Registro(engine, match_th=th)
        else:
            log.warning("PPE Detector: identidad por track ByteTrack solamente (sin TransReID)")

    def warmup(self) -> None:
        dummy = np.zeros((360, 640, 3), np.uint8)
        with self._yolo_lock:
            self.yolo.predict(dummy, classes=[0], imgsz=640, half=True, verbose=False)
        self.sam3.detect(dummy, ["chaleco"], {"chaleco": 0.5})

    def unload(self) -> None:
        self.yolo = None
        self.sam3 = None

    # -- helpers compartidos por sesiones -----------------------------------
    def new_tracker(self):
        from ultralytics.trackers.byte_tracker import BYTETracker  # noqa: PLC0415
        from ultralytics.utils import IterableSimpleNamespace  # noqa: PLC0415
        cfg = IterableSimpleNamespace(track_high_thresh=0.25, track_low_thresh=0.1, new_track_thresh=0.3,
                                      track_buffer=60, match_thresh=0.8, fuse_score=True)
        return BYTETracker(cfg)

    def track(self, tracker, frame, conf, zone_rects=None) -> list[tuple[int, list[float], float]]:
        from ultralytics.engine.results import Boxes  # noqa: PLC0415
        import torch  # noqa: PLC0415
        H, W = frame.shape[:2]
        with self._yolo_lock:
            r = self.yolo.predict(frame, classes=[0], conf=conf, imgsz=800, half=True, verbose=False)[0]
        b = r.boxes
        dets = (np.concatenate([b.xyxy.cpu().numpy(), b.conf.cpu().numpy()[:, None]], axis=1)
                if len(b) else np.zeros((0, 5), np.float32))
        # V5: zoom por zona — YOLO reescala cada imagen a imgsz, asi que pasarle solo el
        # recorte de la zona la analiza "con lupa" (personas pequeñas en zonas lejanas)
        for zx1, zy1, zx2, zy2 in (zone_rects or []):
            with self._yolo_lock:
                r2 = self.yolo.predict(np.ascontiguousarray(frame[zy1:zy2, zx1:zx2]), classes=[0],
                                       conf=conf, imgsz=800, half=True, verbose=False)[0]
            for xy, cf in zip(r2.boxes.xyxy.cpu().numpy(), r2.boxes.conf.cpu().numpy()):
                e = [xy[0] + zx1, xy[1] + zy1, xy[2] + zx1, xy[3] + zy1, float(cf)]
                if all(_iou(e[:4], d[:4]) < 0.55 for d in dets):
                    dets = np.vstack([dets, np.array(e, np.float32)[None]]) if len(dets) else np.array([e], np.float32)
        n = len(dets)
        data = (np.concatenate([dets, np.zeros((n, 1), np.float32)], axis=1).astype(np.float32)
                if n else np.zeros((0, 6), np.float32))
        tracks = tracker.update(Boxes(torch.from_numpy(data), (H, W)).numpy(), frame)
        return [(int(t[4]), [float(v) for v in t[:4]], float(t[5])) for t in np.asarray(tracks).reshape(-1, 8)]

    # -- IMAGE: una foto -> veredicto instantaneo por persona ----------------
    def predict_image(self, frame: np.ndarray) -> list[Detection]:
        session = _PPESession(self, {})
        with self._yolo_lock:
            r = self.yolo.predict(frame, classes=[0], conf=session.conf_person, imgsz=800, half=True, verbose=False)[0]
        out: list[Detection] = []
        for box, score in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy()):
            faltas, epp_dets = session._evaluar(frame, [float(v) for v in box])
            etiqueta = "EPP OK" if not faltas else _etiqueta(sorted(faltas))
            x1, y1, x2, y2 = [int(v) for v in box]
            out.append(Detection(label=f"persona: {etiqueta}", score=float(score), bbox=(x1, y1, x2, y2)))
            out.extend(epp_dets)
        return out

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        return _PPESession(self, config or {})
