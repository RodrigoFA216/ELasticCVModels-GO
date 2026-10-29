# -*- coding: utf-8 -*-
"""corridor-counter — contador de personas por corredor (countPerson V3), contrato Kinich.

NO confundir con el `people-counter` NATIVO de Kinich — cuentan cosas distintas:
  · `people-counter` (nativo): AFORO instantáneo — ¿cuántas personas hay A LA
    VISTA ahora? (grid + YOLO por patch + NMS; evento CONTEO).
  · `corridor-counter` (este): FLUJO direccional — ¿cuántas ENTRARON y SALIERON
    por la puerta/corredor? (box de 4 puntos + escalera de líneas; eventos
    ENTRADA/SALIDA y DENTRO acumulado que persiste por `contador_id`).
Ninguno sustituye al otro: aforo de sala → nativo; tráfico por acceso → este.

MONOLITO autocontenido: el modelo people_counter V3 del banco countPerson
(nativo del contrato Full_CV, byte a byte; recall medido >=98.8% de tránsitos,
n=308, IC 95%, con el box sin solapar objetos fijos — cae a ~60% solapado) +
un shim del contrato Full_CV que ese código espera + el adaptador al contrato
Kinich. Ediciones únicas marcadas `PORT-MONOLITO`.

La burbuja facial del original (models.area_reid.face_bubble, ~267 MB de pila
facial de Full_CV) NO viaja: `load(faces=...)` ya era inyectable y aquí se
inyecta un stub no-op — la decisión de conteo no depende de ella (acordado
2026-08-31). El conteo persiste por `contador_id` (B-18: counters_dir
inyectable); dos streams con el mismo id no deben correr a la vez.
"""
from __future__ import annotations


# ═════════════════════════════════════════════════════════════════════════════
# SECCIÓN VENDIDA: app/contract.py de Full_CV (el contrato que el núcleo espera)
# ═════════════════════════════════════════════════════════════════════════════

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any as _AnyC

@dataclass
class Detection:
    """Una detección individual en un frame."""
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2 en píxeles
    label: str
    score: float


@dataclass
class InferenceResult:
    """Resultado de procesar un frame."""
    frame: np.ndarray                                   # frame anotado (BGR)
    detections: list[Detection] = field(default_factory=list)
    stats: dict[str, _AnyC] = field(default_factory=dict)  # ej: {"libres": 3, "ocupadas": 5}
    # Eventos puntuales para el feed (ej: una alerta que se acaba de disparar).
    # Cada evento: {"level": "alert"|"info", "text": "..."}
    events: list[dict] = field(default_factory=list)


@dataclass
class ConfigField:
    """
    Describe un parámetro que el modelo necesita del usuario.
    El frontend renderiza el widget según `type` (ver STANDARD.md §3).
    """
    key: str
    type: str          # "zones"|"polygons"|"lines"|"points"|"number"|"select"|"toggle"
    label: str
    required: bool = False
    default: _AnyC = None
    options: list[str] = field(default_factory=list)  # select / tipos de zona
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    help: str = ""


@dataclass
class ModelMeta:
    """Metadatos del modelo. Se leen sin cargar pesos (es barato)."""
    id: str
    name: str
    task: str                                              # "parking", "detection", ...
    description: str = ""
    modality: str = "video"                                # "video" | "audio"
    needs_reference_frame: bool = False                    # ¿necesita un frame para configurar?
    config_schema: list[ConfigField] = field(default_factory=list)
    # Métricas de evaluación del modelo (se muestran en el dashboard).
    # Floats en [0,1] se pintan como %. Strings se muestran como info.
    # Ej: {"precision": 0.0, "recall": 0.0, "mAP50": 0.0, "dataset": "Anáhuac"}
    metrics: dict[str, _AnyC] = field(default_factory=dict)
    # Info descriptiva para el panel del modelo: clases, entrada/salida, about.
    info: dict[str, _AnyC] = field(default_factory=dict)


class BaseCVModel(ABC):
    """Interfaz que todo modelo debe implementar."""

    meta: ModelMeta

    @abstractmethod
    def load(self) -> None:
        """Carga los pesos en GPU/memoria. Se llama al seleccionar el modelo."""

    @abstractmethod
    def unload(self) -> None:
        """Libera GPU/memoria. Se llama al cambiar a otro modelo."""

    def configure(self, config: dict[str, _AnyC]) -> None:
        """Recibe la configuración del usuario (ej: polígonos de plazas)."""
        self._config = config

    @abstractmethod
    def infer_frame(self, frame: np.ndarray) -> InferenceResult:
        """Procesa un frame y devuelve el resultado anotado + estadísticas."""

    def on_source_restart(self) -> None:
        """
        Gancho OPCIONAL: la plataforma lo llama cuando la fuente de video
        reinicia desde el principio (un archivo en bucle llegó al final y
        se re-abrió). Útil para limpiar estado temporal ligado a la línea
        de tiempo (trackers, buffers de suavizado) y no arrastrar
        "fantasmas" de la vuelta anterior. Por defecto no hace nada.
        NO debe recargar pesos ni tocar la configuración del usuario.
        """

    def reset_state(self) -> None:
        """
        Reset de demo entre TURNOS de la cola multi-usuario: limpia la
        configuración del usuario anterior y todo el estado temporal, para
        que el siguiente turno no vea nada del anterior (zonas, trackers,
        contadores). NO recarga pesos (eso lo hace select() al cambiar de
        modelo). Los modelos con estado persistente (p.ej. contadores en
        disco) deben sobreescribirlo para ponerlo también en cero.
        """
        try:
            self.configure({})
        except Exception:
            pass
        try:
            self.on_source_restart()
        except Exception:
            pass

    def stress_profile(self, frame: np.ndarray | None = None) -> dict[str, _AnyC]:
        """
        Gancho OPCIONAL para métricas de estrés específicas del modelo.
        Las métricas genéricas (VRAM por flujo, flujos que caben) las mide la
        plataforma sola. Aquí cada modelo añade lo suyo si lo necesita, p.ej.
        un modelo con crop+zoom puede devolver FPS según el nº de crops y los
        FLOPs por frame. Por defecto no devuelve nada.
        """
        return {}


# ══════ SECCIÓN VENDIDA: app/contract_v2.py de Full_CV (geometría de coords) ══════
import math
from typing import Iterable, Sequence

def clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else float(v))


def _dims(w: int, h: int) -> tuple[float, float]:
    if w <= 0 or h <= 0:
        raise ValueError(f"dimensiones inválidas: w={w} h={h}")
    return float(w), float(h)


def norm_point(pt: Sequence[float], w: int, h: int) -> list[float]:
    """(x_px, y_px) -> [x, y] en 0..1."""
    fw, fh = _dims(w, h)
    return [float(pt[0]) / fw, float(pt[1]) / fh]


def denorm_point(pt: Sequence[float], w: int, h: int, as_int: bool = False) -> list[float]:
    """[x, y] en 0..1 -> (x_px, y_px)."""
    fw, fh = _dims(w, h)
    x, y = float(pt[0]) * fw, float(pt[1]) * fh
    return [int(round(x)), int(round(y))] if as_int else [x, y]


def norm_box(box: Sequence[float], w: int, h: int) -> tuple[float, float, float, float]:
    """(x1,y1,x2,y2) en píxeles -> normalizado 0..1. NO recorta: una caja que sale
    del frame sigue saliendo (recortar es decisión del que pinta, ver `clamp_box`)."""
    fw, fh = _dims(w, h)
    x1, y1, x2, y2 = (float(v) for v in box)
    return (x1 / fw, y1 / fh, x2 / fw, y2 / fh)


def denorm_box(box: Sequence[float], w: int, h: int,
               as_int: bool = False) -> tuple[float, float, float, float]:
    """Normalizado 0..1 -> píxeles (x1,y1,x2,y2)."""
    fw, fh = _dims(w, h)
    x1, y1, x2, y2 = (float(v) for v in box)
    out = (x1 * fw, y1 * fh, x2 * fw, y2 * fh)
    return tuple(int(round(v)) for v in out) if as_int else out  # type: ignore[return-value]


def clamp_box(box: Sequence[float]) -> tuple[float, float, float, float]:
    return tuple(clamp01(v) for v in box)  # type: ignore[return-value]


def norm_poly(poly: Iterable[Sequence[float]], w: int, h: int) -> list[list[float]]:
    """Polígono en píxeles -> normalizado 0..1."""
    fw, fh = _dims(w, h)
    return [[float(p[0]) / fw, float(p[1]) / fh] for p in poly]


def denorm_poly(poly: Iterable[Sequence[float]], w: int, h: int,
                as_int: bool = False) -> list[list[float]]:
    """Polígono normalizado 0..1 -> píxeles."""
    fw, fh = _dims(w, h)
    if as_int:
        return [[int(round(float(p[0]) * fw)), int(round(float(p[1]) * fh))] for p in poly]
    return [[float(p[0]) * fw, float(p[1]) * fh] for p in poly]


def looks_normalized(coords: Iterable[Sequence[float]]) -> bool:
    """Heurística de MIGRACIÓN (§8): ¿estas coords ya vienen en 0..1?

    Solo para el puente con configuraciones v1 guardadas en píxeles. Un polígono
    en píxeles de un frame real tiene al menos un valor > 1; uno normalizado,
    ninguno.

    Ambiguo únicamente para figuras degeneradas (todo dentro del píxel (0,0)-(1,1)),
    que no son configurables a mano.

    **No cambiar la semántica de esta función.** La consume `models/fardeo` (en producción) y
    la refleja `frontend/lib/geometry.ts`, con paridad sujeta por el fixture compartido
    `tools/fixtures/geometry_cases.json`. Para clasificar configuración de ENTRADA usa
    `classify_coords`, que distingue los casos que este bool colapsa: `False` significa a la
    vez "son píxeles" y "no hay datos", y `True` incluye no finitos y figuras degeneradas.
    """
    seen = False
    for p in coords:
        for v in p:
            seen = True
            if abs(float(v)) > 1.0:
                return False
    return seen


# Veredictos de `classify_coords`.
COORDS_NORM = "norm"            # 0..1 sobre el frame de entrada (contrato v2 §1.2)
COORDS_PIXELS = "px"            # píxeles del frame (configuración v1)
COORDS_EMPTY = "empty"          # sin vértices
COORDS_INVALID = "invalid"      # algún valor no finito (NaN/Inf)
COORDS_DEGENERATE = "degenerate"  # ambiguo Y sin lectura útil en ninguno de los dos


