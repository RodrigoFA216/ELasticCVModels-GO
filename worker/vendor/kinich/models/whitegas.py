# -*- coding: utf-8 -*-
"""whitegas — fugas de gas blanco por segmentación de texto (SAM 3.1 INT4), contrato Kinich.

NO confundir con el `fire` NATIVO de Kinich — atacan cosas distintas:
  · `fire` (nativo): FUEGO/HUMO por CNN entrenada, cámara óptica.
  · `whitegas` (este): pluma de GAS BLANCO por segmentación con prompt de
    texto (SAM 3.1 cuantizado INT4 W4A4, sin clases entrenadas de gas) —
    detecta lo que el prompt describa, frame a frame, sin tracking.

MONOLITO autocontenido: WhiteGas/detector.py del banco Gas (byte a byte,
2026-09-08, sha 772873e1…) + su contrato Base/base_detector.py + la rebanada
de holders de sam31_async.py que ese código reusa + el adaptador al contrato
Kinich. Ediciones únicas marcadas `PORT-MONOLITO`. Requiere la pila ComfyUI
vendorizada (comfy.*) y los kernels INT4 (comfy-kitchen/comfy-aimdo) — ver
envs abajo.

El original NO emite alertas (su `alert` es False incondicional — misma
trampa que QueueAnalytics): los eventos FUGA_GAS/FUGA_GAS_OFF los deriva el
ADAPTADOR del score contra el umbral, con confirmación sostenida
(`confirm_s`) y apagado por ausencia (`off_s`) en tiempo del video.

Limitaciones heredadas (deliberadas en el original, documentadas en su
README): sin tracking, sin vetos WATER/CLOUD del pipeline INT8 de
producción (puede confundir vapor de agua o nubes), un prompt POR PROCESO
(el conditioning se computa al cargar el runtime; sesiones posteriores con
otro prompt reciben aviso y usan el cargado).

Envs: KINICH_GAS_CKPT (checkpoint .safetensors, requerido), KINICH_GAS_COMFY
(raíz del ComfyUI vendorizado si `comfy` no es importable ya),
KINICH_GAS_FPS (target_fps; default 5 — SAM INT4 rinde ~11 fps dedicado en
RTX 4090).
"""
from __future__ import annotations


# ═════════════════════════════════════════════════════════════════════════════
# SECCIÓN VENDIDA: Base/base_detector.py del banco Gas (el contrato que el
# detector espera) — sha a31013e6…, sin cambios
# ═════════════════════════════════════════════════════════════════════════════

import cv2