def classify_coords(coords: Iterable[Sequence[float]]) -> str:
    """Clasifica una figura de configuración ANTES de convertirla.

    Existe porque `looks_normalized` devolvía un bool y eso obligaba a tratar dos casos
    peligrosos como si fueran una lectura legítima:

    **No finitos.** Con un NaN, `abs(nan) > 1.0` es False, así que la figura se tomaba por
    normalizada y reventaba MÁS TARDE, dentro de la conversión diferida: `int(round(nan*w))`
    lanza en el primer frame y en todos los siguientes (la clave de caché solo se escribe al
    final, así que no llega a cachearse). El usuario veía "Configuración aplicada" y un stream
    permanentemente en error. Antes de diferir la conversión, esto era un 400 inmediato. Con
    `COORDS_INVALID` el llamador vuelve a fallar rápido, en `configure`, que es lo correcto.

    **Figura entera dentro de (0,0)-(1,1) con vértices ENTEROS.** Es el único caso realmente
    ambiguo, y las dos lecturas son basura:
      · como píxeles → una zona de 1 px, inerte;
      · como normalizada → una zona que cubre el FRAME COMPLETO.
    Antes se resolvía como normalizada, y eso cambió el comportamiento del camino que hoy está
    en producción: una zona prohibida degenerada pasaba de no hacer nada a **alertar con cada
    vehículo**, y un corredor de conteo a contar el frame entero, en ambos casos sin un log.
    Nadie dibuja a mano una figura de 1 px, así que `COORDS_DEGENERATE` dice "esto no se puede
    interpretar": el llamador la descarta y lo registra, en vez de elegir por el usuario.

    Ojo con lo que NO es ambiguo: una figura con vértices FRACCIONARIOS dentro de [0,1] es
    normalizada sin duda (los píxeles llegan enteros), y ahí se incluye el rectángulo de frame
    completo [[0,0],[1,0],[1,1],[0,1]] si se dibuja de verdad — es intencional y se respeta.
    """
    seen = False
    max_abs = 0.0
    has_fraction = False
    for p in coords:
        for v in p:
            seen = True
            f = float(v)
            if f != f or f in (float("inf"), float("-inf")):
                return COORDS_INVALID
            a = abs(f)
            if a > max_abs:
                max_abs = a
            if f != int(f):
                has_fraction = True
    if not seen:
        return COORDS_EMPTY
    if max_abs > 1.0:
        return COORDS_PIXELS
    return COORDS_NORM if has_fraction else COORDS_DEGENERATE


# =============================================================================
# §2 — Salida estructurada
# =============================================================================


# PORT-MONOLITO: stub de la burbuja facial (la pila de Full_CV no viaja)
class FaceBubble:  # stub no-op: la pila facial de Full_CV no viaja a Kinich
    # firmas usadas por el núcleo (update/draw/limpieza); no reconocen a nadie
    def update(self, tid, frame, bbox, frame_idx):
        pass

    def draw_bubble(self, out, tid, bbox):
        pass

    def forget(self, tid):
        pass

    def reset(self):
        pass

    def __getattr__(self, _n):  # cualquier otro método futuro: no-op seguro
        return lambda *a, **k: None


# ═════════════════════════════════════════════════════════════════════════════
# SECCIÓN VENDIDA: geometry.py (código validado del banco countPerson; no editar)
# ═════════════════════════════════════════════════════════════════════════════

"""
Coordenadas del corredor de conteo — lógica PURA (stdlib, sin cv2 ni numpy).

Está separada de `model.py` por lo mismo que `models/fardeo/geometry.py`: es la
pieza que decide EN QUÉ ESPACIO está cada punto, y por eso tiene que poder
probarse sin OpenCV, sin torch y sin pesos
(`tools/checks/dual_people_counter_v2.py`).

## Aceptación DUAL (§1.2 + §8)

Durante el release conviven dos fronts: el viejo manda el box en PÍXELES del
frame de referencia, el nuevo en NORMALIZADO 0..1. Este modelo es NATIVO EN
PÍXELES, así que la dirección útil es la contraria a la de fardeo:

    0..1      --(u*W, v*H)--> px    camino DESEADO (contrato v2)
    píxeles   --(tal cual)--> px    camino HEREDADO (se loguea UNA vez)

## Por qué la conversión NO puede pasar en `configure()`

Dos razones, y la primera es la grave:

1. `np.array([[0.31, 0.52], ...], dtype=np.int32)` no falla: da un array de
   CEROS. Box degenerado, sin excepción y sin log — el modelo simplemente no
   cuenta a nadie, nunca. Es el peor fallo posible porque es silencioso.
2. Aunque quisiéramos desnormalizar en `configure()`, no hay con qué: el tamaño
   de la fuente no se conoce hasta el primer frame (el corredor se arma en
   `_prepare_scale`).

De modo que `configure()` solo CLASIFICA (`classify_coords`, una vez por figura)
y guarda crudo en float; la conversión se difiere a `resolve_box`.

## Por qué `classify_coords` y no el bool `looks_normalized`

Diferir la conversión movió el momento del fallo, y con un bool no hay forma de
distinguir los dos casos que NO son una lectura legítima:

  · NaN/Inf: `abs(nan) > 1` es False, así que el box pasaba por "normalizado" y
    reventaba en `int(round(nan*W))` en el PRIMER frame — y en todos los
    siguientes. El usuario veía "Configuración aplicada" y el stream muerto.
    Ahora `parse_box` LANZA en `configure()`, que es donde el error se ve.
  · figura entera de vértices ENTEROS dentro de (0,0)-(1,1): como píxeles es un
    corredor de 1 px (inerte, no cuenta a nadie); como normalizada CUBRE EL
    FRAME COMPLETO (cuenta el frame entero). Las dos lecturas son basura, así
    que la figura se DESCARTA y se avisa, en vez de elegir por el usuario.

## El ORDEN de los 4 puntos es SEMÁNTICO

El box no es un conjunto de vértices: el lado p0-p1 es AFUERA, y de él salen el
eje AFUERA→ADENTRO y la escalera de N líneas internas. Ninguna función de aquí
reordena, deduplica, cierra el polígono ni normaliza el sentido de giro: entra
punto i, sale punto i.
"""
# PORT-MONOLITO: __future__ va una sola vez, al inicio del archivo

from typing import Any, Dict, Iterable, List, Sequence, Tuple

# PORT-MONOLITO (definido arriba en este archivo):
# from app.contract_v2 import (COORDS_DEGENERATE, COORDS_INVALID, COORDS_NORM,
#                              COORDS_PIXELS, clamp01, classify_coords, denorm_poly)

#: el corredor es exactamente un box de 4 puntos (lo declara `ui`/`config_schema`)
BOX_POINTS = 4

#: Mensaje del descarte por figura ambigua. Vive aquí (y no en `model.py`) porque lo
#: usan las DOS salidas: el log del servidor y el `_zones_error` que ve el usuario.
DEGENERATE_MSG = (
    "BOX DESCARTADO: las esquinas caen todas dentro del pixel (0,0)-(1,1), "
    "asi que no se puede saber si son pixeles o 0..1. Redibuja el corredor."
)


def parse_box(raw: Iterable[Iterable[Sequence[float]]] | None
              ) -> Tuple[List[Dict[str, Any]], List[int]]:
    """`config["box"]` crudo -> `([{"poly": [[x,y],...], "coords": veredicto}], descartadas)`.

    Se queda en `float`: castear a `int` aquí es exactamente el bug que esto
    evita. `coords` es el veredicto de `classify_coords`, tomado una sola vez por
    figura para que el resto del pipeline no vuelva a adivinar.

    Dos veredictos NO producen figura, y por eso esta función devuelve una tupla:
    obligar al llamador a mirar `descartadas` es lo que garantiza que el descarte
    se AVISE. El fallo que se cerró aquí es justo el silencio: la figura ambigua
    se resolvía como normalizada, pasaba a cubrir el frame completo y contaba el
    frame entero, sin una sola línea de log.

    · `COORDS_INVALID` (NaN/Inf) -> LANZA `ValueError`. Fallar aquí es fallar en
      `configure()`, que es donde el usuario está mirando; si se deja pasar, el
      `int(round(nan*W))` de `resolve_box` revienta en cada frame para siempre.
    · `COORDS_DEGENERATE` -> se descarta la figura y su índice sale en
      `descartadas` (ver `DEGENERATE_MSG`).
    """
    out: List[Dict[str, Any]] = []
    dropped: List[int] = []
    for i, r in enumerate(raw or []):
        poly = [[float(p[0]), float(p[1])] for p in r]
        # Clasificar SOBRE EL CRUDO: cualquier saneado previo (un clamp01, p.ej.)
        # falsearía el veredicto — ver el comentario de `resolve_box`.
        verdict = classify_coords(poly)
        if verdict == COORDS_INVALID:
            raise ValueError(
                f"BOX INVALIDO: la figura {i + 1} trae coordenadas no finitas "
                f"(NaN/Inf): {poly}. Redibuja el corredor: con un NaN el box no se "
                f"puede convertir a pixeles y el stream se cae en el primer frame."
            )
        if verdict == COORDS_DEGENERATE:
            dropped.append(i)
            continue
        out.append({"poly": poly, "coords": verdict})
    return out, dropped


def check_box(regions: Sequence[Dict[str, Any]],
              dropped: Sequence[int] = ()) -> str:
    """Mensaje de error de configuración, `""` si el box es usable.

    Mira SOLO la aridad (una figura de 4 puntos) y el descarte por ambigüedad;
    nada de eso depende del espacio de coordenadas, así que se puede validar ya
    en `configure()` y el usuario se entera antes de arrancar el stream.
    """
    if dropped:
        # Antes que la aridad: si la única figura se descartó, "SIN CORREDOR" mandaría
        # al usuario a dibujar otra vez lo mismo sin decirle qué tenía de malo.
        return DEGENERATE_MSG
    if not regions:
        return "SIN CORREDOR: dibuja un box de 4 puntos"
    if len(regions) != 1 or len(regions[0]["poly"]) != BOX_POINTS:
        return (f"DIBUJA UN SOLO BOX de {BOX_POINTS} puntos "
                f"(hay {len(regions)} figura(s) de "
                f"{[len(r['poly']) for r in regions]} puntos)")
    return ""


def resolve_box(regions: Sequence[Dict[str, Any]], w: int, h: int
                ) -> Tuple[List[List[List[int]]], bool]:
    """`[{poly, norm}]` -> box en PÍXELES de un frame `w x h`.

    Devuelve `(regiones_px, legacy)`; `legacy=True` significa que al menos una
    figura llegó en píxeles, para que el modelo lo avise UNA sola vez.

    Se llama con las dimensiones REALES de la fuente (primer frame), nunca desde
    `configure()`. Corolario del camino normalizado: la misma config sirve para
    720p y 4K sin redibujar el corredor.
    """
    out: List[List[List[int]]] = []
    legacy = False
    for r in regions:
        if r["coords"] == COORDS_NORM:
            # clamp01 ANTES de escalar, y solo puede morder POR ABAJO: un vértice en
            # -0.004 (el ratón salió por el borde izquierdo/superior al soltar) se pega
            # a 0 en vez de empujar el box fuera del frame, porque de este box sale
            # también el recorte de análisis.
            #
            # Por arriba NO puede morder, aunque este comentario afirmaba lo contrario:
            # un vértice en 1.0002 nunca llega por esta rama. `classify_coords` mira el
            # crudo y |1.0002| > 1 lo hace COORDS_PIXELS, así que ese box entra por el
            # camino heredado y se usa tal cual. Es el orden de las operaciones, no el
            # clamp, lo que decide: clasificar primero, convertir después.
            poly = denorm_poly([[clamp01(x), clamp01(y)] for x, y in r["poly"]],
                               w, h, as_int=True)
        elif r["coords"] == COORDS_PIXELS:
            # Camino heredado: ya son píxeles de ESTE frame, no se toca la
            # geometría. Se redondea (no se trunca) porque el front manda floats.
            legacy = True
            poly = [[int(round(x)), int(round(y))] for x, y in r["poly"]]
        else:
            # COORDS_EMPTY (figura sin vértices). No la deja pasar `check_box`, pero si
            # llegara NO puede marcar `legacy`: eso imprimiría el aviso de "config v1"
            # por una figura que no trae coordenada alguna.
            poly = []
        out.append([[int(p[0]), int(p[1])] for p in poly])
    return out, legacy


# ═════════════════════════════════════════════════════════════════════════════
# SECCIÓN VENDIDA: model.py (people_counter V3, código validado; no editar)
# ═════════════════════════════════════════════════════════════════════════════

"""
Modelo: Contador de Personas V2 — Corredor de Conteo (people_counter_v2).

Port fiel de PeopleCounter_V2_Web.py (JonathanTools/areaRest/NewModels) al
contrato BaseCVModel. La V1 (people_counter, línea simple) queda intacta;
esta versión la refina con el método del box + escalera de líneas:

  - Se dibuja UN BOX de 4 puntos atravesando la puerta.
    El PRIMER lado que dibujas (clicks 1-2) = AFUERA. 'Invertir' lo voltea.
  - Dentro se genera una ESCALERA de N líneas internas.
  - Cada track tiene su PROGRESO p (0-100%) a lo largo del eje
    AFUERA→ADENTRO. Las líneas son umbrales: brincarse una entre frames
    cuenta igual (no depende del instante exacto del cruce).

REGLAS DE DECISIÓN (R1-R5):
  R1  Mayoría: cruzó 50%+1 de las líneas con dirección neta → cuenta.
  R2  Salida del box: si salió por el lado contrario al que entró y no
      había contado → cuenta por lado de salida.
  R3  Track perdido dentro del box: con mayoría → cuenta (inferido);
      sin mayoría → queda PENDIENTE (fantasma).
  R4  Id nuevo dentro del box: hereda el progreso de un fantasma
      compatible (cruce partido en dos ids por oclusión).
  R5  Se asoma y regresa por donde vino → NO cuenta.

GUARDIÁN DE LUZ:
  brillo promedio del corredor (0-255, suavizado EMA):
    > LUZ_BAJA            NORMAL
    LUZ_OSCURO..LUZ_BAJA  BAJA LUZ → conf reducida + CLAHE
    < LUZ_OSCURO          OSCURO → ALERTA "sin luz suficiente" + conteo EN PAUSA

COORDENADAS DEL BOX (§1.2 + §8): se aceptan los DOS formatos mientras convivan
los dos fronts — píxeles del frame de referencia (viejo) y normalizado 0..1
(contrato v2). El modelo es nativo en píxeles, así que la conversión que importa
es normalizado→px y se DIFIERE al primer frame, cuando ya se conoce el tamaño de
la fuente. El por qué (y por qué no puede hacerse en `configure()`) está en
`geometry.py`.

Dos entradas NO son configuración válida en ningún formato, y las dos se cortan en
`configure()` para que el usuario lo vea al aplicar y no con el stream ya corriendo:
coordenadas no finitas (NaN/Inf) -> se RECHAZA la configuración; box entero dentro
del píxel (0,0)-(1,1) con vértices enteros -> se DESCARTA con aviso, porque como
píxeles sería un corredor inerte y como 0..1 contaría el FRAME COMPLETO.

Se conserva de la V1: contador persistente + "Fijar N dentro", evidencia por
evento (foto + crop + rostro con nombre + jsonl), nube de rostro, recorte de
análisis, ByteTrack afinado.

INSTRUMENTACIÓN AÑADIDA EN LA COPIA V3 (M0). Las dos son observabilidad pura: no
tocan ninguna regla, ningún umbral y ninguna decisión de conteo (ver
`CAMBIOS_V3.md`).

  · B-4 — el fantasma de R3 guarda su `track_id`, y cuando R4 hereda se dice de
    QUIÉN: evento propio en el instante de la herencia, sufijo `heredado_de=` en
    el evento de conteo y campo `inherited_from` en el jsonl de evidencia. Sin
    esto la herencia no deja rastro en memoria y `φ_R4`/`η_R4`
    (`metrics/SPEC.md §5.1`) no son computables ⇒ H2 no es falsable.
  · B-18 — la ruta del contador persistente es inyectable por la variable
    `FULLCV_COUNTERS_DIR` o por `config["counters_dir"]`. El default es la de
    siempre (`counters/` del módulo): sin ninguna de las dos, no cambia un byte.

CAMBIOS ESTRUCTURALES DE F3. Estos SÍ reorganizan el código, y existen porque diez
cámaras con una sola instancia de YOLO no caben en la forma del V2 (`CONTEXT_V3
§2.1`). Ninguno cambia una decisión de conteo por la ruta de una cámara, y el
criterio de que eso es cierto es `prueba_igualdad.py`, no la inspección.

  · `infer_frame` partido en `preparar()` + `detectar()` + `consumir()`, del que es
    la composición exacta. La detección tenía que salir del medio del ciclo para
    poder hacerse en lote.
  · `load(yolo=..., faces=...)` — una instancia puede ADOPTAR un detector y un
    stack facial ya construidos en vez de cargar los suyos. Sin argumentos, el
    comportamiento de siempre. Lo adoptado se registra con su SHA256 y no se
    valida: ver el docstring del método, que explica por qué esa asimetría.
  · `load(pesos=...)` — la RUTA de los pesos es inyectable, con el mismo patrón que
    B-18 usó para `counters/`: parámetro, `FULLCV_YOLO_WEIGHTS`, y default. Y el
    default es **producción** (`yolo11s`), no `yolo11x`: un default que no sea el de
    producción convierte «olvidarse del parámetro» en «correr otro detector», y eso
    solo se ve leyendo el log. Los de `yolo11x` siguen existiendo en
    `_YOLO_WEIGHTS_PARIDAD`, y el único sitio que los pide es `prueba_igualdad.py`.
  · Los pesos se registran con su SHA256 **por las dos vías**, la propia y la
    adoptada. Antes la propia solo imprimía el nombre del archivo, así que había una
    ruta por la que se podía cargar un detector sin dejar rastro — y el rastro es lo
    que hace comprobable C4 sobre una corrida ya hecha. `pesos_cargados()` devuelve
    lo que se cargó de verdad, para que nadie tenga que suponerlo de la constante.
"""
# PORT-MONOLITO: __future__ va una sola vez, al inicio del archivo

import gc
import hashlib
import json
import os
import os.path as osp
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

# PORT-MONOLITO (definido arriba en este archivo): from app.contract import BaseCVModel, Detection, InferenceResult, ModelMeta
# PORT-MONOLITO (definido arriba en este archivo): from app.contract_v2 import COORDS_NORM
# PORT-MONOLITO (definido arriba en este archivo): from models.area_reid.face_bubble import FaceBubble

# PORT-MONOLITO (definido arriba en este archivo): from .geometry import DEGENERATE_MSG, check_box, parse_box, resolve_box

# ============================================================================
# PATHS
# ============================================================================
_MODULE_DIR = Path(__file__).resolve().parent
ROOT = _MODULE_DIR.parent.parent

# Los pesos de PRODUCCIÓN, y el default de `load()`. Es `yolo11s` porque es lo que
# exige C4 (`CONTEXT_V3 §1`) y porque un default que no sea el de producción es un
# footgun: cualquier llamante que se olvide del parámetro cargaría otro detector, y
# «visible en el log» no es lo mismo que «imposible». Así, olvidarse no rompe nada.
#
# La ruta se resuelve relativa al módulo (`ROOT/"weights"/…`), así que el directorio
# de al lado ES el sitio: no hay dependencia del layout de ningún otro árbol.
_YOLO_WEIGHTS = ROOT / "weights" / "yolo11s.pt"

# SOLO `prueba_igualdad.py`. NUNCA producción.
#
# El lado V2 de esa prueba vive en `Full_CV/models/people_counter_v2`, es de SOLO
# LECTURA y su `load()` no acepta ni un parámetro (`def load(self) -> None:`), así que
# su default es `yolo11x` para siempre. Igualar los dos lados es la única forma de que
# los 1852 frames idénticos signifiquen algo — y esa prueba es el único guardarraíl de
# regresión de un proyecto que no puede medir exactitud.
#
# Cambiar esto a `yolo11s` NO arregla nada: rompe el único control de paridad que hay.
# Si algún día molesta, lo que se discute es la prueba, no la constante.
_YOLO_WEIGHTS_PARIDAD = ROOT / "weights" / "yolo11x.pt"

#: Sobreescribe la ruta sin tocar código ni config. La usa el wrapper que lanza el
#: runner del arnés, que no puede conocer F3 (`metrics/CLAUDE.md` R-2).
_ENV_PESOS = "FULLCV_YOLO_WEIGHTS"
_TRACKER_LOCAL = str(_MODULE_DIR / "bytetrack_contador.yaml")
TRACKER_YAML = _TRACKER_LOCAL if osp.isfile(_TRACKER_LOCAL) else "bytetrack.yaml"

EVENTS_DIR = str(_MODULE_DIR / "events")

# DEMO: no se guarda NADA en runtime (fotos/crops/rostros de eventos, jsonl).
# Solo para debugging de devs: exportar FULLCV_DEBUG_SAVE=1.
# (Los contadores in/out sí persisten: son estado funcional, no evidencia.)
_DEBUG_SAVE = os.environ.get("FULLCV_DEBUG_SAVE", "0") == "1"

# ═══════════════════════════════════════════════════════════════════════════
#  VARIABLES DE PRUEBA (debug)
# ═══════════════════════════════════════════════════════════════════════════
# ── Corredor ───────────────────────────────────────────────────────────────
N_LINES = 5           # líneas internas de la escalera (mayoría = N//2+1)
                      #   ↑ más líneas = más evidencia requerida (más estricto)
DETECT_CLASSES = [0]  # persona (COCO)
CONF_NORMAL   = 0.25  # confianza YOLO con luz normal
CONF_BAJA_LUZ = 0.15  # confianza YOLO en baja luz (detecta en penumbra)

# ── Guardián de luz (brillo 0-255 del corredor, suavizado EMA) ────────────
LUZ_OSCURO = 35       # debajo: ALERTA + conteo en pausa
LUZ_BAJA   = 70       # debajo: aviso + conf reducida + CLAHE
LUZ_EMA    = 0.05     # suavizado (~2 s a 15-20 fps)

# ── Tracks / fantasmas (R3-R4) ─────────────────────────────────────────────
MIN_TRACK_AGE   = 4    # frames vivos antes de poder contar
TRACK_TTL       = 60   # frames sin verlo → track perdido (dispara R3)
PENDING_TTL     = 60   # frames que vive un fantasma esperando heredero (R4)
PENDING_MATCH_P = 0.35 # distancia max de progreso |p_fantasma - p_nuevo|

# ── Recorte de análisis alrededor del box ──────────────────────────────────
CROP_PAD    = 200      # margen lateral px
CROP_TOP_X  = 1.0      # margen superior extra = este factor * alto del box
CROP_MIN    = 480      # lado mínimo del recorte
# ═══════════════════════════════════════════════════════════════════════════

# ── Paleta (mejora de colores V2) — BGR ────────────────────────────────────
C_BOX_BORDE   = (235, 235, 235)   # blanco suave
C_BOX_FILL    = (180, 120,  40)   # azul acero (con alpha)
C_ESCALERA    = (200, 170, 120)   # azul claro tenue
C_AFUERA      = (  0, 190, 255)   # ámbar
C_ADENTRO     = ( 90, 200,  60)   # verde
C_PERSONA     = (255, 210,  80)   # cyan claro
C_PROG_IN     = ( 90, 200,  60)   # barra progreso entrando
C_PROG_OUT    = ( 60,  80, 230)   # barra progreso saliendo
C_TXT         = (245, 245, 245)
C_LUZ = {"NORMAL": (90, 200, 60), "BAJA": (0, 190, 255),
         "OSCURO": (60, 60, 230)}