class BaseDetector:
    NAME = "BASE"

    # Colores BGR
    COLOR_OK = (60, 120, 60)
    COLOR_ALERT = (0, 0, 220)
    COLOR_BOX = (0, 200, 255)
    COLOR_TEXT = (255, 255, 255)

    # ------------------------------------------------------------------ #
    #  Hooks a implementar / sobreescribir en cada solucion
    # ------------------------------------------------------------------ #
    def add_arguments(self, parser):
        """Agrega argumentos CLI propios de la solucion (opcional)."""

    def configure(self, args):
        """Recibe los args ya parseados, antes de setup() (opcional)."""
        self.args = args

    def setup(self):
        """Carga de modelos y recursos. Se llama una vez antes del loop."""

    def process_frame(self, frame, frame_idx):
        """Inferencia por frame. DEBE devolver un dict (ver docstring)."""
        raise NotImplementedError

    def teardown(self):
        """Liberar recursos al terminar (opcional)."""

    # ------------------------------------------------------------------ #
    #  Anotacion por defecto (overlay estandar). Sobreescribible.
    # ------------------------------------------------------------------ #
    def annotate(self, frame, result, stats):
        out = frame.copy()
        self.draw_boxes(out, result.get("boxes") or [])
        self.draw_banner(out, result.get("alert", False),
                         result.get("label", ""), result.get("score"))
        self.draw_infobar(out, stats, result.get("extra", ""))
        return out

    # ------------------------------------------------------------------ #
    #  Helpers de dibujo
    # ------------------------------------------------------------------ #
    def draw_boxes(self, frame, boxes):
        for box in boxes:
            x1, y1, x2, y2 = [int(v) for v in box[:4]]
            label = box[4] if len(box) > 4 else ""
            conf = box[5] if len(box) > 5 else None
            cv2.rectangle(frame, (x1, y1), (x2, y2), self.COLOR_BOX, 2)
            if label or conf is not None:
                txt = label if conf is None else f"{label} {conf:.2f}"
                cv2.putText(frame, txt, (x1, max(15, y1 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, self.COLOR_BOX, 2)

    def draw_banner(self, frame, alert, label="", score=None):
        h, w = frame.shape[:2]
        color = self.COLOR_ALERT if alert else self.COLOR_OK
        cv2.rectangle(frame, (0, 0), (w, 50), color, -1)
        if alert:
            txt = f"{self.NAME} | ALERTA"
            if label:
                txt += f": {label}"
            if score is not None:
                txt += f" ({score:.2f})"
        else:
            txt = f"{self.NAME} | monitoreando..."
        cv2.putText(frame, txt, (12, 33), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, self.COLOR_TEXT, 2)

    def draw_infobar(self, frame, stats, extra=""):
        h, w = frame.shape[:2]
        cv2.rectangle(frame, (0, h - 30), (w, h), (30, 30, 30), -1)
        src = str(stats.get("source", ""))
        if len(src) > 40:
            src = "..." + src[-37:]
        txt = f"FPS: {stats.get('fps', 0.0):.1f} | frame: {stats.get('frame', 0)} | {src}"
        if extra:
            txt += f" | {extra}"
        cv2.putText(frame, txt, (10, h - 9), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, self.COLOR_TEXT, 1)


# ═════════════════════════════════════════════════════════════════════════════
# SECCIÓN VENDIDA: sam31_async.py del banco Gas — REBANADA (solo las dos
# utilidades genéricas slot=1 que detector.py reusa; SAM31AsyncWorker y el
# resto de ese archivo son de la producción INT8 y no viajan) — sin cambios
# ═════════════════════════════════════════════════════════════════════════════

import threading
import time

import torch


class LatestFrameHolder:
    """Slot=1: put() sobreescribe, nunca crece, nunca bloquea al productor."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._source_seq = None
        self._internal_seq = 0
        self.produced_total = 0
        self.wake = threading.Event()

    def put(self, frame, source_seq):
        with self._lock:
            self._frame = frame
            self._source_seq = source_seq
            self._internal_seq += 1
            self.produced_total += 1
        self.wake.set()

    def get_latest(self, last_seen_internal_seq):
        with self._lock:
            if self._internal_seq > last_seen_internal_seq:
                return self._frame, self._source_seq, self._internal_seq
            return None, None, last_seen_internal_seq


class LatestResultHolder:
    """Slot=1, protegido por lock: publica el resultado SAM mas reciente."""

    def __init__(self):
        self._lock = threading.Lock()
        self._result = None
        self._source_seq = None
        self._publish_t = None
        self.published_total = 0

    def put(self, result, source_seq):
        with self._lock:
            self._result = result
            self._source_seq = source_seq
            self._publish_t = time.time()
            self.published_total += 1

    def peek(self):
        with self._lock:
            return self._result, self._source_seq, self._publish_t


# ═════════════════════════════════════════════════════════════════════════════
# SECCIÓN VENDIDA: WhiteGas/detector.py del banco Gas — sha 772873e1…
# Ediciones PORT-MONOLITO (únicas):
#   1) el bloque de rutas/sys.path/imports del original (BaseDetector y los
#      holders ya están definidos arriba; comfy entra por KINICH_GAS_COMFY)
#   2) CKPT_PATH configurable por KINICH_GAS_CKPT
# Todo lo demás — scheduler, pool de runtimes, _infer, annotate — intacto.
# ═════════════════════════════════════════════════════════════════════════════

"""
WhiteGas/detector.py
=====================
WhiteGasDetector(BaseDetector): integra SAM3.1 INT4 (W4A4 ConvRot) en la
arquitectura Base existente (VideoSource/SourceWorker/web_server_split.py,
/home/litzy/gas/Base) SIN tocar ninguno de esos archivos ni la produccion
GAS (SAM3.1 INT8) que ya corre sobre la misma arquitectura.

(Docstring completo del original en WhiteGas/detector.py del banco Gas —
arquitectura del pool de 2 runtimes, scheduler round-robin y decisiones de
carga del checkpoint. Aquí el scheduler viaja pero NO se usa: el contrato
Kinich procesa síncrono por sesión, ver el adaptador al final.)
"""

import importlib.machinery
import json
import os
import sys
import types

import numpy as np

# PORT-MONOLITO: en el original aquí se resolvían _GAS_ROOT/_COMFY desde la
# ruta del archivo y se importaban base_detector/sam31_async tras insertar
# sys.path — BaseDetector y los holders ya están definidos arriba; el
# ComfyUI vendorizado entra por entorno (si `comfy` no es ya importable).
_AQUI = os.path.dirname(os.path.abspath(__file__))
_COMFY = os.environ.get("KINICH_GAS_COMFY", "")
if _COMFY and _COMFY not in sys.path:
    sys.path.insert(0, _COMFY)


# --------------------------------------------------------------------------- #
#  Stub minimo de torchaudio -- requisito de la venv de GAS (ver docstring del
#  modulo). Identico en efecto al de sam31_adapter.py.
# --------------------------------------------------------------------------- #
def _install_torchaudio_stub():
    existing = sys.modules.get("torchaudio")
    if existing is not None:
        return

    def _trap(name):
        def _raise(*_a, **_k):
            raise RuntimeError(f"{name}: stub de torchaudio, SAM3 no deberia tocar esto.")
        return _raise

    mod = types.ModuleType("torchaudio")
    mod.__spec__ = importlib.machinery.ModuleSpec("torchaudio", loader=None)
    mod.__path__ = []
    functional = types.ModuleType("torchaudio.functional")
    functional.__spec__ = importlib.machinery.ModuleSpec("torchaudio.functional", loader=None)
    functional.resample = _trap("torchaudio.functional.resample")
    transforms = types.ModuleType("torchaudio.transforms")
    transforms.__spec__ = importlib.machinery.ModuleSpec("torchaudio.transforms", loader=None)
    transforms.MelSpectrogram = _trap("torchaudio.transforms.MelSpectrogram")
    mod.functional = functional
    mod.transforms = transforms
    sys.modules["torchaudio"] = mod
    sys.modules["torchaudio.functional"] = functional
    sys.modules["torchaudio.transforms"] = transforms


# PORT-MONOLITO: checkpoint por entorno (en el original era una ruta fija
# dentro de WhiteGas/models/checkpoints/); default = volumen weights/ de la
# imagen de Kinich, mismo patrón que el resto de la familia.
CKPT_PATH = os.environ.get(
    "KINICH_GAS_CKPT",
    "weights/sam3.1_multiplex_w4a4_convrot_selective_fp16clip_fp32source.safetensors")
DEFAULT_PROMPT = "a white gas leak plume"
DEFAULT_THRESHOLD = 0.50
INPUT_W, INPUT_H = 800, 600
INTERNAL_RESOLUTION = 1008  # resolucion interna fija de SAM3 (igual que PPE/GAS)

# Pool de runtimes INT4 independientes -- reparto FIJO de fuentes por indice
# de alta modulo WHITEGAS_NUM_RUNTIMES (ver WhiteGasDetector.setup/
# set_runtime_index). Con 12 fuentes en sources.json: 6/6 exacto.
WHITEGAS_NUM_RUNTIMES = 2

MASK_COLOR_BGR = (60, 220, 60)
MASK_ALPHA = 0.45
BBOX_COLOR_BGR = (0, 200, 255)


# --------------------------------------------------------------------------- #
#  Instrumentacion de diagnostico (OPT-IN via WHITEGAS_DEBUG_STATS=1, cero
#  costo si no esta activada): log JSONL con 1 linea por process_frame() de
#  cada fuente -- source_seq, result_seq, age_ms, inference_ms, lock_wait_ms,
#  sam_fps, dropped. Usado por bench_masks.py para medir mask_age/starvation/
#  SAM FPS agregado antes/despues de un cambio de arquitectura, sin tocar
#  produccion ni PPE.
# --------------------------------------------------------------------------- #
_STATS_ENABLED = os.environ.get("WHITEGAS_DEBUG_STATS") == "1"
_STATS_LOG_LOCK = threading.Lock()
_stats_log_fh = None


def _stats_log(sid, **fields):
    global _stats_log_fh
    if not _STATS_ENABLED:
        return
    with _STATS_LOG_LOCK:
        if _stats_log_fh is None:
            _stats_log_fh = open(os.path.join(_AQUI, "live_stats.jsonl"), "a", buffering=1)
        fields["sid"] = sid
        fields["t"] = time.time()
        _stats_log_fh.write(json.dumps(fields) + "\n")


class WhiteGasScheduler:
    """UN SOLO hilo dedicado a SAM3 -- el UNICO que toca el modelo/GPU en
    todo el proceso. Reemplaza el diseno anterior (N hilos, uno por fuente,
    compitiendo por un `threading.Lock()` compartido -- WhiteGasAsyncWorker).

    Diagnostico (ver informe de benchmark): con 11 hilos compitiendo por un
    Lock crudo, ~815-820ms de cada ciclo de ~900ms eran ESPERA por el lock
    (lock_wait_ms), no computo -- el reparto resultaba razonablemente justo
    en la practica (ratio peor/mediana ~1.05x en la corrida medida), pero
    sin ninguna garantia: dependia de como el GIL/SO decidiera despertar a
    los hilos bloqueados en el Lock, y anadia el overhead de 11 hilos
    reintentando adquirirlo constantemente.

    Este scheduler ELIMINA esa carrera por diseno: recorre las fuentes
    registradas en RONDA ROBIN estricta (mismo orden de alta, sin
    prioridades ni heuristicas), atiende como maximo UNA vez a cada fuente
    por vuelta (la que tenga frame nuevo desde la ultima vez que se le
    sirvio) y sigue inmediatamente a la siguiente. Cota superior de espera
    GARANTIZADA (no empirica): a lo sumo N-1 servicios de otras fuentes
    entre dos servicios consecutivos a la misma fuente -- imposible que una
    fuente se quede sin turno mientras haya trabajo pendiente en otras.

    Con el pool de 2 runtimes (ver WhiteGasDetector._runtimes), hay UNA
    instancia de este scheduler POR runtime (WhiteGasDetector._schedulers,
    indexado 0/1) -- cada una sirve, en ronda robin, solo a las fuentes
    asignadas a SU runtime (6/6). Dentro de cada instancia sigue siendo un
    solo hilo el que llama a `infer_fn`, asi que el `infer_lock` de ESE
    runtime ya no protege concurrencia real entre las fuentes de ese grupo
    (se conserva igual como invariante defensiva de bajo costo, ver
    WhiteGasDetector._infer) -- pero SI protege entre los dos hilos-
    scheduler si alguna vez compartieran runtime (no es el caso hoy: cada
    scheduler tiene su propio runtime, con su propio infer_lock)."""

    def __init__(self, infer_fn):
        self._infer_fn = infer_fn
        self._lock = threading.Lock()          # protege SOLO las listas de registro
        self._sources = []                      # orden de alta (round robin)
        self._frame_holders = {}
        self._result_holders = {}
        self._last_seen = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="whitegas-scheduler")
        self._stats_lock = threading.Lock()
        self._served_total = 0
        self._served_for = {}
        self._sam_fps_per_sid = {}
        self._t_last_serve_per_sid = {}
        self._idx = 0

    def start(self):
        self._thread.start()
        return self

    def stop(self, timeout=5.0):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def register(self, sid):
        with self._lock:
            self._sources.append(sid)
            self._frame_holders[sid] = LatestFrameHolder()
            self._result_holders[sid] = LatestResultHolder()
            self._last_seen[sid] = 0

    def unregister(self, sid):
        with self._lock:
            if sid in self._sources:
                self._sources.remove(sid)
            self._frame_holders.pop(sid, None)
            self._result_holders.pop(sid, None)
            self._last_seen.pop(sid, None)

    def submit_frame(self, sid, frame, source_seq):
        self._frame_holders[sid].put(frame, source_seq)
        self._wake.set()

    def latest_result(self, sid):
        return self._result_holders[sid].peek()

    def per_source_stats(self, sid):
        holder = self._frame_holders.get(sid)
        produced = holder.produced_total if holder else 0
        with self._stats_lock:
            served = self._served_for.get(sid, 0)
            sam_fps = self._sam_fps_per_sid.get(sid, 0.0)
        return {
            "sam_fps": sam_fps,
            "sam_calls_total": served,
            "frames_produced": produced,
            "frames_dropped": max(0, produced - served),
        }

    # ------------------------------------------------------------------ #
    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                sources = list(self._sources)
            n = len(sources)
            if n == 0:
                self._wake.wait(timeout=0.2)
                self._wake.clear()
                continue

            served_this_lap = False
            for _ in range(n):
                self._idx %= n
                sid = sources[self._idx]
                self._idx += 1
                fh = self._frame_holders.get(sid)
                if fh is None:
                    continue
                frame, source_seq, internal_seq = fh.get_latest(self._last_seen.get(sid, 0))
                if frame is None:
                    continue  # esta fuente no tiene frame nuevo desde su ultimo turno -- se salta, SIN esperar

                self._last_seen[sid] = internal_seq
                served_this_lap = True

                t0 = time.perf_counter()
                result = self._infer_fn(frame)
                result["inference_ms"] = (time.perf_counter() - t0) * 1000.0

                rh = self._result_holders.get(sid)
                if rh is not None:
                    rh.put(result, source_seq)

                now = time.time()
                with self._stats_lock:
                    self._served_total += 1
                    self._served_for[sid] = self._served_for.get(sid, 0) + 1
                    t_prev = self._t_last_serve_per_sid.get(sid)
                    if t_prev is not None:
                        dt = now - t_prev
                        if dt > 0:
                            inst = 1.0 / dt
                            prev_fps = self._sam_fps_per_sid.get(sid, 0.0)
                            self._sam_fps_per_sid[sid] = inst if prev_fps == 0 else 0.9 * prev_fps + 0.1 * inst
                    self._t_last_serve_per_sid[sid] = now
                break  # UNA fuente por vuelta del for; retomamos desde self._idx

            if not served_this_lap:
                # nadie tenia frame nuevo (video mas lento que SAM) -- no
                # busy-loop: esperamos a que llegue el proximo submit_frame.
                self._wake.wait(timeout=0.05)
                self._wake.clear()


class WhiteGasDetector(BaseDetector):
    NAME = "WHITEGAS_INT4"

    # ------------------------------------------------------------------ #
    #  Pool de runtimes de PROCESO -- mismo patron classmethod+lock que
    #  PPEDetector._sam3/_conds/_model_lock (ppe_detector.py), generalizado
    #  a un dict indexado por runtime_index (0/1), igual concepto que
    #  sam31_adapter.py::_runtime_pool (produccion INT8). Cada entrada:
    #  {"model", "cond", "device", "dtype", "scale", "infer_lock",
    #  "cuda_stream"} -- 2 MODEL, 2 infer_lock, 2 cuda_stream INDEPENDIENTES.
    # ------------------------------------------------------------------ #
    _runtimes = {}
    _model_lock = threading.Lock()

    def add_arguments(self, parser):
        parser.add_argument("--wg-prompt", default=DEFAULT_PROMPT)
        parser.add_argument("--wg-threshold", type=float, default=DEFAULT_THRESHOLD)

    # ------------------------------------------------------------------ #
    #  Hook generico que web_server_split.py llama (via hasattr) al dar de
    #  alta cada fuente, EN ORDEN, antes de start() -- ver _add_source() y
    #  el mismo mecanismo ya usado por SAM31WebDetector.set_runtime_index.
    #  Da el indice de alta SECUENCIAL (0,1,2,...); el reparto real
    #  (modulo WHITEGAS_NUM_RUNTIMES) se resuelve en setup().
    # ------------------------------------------------------------------ #
    def set_runtime_index(self, idx):
        self._runtime_seq_index = idx

    # ------------------------------------------------------------------ #
    @classmethod
    def _load_models(cls, prompt, runtime_index):
        with cls._model_lock:
            if runtime_index in cls._runtimes:
                return
            _install_torchaudio_stub()

            import comfy.ldm.sam3.detector  # noqa: F401  (registra el modelo SAM3/SAM31)
            import comfy.ldm.sam3.tracker  # noqa: F401
            import comfy.model_management as mm
            import comfy.options
            import comfy.sd
            import comfy.text_encoders.sam3_clip  # noqa: F401

            comfy.options.enable_args_parsing(False)

            # cuda_stream PROPIO de este runtime -- la carga del checkpoint y
            # cada infer() posterior de este runtime corren dentro de
            # `with torch.cuda.stream(cuda_stream)` (ver _infer), igual
            # patron que sam31_adapter.py::SAM31SharedRuntime.
            cuda_stream = torch.cuda.Stream()
            with torch.cuda.stream(cuda_stream):
                model, clip, vae, clipvision = comfy.sd.load_checkpoint_guess_config(
                    CKPT_PATH, output_vae=False, output_clip=True,
                    output_clipvision=False, embedding_directory=None, output_model=True,
                )
                if clip is not None and getattr(clip, "patcher", None) is not None:
                    mm.load_models_gpu([clip.patcher])
                mm.load_model_gpu(model)

                device = mm.get_torch_device()
                dtype = model.model.get_dtype()

                # tokenize + encode_from_tokens_scheduled == lo mismo que
                # nodes.CLIPTextEncode().encode(clip, prompt)[0] (PPE), sin
                # importar nodes.py -- ver docstring del modulo.
                tokens = clip.tokenize(prompt)
                conditioning = clip.encode_from_tokens_scheduled(tokens)
                cond_tensor = conditioning[0][0].to(device=device, dtype=dtype)
                amask = conditioning[0][1].get("attention_mask")
                amask = (amask.to(device) if amask is not None
                         else torch.ones(cond_tensor.shape[0], cond_tensor.shape[1],
                                          dtype=torch.int64, device=device))

                clip.cond_stage_model.to("cpu")
            torch.cuda.empty_cache()

            # W,H de entrada son fijos (800x600, --resize del panel) para
            # las 6 fuentes de este runtime -- se precalcula UNA vez en vez
            # de crear un tensor nuevo en cada llamada a _infer().
            scale = torch.tensor([INPUT_W, INPUT_H, INPUT_W, INPUT_H], device=device)

            cls._runtimes[runtime_index] = {
                "model": model,
                "cond": (cond_tensor, amask),
                "device": device,
                "dtype": dtype,
                "scale": scale,
                "infer_lock": threading.Lock(),
                "cuda_stream": cuda_stream,
            }
            print(f"[WhiteGas] runtime {runtime_index}/{WHITEGAS_NUM_RUNTIMES} SAM3.1 INT4 (W4A4) cargado: "
                  f"{os.path.basename(CKPT_PATH)} | prompt={prompt!r} | device={device} | dtype={dtype} "
                  f"| cuda_stream={cuda_stream.cuda_stream} | model_id={id(model)}")

    # ------------------------------------------------------------------ #
    @classmethod
    def _infer(cls, frame_bgr, threshold, runtime_index):
        """MISMA llamada de bajo nivel que PPE (_sam3_recorte,
        ppe_detector.py): det._get_backbone_features + det._detect sobre el
        frame completo (aqui, sin recorte de persona) -- corre sobre el
        runtime (`cls._runtimes[runtime_index]`) asignado a esta fuente:
        su propio MODEL, su propio infer_lock, su propio cuda_stream.

        Recortes de overhead por llamada (ver informe de benchmark) que NO
        cambian el resultado numerico ni la calidad del modelo:
          - mm.load_model_gpu() ya NO se llama aqui: el modelo se carga (y
            se fija en GPU) UNA sola vez en _load_models(); llamarlo en cada
            frame (como hacia PPE en _sam3_recorte, defensivo ante posible
            desalojo por otros loads de ComfyUI) es redundante aqui porque
            WhiteGas nunca vuelve a llamar a ningun otro load de ComfyUI
            despues del arranque -- no hay nada que pueda desalojar el
            modelo. Coincide con el patron ya usado por sam31_adapter.py
            (produccion INT8): load_model_gpu solo en __init__, no en infer().
          - `scale` ya no se reconstruye por llamada (precalculado por
            runtime en _load_models) -- W,H de entrada son fijos.
          - torch.inference_mode() en vez de no_grad() (mismo efecto de
            "sin gradientes", overhead de Python ligeramente menor).
        """
        rt = cls._runtimes[runtime_index]
        H, W = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        t = torch.from_numpy(rgb.astype(np.float32) / 255.0)[None]

        import comfy.utils
        img = comfy.utils.common_upscale(t.movedim(-1, 1), INTERNAL_RESOLUTION, INTERNAL_RESOLUTION,
                                          "bilinear", crop="disabled")

        out = {"mask": None, "bbox": None, "score": None, "objects": 0, "runtime_index": runtime_index}
        _t_wait0 = time.perf_counter()
        with rt["infer_lock"], torch.cuda.stream(rt["cuda_stream"]):
            out["lock_wait_ms"] = (time.perf_counter() - _t_wait0) * 1000.0
            device, dtype = rt["device"], rt["dtype"]
            det = rt["model"].model.diffusion_model.detector
            with torch.inference_mode():
                frame = img.to(device=device, dtype=dtype)
                features, positions, _, _ = det._get_backbone_features(frame)
                resizer = det.backbone["language_backbone"]["resizer"]
                scale = rt["scale"] if (W, H) == (INPUT_W, INPUT_H) else torch.tensor([W, H, W, H], device=device)
                emb, amask = rt["cond"]
                boxes, scores, masks, _ = det._detect(features, positions, resizer(emb), amask.bool(), None, None)
                probs = scores[0].sigmoid()
                keep = probs > threshold
                if keep.any():
                    bx = (boxes[0][keep] * scale.to(boxes.dtype)).float().cpu()
                    sc = probs[keep].float().cpu()
                    orden = sc.argsort(descending=True)
                    best = int(orden[0])
                    m = masks[0][keep][orden[:1].to(masks.device)].unsqueeze(1).float()
                    m = torch.nn.functional.interpolate(m, size=(H, W), mode="bilinear", align_corners=False)
                    m = (m[0, 0] > 0).cpu().numpy()
                    b = bx[best].tolist()
                    out["bbox"] = {"x": float(b[0]), "y": float(b[1]),
                                    "width": float(b[2] - b[0]), "height": float(b[3] - b[1])}
                    out["score"] = float(sc[best])
                    out["mask"] = m
                    out["objects"] = int(keep.sum())
            out["vram_allocated_mb"] = torch.cuda.memory_allocated() / 2**20
            out["vram_peak_mb"] = torch.cuda.max_memory_allocated() / 2**20
        return out

    # ------------------------------------------------------------------ #
    #  UN scheduler (WhiteGasScheduler) POR runtime -- _schedulers[0] sirve
    #  en ronda robin solo a las fuentes de runtime 0, _schedulers[1] solo
    #  a las de runtime 1. Mismo patron classmethod+lock que _load_models.
    # ------------------------------------------------------------------ #
    _schedulers = {}
    _scheduler_lock = threading.Lock()

    @classmethod
    def _get_scheduler(cls, threshold, runtime_index):
        with cls._scheduler_lock:
            if runtime_index not in cls._schedulers:
                cls._schedulers[runtime_index] = WhiteGasScheduler(
                    lambda frame, ri=runtime_index: cls._infer(frame, threshold, ri)
                ).start()
            return cls._schedulers[runtime_index]

    def setup(self):
        args = getattr(self, "args", None)
        self.prompt = getattr(args, "wg_prompt", DEFAULT_PROMPT) if args else DEFAULT_PROMPT
        self.threshold = getattr(args, "wg_threshold", DEFAULT_THRESHOLD) if args else DEFAULT_THRESHOLD

        # Reparto FIJO: indice de alta (set_runtime_index, dado por
        # web_server_split.py::_add_source en orden de sources.json) modulo
        # WHITEGAS_NUM_RUNTIMES=2 -- alternando 0,1,0,1,... da 6/6 exacto
        # con 12 fuentes (6/5 con 11).
        seq_index = getattr(self, "_runtime_seq_index", 0)
        self.runtime_index = seq_index % WHITEGAS_NUM_RUNTIMES
        self._load_models(self.prompt, self.runtime_index)

        self.source_seq = 0
        sid = getattr(self, "source_id", "?")
        self.scheduler = self._get_scheduler(self.threshold, self.runtime_index)
        self.scheduler.register(sid)
        print(f"[{self.NAME}] fuente id={sid} seq={seq_index} -> runtime {self.runtime_index}/{WHITEGAS_NUM_RUNTIMES} "
              f"(scheduler round-robin propio de ese runtime, prompt={self.prompt!r} threshold={self.threshold})")

    def process_frame(self, frame, frame_idx):
        self.source_seq += 1
        seq = self.source_seq
        sid = getattr(self, "source_id", "?")
        self.scheduler.submit_frame(sid, frame, seq)

        result, res_seq, res_t = self.scheduler.latest_result(sid)
        now = time.time()
        worker_stats = self.scheduler.per_source_stats(sid)

        base = {
            "alert": False,
            "source_seq": seq,
            "vram_allocated_mb": torch.cuda.memory_allocated() / 2**20,
            "vram_peak_mb": torch.cuda.max_memory_allocated() / 2**20,
            **worker_stats,
        }
        if result is None:
            base.update({"mask": None, "bbox": None, "score": None, "objects": 0,
                         "mode": "WAIT", "inference_ms": 0.0,
                         "result_seq": None, "age_frames": None, "age_ms": None})
            return base

        base.update(result)
        base["mode"] = "DETECT"
        base["result_seq"] = res_seq
        base["age_frames"] = seq - res_seq
        base["age_ms"] = (now - res_t) * 1000.0 if res_t else None

        _stats_log(
            getattr(self, "source_id", "?"),
            source_seq=seq, result_seq=res_seq, age_ms=base["age_ms"],
            inference_ms=base.get("inference_ms"), lock_wait_ms=base.get("lock_wait_ms"),
            sam_fps=base.get("sam_fps"), sam_calls_total=base.get("sam_calls_total"),
            frames_produced=base.get("frames_produced"), frames_dropped=base.get("frames_dropped"),
            video_fps=getattr(self, "_last_video_fps", None),
        )
        return base

    # ------------------------------------------------------------------ #
    def annotate(self, frame, result, stats):
        self._last_video_fps = stats.get("fps", 0.0)
        out = frame.copy()
        h, w = out.shape[:2]

        mask = result.get("mask")
        if mask is not None and mask.shape[:2] == out.shape[:2] and mask.any():
            overlay = out.copy()
            overlay[mask.astype(bool)] = MASK_COLOR_BGR
            cv2.addWeighted(overlay, MASK_ALPHA, out, 1.0 - MASK_ALPHA, 0, out)

        bbox = result.get("bbox")
        if bbox:
            x1, y1 = int(bbox["x"]), int(bbox["y"])
            x2, y2 = int(bbox["x"] + bbox["width"]), int(bbox["y"] + bbox["height"])
            cv2.rectangle(out, (x1, y1), (x2, y2), BBOX_COLOR_BGR, 2)

        alert = bool(result.get("objects", 0) > 0)
        banner_color = self.COLOR_ALERT if alert else self.COLOR_OK
        cv2.rectangle(out, (0, 0), (w, 44), banner_color, -1)
        cv2.putText(out, f"WHITEGAS INT4 (W4A4) | {'DETECCION' if alert else 'monitoreando'}",
                    (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.75, self.COLOR_TEXT, 2)

        score = result.get("score")
        score_txt = "-" if score is None else f"{score:.3f}"
        lines = [
            f"VIDEO FPS: {stats.get('fps', 0.0):.1f} | SAM FPS: {result.get('sam_fps', 0.0):.1f}",
            f"score: {score_txt} | objetos: {result.get('objects', 0)} | "
            f"inference: {result.get('inference_ms', 0.0):.1f} ms",
            f"VRAM alloc {result.get('vram_allocated_mb', 0.0):.0f} MB | "
            f"peak {result.get('vram_peak_mb', 0.0):.0f} MB",
            f"source_seq: {result.get('source_seq', 0)} | dropped: {result.get('frames_dropped', 0)} "
            f"| runtime: {result.get('runtime_index', getattr(self, 'runtime_index', '?'))}",
        ]
        panel_h = 14 + len(lines) * 18
        cv2.rectangle(out, (0, h - panel_h), (w, h), (20, 20, 20), -1)
        yy = h - panel_h + 16
        for line in lines:
            cv2.putText(out, line, (10, yy), cv2.FONT_HERSHEY_SIMPLEX, 0.44, self.COLOR_TEXT, 1)
            yy += 18
        return out

    # ------------------------------------------------------------------ #
    def teardown(self):
        # Solo se da de baja ESTA fuente del scheduler compartido -- el
        # hilo del scheduler (y el modelo) sigue sirviendo a las demas.
        if hasattr(self, "scheduler"):
            sid = getattr(self, "source_id", "?")
            self.scheduler.unregister(sid)
            print(f"[{self.NAME}] fuente id={sid} dada de baja del scheduler")


# ═════════════════════════════════════════════════════════════════════════════
# ADAPTADOR AL CONTRATO KINICH
# El scheduler round-robin de arriba era el reparto de UNA GPU entre 11
# fuentes de UN proceso-panel; en Kinich cada stream tiene su worker y su
# paceo (`target_fps`), así que la sesión llama a `_infer` SÍNCRONO y el
# `gpu_semaphore` de Kinich reparte la GPU entre streams. El scheduler viaja
# (verbatim) pero no se instancia.
# ═════════════════════════════════════════════════════════════════════════════

import logging
from typing import Any

from kinich.contract import Capability, CVModel, Event, ModelInfo, StreamSession
from kinich.contract import Detection as KDetection

log = logging.getLogger(__name__)

# PORT-MONOLITO: fps de consumo configurable por despliegue. SAM3.1 INT4
# rinde ~11 fps DEDICADO en RTX 4090 (~80-90 ms/frame); 5 deja aire para
# convivir con otros streams. Para archivos, subirlo solo alarga la pared.
TARGET_FPS = float(os.environ.get("KINICH_GAS_FPS", "5"))

#: prompt con el que se cargó el runtime 0 (el conditioning se computa UNA
#: vez por proceso en _load_models — limitación heredada del original:
#: un prompt por proceso; sesiones con otro prompt reciben aviso).
_PROMPT_CARGADO: str | None = None

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "default": DEFAULT_PROMPT,
                   "description": ("qué busca SAM3 (texto). UN prompt por PROCESO: la "
                                    "primera sesión fija el conditioning; las siguientes "
                                    "con otro prompt reciben aviso y usan el cargado")},
        "threshold": {"type": "number", "default": DEFAULT_THRESHOLD,
                      "description": "score (sigmoid) mínimo para considerar detección"},
        "confirm_s": {"type": "number", "default": 2.0,
                      "description": ("segundos SOSTENIDOS de detección antes de emitir "
                                       "FUGA_GAS (anti-parpadeo; el modelo no trae "
                                       "histéresis propia: su alert es False incondicional)")},
        "off_s": {"type": "number", "default": 5.0,
                  "description": "segundos sin detección antes de FUGA_GAS_OFF"},
        "resumen_cada_s": {"type": "number", "default": 0,
                           "description": "cada cuántos s emitir RESUMEN (0 = nunca)"},
    },
}


def _detecciones(r: dict) -> list[KDetection]:
    if not r.get("bbox"):
        return []
    b = r["bbox"]
    # el contrato Kinich lleva bbox en píxeles ENTEROS de la fuente
    return [KDetection(label="gas", score=float(r.get("score") or 0.0),
                       bbox=(int(round(b["x"])), int(round(b["y"])),
                             int(round(b["x"] + b["width"])),
                             int(round(b["y"] + b["height"]))))]


def _area_pct(r: dict) -> float | None:
    m = r.get("mask")
    if m is None:
        return None
    return round(float(m.mean()) * 100.0, 2)


class _Session(StreamSession):
    def __init__(self, config: dict[str, Any]):
        global _PROMPT_CARGADO
        prompt = str(config.get("prompt") or DEFAULT_PROMPT)
        self.threshold = float(config.get("threshold") or DEFAULT_THRESHOLD)
        self.confirm_s = float(config.get("confirm_s") or 2.0)
        self.off_s = float(config.get("off_s") or 5.0)
        self._resumen_cada = float(config.get("resumen_cada_s") or 0)

        WhiteGasDetector._load_models(prompt, 0)
        if _PROMPT_CARGADO is None:
            _PROMPT_CARGADO = prompt
        elif prompt != _PROMPT_CARGADO:
            log.warning("whitegas: el runtime ya está cargado con el prompt %r; "
                        "esta sesión pidió %r y usará el cargado (un prompt por "
                        "proceso — limitación heredada del original)",
                        _PROMPT_CARGADO, prompt)

        # histéresis del emisor, en TIEMPO DEL VIDEO (ts del contrato)
        self._en_alerta = False
        self._detectando_desde: float | None = None
        self._ultima_det: float | None = None
        self._max_score = 0.0
        self._ultimo_resumen: float | None = None

    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        # en el original inference_ms lo medía el scheduler; aquí la llamada
        # es síncrona y se mide igual, alrededor de la misma _infer
        _t0 = time.perf_counter()
        r = WhiteGasDetector._infer(frame, self.threshold, 0)
        r["inference_ms"] = (time.perf_counter() - _t0) * 1000.0
        events: list[Event] = []
        detectando = bool(r.get("objects", 0) > 0)
        score = float(r.get("score") or 0.0)
        if score > self._max_score:
            self._max_score = score

        if detectando:
            self._ultima_det = ts
            if self._detectando_desde is None:
                self._detectando_desde = ts
            if (not self._en_alerta
                    and ts - self._detectando_desde >= self.confirm_s):
                self._en_alerta = True
                events.append(Event(
                    type="FUGA_GAS", score=score, ts=ts,
                    detections=_detecciones(r),
                    extra={"score": round(score, 4), "objetos": r.get("objects", 0),
                           "area_pct": _area_pct(r),
                           "sostenida_s": round(ts - self._detectando_desde, 2),
                           "prompt": _PROMPT_CARGADO}))
        else:
            if not self._en_alerta:
                self._detectando_desde = None
            if (self._en_alerta and self._ultima_det is not None
                    and ts - self._ultima_det >= self.off_s):
                self._en_alerta = False
                self._detectando_desde = None
                events.append(Event(
                    type="FUGA_GAS_OFF", score=0.0, ts=ts,
                    extra={"sin_deteccion_s": round(ts - self._ultima_det, 2),
                           "max_score_episodio": round(self._max_score, 4)}))
                self._max_score = 0.0

        if self._resumen_cada > 0 and (self._ultimo_resumen is None
                                       or ts - self._ultimo_resumen >= self._resumen_cada):
            self._ultimo_resumen = ts
            events.append(Event(
                type="RESUMEN", score=0.0, ts=ts,
                extra={"score": round(score, 4) if detectando else None,
                       "objetos": r.get("objects", 0),
                       "en_alerta": self._en_alerta,
                       "area_pct": _area_pct(r),
                       "inference_ms": round(r.get("inference_ms", 0.0), 1)
                       if r.get("inference_ms") is not None else None}))
        return events

    def close(self) -> None:
        # el runtime (checkpoint en GPU) es de PROCESO y sigue para las
        # demás sesiones — igual que el teardown del original, que solo
        # daba de baja la fuente y nunca descargaba el modelo.
        pass


class WhiteGasKinichDetector(CVModel):
    info = ModelInfo(
        name="whitegas",
        version="1.0",
        task=("Fugas de gas blanco por segmentación de texto (SAM 3.1 INT4 "
              "W4A4, prompt configurable, sin tracking ni vetos WATER/CLOUD); "
              "FUGA_GAS con confirmación sostenida y máscara→bbox+área. "
              "No es el fire nativo (fuego/humo por CNN)"),
        capabilities={Capability.STREAM},
        event_types=["FUGA_GAS", "FUGA_GAS_OFF", "RESUMEN"],
        config_schema=CONFIG_SCHEMA,
        target_fps=TARGET_FPS,
    )

    def load(self) -> None:
        if not os.path.isfile(CKPT_PATH):
            raise FileNotFoundError(
                f"falta el checkpoint SAM3.1 INT4 {CKPT_PATH} (KINICH_GAS_CKPT)")
        try:
            _install_torchaudio_stub()
            import comfy.sd  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "no se puede importar la pila ComfyUI (comfy.*): vendorizar "
                "ComfyUI y apuntar KINICH_GAS_COMFY a su raíz, e instalar "
                "comfy-kitchen/comfy-aimdo (kernels INT4). Detalle: " + str(e)) from e
        # el checkpoint (~1.05 GB por runtime) se carga perezoso en la
        # primera sesión, CON el prompt de esa sesión — cargarlo aquí
        # fijaría el prompt default aunque nadie lo use.

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        return _Session(config or {})