_INFOBAR_H = 24


# ── Registro de pesos adoptados (ver `load(yolo=...)`) ─────────────────────
#: SHA256 por ruta, cacheado: con 10 cámaras adoptando el MISMO detector, el
#: archivo se hashea una vez y se imprime una vez. Una ruta nueva vuelve a
#: imprimir — que es justo el caso que este registro existe para hacer visible.
_SHA_PESOS: dict[str, str] = {}


def _registra_pesos(ruta: str, *, adoptados: bool) -> None:
    """Imprime origen y SHA256 de unos pesos. No valida nada.

    Se llama por LAS DOS vías —los que carga esta clase y los que adopta— porque la
    rama propia solo imprimía el nombre del archivo, sin hash: unos pesos cargados sin
    dejar rastro hacen C4 incomprobable a posteriori sobre una corrida ya hecha, que
    es justo lo que el hash existe para permitir.
    """
    if not ruta or not osp.isfile(ruta):
        return
    if ruta not in _SHA_PESOS:
        h = hashlib.sha256()
        with open(ruta, "rb") as f:
            for bloque in iter(lambda: f.read(1 << 20), b""):
                h.update(bloque)
        _SHA_PESOS[ruta] = h.hexdigest()
    verbo = "adoptado" if adoptados else "cargado"
    print(f"[people_counter_v2] YOLO {verbo}: {osp.basename(ruta)} "
          f"sha256={_SHA_PESOS[ruta]}")
    print(f"[people_counter_v2]   origen: {ruta}")
    print("[people_counter_v2]   NO se ha comprobado que sea el que pide la "
          "config: compárese ese hash contra C4.")


def _registra_pesos_adoptados(yolo: Any) -> str:
    """Imprime origen y SHA256 de unos pesos adoptados. No valida nada.

    Devuelve la ruta averiguada, o `""` si el objeto recibido no la expone: la usa
    `load()` para poder decir DESPUÉS qué se cargó de verdad (`pesos_cargados()`), en
    vez de que el llamante tenga que fiarse de la constante.
    """
    origen = getattr(yolo, "ckpt_path", None) or getattr(yolo, "model_name", None)
    ruta = str(origen) if origen else ""
    if not ruta or not osp.isfile(ruta):
        print("[people_counter_v2] ⚠ PESOS ADOPTADOS DE ORIGEN DESCONOCIDO: la "
              "instancia recibida no expone un `ckpt_path` legible "
              f"(origen={origen!r}). C4 —pesos yolo11s, verificado por SHA256— "
              "NO es comprobable desde aquí.")
        return ""
    _registra_pesos(ruta, adoptados=True)
    return ruta


class PeopleCounterV2Model(BaseCVModel):

    def __init__(self, meta: ModelMeta) -> None:
        self.meta = meta

        self._model = None
        self._faces: Optional[FaceBubble] = None
        self._loaded = False
        # Ruta de los pesos REALMENTE cargados, o "" si no se pudo averiguar. La
        # rellena `load()` y la lee `pesos_cargados()`: un endpoint de procedencia que
        # reporta la CONSTANTE en vez de lo cargado es peor que no tenerlo.
        self._pesos_ruta = ""

        # Configuración
        self._invert = False
        self._full_frame = False
        self.n_lines = N_LINES
        self.axis_rot = False

        # Box. Llega en píxeles (front viejo) o normalizado 0..1 (contrato v2):
        # `_regions_in` guarda lo CRUDO + el veredicto de `classify_coords`.
        #
        # INVARIANTE (cambió en la aceptación dual, y se documenta porque no tiene
        # ningún otro lector que lo sujete): `_regions_orig` es DERIVADO. Antes
        # contenía las figuras crudas truncadas a `int` desde `configure()` —ese
        # truncado ES el bug: aplastaba a CEROS cualquier box en 0..1—, así que hoy
        # está VACÍO hasta el primer frame y lo rellena `_prepare_scale` con píxeles
        # de la resolución real de la fuente. Se vacía en cada `configure()` para que
        # no pueda quedar geometría del box anterior, y se REESCRIBE en cada cambio de
        # resolución. Nadie fuera de esta clase lo lee; quien lo lea, que lea esto.
        self._regions_in: List[Dict[str, Any]] = []
        self._regions_orig: List[List[List[int]]] = []
        self._legacy_warned = False
        # Dos errores, no uno: `_zones_cfg_error` es el de CONFIGURACIÓN (aridad,
        # figura ambigua) y sobrevive a los frames; `_zones_error` es el que ve el
        # usuario y puede añadir el fallo dependiente de la RESOLUCIÓN (box que
        # colapsa sobre este frame), por lo que `_prepare_scale` lo recalcula.
        self._zones_cfg_error = "SIN CORREDOR: dibuja un box de 4 puntos"
        self._zones_error = self._zones_cfg_error

        # Contadores (persistentes)
        # B-18: dónde vive el estado persistente. Se resuelve en la INSTANCIACIÓN,
        # no en el import —esa es la diferencia con `_DEBUG_SAVE`, que por leerse a
        # nivel de módulo no se puede activar por corrida (B-15)—, y el default es
        # exactamente la ruta de siempre: sin variable ni clave de config, todo lo
        # que se lee y se escribe es idéntico a antes de este cambio.
        self._counters_dir = Path(os.environ.get(
            "FULLCV_COUNTERS_DIR", str(_MODULE_DIR / "counters")))
        self.count_in = self.count_out = self.offset = 0
        # Último "Fijar N dentro" aplicado (persistido): evita que re-mandar
        # la misma config (Iniciar, reinicios) re-fije el contador.
        self._set_n: Optional[int] = None
        self._last_toggle = False

        self._clahe = None
        self._reset_state()

    # ------------------------------------------------------------- helpers
    def _reset_state(self) -> None:
        # tid -> {"p_start","p","inside","counted","born","seen","regla"}
        self.tracks: Dict[int, dict] = {}
        self.pendings: List[dict] = []       # fantasmas R3/R4
        self.luz = 128.0
        self.luz_estado = "NORMAL"
        self._scaled = False
        self._src_wh = None                  # (W,H) con el que se armó el corredor
        self._crop_rect = None
        self._geo = None                     # geometría del corredor
        self._frame_idx = 0
        self._pending_events: List[dict] = []

    def _inv(self) -> bool:
        return bool(self._invert)

    # ------------------------------------------------------------------ load
    def load(self, *, yolo: Any = None, faces: Any = None,
             pesos: Any = None) -> None:
        """Deja la instancia lista para inferir.

        Los tres parámetros son **keyword-only**, igual que la ruta de `counters/` que
        B-18 hizo inyectable, y por la misma razón: el llamante sabe cosas que esta
        clase no puede saber.

        ## De dónde salen los pesos, en orden

        1. `pesos=` — ruta explícita. La usa `prueba_igualdad.py` para forzar los de
           paridad.
        2. `FULLCV_YOLO_WEIGHTS` — variable de entorno. La usa el wrapper que lanza el
           runner del arnés, que no puede conocer F3 (`metrics/CLAUDE.md` R-2).
        3. `_YOLO_WEIGHTS` — el default, que es **producción** (`yolo11s`). Un
           llamante que se olvide del parámetro carga lo correcto.

        Existen porque F3 (`multicam/`) necesita DIEZ instancias de esta clase
        —una por cámara, con su geometría, su contador y su tracker— pero **un
        solo YOLO**: diez instancias completas son 5.76 GB medidos y rompen C2
        (`CONTEXT_V3 §2.1`, `spike/RESULTADOS_F1 §2`).

        * `yolo` — detector ya construido que esta cámara **adopta** en vez de
          cargar el suyo.
        * `faces` — stack facial a adoptar. `None` construye el `FaceBubble` de
          siempre. F3 pasa un stub sin rostros (`CONTEXT_V3 §4.5`: SCRFD corre en
          CPU y a 10 cámaras es el cuello de botella).

        ## Lo adoptado se REGISTRA, y no se valida

        Aquí no se comprueba que el `yolo` recibido sea el que la configuración
        dice. No se puede: esta clase no conoce la config de F3. Y **un silencio
        sería peor que una comprobación imperfecta**, porque es la vía más fácil
        de incumplir C4 —pesos `yolo11s`, que se verifica por SHA256— sin que
        nadie se entere: el archivo de config diría `yolo11s`, el proceso correría
        `yolo11x`, y no habría una sola línea de log donde se viera.

        Así que se hace lo único honesto que cabe en este nivel: imprimir el
        origen y el SHA256 de los pesos que de verdad se están usando —**por las dos
        vías**, la propia y la adoptada—, y gritar cuando ni siquiera eso se pueda
        determinar. Comparar ese hash contra C4 es trabajo del llamante, que sí sabe
        qué esperaba; y `pesos_cargados()` se lo devuelve para que no tenga que
        suponerlo de una constante.
        """
        if yolo is None:
            from ultralytics import YOLO
            ruta = Path(pesos) if pesos else Path(
                os.environ.get(_ENV_PESOS) or _YOLO_WEIGHTS)
            if not ruta.exists():
                raise FileNotFoundError(f"Pesos YOLO no encontrados: {ruta}")
            print(f"[people_counter_v2] Cargando YOLO {ruta.name} ...")
            self._model = YOLO(str(ruta))
            self._pesos_ruta = str(ruta)
            _registra_pesos(str(ruta), adoptados=False)
        else:
            self._model = yolo
            self._pesos_ruta = _registra_pesos_adoptados(yolo)
        self._faces = FaceBubble() if faces is None else faces
        self._clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        self._load_counts()
        self._loaded = True

    def pesos_cargados(self) -> str:
        """Ruta de los pesos que esta instancia cargó de verdad, o `""`.

        Existe para que quien tenga que acreditar C4 —`/api/estado`, el preflight, un
        informe— no lo lea de la constante: con la ruta inyectable, la constante y lo
        cargado pueden ser cosas distintas, y un dato de procedencia que reporta la
        constante es peor que no tenerlo. `""` cuando se adoptó un detector que no
        expone su `ckpt_path`, que es la única situación en que no se puede saber.
        """
        return self._pesos_ruta

    # ---------------------------------------------------------------- unload
    def unload(self) -> None:
        self._loaded = False
        self._model = None
        self._faces = None
        self._pesos_ruta = ""
        self.tracks.clear()
        self.pendings.clear()
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass

    # ------------------------------------------------------------- configure
    def configure(self, config: Dict[str, Any]) -> None:
        # El box se parsea ANTES de tocar estado, porque `parse_box` LANZA con
        # coordenadas no finitas: así el rechazo deja la configuración anterior
        # ENTERA en vez de media aplicada (n_lines nuevo con el box viejo). Y se
        # rechaza aquí, no en el primer frame, porque diferir la conversión movió
        # el fallo a un sitio donde el usuario ya no lo ve: veía "Configuración
        # aplicada" y un stream muerto para siempre.
        regions_in, descartadas = parse_box(config.get("box", []) or [])

        self._config = config
        self._invert = bool(config.get("invert", False))
        self._full_frame = bool(config.get("full_frame", False))
        self.n_lines = max(1, min(9, int(config.get("n_lines", N_LINES))))
        self.axis_rot = bool(config.get("axis_rot", False))

        # Box: 1 región de exactamente 4 puntos. Aquí NO se convierte nada, solo
        # se clasifica el espacio de coordenadas: desnormalizar exige el tamaño
        # del frame y todavía no lo conocemos. El `int()` que estaba aquí
        # aplastaba a CEROS cualquier box normalizado, en silencio.
        self._regions_in = regions_in
        self._regions_orig = []          # DERIVADO: lo rellena el primer frame (ver __init__)
        self._check_zones(descartadas)

        self._reset_state()

        # B-18: `counters_dir` re-apunta el estado persistente en caliente. Se
        # resuelve ANTES del bloque de "Fijar N dentro" para que la detección de
        # flanco mire el estado del archivo NUEVO y no el del anterior.
        #
        # Al cambiar de ruta el estado se pone a cero y se relee desde allí: la
        # semántica es «el contador sigue a la ruta». Arrastrar lo que `load()`
        # dejó del `counters/` viejo sería peor que inútil — escribiría el conteo
        # de producción en el directorio de quien solo quería aislarse de él.
        cdir = config.get("counters_dir")
        if cdir:
            nuevo = Path(str(cdir)).expanduser()
            if nuevo != self._counters_dir:
                self._counters_dir = nuevo
                self.count_in = self.count_out = self.offset = 0
                self._set_n = None
                self._last_toggle = False
                self._load_counts()
                print(f"[people_counter_v2] Contadores en {nuevo} "
                      f"(dentro={self.inside()})")

        # Fijar "hay N personas dentro ahora" (reinicia in/out).
        # Se aplica cuando CAMBIA el número o cuando el toggle pasa de
        # OFF→ON. Re-mandar la misma config (Iniciar, reinicios, cambiar
        # otro parámetro) NO re-fija el contador ni borra in/out.
        n = int(config.get("dentro_inicial", 0))
        toggle = bool(config.get("reset_conteo", False))
        if (toggle and not self._last_toggle) or \
                (self._set_n is not None and n != self._set_n):
            self.set_inside(n)
        self._set_n = n
        self._last_toggle = toggle
        self._save_counts()

        fmt = ("—" if not self._regions_in
               else "0..1" if all(r["coords"] == COORDS_NORM
                                  for r in self._regions_in) else "px")
        print(f"[people_counter_v2] Config: corredor="
              f"{'OK' if not self._zones_error else 'NO'} "
              f"coords={fmt} "
              f"n_lines={self.n_lines} "
              f"eje {'ROTADO 90°' if self.axis_rot else 'normal'} "
              f"{'INVERTIDO' if self._inv() else 'normal'} "
              f"full_frame={self._full_frame} dentro={self.inside()}")

    # ------------------------------------------------------------------ #
    def _check_zones(self, descartadas: Tuple[int, ...] | List[int] = ()):
        # La aridad no depende del espacio de coordenadas, así que se valida ya
        # en configure() (antes de que exista un frame) y el usuario se entera
        # antes de arrancar el stream. Lo mismo el descarte por figura ambigua.
        self._zones_cfg_error = check_box(self._regions_in, descartadas)
        self._zones_error = self._zones_cfg_error
        if descartadas:
            # Se loguea UNA vez (configure() se llama una vez por configuración) y con
            # las coordenadas: sin esto, el usuario cambia el corredor por otro que el
            # modelo ignora y no hay nada en el log que explique por qué no cuenta.
            crudo = [list(self._config.get("box", []) or [])[i] for i in descartadas]
            print(f"[people_counter_v2] {DEGENERATE_MSG} figura(s) "
                  f"{[i + 1 for i in descartadas]}: {crudo}")
        elif self._zones_error:
            print(f"[people_counter_v2] {self._zones_error}")

    # ------------------------------------------------------------------ #
    #  Contadores persistentes (igual que V1)
    # ------------------------------------------------------------------ #
    def _counts_path(self) -> str:
        d = str(self._counters_dir)          # B-18 — inyectable, default de siempre
        os.makedirs(d, exist_ok=True)
        return osp.join(d, "default.json")

    def _load_counts(self):
        p = self._counts_path()
        if osp.isfile(p):
            try:
                with open(p) as f:
                    d = json.load(f)
                self.count_in  = int(d.get("in", 0))
                self.count_out = int(d.get("out", 0))
                self.offset    = int(d.get("offset", 0))
                sn = d.get("set_n")
                self._set_n = int(sn) if sn is not None else None
                self._last_toggle = bool(d.get("toggle", False))
                print(f"[people_counter_v2] Contadores restaurados: "
                      f"dentro={self.inside()} (in={self.count_in} "
                      f"out={self.count_out} inicial={self.offset})")
            except Exception as e:
                print(f"[people_counter_v2] Error leyendo contadores: {e}")

    def _save_counts(self):
        try:
            with open(self._counts_path(), "w") as f:
                json.dump({"in": self.count_in, "out": self.count_out,
                           "offset": self.offset, "set_n": self._set_n,
                           "toggle": self._last_toggle}, f)
        except Exception as e:
            print(f"[people_counter_v2] Error guardando contadores: {e}")

    def inside(self) -> int:
        return self.offset + self.count_in - self.count_out

    def set_inside(self, n: int):
        self.count_in = 0
        self.count_out = 0
        self.offset = max(0, int(n))
        self._save_counts()
        self._pending_events.append({
            "level": "info",
            "text": (f"Contador fijado: hay {self.offset} persona(s) dentro "
                     f"(entradas/salidas reiniciadas)")})
        print(f"[people_counter_v2] Contador fijado: hay {self.offset} "
              f"persona(s) dentro — contando desde ahí (in/out reiniciados)")

    def reset_state(self) -> None:
        """Reset entre turnos: además del estado temporal, pone en CERO los
        contadores persistidos en disco (semántica demo)."""
        self.set_inside(0)
        # El aviso de config heredada es por turno: si el usuario siguiente
        # también manda píxeles, quien lea el log tiene que verlo en SU turno.
        self._legacy_warned = False
        super().reset_state()

    def get_counts(self) -> dict:
        return {"inside": self.inside(), "in": self.count_in,
                "out": self.count_out, "offset": self.offset}

    # ------------------------------------------------------------------ #
    #  Geometría del corredor
    # ------------------------------------------------------------------ #
    def _needs_rescale(self, frame) -> bool:
        """¿Hay que (re)armar el corredor en píxeles? Primer frame de la fuente, o la fuente
        cambió de resolución a mitad de stream (rotación, cámara nueva): con el box en 0..1,
        quedarse con los píxeles del tamaño anterior deja el corredor corrido —a 1/3 del sitio
        al pasar de 720p a 4K— y el modelo cuenta mal SIN un solo log.

        Está extraído a un método, y no en línea dentro de `infer_frame`, para que el check de
        `tools/checks/` pueda aseverarlo sin cv2 ni pesos: en línea, ningún test lo tocaba y se
        podía revertir la condición a `not self._scaled` con los 13 asserts en verde."""
        return not self._scaled or self._src_wh != (frame.shape[1], frame.shape[0])

    def _prepare_scale(self, frame):
        H, W = frame.shape[:2]
        self._geo = None
        self._crop_rect = None
        self._src_wh = (W, H)
        # El error visible se REGENERA a partir del de configuración en cada re-armado:
        # el fallo de más abajo (box que colapsa) depende de W×H, así que si se quedara
        # pegado, una fuente que sube de 100x100 a 4K —donde el mismo box en 0..1 sí da
        # un corredor válido— arrastraría el error para siempre y no contaría nunca.
        self._zones_error = self._zones_cfg_error
        if not self._zones_cfg_error and self._regions_in:
            # AQUÍ —y solo aquí— el box pasa a píxeles: es el primer punto del
            # pipeline donde se conoce el tamaño real de la fuente. El orden de
            # los 4 puntos se preserva porque de él sale el eje (ver geometry.py).
            self._regions_orig, legacy = resolve_box(self._regions_in, W, H)
            if legacy and not self._legacy_warned:
                self._legacy_warned = True
                print(f"[people_counter_v2] box en PÍXELES (config v1): se usa "
                      f"tal cual sobre {W}x{H}. El front debería enviarlo "
                      f"normalizado 0..1 (contrato v2 §1.2), así la misma "
                      f"config sirve para cualquier resolución.")
            box = np.array(self._regions_orig[0], dtype=np.float32)
            # eje AFUERA→ADENTRO: del centro del lado 1 (clicks 1-2) al
            # centro del lado opuesto. Con axis_rot se usa el otro par de
            # lados (por si el box se dibujó empezando por el lado "largo"
            # y la escalera quedó paralela al paso).
            if self.axis_rot:
                o = (box[1] + box[2]) / 2.0
                e = (box[3] + box[0]) / 2.0
            else:
                o = (box[0] + box[1]) / 2.0
                e = (box[2] + box[3]) / 2.0
            v = e - o
            l2 = float(v @ v)
            if l2 > 1:
                n = self.n_lines
                self._geo = {"poly": box.astype(np.int32), "o": o, "v": v,
                             "l2": l2, "box": box,
                             "thr": [i / (n + 1) for i in range(1, n + 1)],
                             "mayoria": n // 2 + 1}
                print(f"[people_counter_v2] Corredor listo: {n} lineas, "
                      f"mayoria={self._geo['mayoria']}, "
                      f"eje AFUERA→ADENTRO {int(np.hypot(*v))}px"
                      + ("  [eje ROTADO 90°]" if self.axis_rot else "")
                      + ("  [INVERTIDO]" if self._inv() else ""))
            else:
                # Eje de menos de 1px: el box colapsó SOBRE ESTA RESOLUCIÓN. Ya no es
                # el caso ambiguo —esa figura la descarta `parse_box`—, sino uno de
                # estos dos: un box en píxeles cuyos dos lados opuestos coinciden (4
                # clicks casi en el mismo sitio), o un box en 0..1 tan pequeño que a
                # esta W×H cabe en un píxel (y a 4K sí valdría: por eso el mensaje
                # dice la resolución y por eso se recalcula en cada re-armado).
                #
                # El error va TAMBIÉN a `_zones_error`, no solo al log: dejarlo vacío
                # es lo que hacía que el front mostrara un plácido "Dentro: 0" con el
                # corredor desactivado — el único aviso vivía en el log del servidor,
                # que el usuario no ve. Un corredor mudo que nunca cuenta es peor que
                # un error.
                self._zones_error = (
                    f"BOX DEGENERADO sobre {W}x{H}: el corredor colapso "
                    f"(eje de {l2:.2f}px2) y esta DESACTIVADO — redibuja el box"
                )
                print(f"[people_counter_v2] BOX DEGENERADO sobre {W}x{H}: "
                      f"{self._regions_orig[0]} — eje de {l2:.2f}px², corredor "
                      f"DESACTIVADO (revisa el formato de las coordenadas)")
            if self._geo is not None and not self._full_frame:
                bh = int(box[:, 1].max() - box[:, 1].min())
                x1 = int(box[:, 0].min()) - CROP_PAD
                y1 = int(box[:, 1].min()) - CROP_PAD - int(CROP_TOP_X * bh)
                x2 = int(box[:, 0].max()) + CROP_PAD
                y2 = int(box[:, 1].max()) + CROP_PAD
                if x2 - x1 < CROP_MIN:
                    cx = (x1 + x2) // 2
                    x1, x2 = cx - CROP_MIN // 2, cx + CROP_MIN // 2
                if y2 - y1 < CROP_MIN:
                    cy = (y1 + y2) // 2
                    y1, y2 = cy - CROP_MIN // 2, cy + CROP_MIN // 2
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(W, x2), min(H, y2)
                if x2 - x1 >= 32 and y2 - y1 >= 32:
                    self._crop_rect = (x1, y1, x2, y2)
                    print(f"[people_counter_v2] YOLO analiza "
                          f"{x2-x1}x{y2-y1}px de {W}x{H} "
                          f"({100*(x2-x1)*(y2-y1)/(W*H):.0f}% del frame)")
        if self._crop_rect is None:
            print(f"[people_counter_v2] YOLO en frame completo {W}x{H}")
        self._scaled = True

    def _progress(self, pt) -> float:
        g = self._geo
        d = (np.array(pt, dtype=np.float32) - g["o"]) @ g["v"] / g["l2"]
        return float(min(1.0, max(0.0, d)))

    def _inside_box(self, pt) -> bool:
        return cv2.pointPolygonTest(self._geo["poly"],
                                    (float(pt[0]), float(pt[1])), False) >= 0

    def _lines_between(self, p_a: float, p_b: float) -> int:
        lo, hi = min(p_a, p_b), max(p_a, p_b)
        return sum(1 for t in self._geo["thr"] if lo < t <= hi)

    # ------------------------------------------------------------------ #
    #  Conteo + evidencia
    # ------------------------------------------------------------------ #
    def _count(self, hacia_adentro: bool, tid: int, frame_idx: int,
               regla: str, lineas: str, frame=None, bbox=None,
               heredado_de: Optional[int] = None):
        entrada = hacia_adentro
        if self._inv():
            entrada = not entrada
        if entrada:
            self.count_in += 1
            ev = "ENTRADA"
        else:
            self.count_out += 1
            ev = "SALIDA"
            if self.inside() < 0:
                # No puede haber menos de 0 personas dentro: una salida que
                # deja negativo implica una entrada no detectada → corregir.
                self.offset -= self.inside()
                self._pending_events.append({
                    "level": "info",
                    "text": ("Ajuste: DENTRO no puede ser negativo — "
                             "probable entrada no detectada (corregido a 0)")})
        self._save_counts()
        base = ""
        if _DEBUG_SAVE and frame is not None:
            base = self._save_evidence(ev, tid, frame, bbox, regla, lineas,
                                       heredado_de)
        # B-4: el donante va al FINAL de la cadena, nunca en medio. El
        # `RE_EVENT_TEXT` del arnés (`metrics/io/adapters.py:81-84`) y el del canal
        # de stdout están anclados solo al principio —`^ENTRADA|SALIDA … [Rk lc/n]`,
        # sin `$`—, así que un sufijo deja intacto todo el parseo que ya existe y
        # `runner/events.py::reconcile` sigue cuadrando. Metido en medio, lo rompe.
        suf = f" heredado_de={heredado_de}" if heredado_de is not None else ""
        self._pending_events.append({
            "level": "info",
            "text": (f"{ev} id={tid} [{regla} {lineas}] → "
                     f"dentro={self.inside()} "
                     f"(in={self.count_in} out={self.count_out}){suf}"),
        })
        print(f"[people_counter_v2][F{frame_idx:05d}] {ev}  id={tid}  "
              f"[{regla} {lineas}]  → dentro={self.inside()} "
              f"(in={self.count_in} out={self.count_out}){suf}"
              + (f"  foto={base}.jpg" if base else ""))

    def _save_evidence(self, ev, tid, frame, bbox, regla, lineas,
                       heredado_de=None) -> str:
        try:
            now = datetime.now()
            base = f"{now.strftime('%Y%m%d_%H%M%S')}_{ev}_id{tid}"
            d = EVENTS_DIR
            os.makedirs(d, exist_ok=True)

            full = frame.copy()
            if self._geo is not None:
                cv2.polylines(full, [self._geo["poly"]], True, C_BOX_BORDE, 2)
            color = C_PROG_IN if ev == "ENTRADA" else C_PROG_OUT
            if bbox is not None:
                x1, y1, x2, y2 = bbox
                cv2.rectangle(full, (x1, y1), (x2, y2), color, 3)
            label = (f"{ev} id{tid} [{regla} {lineas}] "
                     f"{now.strftime('%H:%M:%S')} dentro={self.inside()}")
            cv2.rectangle(full, (0, 0), (720, 42), (20, 20, 20), -1)
            cv2.putText(full, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX,
                        0.75, color, 2)
            cv2.imwrite(osp.join(d, base + ".jpg"), full)

            if bbox is not None:
                H, W = frame.shape[:2]
                x1, y1, x2, y2 = bbox
                mx, my = int((x2 - x1) * 0.2), int((y2 - y1) * 0.2)
                crop = frame[max(0, y1-my):min(H, y2+my),
                             max(0, x1-mx):min(W, x2+mx)]
                if crop.size > 0:
                    cv2.imwrite(osp.join(d, base + "_crop.jpg"), crop)

            face = self._faces.get_face(tid)
            if face is not None:
                cv2.imwrite(osp.join(d, base + "_rostro.jpg"), face)
            fname, fscore = self._faces.get_name(tid)

            meta = {
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                "evento": ev, "track_id": tid,
                "regla": regla, "lineas": lineas,
                # B-4 — `null` salvo que este track heredara de un fantasma. Es el
                # campo `inherited_from` de `pred_events/v1` (`SPEC.md §2.1`).
                "inherited_from": heredado_de,
                "luz": round(self.luz, 1),
                "dentro": self.inside(),
                "entradas": self.count_in, "salidas": self.count_out,
                "foto": base + ".jpg",
                "crop": (base + "_crop.jpg") if bbox is not None else None,
                "rostro": (base + "_rostro.jpg") if face is not None else None,
                "rostro_nombre": fname,
                "rostro_score": round(fscore, 3) if fname else None,
            }
            with open(osp.join(d, "eventos.jsonl"), "a") as f:
                f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            return base
        except Exception as e:
            print(f"[people_counter_v2] Error guardando evidencia: {e}")
            return ""

    def _log_luz_event(self, evento: str):
        if not _DEBUG_SAVE:
            return
        try:
            os.makedirs(EVENTS_DIR, exist_ok=True)
            with open(osp.join(EVENTS_DIR, "eventos.jsonl"), "a") as f:
                f.write(json.dumps({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "evento": evento, "luz": round(self.luz, 1),
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════════
    #  COSTURA — `infer_frame` partido en tres (F3, 2026-08-14)
    #
    #  Es un corte ESTRUCTURAL: no cambia una sola decisión, ni el orden de
    #  evaluación de R1-R4, ni el guardián de luz, ni la persistencia. Todo el
    #  código de las tres fases está movido verbatim desde `infer_frame`, que
    #  queda como su composición exacta y conserva firma y comportamiento
    #  (criterio de aceptación: `prueba_igualdad.py`, 1852 frames idénticos).
    #
    #  Existe porque F3 necesita 10 cámaras con UNA sola instancia de YOLO
    #  (`CONTEXT_V3 §2.1`): la detección tiene que salir del medio para poder
    #  hacerse en lote, y lo de antes y lo de después se queda por cámara.
    #
    #      preparar(frame)  →  [ lote de las N cámaras, FUERA ]  →  consumir(dets)
    #
    #  El V2 no tiene esto: es la primera divergencia ESTRUCTURAL de la copia
    #  (las de M0 eran aditivas). Ver `CAMBIOS_V3.md`.
    # ═══════════════════════════════════════════════════════════════════════
    def preparar(self, frame: np.ndarray) -> dict[str, Any]:
        """FASE 1 — todo lo anterior a detectar: escala, recorte, luz, CLAHE.

        Devuelve un contexto con `listo`:

        * `listo=False` ⇒ **esta cámara no entra en el lote** y `resultado` ya
          trae su `InferenceResult` terminado. Son los dos casos en que el V2
          tampoco detectaba: modelo sin cargar, y conteo en pausa por oscuridad.
        * `listo=True` ⇒ `proc` es el recorte listo para el detector, y `conf`,
          `ox`, `oy` y `frame_idx` hacen falta luego en `consumir`.
        """
        if not self._loaded or self._model is None:
            return {"listo": False, "resultado": InferenceResult(frame=frame)}

        frame_idx = self._frame_idx
        self._frame_idx += 1

        if self._needs_rescale(frame):
            self._prepare_scale(frame)

        # ── Recorte ────────────────────────────────────────────────────
        if self._crop_rect is not None:
            cx1, cy1, cx2, cy2 = self._crop_rect
            proc = frame[cy1:cy2, cx1:cx2]
            ox, oy = cx1, cy1
        else:
            proc = frame
            ox = oy = 0

        # ── Guardián de luz ────────────────────────────────────────────
        gris = cv2.cvtColor(proc, cv2.COLOR_BGR2GRAY)
        self.luz = (1 - LUZ_EMA) * self.luz + LUZ_EMA * float(gris.mean())
        estado_ant = self.luz_estado
        self.luz_estado = ("OSCURO" if self.luz < LUZ_OSCURO
                           else "BAJA" if self.luz < LUZ_BAJA else "NORMAL")
        if self.luz_estado != estado_ant:
            if self.luz_estado == "OSCURO":
                print(f"[people_counter_v2][F{frame_idx:05d}] ⚠ SIN LUZ "
                      f"SUFICIENTE (brillo={self.luz:.0f}) — conteo EN PAUSA")
                self._log_luz_event("PAUSA_POR_LUZ")
                self._pending_events.append(
                    {"level": "alert",
                     "text": (f"SIN LUZ SUFICIENTE (brillo {self.luz:.0f}) "
                              f"— conteo EN PAUSA")})
            elif estado_ant == "OSCURO":
                print(f"[people_counter_v2][F{frame_idx:05d}] Luz recuperada "
                      f"(brillo={self.luz:.0f}) — conteo reanudado")
                self._log_luz_event("REANUDA_POR_LUZ")
                self._pending_events.append(
                    {"level": "info",
                     "text": (f"Luz recuperada (brillo {self.luz:.0f}) — "
                              f"conteo reanudado")})
            else:
                print(f"[people_counter_v2][F{frame_idx:05d}] Luz: "
                      f"{estado_ant} → {self.luz_estado} "
                      f"(brillo={self.luz:.0f})")

        if self.luz_estado == "OSCURO":
            # No hay luz: alerta explícita, sin conteo (no contar basura)
            out = self._annotate(frame, [])
            events = self._pending_events
            self._pending_events = []
            return {"listo": False, "resultado": InferenceResult(
                frame=out,
                stats={"estado": f"SIN LUZ (brillo {self.luz:.0f}) — PAUSA",
                       "entraron": self.count_in,
                       "salieron": self.count_out,
                       "dentro": self.inside(),
                       "luz": round(self.luz, 1)},
                events=events)}

        conf = CONF_NORMAL
        if self.luz_estado == "BAJA":
            conf = CONF_BAJA_LUZ
            lab = cv2.cvtColor(proc, cv2.COLOR_BGR2LAB)
            lab[:, :, 0] = self._clahe.apply(lab[:, :, 0])
            proc = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        return {"listo": True, "proc": proc, "conf": conf,
                "ox": ox, "oy": oy, "frame_idx": frame_idx}

    def detectar(self, prep: dict[str, Any]) -> list[dict]:
        """FASE 2 — la detección de UNA cámara, tal como la hace el V2.

        F3 **no llama a esto**: hace el lote de las 10 con una sola instancia y
        su propio ByteTrack (`A3`: el tracker integrado de ultralytics no
        conserva estado por cámara con entrada en lote). La usa `infer_frame`,
        que es el camino de una cámara y el que `prueba_igualdad` compara.
        """
        proc, conf = prep["proc"], prep["conf"]
        ox, oy = prep["ox"], prep["oy"]
        res = self._model.track(proc, classes=DETECT_CLASSES, persist=True,
                                verbose=False, tracker=TRACKER_YAML)[0]
        dets = []
        if res.boxes is not None and len(res.boxes) > 0:
            xyxy = res.boxes.xyxy.cpu().numpy()
            confs = res.boxes.conf.cpu().numpy()
            ids = (res.boxes.id.cpu().numpy().astype(int)
                   if res.boxes.id is not None
                   else np.full(len(xyxy), -1, dtype=int))
            for (x1, y1, x2, y2), c, tid in zip(xyxy, confs, ids):
                if c < conf:
                    continue
                dets.append(dict(bbox=(int(x1)+ox, int(y1)+oy,
                                       int(x2)+ox, int(y2)+oy),
                                 conf=float(c), track_id=int(tid)))
        return dets

    def consumir(self, dets: list[dict], frame: np.ndarray,
                 prep: dict[str, Any]) -> InferenceResult:
        """FASE 3 — corredor, reglas R1–R4, barrido de perdidos y overlay.

        `dets` son cajas **en coordenadas del frame completo** con su
        `track_id` ya asignado. De dónde salga ese id es de quien llame: en
        `infer_frame` lo pone el ByteTrack de ultralytics; en F3, el
        `BYTETracker` propio de esa cámara. **La lógica de conteo es la misma
        en los dos casos** — es lo único que esta costura promete.
        """
        frame_idx = prep["frame_idx"]
        do_diag = (frame_idx % 30 == 1)

        # ── Corredor: progreso + reglas ────────────────────────────────
        for d in dets:
            tid = d["track_id"]
            if tid < 0 or self._geo is None:
                continue
            self._faces.update(tid, frame, d["bbox"], frame_idx)

            pt = ((d["bbox"][0] + d["bbox"][2]) / 2.0,
                  (d["bbox"][1] + d["bbox"][3]) / 2.0)
            p = self._progress(pt)
            dentro = self._inside_box(pt)
            st = self.tracks.get(tid)

            if st is None:
                st = {"p_start": p, "p": p, "inside": dentro,
                      "counted": False, "born": frame_idx,
                      "seen": frame_idx, "regla": "", "inherited_from": None}
                # R4: ¿heredero de un fantasma?
                if dentro:
                    for g in self.pendings:
                        if (frame_idx - g["frame"] <= PENDING_TTL
                                and abs(g["p"] - p) <= PENDING_MATCH_P):
                            dp = abs(g["p"] - p)
                            st["p_start"] = g["p_start"]
                            st["regla"] = "R4-heredado"
                            st["inherited_from"] = g.get("tid")   # B-4
                            self.pendings.remove(g)
                            # La herencia se emite AQUÍ, cuando ocurre, y no solo
                            # cuando acaba contando: `φ_R4` (SPEC §5.1) se define
                            # sobre TODAS las herencias. Una que nunca cuenta —o
                            # que al re-entrar acaba etiquetada R1 (B-12)— falta
                            # del denominador si el único rastro es el conteo.
                            # `dp` es la cantidad exacta sobre la que se juega H2:
                            # PENDING_MATCH_P=0.35 contra un espaciado de 0.167.
                            self._pending_events.append({
                                "level": "info",
                                "text": (f"Herencia R4: id={tid} hereda del "
                                         f"fantasma id={g.get('tid')} "
                                         f"(p_start={st['p_start']:.0%} "
                                         f"dp={dp:.3f} "
                                         f"{frame_idx - g['frame']} frames)")})
                            print(f"[people_counter_v2][F{frame_idx:05d}] "
                                  f"id={tid} hereda fantasma id={g.get('tid')} "
                                  f"(p_start={st['p_start']:.0%} dp={dp:.3f})")
                            break
                self.tracks[tid] = st

            st["seen"] = frame_idx
            prev_inside = st["inside"]

            if dentro:
                if not prev_inside:            # re-entró: nueva travesía
                    # `inherited_from` se limpia con `regla`: el `p_start` del
                    # fantasma ya no gobierna esta travesía, así que atribuirle a
                    # aquel donante un conteo posterior sería falso.
                    st.update(p_start=p, counted=False, regla="",
                              inherited_from=None)
                st["inside"] = True
                st["p"] = p
                if (not st["counted"]
                        and frame_idx - st["born"] >= MIN_TRACK_AGE):
                    lc = self._lines_between(st["p_start"], p)
                    if lc >= self._geo["mayoria"]:
                        regla = "R1" if not st["regla"] else "R4"
                        self._count(hacia_adentro=(p > st["p_start"]),
                                    tid=tid, frame_idx=frame_idx,
                                    regla=regla,
                                    lineas=f"{lc}/{self.n_lines}",
                                    frame=frame, bbox=d["bbox"],
                                    heredado_de=st.get("inherited_from"))
                        st["counted"] = True
            else:
                if prev_inside:                # salió del box → R2
                    if not st["counted"]:
                        lado_salida = p > 0.5
                        lado_entrada = st["p_start"] > 0.5
                        lc = self._lines_between(st["p_start"], p)
                        if lado_salida != lado_entrada and lc >= 1:
                            self._count(hacia_adentro=lado_salida,
                                        tid=tid, frame_idx=frame_idx,
                                        regla="R2",
                                        lineas=f"{lc}/{self.n_lines}",
                                        frame=frame, bbox=d["bbox"],
                                        heredado_de=st.get("inherited_from"))
                    st["counted"] = False      # travesía terminada
                st["inside"] = False
                st["p"] = p

            if do_diag:
                lc = self._lines_between(st["p_start"], st["p"])
                print(f"[people_counter_v2][F{frame_idx:05d}] diag id={tid} "
                      f"p={p:.0%} lineas={lc}/{self.n_lines} "
                      f"{'dentro' if dentro else 'fuera'} "
                      f"luz={self.luz:.0f}")

        # ── Tracks perdidos → R3 ───────────────────────────────────────
        for tid in list(self.tracks):
            st = self.tracks[tid]
            if frame_idx - st["seen"] > TRACK_TTL:
                if self._geo is not None and st["inside"] and not st["counted"]:
                    lc = self._lines_between(st["p_start"], st["p"])
                    if lc >= self._geo["mayoria"]:
                        self._count(hacia_adentro=(st["p"] > st["p_start"]),
                                    tid=tid, frame_idx=frame_idx,
                                    regla="R3-inferido",
                                    lineas=f"{lc}/{self.n_lines}",
                                    frame=frame, bbox=None,
                                    heredado_de=st.get("inherited_from"))
                    else:
                        # B-4: el fantasma se lleva su `tid`. Es lo único que
                        # convierte la herencia de R4 en algo medible: con esto,
                        # `inherited_from` deja de ser irrecuperable.
                        self.pendings.append({"tid": tid,
                                              "p_start": st["p_start"],
                                              "p": st["p"],
                                              "frame": st["seen"]})
                del self.tracks[tid]
        self.pendings = [g for g in self.pendings
                         if frame_idx - g["frame"] <= PENDING_TTL]
        self._faces.purge(list(self.tracks))

        # ── Overlay ────────────────────────────────────────────────────
        out = self._annotate(frame, dets)

        detections = [Detection(bbox=tuple(map(float, d["bbox"])),
                                label="persona", score=d["conf"])
                      for d in dets]

        events = self._pending_events
        self._pending_events = []

        stats: Dict[str, Any] = {
            "estado": self._zones_error or f"Dentro: {self.inside()}",
            "entraron": self.count_in,
            "salieron": self.count_out,
            "dentro": self.inside(),
            "personas": len(dets),
            "luz": round(self.luz, 1),
        }

        return InferenceResult(frame=out, detections=detections,
                               stats=stats, events=events)

    # ------------------------------------------------------------- infer_frame
    def infer_frame(self, frame: np.ndarray) -> InferenceResult:
        """El ciclo de UNA cámara: la composición exacta de las tres fases.

        Firma y comportamiento del V2, sin excepción. Lo que antes era un método
        de 180 líneas es ahora `preparar → detectar → consumir`, y nada más.
        """
        prep = self.preparar(frame)
        if not prep["listo"]:
            return prep["resultado"]
        return self.consumir(self.detectar(prep), frame, prep)

    # ================================================================
    # OVERLAY
    # ================================================================
    def _annotate(self, frame, dets):
        out = frame.copy()
        h, w = out.shape[:2]

        if self._crop_rect is not None:
            cx1, cy1, cx2, cy2 = self._crop_rect
            cv2.rectangle(out, (cx1, cy1), (cx2, cy2), (110, 110, 110), 1)

        # ── Corredor + escalera ────────────────────────────────────────
        if self._geo is not None:
            g = self._geo
            overlay = out.copy()
            cv2.fillPoly(overlay, [g["poly"]], C_BOX_FILL)
            cv2.addWeighted(overlay, 0.14, out, 0.86, 0, out)
            cv2.polylines(out, [g["poly"]], True, C_BOX_BORDE, 2)

            box = g["poly"].astype(np.float32)
            # Escalera: líneas DISCONTINUAS visibles (contorno oscuro +
            # color vivo) y numeradas — visibles sobre cualquier fondo
            for i, t in enumerate(g["thr"], 1):
                if self.axis_rot:
                    a = box[1] + (box[0] - box[1]) * t
                    b = box[2] + (box[3] - box[2]) * t
                else:
                    a = box[0] + (box[3] - box[0]) * t
                    b = box[1] + (box[2] - box[1]) * t
                segs = 9
                for s in range(segs):
                    if s % 2:
                        continue
                    q1 = a + (b - a) * (s / segs)
                    q2 = a + (b - a) * ((s + 1) / segs)
                    p1 = (int(q1[0]), int(q1[1]))
                    p2 = (int(q2[0]), int(q2[1]))
                    cv2.line(out, p1, p2, (25, 25, 25), 4)
                    cv2.line(out, p1, p2, (60, 130, 255), 2)
                num = (int(a[0]), int(a[1]))
                cv2.putText(out, str(i), (num[0] - 4, num[1] - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (25, 25, 25), 3)
                cv2.putText(out, str(i), (num[0] - 4, num[1] - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                            (60, 130, 255), 1)

            # Etiquetas: el color sigue a la etiqueta (ADENTRO siempre
            # verde, AFUERA siempre ámbar), con contorno para legibilidad
            lados   = ("AFUERA", "ADENTRO")
            colores = (C_AFUERA, C_ADENTRO)
            if self._inv():
                lados   = ("ADENTRO", "AFUERA")
                colores = (C_ADENTRO, C_AFUERA)
            if self.axis_rot:
                m1 = ((box[1] + box[2]) / 2).astype(int)
                m2 = ((box[3] + box[0]) / 2).astype(int)
            else:
                m1 = ((box[0] + box[1]) / 2).astype(int)
                m2 = ((box[2] + box[3]) / 2).astype(int)
            for txt, m, col in ((lados[0], m1, colores[0]),
                                (lados[1], m2, colores[1])):
                cv2.putText(out, txt, (m[0] - 45, m[1] - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.75, (25, 25, 25), 4)
                cv2.putText(out, txt, (m[0] - 45, m[1] - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.75, col, 2)

        # ── Personas: bbox + centroide + barra de progreso ─────────────
        for d in dets:
            tid = d["track_id"]
            x1, y1, x2, y2 = d["bbox"]
            cv2.rectangle(out, (x1, y1), (x2, y2), C_PERSONA, 2)
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            cv2.circle(out, (cx, cy), 6, (60, 60, 230), -1)
            cv2.circle(out, (cx, cy), 7, (255, 255, 255), 1)

            st = self.tracks.get(tid)
            if st is not None and self._geo is not None:
                p = st["p"]
                avanza = p >= st["p_start"]
                colb = C_PROG_IN if avanza != self._inv() else C_PROG_OUT
                bw = max(60, x2 - x1)
                by = max(60, y1 - 14)
                cv2.rectangle(out, (x1, by), (x1 + bw, by + 9),
                              (25, 28, 33), -1)
                cv2.rectangle(out, (x1, by), (x1 + int(bw * p), by + 9),
                              colb, -1)
                cv2.rectangle(out, (x1, by), (x1 + bw, by + 9),
                              (90, 95, 105), 1)
                lc = self._lines_between(st["p_start"], p)
                cv2.putText(out, f"id{tid} {p:.0%} {lc}/{self.n_lines}",
                            (x1, by - 4), cv2.FONT_HERSHEY_SIMPLEX,
                            0.5, C_TXT, 1)
            else:
                cv2.putText(out, f"id{tid}", (x1, max(15, y1 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, C_PERSONA, 1)
            self._faces.draw_bubble(out, tid, d["bbox"])

        # ── Banner: contadores + semáforo de luz ───────────────────────
        cv2.rectangle(out, (0, 0), (w, 56), (24, 27, 32), -1)
        cv2.putText(out, f"ENTRARON: {self.count_in}", (12, 38),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, C_PROG_IN, 2)
        cv2.putText(out, f"SALIERON: {self.count_out}", (w // 3, 38),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, C_PROG_OUT, 2)
        cv2.putText(out, f"DENTRO: {self.inside()}", (2 * w // 3, 38),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, C_TXT, 2)
        cl = C_LUZ.get(self.luz_estado, C_TXT)
        cv2.circle(out, (w - 130, 28), 10, cl, -1)
        cv2.putText(out, f"{self.luz:.0f}", (w - 112, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, cl, 2)
        if self.luz_estado == "OSCURO":
            cv2.rectangle(out, (0, 56), (w, 96), (40, 40, 200), -1)
            cv2.putText(out, "SIN LUZ SUFICIENTE PARA DETECTAR - "
                        "CONTEO EN PAUSA", (12, 84),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2)

        # ── Infobar inferior ───────────────────────────────────────────
        if self._zones_error:
            extra = self._zones_error
        elif self.luz_estado == "OSCURO":
            extra = f"CONTEO EN PAUSA | luz: {self.luz:.0f}"
        else:
            cw = (f"{self._crop_rect[2]-self._crop_rect[0]}x"
                  f"{self._crop_rect[3]-self._crop_rect[1]}px"
                  if self._crop_rect else "frame completo")
            extra = (f"personas: {len(dets)} | luz: {self.luz:.0f} "
                     f"({self.luz_estado}) | analisis en {cw}")
        cv2.rectangle(out, (0, h - _INFOBAR_H), (w, h), (0, 0, 0), -1)
        cv2.putText(out, extra, (10, h - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (200, 200, 200), 1, cv2.LINE_AA)

        return out


# ═════════════════════════════════════════════════════════════════════════════
# SECCIÓN DEL MODELO (adaptador al contrato Kinich)
# ═════════════════════════════════════════════════════════════════════════════

"""Adaptador: sesión Kinich sobre PeopleCounterV2Model (V3 instrumentado)."""
import logging
import os
from pathlib import Path as _PathK
from typing import Any

import numpy as np

from kinich.contract import Capability, CVModel, ModelInfo, StreamSession
from kinich.contract import Detection as KDetection
from kinich.contract import Event as KEvent

log = logging.getLogger(__name__)

# PORT-MONOLITO: fps de consumo configurable por despliegue (misma lección
# medida en queue-cashier el 2026-09-01: 10 sobre fuentes de 15 descarta
# cuadros y degrada tracks). La referencia de métricas corrió a 15.
TARGET_FPS = float(os.environ.get("KINICH_COUNT_FPS", "10"))
#: Pesos: KINICH_COUNT_YOLO > FULLCV_YOLO_WEIGHTS (env del núcleo) > default
#: del núcleo, que ya resuelve a <raíz>/weights/yolo11s.pt — el volumen de
#: Kinich cuando este archivo vive en kinich/models/. Siempre con ruta.
PESOS_ENV = os.environ.get("KINICH_COUNT_YOLO", "")
#: Conteo persistente: base por env; cada sesión escribe en <base>/<contador_id>.
COUNTERS_BASE = os.environ.get("KINICH_COUNT_COUNTERS", "/data/corridor-counter")

CONFIG_SCHEMA = {
    "type": "object",
    "required": ["box"],
    "properties": {
        "box": {
            "type": "array",
            "description": ("UNA región de exactamente 4 puntos [[x,y]x4] que "
                            "atraviesa la puerta/corredor; el PRIMER lado = "
                            "AFUERA. Píxeles del frame del stream, o 0..1 "
                            "(se detecta solo). El recall medido exige que el "
                            "box NO solape objetos fijos (cae de 98.8% a ~60%)"),
        },
        "n_lines": {"type": "integer", "default": 5, "description": "escalera interna (mayoría = N//2+1)"},
        "invert": {"type": "boolean", "default": False, "description": "voltea AFUERA/ADENTRO"},
        "axis_rot": {"type": "boolean", "default": False, "description": "usa el otro par de lados como eje"},
        "full_frame": {"type": "boolean", "default": False, "description": "analiza el frame completo"},
        "dentro_inicial": {"type": "integer", "default": 0, "description": "punto de partida del contador DENTRO"},
        "reset_conteo": {"type": "boolean", "default": False, "description": "re-aplica dentro_inicial (in/out a 0)"},
        "contador_id": {"type": "string", "default": "default",
                        "description": ("id ESTABLE del contador (p.ej. la cámara): el conteo "
                                        "persiste en <KINICH_COUNT_COUNTERS>/<id> y sobrevive "
                                        "reinicios del stream. No correr dos streams con el mismo id")},
        "resumen_cada_s": {"type": "number", "default": 0, "description": "cada cuántos s emitir RESUMEN (0 = nunca)"},
    },
}


class _Session(StreamSession):
    def __init__(self, config: dict[str, Any]):
        self.core = PeopleCounterV2Model(META_FULLCV)
        self.core.load(faces=FaceBubble(), pesos=PESOS_ENV or None)
        cid = str(config.get("contador_id") or "default")
        cfg = {k: config[k] for k in ("n_lines", "invert", "axis_rot",
                                      "full_frame", "dentro_inicial", "reset_conteo")
               if k in config}
        box = config.get("box") or []
        # el cliente manda 4 puntos planos [[x,y]x4]; el núcleo espera una
        # lista de regiones [[[x,y]x4]] — se envuelve si viene plano
        if box and isinstance(box[0], (list, tuple)) and box[0] and \
                isinstance(box[0][0], (int, float)):
            box = [box]
        cfg["box"] = box
        cfg["counters_dir"] = str(_PathK(COUNTERS_BASE) / cid)
        cfg["fps"] = TARGET_FPS
        self.core.configure(cfg)
        self._prev_in = self._prev_out = None
        self._prev_luz = "NORMAL"
        self._resumen_cada = float(config.get("resumen_cada_s") or 0)
        self._ultimo_resumen = None

    def process_frame(self, frame: np.ndarray, ts: float) -> list:
        r = self.core.infer_frame(frame)
        s = r.stats
        dets = [KDetection(label=d.label, score=float(d.score), bbox=d.bbox)
                for d in r.detections]
        events: list[KEvent] = []
        ein, eout = int(s.get("entraron", 0)), int(s.get("salieron", 0))
        extra = {"dentro": s.get("dentro"), "entraron": ein, "salieron": eout}
        if self._prev_in is not None and ein > self._prev_in:
            for _ in range(ein - self._prev_in):
                events.append(KEvent(type="ENTRADA", score=1.0, ts=ts,
                                     detections=dets, extra=dict(extra)))
        if self._prev_out is not None and eout > self._prev_out:
            for _ in range(eout - self._prev_out):
                events.append(KEvent(type="SALIDA", score=1.0, ts=ts,
                                     detections=dets, extra=dict(extra)))
        self._prev_in, self._prev_out = ein, eout
        luz = getattr(self.core, "luz_estado", "NORMAL")
        if luz == "OSCURO" and self._prev_luz != "OSCURO":
            events.append(KEvent(type="CONTEO_PAUSADO", score=1.0, ts=ts,
                                 extra={"luz": s.get("luz")}))
        elif luz != "OSCURO" and self._prev_luz == "OSCURO":
            events.append(KEvent(type="CONTEO_PAUSADO_OFF", score=1.0, ts=ts,
                                 extra={"luz": s.get("luz")}))
        self._prev_luz = luz
        if self._resumen_cada > 0 and (self._ultimo_resumen is None
                                       or ts - self._ultimo_resumen >= self._resumen_cada):
            self._ultimo_resumen = ts
            events.append(KEvent(type="RESUMEN", score=0.0, ts=ts, extra=dict(s)))
        return events

    def close(self) -> None:
        try:
            self.core.reset_state()
        except Exception:  # noqa: BLE001
            pass


#: ficha mínima que el núcleo (contrato Full_CV) exige para instanciarse
META_FULLCV = ModelMeta(id="people_counter_v2", name="Contador de Personas V2 (Corredor)",
                        task="conteo")


class CorridorCounterDetector(CVModel):
    info = ModelInfo(
        name="corridor-counter",
        version="3.0",
        task=("Entradas y salidas por un corredor de conteo (box + escalera de "
              "líneas, reglas anti-oclusión R1-R5, guardián de luz); DENTRO "
              "persistente por contador_id. No es aforo instantáneo (eso es "
              "people-counter)"),
        capabilities={Capability.STREAM},
        event_types=["ENTRADA", "SALIDA", "CONTEO_PAUSADO",
                     "CONTEO_PAUSADO_OFF", "RESUMEN"],
        config_schema=CONFIG_SCHEMA,
        target_fps=TARGET_FPS,
    )

    def load(self) -> None:
        # valida que el peso exista donde el núcleo lo va a buscar (su orden:
        # KINICH_COUNT_YOLO > FULLCV_YOLO_WEIGHTS > <raíz>/weights/yolo11s.pt)
        ruta = (PESOS_ENV or os.environ.get(_ENV_PESOS, "") or str(_YOLO_WEIGHTS))
        if not _PathK(ruta).is_file():
            raise FileNotFoundError(
                f"faltan los pesos {ruta} (la copia VALIDADA; apúntalos con "
                f"KINICH_COUNT_YOLO). Cargan por sesión; se valida aquí para "
                f"que el fallo salga en el arranque, no al conectar la cámara")

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        return _Session(config or {})
