# -*- coding: utf-8 -*-
"""queue-cashier — el cruce filas↔cajero Coppel por carril, contrato Kinich.

MONOLITO autocontenido: secciones vendidas del panel fusionado Queue-Cashier
(validado 2026-08, byte a byte) + adaptador al contrato Kinich, generado
mecánicamente; ediciones únicas marcadas `PORT-MONOLITO`. Ficha del modelo al
final; `bytetrack_queue.yaml` viaja junto a este archivo. Costos de la forma
monolito (duplicación entre los 3 archivos, motores ReID/facial no
compartidos): ver README.md.
"""
from __future__ import annotations

import sys as _sys_mono
from pathlib import Path as _Path_mono

BYTETRACK_QUEUE_YAML = _Path_mono(__file__).resolve().parent / "bytetrack_queue.yaml"


# ══════ SECCIÓN VENDIDA: zonas.py (código validado 2026-08; no editar) ══════
# -*- coding: utf-8 -*-
"""
zonas.py — Protocolo de zonas UNIFICADO del proyecto Queue-Cashier.

El problema que resuelve (ver README_FUSION.md §4): hasta ahora una zona era
una lista pelada de puntos y su IDENTIDAD era su posicion en el array. Cada
detector le daba a esa posicion un significado distinto:

  - QueueAnalytics: regions[0]=FILA, regions[1]=CAJA, regions[2]=AFORO.
    Borrar el primer poligono NO renombraba nada: convertia la zona de caja
    en zona de fila, EN SILENCIO, y la maquina de estados seguia corriendo
    sin error.
  - CashierDetector: regions[i] = "Zona de cajero i+1". Borrar un poligono
    corria la numeracion y el historial guardado con "zona": 1 empezaba a
    mentir. Peor: sus veredictos vivian keyed por ese indice, asi que el
    poligono siguiente HEREDABA el veredicto (tolerancia acumulada, estado
    previo) del borrado.

A partir de este modulo una zona es un dict:

    {"id": "z1", "tipo": "FILA", "nombre": "Fila principal",
     "puntos": [[x, y], ...], "puesto": null}

  id      -> estable PARA SIEMPRE: no cambia al borrar otras zonas ni al
             reordenar. Lo asigna este modulo (primer "zN" libre) si viene
             en None; el editor lo conserva en el round-trip.
  tipo    -> decide QUE CALCULO aplica (ver TIPOS). CAJA y CAJERO son los
             dos lados del mismo mostrador, NO la misma zona: el cliente
             de frente (con cupo, mide servicio) y el trabajador que
             atiende detras (sin cupo, mide presencia e identidad).
  nombre  -> para los humanos y los reportes. Editable, con default por tipo.
  puesto  -> el JUEGO/carril al que pertenece la zona ("1", "2", ...).
             Permite VARIOS juegos de FILA-CAJA-AFORO en la misma camara
             (un analisis de filas independiente por juego, ver
             queue_cashier.py) y empareja cada zona CAJERO con SU caja:
             la señal "cajero presente" de un juego sale solo de las
             zonas CAJERO de ese puesto. None equivale a "1" para
             FILA/CAJA/AFORO (compat con todo lo ya dibujado); en una
             zona CAJERO, None significa COMODIN: su presencia habilita
             la atencion de TODOS los juegos.

En DISCO (zones_web/<source_key>.json) el formato v2 guarda las dos vistas:

    {"version": 2, "zonas": [...], "regions": [[[x,y],...], ...], ...}

"regions" se DERIVA de "zonas" (mismo orden) y se conserva para que
cualquier codigo viejo que lea el archivo siga funcionando. Un archivo
legacy (solo "regions") se convierte al leerlo con desde_legacy(), asignando
tipos por la posicion historica de cada detector (LEGACY_POR_POSICION).
"""

# ------------------------------------------------------------------ #
#  Registro de tipos: el vocabulario completo del proyecto fusionado.
#  color = BGR (OpenCV); color_css = el mismo color para el editor web.
#  multiple = si puede haber MAS de una zona de este tipo por fuente
#  (la maquina de estados de Queue espera UNA fila/caja/aforo; el
#  Cashier vigila N cajas en el mismo encuadre).
# ------------------------------------------------------------------ #
TIPOS = {
    "FILA": dict(
        nombre_default="Fila",
        color=(0, 140, 255), color_css="#ff8c00",     # naranja
        multiple=False, min_puntos=3,
        descripcion="Area donde los clientes esperan. Acumula ESPERA y "
                    "abre expediente (--min-fila)."),
    "CAJA": dict(
        nombre_default="Caja (cliente)",
        color=(0, 170, 0), color_css="#00aa00",       # verde
        multiple=False, min_puntos=3,
        descripcion="Lado del CLIENTE frente al mostrador. Acumula "
                    "SERVICIO y consume cupo (--capacidad-caja)."),
    "AFORO": dict(
        nombre_default="Aforo",
        color=(0, 0, 255), color_css="#dc0000",       # rojo
        multiple=False, min_puntos=3,
        descripcion="Conteo de ocupacion. No abre expediente ni consume "
                    "cupo; alerta al superar --limite-aforo."),
    "CAJERO": dict(
        nombre_default="Zona de cajero",
        color=(255, 200, 0), color_css="#00c8ff",     # cian (BGR 255,200,0)
        multiple=True, min_puntos=3,
        descripcion="Lado del TRABAJADOR que atiende, detras del "
                    "mostrador. Presencia + identidad (ReID/rostro); "
                    "alerta DESATENDIDA. Describe el puesto, no a la "
                    "persona: su caso mas importante es estar vacia."),
}

# Que tipos puede dibujar cada detector (por NAME). El editor filtra su
# selector con esto y el endpoint lo valida en el POST.
TIPOS_POR_DETECTOR = {
    "QUEUE_ANALYTICS": ("FILA", "CAJA", "AFORO"),
    "CAJERO": ("CAJERO",),
    # Detector COMPUESTO (queue_cashier.py): la misma camara puede llevar
    # zonas de fila Y de cajero; el compuesto las reparte por tipo a sus
    # dos sub-detectores.
    "QUEUE_CASHIER": ("FILA", "CAJA", "AFORO", "CAJERO"),
}

# Migracion de archivos legacy (lista pelada de listas de puntos): que tipo
# le toca a cada POSICION historica. None en la lista = posiciones extra sin
# convencion (no deberian existir; se descartan con aviso). El Cashier no
# tiene convencion posicional: TODOS sus poligonos son CAJERO.
LEGACY_POR_POSICION = {
    "QUEUE_ANALYTICS": ("FILA", "CAJA", "AFORO"),
    "CAJERO": None,   # None = todas las posiciones son del tipo unico CAJERO
}


def tipos_para(detector_name):
    """Tipos permitidos para un detector (tupla). Detector desconocido ->
    todos (mejor permisivo que bloquear un detector futuro)."""
    return TIPOS_POR_DETECTOR.get(detector_name, tuple(TIPOS))


def nuevo_id(existentes):
    """Primer id 'zN' libre. `existentes` = ids ya usados (cualquier
    iterable). N nunca se reutiliza hacia abajo dentro de un mismo guardado
    porque se consulta contra el conjunto completo cada vez."""
    usados = set(existentes)
    n = 1
    while f"z{n}" in usados:
        n += 1
    return f"z{n}"


def _puntos_limpios(puntos, tipo):
    """Valida y normaliza la lista de puntos de UNA zona. -> [[int,int],...]
    Lanza ValueError con mensaje apto para un 400 del endpoint."""
    minimo = TIPOS[tipo]["min_puntos"]
    if not isinstance(puntos, list) or len(puntos) < minimo:
        raise ValueError(f"zona tipo {tipo}: minimo {minimo} puntos")
    try:
        return [[int(p[0]), int(p[1])] for p in puntos]
    except (TypeError, ValueError, IndexError):
        raise ValueError(f"zona tipo {tipo}: puntos invalidos "
                         f"(se espera [[x, y], ...])")


def normalizar(zonas_crudas, detector_name=None):
    """Valida y CANONICALIZA una lista de zonas tipadas (la que manda el
    editor nuevo, o la que devuelve desde_legacy). Asigna id a las que
    vengan sin el (zonas nuevas del editor: id=None), respeta los que ya
    traen (round-trip: asi el id sobrevive a borrar/agregar otras zonas),
    valida tipo contra el detector y unicidad de los tipos no-multiple.

    -> lista NUEVA de dicts canonicos. Lanza ValueError si algo no valida.
    """
    permitidos = tipos_para(detector_name)
    out = []
    ids = set()
    vistos_unicos = set()
    for i, z in enumerate(zonas_crudas or []):
        if not isinstance(z, dict):
            raise ValueError(f"zona {i + 1}: se espera un objeto "
                             f"{{id, tipo, nombre, puntos}}")
        tipo = z.get("tipo")
        if tipo not in TIPOS:
            raise ValueError(f"zona {i + 1}: tipo desconocido {tipo!r} "
                             f"(validos: {sorted(TIPOS)})")
        if tipo not in permitidos:
            raise ValueError(f"zona {i + 1}: tipo {tipo} no permitido para "
                             f"{detector_name} (permitidos: {list(permitidos)})")
        puesto = z.get("puesto")
        puesto = str(puesto) if puesto not in (None, "") else None
        if not TIPOS[tipo]["multiple"]:
            # UNA por (tipo, juego): varios juegos de FILA-CAJA-AFORO
            # conviven si cada uno lleva su puesto ("1", "2", ...).
            # None equivale a "1" (todo lo dibujado antes de los juegos).
            clave = (tipo, puesto or "1")
            if clave in vistos_unicos:
                raise ValueError(f"ya hay una zona {tipo} en el juego "
                                 f"{clave[1]} — usa otro numero de juego")
            vistos_unicos.add(clave)
        zid = z.get("id")
        if zid is not None and not isinstance(zid, str):
            raise ValueError(f"zona {i + 1}: id invalido {zid!r}")
        if zid in ids:
            raise ValueError(f"zona {i + 1}: id duplicado {zid!r}")
        nombre = z.get("nombre") or TIPOS[tipo]["nombre_default"]
        out.append({
            "id": zid,                       # se resuelve abajo si es None
            "tipo": tipo,
            "nombre": str(nombre)[:60],
            "puntos": _puntos_limpios(z.get("puntos"), tipo),
            "puesto": puesto,
        })
        if zid is not None:
            ids.add(zid)
    for z in out:                            # ids nuevos al final: nunca
        if z["id"] is None:                  # roban un zN que otra trae
            z["id"] = nuevo_id(ids)
            ids.add(z["id"])
    return out


def desde_legacy(regions, detector_name):
    """Convierte el formato viejo (lista pelada de listas de puntos) en
    zonas tipadas, asignando el tipo por la POSICION historica de ese
    detector. Posiciones sin convencion se DESCARTAN con aviso por consola
    (nunca existieron para la logica del detector: Queue ignora regions[3+]
    hoy mismo).

    Con nombre_default numerado para los tipos multiple ("Zona de cajero 1",
    "Zona de cajero 2"), que es la etiqueta historica del Cashier."""
    convencion = LEGACY_POR_POSICION.get(detector_name)
    zonas = []
    for i, r in enumerate(regions or []):
        if convencion is None:
            tipo = tipos_para(detector_name)[0]      # Cashier: todas CAJERO
            nombre = f"{TIPOS[tipo]['nombre_default']} {i + 1}"
        elif i < len(convencion):
            tipo = convencion[i]
            nombre = TIPOS[tipo]["nombre_default"]
        else:
            print(f"[zonas] legacy: poligono {i + 1} sin convencion "
                  f"posicional en {detector_name} — descartado")
            continue
        if not r or len(r) < TIPOS[tipo]["min_puntos"]:
            print(f"[zonas] legacy: poligono {i + 1} ({tipo}) con "
                  f"{len(r or [])} punto(s) — descartado")
            continue
        zonas.append({"id": None, "tipo": tipo, "nombre": nombre,
                      "puntos": [[int(p[0]), int(p[1])] for p in r],
                      "puesto": None})
    return normalizar(zonas, detector_name)


def casar_ids(nuevas, previas):
    """Preserva ids a traves del CAMINO LEGACY (un set_zones/set_regions
    viejo manda puntos pelados y desde_legacy les asigna ids desde cero):
    toda zona nueva cuyos PUNTOS coincidan exactamente con una previa del
    mismo tipo hereda su id (y su nombre/puesto si la nueva trae defaults).
    Muta y devuelve `nuevas`. El editor nuevo no necesita esto (round-trip
    de ids), pero sin esto un guardado desde codigo viejo re-barajaria
    TODOS los ids y el historial quedaria mentiroso igual."""
    disponibles = {(z["tipo"], _clave_puntos(z["puntos"])): z
                   for z in (previas or [])}
    usados = set()
    for z in nuevas:
        prev = disponibles.get((z["tipo"], _clave_puntos(z["puntos"])))
        if prev is not None and prev["id"] not in usados:
            if z["nombre"] == TIPOS[z["tipo"]]["nombre_default"] \
                    or z["nombre"].startswith(TIPOS[z["tipo"]]["nombre_default"]):
                z["nombre"] = prev["nombre"]
            z["id"] = prev["id"]
            z["puesto"] = z["puesto"] or prev.get("puesto")
            usados.add(prev["id"])
    # tras heredar, garantizar unicidad global de los que quedaron nuevos
    ids = {z["id"] for z in nuevas if z["id"] is not None}
    for z in nuevas:
        if z["id"] is None:
            z["id"] = nuevo_id(ids)
            ids.add(z["id"])
    return nuevas


def _clave_puntos(puntos):
    return tuple((int(x), int(y)) for x, y in puntos)


def a_regions(zonas):
    """Vista legacy: lista pelada de listas de puntos, MISMO ORDEN que
    `zonas`. Es lo que se persiste como "regions" en el JSON v2 (compat) y
    lo que consume el codigo interno que trabaja por indice."""
    return [list(z["puntos"]) for z in (zonas or [])]


def por_tipo(zonas, tipo):
    """Primera zona de ese tipo, o None. Para los tipos no-multiple es LA
    zona (normalizar garantiza unicidad)."""
    for z in (zonas or []):
        if z["tipo"] == tipo:
            return z
    return None


def leer_payload(payload, detector_name):
    """Interpreta un dict LEIDO DE DISCO (zones_web/<key>.json): v2 usa
    "zonas"; legacy solo "regions" y se convierte por posicion. -> lista
    canonica (posiblemente vacia). Nunca lanza por contenido corrupto de
    UNA zona: el llamador ya decide arrancar sin zonas si esto lanza."""
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("zonas"), list):
        return normalizar(payload["zonas"], detector_name)
    return desde_legacy(payload.get("regions") or [], detector_name)


def empaquetar(zonas, **extras):
    """Payload v2 para persistir en zones_web/<key>.json: zonas tipadas +
    "regions" derivado (compat con lectores viejos) + extras del panel
    (zone_expand, created, source...)."""
    payload = {"version": 2, "zonas": list(zonas or []),
               "regions": a_regions(zonas)}
    payload.update({k: v for k, v in extras.items() if v is not None})
    return payload


def agrupar_puestos(zonas):
    """Agrupa las zonas de FILAS (FILA/CAJA/AFORO) por su juego/puesto.
    -> dict {puesto: [zonas]} con puesto None normalizado a "1". Las zonas
    CAJERO NO entran aqui (el detector de cajero maneja todas juntas; su
    puesto solo se usa para la señal por juego, ver queue_cashier.py)."""
    grupos = {}
    for z in (zonas or []):
        if z["tipo"] in ("FILA", "CAJA", "AFORO"):
            grupos.setdefault(z.get("puesto") or "1", []).append(z)
    return grupos


def tipos_publicos(detector_name):
    """Lo que el editor web necesita para su selector: [{tipo, nombre,
    color_css, multiple, descripcion}] en el orden del registro."""
    return [dict(tipo=t, nombre=TIPOS[t]["nombre_default"],
                 color_css=TIPOS[t]["color_css"],
                 multiple=TIPOS[t]["multiple"],
                 descripcion=TIPOS[t]["descripcion"])
            for t in tipos_para(detector_name)]


# PORT-MONOLITO: los consumidores usan `_zonas.<fn>`; aquí es este módulo.
_zonas = _sys_mono.modules[__name__]


# ══════ SECCIÓN VENDIDA: params_mixin.py (código validado 2026-08; no editar) ══════
# -*- coding: utf-8 -*-
"""
params_mixin.py
===============
Protocolo OPCIONAL (framework Base) de parametros ajustables en caliente
desde el panel web (web_server.py). Un detector que quiera exponer sus
parametros hereda de ParamsMixin y declara PARAMS:

    class MiDetector(ParamsMixin, BaseDetector):
        PARAMS = [
            dict(key="conf", label="Confianza YOLO", type="float",
                 min=0.05, max=0.95, step=0.01,
                 help="Umbral minimo de deteccion"),
            dict(key="modo", label="Modo", type="choice",
                 choices=["a", "b"]),
            dict(key="invert", label="Invertir sentido", type="bool"),
        ]

Cada entrada:
  key      : nombre del parametro (y del atributo en self, salvo 'attr')
  attr     : (opcional) nombre del atributo si difiere de key
  label    : etiqueta que ve el usuario
  type     : "float" | "int" | "bool" | "choice"
  min/max  : clamp para float/int (opcional)
  step     : paso sugerido para la UI (opcional)
  choices  : lista de strings (solo type="choice")
  help     : descripcion corta (opcional)

El servidor llama:
  get_params()      → lista de specs + "value" actual (para pintar el form)
  set_params(dict)  → aplica con cast/clamp; retorna {"applied","errors"}
                      y dispara on_params_changed(set_de_keys) SOLO con las
                      keys cuyo valor realmente cambio (el form del panel
                      manda todos los campos en cada guardado).

Los valores guardados se persisten en params_web/<source_key>.json y se
re-aplican al agregar la fuente (tras configure(), antes de setup()).
"""

from typing import Dict, List


class ParamsMixin:
    PARAMS: List[dict] = []

    # ------------------------------------------------------------------ #
    def get_params(self) -> List[dict]:
        out = []
        for p in self.PARAMS:
            q = {k: v for k, v in p.items() if k != "attr"}
            q["value"] = getattr(self, p.get("attr", p["key"]), None)
            out.append(q)
        return out

    # ------------------------------------------------------------------ #
    def set_params(self, data: Dict) -> Dict:
        applied, errors = {}, {}
        changed = set()
        for p in self.PARAMS:
            key = p["key"]
            if key not in data:
                continue
            raw = data[key]
            typ = p.get("type", "float")
            try:
                if typ == "int":
                    val = int(float(raw))
                elif typ == "float":
                    val = float(raw)
                elif typ == "bool":
                    val = raw if isinstance(raw, bool) else \
                        str(raw).strip().lower() in ("1", "true", "on", "si", "sí")
                elif typ == "choice":
                    val = str(raw)
                    if val not in p.get("choices", []):
                        raise ValueError(f"opciones: {p.get('choices')}")
                else:
                    raise ValueError(f"tipo desconocido: {typ}")
            except (TypeError, ValueError) as e:
                errors[key] = f"valor invalido ({e})"
                continue

            if typ in ("int", "float"):
                if "min" in p and val < p["min"]:
                    val = p["min"]
                if "max" in p and val > p["max"]:
                    val = p["max"]

            attr = p.get("attr", key)
            old = getattr(self, attr, None)
            setattr(self, attr, val)
            applied[key] = val
            if old != val:          # solo lo que REALMENTE cambio de valor
                changed.add(key)    # (el form manda todos los campos juntos)

        if changed:
            try:
                self.on_params_changed(changed)
            except Exception as e:
                errors["_hook"] = str(e)

        return {"applied": applied, "errors": errors}

    # ------------------------------------------------------------------ #
    def on_params_changed(self, changed: set):
        """Hook opcional: recalcular estado derivado de los parametros."""
        pass


# ══════ SECCIÓN VENDIDA: base_detector.py (código validado 2026-08; no editar) ══════
# -*- coding: utf-8 -*-
"""
BaseDetector: clase base para cualquier solucion (Fire, Crash, Fight, ...).

Para crear una solucion nueva SOLO necesitas:

    from base_detector import BaseDetector
    from runner import main

    class MiDetector(BaseDetector):
        NAME = "MI_SOLUCION"

        def setup(self):
            # cargar modelos (YOLO, CLIP, etc.) UNA sola vez
            ...

        def process_frame(self, frame, frame_idx):
            # tu inferencia. Devuelve un dict con el resultado:
            return {
                "alert": False,            # True dispara snapshot de alerta
                "label": "",               # etiqueta de la alerta
                "score": None,             # score 0..1 opcional
                "boxes": [],               # [(x1,y1,x2,y2,label,conf), ...]
                "extra": "",               # texto libre para la barra inferior
            }

    if __name__ == "__main__":
        main(MiDetector())

Todo lo demas (video/RTSP, reconexion, grabacion, MJPEG, alertas, CLI)
lo pone el runner.
"""

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
    #  ZONAS TIPADAS (protocolo unificado Queue-Cashier, ver Base/zonas.py):
    #  cada zona es {"id","tipo","nombre","puntos","puesto"} con id ESTABLE
    #  (sobrevive a borrar/reordenar) y tipo que decide QUE CALCULO aplica.
    #  Un detector que las soporte declara ZONA_TIPOS y sobreescribe los
    #  dos hooks; el panel (web_server_zones.py) llama set_zonas() con la
    #  lista canonica ya validada, y cae a set_zones()/set_regions()
    #  legacy si el detector no lo implementa.
    # ------------------------------------------------------------------ #
    ZONA_TIPOS = ()   # tipos que este detector sabe procesar (informativo;
                      # la fuente de verdad es zonas.TIPOS_POR_DETECTOR)

    def get_zonas(self):
        """Zonas tipadas actuales (lista canonica) o None si no soportado."""
        return None

    def set_zonas(self, zonas):
        """Aplica zonas tipadas en caliente. Devuelve True si soportado.
        zonas=[] significa SIN zonas (reposo/stream limpio, segun detector)."""
        return False

    # ------------------------------------------------------------------ #
    #  ROI editable (opcional, LEGACY): si la solucion trabaja con regiones,
    #  sobreescribe estos dos hooks y el panel web mostrara el editor.
    # ------------------------------------------------------------------ #
    def get_regions(self):
        """Poligonos actuales [[[x,y],...], ...] o None si no aplica."""
        return None

    def set_regions(self, regions):
        """Aplica nuevos poligonos en caliente. Devuelve True si soportado.
        regions=None significa SIN ROI: el detector queda en reposo y el
        stream se muestra limpio (sin overlay)."""
        return False

    # ------------------------------------------------------------------ #
    #  Registro de empleados (opcional): si la solucion mantiene una base
    #  de identidades, sobreescribe estos hooks y el panel web mostrara
    #  el registro por segmento de stream y por fotos.
    # ------------------------------------------------------------------ #
    def start_enroll(self, name, seconds, use_roi):
        """Inicia captura de vistas desde el stream en vivo. True si soportado."""
        return False

    def enroll_status(self):
        """Estado del registro en curso: dict con al menos {'active': bool}."""
        return {"active": False}

    def enroll_photos(self, name, images):
        """Registra desde una lista de imagenes BGR. -> (ok, mensaje)."""
        return False, "Este detector no soporta registro"

    def reload_db(self):
        """Recarga la base de identidades (tras un registro nuevo)."""

    def list_identities(self):
        """Identidades registradas: [{'name','views','thumb'(b64 jpg|None)}]
        o None si el detector no maneja identidades."""
        return None

    def delete_identity(self, name):
        """Elimina una identidad de la base (archivo + estado en caliente).
        Devuelve True si se eliminó."""
        return False

    def get_identity_views(self, name):
        """Vistas individuales (cuerpo y rostro) de una identidad, para
        revisión humana. -> {'body': [{'idx','thumb_b64','coherence'}, ...],
        'face': [...]} o None si el detector no soporta esto."""
        return None

    def delete_identity_view(self, name, kind, index):
        """Elimina UNA vista específica (kind='body'|'face') de la galería
        de `name`. Devuelve True si se eliminó."""
        return False

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


# ══════ SECCIÓN VENDIDA: face_engine.py (código validado 2026-08; no editar) ══════
# -*- coding: utf-8 -*-
"""
face_engine.py — Identidad facial para corroborar al ReID corporal.

Filosofía adoptada de ZonaRestringida_ReID (face_id.py), con el stack
estándar open-source en lugar del face_runner privado:

  1. SCRFD (InsightFace buffalo_l)  → detección de rostro
  2. ArcFace w600k_r50              → embedding 512-d L2-normalizado
  3. Coseno CRUDO vs galería facial del empleado (ArcFace se compara
     directo, sin re-centrado — a diferencia del TransReID corporal)

El motor se carga UNA sola vez por proceso y se comparte entre todas las
fuentes (mismo patrón que ZonaRestringida). Si insightface o los pesos no
están disponibles, get_face_engine() devuelve None y el detector sigue
operando solo con ReID corporal.
"""

import threading
from typing import Optional

import numpy as np

FACE_MIN_CROP = 40        # lado mínimo del crop de persona para intentar rostro
FACE_DET_SCORE = 0.50     # confianza mínima de la detección de rostro

_engine = None
_engine_lock = threading.Lock()


def get_face_engine():
    """Singleton compartido entre fuentes. None si el stack no está disponible."""
    global _engine
    with _engine_lock:
        if _engine is None:
            try:
                _engine = _FaceEngine()
            except Exception as e:  # noqa: BLE001
                print(f"[FACE] Stack facial NO disponible ({e}) — "
                      f"se opera solo con ReID corporal")
                _engine = False
    return _engine or None


class _FaceEngine:
    def __init__(self):
        import torch  # noqa: F401  (libs CUDA para onnxruntime)
        from insightface.app import FaceAnalysis
        print("[FACE] Cargando InsightFace buffalo_l (SCRFD + ArcFace)...")
        self.app = FaceAnalysis(
            name="buffalo_l",
            allowed_modules=["detection", "recognition"],
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        self.app.prepare(ctx_id=0, det_size=(320, 320))
        self._lock = threading.Lock()   # una inferencia a la vez (compartido)
        print("[FACE] Listo (512-d, compartido entre fuentes)")

    def extract(self, crop_bgr) -> Optional[np.ndarray]:
        """Crop BGR de una PERSONA → embedding facial 512-d L2 (o None si no
        hay rostro utilizable). Toma el rostro MÁS GRANDE del crop."""
        if crop_bgr is None or crop_bgr.size == 0:
            return None
        h, w = crop_bgr.shape[:2]
        if w < FACE_MIN_CROP or h < FACE_MIN_CROP:
            return None
        with self._lock:
            faces = self.app.get(crop_bgr)
        best, area = None, 0.0
        for f in faces:
            if float(f.det_score) < FACE_DET_SCORE:
                continue
            x1, y1, x2, y2 = f.bbox
            a = max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))
            if a > area:
                best, area = f, a
        if best is None:
            return None
        e = best.normed_embedding.astype(np.float64)
        if not np.isfinite(e).all():
            return None
        return e


# ── PORT-MONOLITO · G1: snapshot del entorno de hilos y cv2 (la sección del
# cajero los fija al ejecutarse); se restaura abajo — el pod manda.
import os as _os_g1
_g1_sin_definir = [v for v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                               "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                               "VECLIB_MAXIMUM_THREADS") if v not in _os_g1.environ]
try:
    import cv2 as _cv2_g1
    _g1_hilos = _cv2_g1.getNumThreads()
except Exception:
    _g1_hilos = None


# ══════ SECCIÓN VENDIDA: cashier_detector.py (código validado 2026-08; no editar) ══════
# -*- coding: utf-8 -*-
"""
CashierDetector V4: port del paquete CashierPresence_V4 al framework Base.

Cambios V4 (vs el port V3 anterior):
  - TODO el procesamiento a resolución fija 800x600 (ROIs y cajas viven en
    esas coordenadas; el stream de salida es 800x600)
  - Inferencia con los modelos ONNX del paquete (onnx/yolo11n-pose.onnx vía
    ultralytics+onnxruntime, onnx/transreid_msmt.onnx vía onnxruntime directo
    con el preprocesamiento cv2 validado en export_onnx_v4.py)
  - Piso de re-escalado de crops ReID: <24x48 se omite; 24..64 x 48..128 se
    sube a 64x128 (cúbico) antes del transform
  - Anti-deadlock del warmup: con base chica (<15 vistas) se ciclan las vistas
    para operar el re-centrado desde el arranque
  - SIN ROI = stream limpio: set_regions(None) deja el detector en reposo y
    el video se muestra sin overlay alguno
  - Registro de empleados EN el detector: por segmento del stream en vivo
    (start_enroll / enroll_status) o por fotografías (enroll_photos), con el
    mismo filtro de coherencia y selección diversa de enroll_employee_v4.py

Algoritmo (idéntico a V3, validado): ROI fijo -> recorte + máscara estricta ->
YOLO11n-pose + ByteTrack -> filtros de persona -> TransReID vs empleados/*.npz
-> integrador temporal -> estado. Alerta por flanco al entrar a DESATENDIDA.

Uso CLI:
    uv run cashier_detector.py --source video.mov --regions rois/mi_roi.json --save
    (sin --regions: stream limpio hasta que se dibuje un ROI en el panel web)
"""

import json
import os
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

# El paralelismo REAL de esta plataforma ya vive en el nivel de cámaras (un
# hilo de Python por fuente, ver Base/web_server.py SourceWorker). Sin esto,
# OpenBLAS/OpenMP disparan su PROPIO pool de tantos hilos como núcleos tenga
# la máquina cada vez que un hilo de cámara llama a numpy/cv2 por primera
# vez — con 10 cámaras en una máquina de 128 núcleos eso son miles de hilos
# nativos peleando por 128 núcleos reales (medido: 5 cámaras -> 1109 hilos,
# 380% CPU solo de sobrecarga de contexto). Debe fijarse ANTES de importar
# numpy/cv2 para que la librería lo respete desde su primera inicialización.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

import cv2
import numpy as np

cv2.setNumThreads(1)   # mismo motivo: el paralelismo de OpenCV ya sobra aquí

HERE = Path(__file__).resolve().parent
# FUSION Queue-Cashier: el proyecto es PLANO (detector, panel y Base/ conviven
# en esta misma carpeta), a diferencia del despliegue original donde este
# archivo vivia en Cashier/ y Base/ era su hermano. Ver README_FUSION.md.
# PORT-KINICH: sin sys.path.insert — el paquete usa imports relativos (G7)
# sys.path.insert(0, str(HERE / "Base"))
# Pesos: modelos/ (yolo11s.pt real; onnx/ es un enlace al TransReID validado,
# 372 MB que no se duplican). La base de empleados NO vive ahi: es dato vivo y
# ESCRIBIBLE (el registro escribe .npz), con copia propia en empleados/ para
# no tocar jamas la del proyecto original.
V4_DIR = HERE / "modelos"
EMPLEADOS_DIR = HERE / "empleados"

# PORT-MONOLITO (definido arriba en este archivo): from .base_detector import BaseDetector  # noqa: E402  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)
# PORT-MONOLITO (definido arriba en este archivo): from .params_mixin import ParamsMixin  # noqa: E402  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)
# PORT Full_CV: runner.py (el arnés CLI del framework Base) NO viaja en este
# paquete — la plataforma llama al detector vía model.py, no por línea de
# comandos. `main` solo se usaba en el bloque __main__ del final.
try:
    from runner import main  # noqa: E402
except ImportError:          # noqa: E402  (paquete portado, sin runner)
    main = None
# Protocolo de zonas TIPADAS (fusion Queue-Cashier, ver Base/zonas.py): cada
# zona es {"id","tipo","nombre","puntos"} con id ESTABLE. Antes, los
# veredictos de este detector vivian keyed por el INDICE del poligono
# (1-based): borrar el primero hacia que el siguiente HEREDARA su veredicto
# (tolerancia acumulada, estado previo). Con ids eso ya no puede pasar:
# _podar_verdicts olvida los ids que desaparecen y los que sobreviven
# conservan el suyo.
# PORT-MONOLITO (definido arriba en este archivo): from . import zonas as _zonas  # noqa: E402  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)
# Donde persiste el panel las zonas por fuente (web_server_zones.py define
# el mismo <dir_del_panel>/zones_web; aqui se replica la ruta en vez de
# importar ese modulo para no arrastrar Flask a un arranque por CLI).
ZONES_WEB_DIR = HERE / "zones_web"


# =============================================================================
# CONFIG (constantes V4 — CashierPresence_Production_V4.py)
# =============================================================================
PROC_W, PROC_H = 800, 600     # V4: resolución fija de procesamiento

# Personas / tracking
PERSON_CONF = 0.10            # detección full-frame (registro sin ROI)
PERSON_CONF_RUN = 0.20        # detección en el ROI (runtime)
PERSON_IOU = 0.45
PERSON_IMGSZ = 320
YOLO_FPS = 3.0
KPT_MIN_MEAN = 0.15           # calibrado PARA máscara estricta + margen 0
ROI_DETECT_MARGIN_FRAC = 0.0
TRACK_DEATH_S = 5.0
GHOST_HIDE_S = 0.7
STRICT_ROI = True
ROI_OVERLAP_FRAC = 0.25

# ReID (TransReID ONNX)
REID_INPUT = (256, 128)       # (H, W) de entrada del grafo
REID_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
REID_STD = np.array([0.229, 0.224, 0.225], np.float32)
REID_MIN_W, REID_MIN_H = 64, 128     # talla destino del upscale de crops chicos
REID_FLOOR_W, REID_FLOOR_H = 24, 48  # por debajo de esto el crop se OMITE
REID_CENTER_WARMUP = 15
GALLERY_SIZE = 5
ENRICH_EVERY_S = 2.0
POOL_TTL_S = 1800.0
POOL_MAX = 4

# Base de empleados
DB_MATCH_TH = 0.65   # umbral para el modo default 'zona_crudo' (coseno CRUDO
                     # 768-d + media sobre buffer + fracción). CALIBRADO CON
                     # DATOS reales de impostor (2026-08-16, video de pasillo
                     # sin nadie registrado): media impostor +0.50, p95=0.61,
                     # p99=0.64 — con el 0.55 anterior, 36% de los intentos
                     # individuales de un DESCONOCIDO cruzaban el umbral (y con
                     # ~1 intento/s, la prob. de al menos 1 acierto falso en
                     # 10s era ~99%). 0.65 deja margen sobre el p99 medido.
                     # OJO: si se cambia --reid-mode a 'v4_recentrado', esta
                     # escala NO aplica — ahí el umbral calibrado es 0.20-0.30
DB_CHECK_COOLDOWN_S = 1.0
DB_MAX_CHECKS = 10

# Lógica de identidad estilo ZonaRestringida_ReID (modo 'zona_crudo')
REID_MODES = ["zona_crudo", "v4_recentrado"]
FEAT_BUFFER_MAX = 12          # vistas acumuladas por track (evidencia)
MIN_FRACTION = 0.30           # fracción mínima de comparaciones sobre el umbral
GRAY_ZONE_FACTOR = 0.85       # score >= th*0.85: "zona gris" — podría ser esa
                              # persona pero falta evidencia: NO consume intento

# Peso del ROSTRO en la identidad (el cuerpo = ropa; el rostro = la persona)
FACE_RECHECK_S = 1.0          # cadencia de chequeo facial por track (TODOS los
                              # estados, incluso VERIFICADO: vigilancia continua)
FACE_REVOKE_TH = 0.20         # rostro visible con sim < esto vs el empleado
                              # asignado = evidencia de que NO es esa persona
FACE_DISAGREE_N = 3           # desacuerdos faciales consecutivos para REVOCAR
                              # una verificación corporal
FACE_GRACE_S = 4.0            # prioridad facial: el cuerpo ESPERA estos segundos
                              # al inicio de cada track (el rostro decide primero)
FACE_EVIDENCE_TTL = 5.0       # el último rostro leído del track vale como
                              # evidencia durante estos segundos: un candidato
                              # corporal (base O pool) cuyo nombre contradiga
                              # esa evidencia (sim < FACE_REVOKE_TH) se rechaza
                              # ANTES de verificar — sin esperar los 3 strikes

# Veredicto temporal
RISE_SECONDS, TOLERANCE_SECONDS, ATTEND_CONFIRM = 1.5, 10.0, 0.50
MAJORITY_SECONDS, MAJORITY_FRAC = 0.30, 0.60

# Registro de empleados (enroll_employee_v4.py)
ENROLL_K = 16                 # vistas por empleado
ENROLL_SAMPLE_FPS = 2.0       # muestreo dentro del tramo
ENROLL_COHERENCE_TH = 0.15    # umbral del filtro anti-contaminación (cuerpo)
FACE_COHERENCE_TH = 0.30      # idem para rostro — EN COSENO CRUDO, sin
                              # re-centrar (ArcFace ya discrimina en crudo;
                              # re-centrar como el cuerpo distorsiona la
                              # separación real). Validado 2026-08-16 contra
                              # la galería real de Israel: aísla exactamente
                              # los 4 rostros mal capturados (borrosos/ángulo)
                              # que un desconocido explotaba para "verificar"
                              # por rostro pese al umbral alto (ver
                              # _filter_coherent_faces)
ENROLL_MIN_VIEWS = 3

_STATE_STYLE = {
    "CAJERO_ATENDIENDO": ((30, 140, 60),  "EMPLEADO ATENDIENDO"),
    "TOLERANCIA":        ((0, 170, 200),  "AUSENCIA BREVE (tolerancia)"),
    "PERSONA_NO_CAJERO": ((0, 90, 200),   "PERSONA NO REGISTRADA"),
    "PERSONA_NO_RECONOCIDA": ((0, 60, 220), "PERSONA EN ZONA DE CAJERO, NO RECONOCIDA"),
    "DESATENDIDA":       ((0, 0, 200),    "CAJA DESATENDIDA"),
    "SIN_ROI":           ((60, 60, 60),   "SIN ROI"),
}
_TRACK_COLOR = {"VERIFICADO": (0, 220, 100), "PENDIENTE": (180, 180, 180),
                "DESCONOCIDO": (0, 90, 230)}

# Etiqueta corta para rotular cada poligono sin taparlo (el texto largo de
# _STATE_STYLE solo cabe en el banner superior).
_STATE_CORTO = {
    "CAJERO_ATENDIENDO": "ATENDIDA",
    "TOLERANCIA": "AUSENCIA BREVE",
    "PERSONA_NO_CAJERO": "NO REGISTRADA",
    "PERSONA_NO_RECONOCIDA": "NO RECONOCIDA",
    "DESATENDIDA": "DESATENDIDA",
    "SIN_ROI": "SIN ROI",
}
# De PEOR a mejor: el estado de la CAMARA es el peor de sus zonas, para que
# una caja sola no quede tapada por otra que si esta atendida.
_PRIORIDAD_ESTADO = ("DESATENDIDA", "PERSONA_NO_RECONOCIDA", "PERSONA_NO_CAJERO",
                     "TOLERANCIA", "CAJERO_ATENDIENDO")


def _peor_estado(estados) -> str:
    """El mas grave de una coleccion de estados de zona."""
    for e in _PRIORIDAD_ESTADO:
        if e in estados:
            return e
    return "DESATENDIDA"


def _fmt_perm(segundos: float) -> str:
    """Permanencia en formato corto para el overlay: M:SS, o H:MM:SS al pasar
    la hora. Es tiempo VIRTUAL de video (frames / eff_fps), el mismo reloj que
    usa el resto del detector: en RTSP en vivo equivale al reloj de pared, en
    un video subido son segundos de video. Solo ASCII (cv2 no dibuja acentos).
    """
    s = int(max(0.0, segundos))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _rescale(frame):
    """Resize incondicional a 800x600 (V4: todo el pipeline vive ahí)."""
    if frame.shape[1] == PROC_W and frame.shape[0] == PROC_H:
        return frame
    interp = cv2.INTER_AREA if frame.shape[1] > PROC_W else cv2.INTER_CUBIC
    return cv2.resize(frame, (PROC_W, PROC_H), interpolation=interp)


# =============================================================================
# GEOMETRÍA DEL ROI (coordenadas 800x600)
# =============================================================================
def roi_bbox(regions, shape_hw, margin_frac=ROI_DETECT_MARGIN_FRAC):
    H, W = shape_hw
    if not regions:
        return None
    pts = np.concatenate([np.asarray(r, np.float32).reshape(-1, 2) for r in regions], axis=0)
    x0, y0 = pts[:, 0].min(), pts[:, 1].min()
    x1, y1 = pts[:, 0].max(), pts[:, 1].max()
    mx, my = margin_frac * (x1 - x0), margin_frac * (y1 - y0)
    x0 = int(max(0, x0 - mx)); y0 = int(max(0, y0 - my))
    x1 = int(min(W, x1 + mx)); y1 = int(min(H, y1 + my))
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _box_in_regions(box, regions_mask, min_frac: float = ROI_OVERLAP_FRAC):
    x1, y1, x2, y2 = box.astype(int)
    H, W = regions_mask.shape
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(W - 1, x2), min(H - 1, y2)
    if x2 <= x1 or y2 <= y1:
        return False
    frac = float((regions_mask[y1:y2, x1:x2] > 0).mean())
    return frac > 0.0 if min_frac <= 0 else frac >= min_frac


def _box_zone(box, label_mask, min_frac: float = ROI_OVERLAP_FRAC):
    """Zona (1-based) que MAS cubre a la caja, o None si no llega al solape
    minimo. `label_mask` guarda el indice del poligono +1 en cada pixel
    (0 = fuera del ROI).

    El criterio de dentro/fuera es EXACTAMENTE el de _box_in_regions —se
    decide con la cobertura TOTAL del ROI— y solo despues se elige a que
    zona pertenece. Con un unico poligono el comportamiento es identico al
    de antes."""
    x1, y1, x2, y2 = box.astype(int)
    H, W = label_mask.shape
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(W - 1, x2), min(H - 1, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    sub = label_mask[y1:y2, x1:x2]
    frac = float((sub > 0).mean())
    if not (frac > 0.0 if min_frac <= 0 else frac >= min_frac):
        return None
    counts = np.bincount(sub.ravel())
    counts[0] = 0                      # el fondo no compite por la zona
    return int(counts.argmax()) or None


# =============================================================================
# ReID: TransReID vía ONNX Runtime (preprocesamiento cv2 de export_onnx_v4.py)
# =============================================================================
def _head_region(person_crop):
    """Recorte del tercio superior del cuerpo (donde está la cabeza), con
    upscale 2x si es chico. Compartido por _face_from_crop (identificación en
    vivo) y el registro (para guardar una miniatura VISIBLE del rostro)."""
    if person_crop is None or person_crop.size == 0:
        return None
    h = person_crop.shape[0]
    head = person_crop[:max(1, int(0.45 * h))]
    if head.size == 0:
        return None
    if max(head.shape[:2]) < 160:
        head = cv2.resize(head, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return head


def _face_from_crop(faces_engine, person_crop):
    """Extracción facial orientada a la CABEZA (filosofía face_bubble de
    ZonaRestringida): busca el rostro solo en el tercio superior del cuerpo —
    multiplica la tasa de aciertos frente a buscar en el crop completo."""
    if faces_engine is None:
        return None
    head = _head_region(person_crop)
    if head is None:
        return None
    return faces_engine.extract(head)


class ReIDEngine:
    """Embeddings TransReID (ONNX) + re-centrado con warmup + galerías.

    El coseno CRUDO da ~0.99 a todo; solo tras restar la media corriente las
    distancias significan algo. score(track, galería) = MÁXIMO sobre galería."""

    def __init__(self, weights_onnx: Path):
        self.ok = False
        self._lock = threading.Lock()   # embed() puede llamarse desde el hilo
                                        # del worker Y del registro por fotos
        # bookkeeping de siembra: existe SIEMPRE (incluso sin pesos) porque
        # esta instancia puede compartirse entre varias camaras (ver
        # get_shared_reid_engine) y delete_identity() la referencia igual
        self.seeded_names: set = set()
        self._seed_lock = threading.Lock()
        if not Path(weights_onnx).exists():
            print(f"[REID] SIN modelo ONNX ({weights_onnx}) — identidad INACTIVA")
            return
        import torch  # noqa: F401  (carga las libs CUDA que onnxruntime dlopen-ea)
        torch.set_num_threads(1)   # el paralelismo real ya es por cámara, no por op
        import onnxruntime as ort
        print(f"[REID] Cargando {Path(weights_onnx).name} (onnxruntime)...")
        self.sess = ort.InferenceSession(
            str(weights_onnx),
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        self._sum = np.zeros(3840, np.float64)
        self._n = 0
        self.ok = True
        print(f"[REID] Listo (3840-d) | providers: {self.sess.get_providers()}")

    @staticmethod
    def _preprocess(crop_bgr):
        """Réplica cv2 del transform de runtime (validada: |Δ sims| <= 0.006)."""
        rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (REID_INPUT[1], REID_INPUT[0]),
                         interpolation=cv2.INTER_LINEAR)
        x = rgb.astype(np.float32) / 255.0
        x = (x - REID_MEAN) / REID_STD
        return x.transpose(2, 0, 1)[None]      # (1,3,256,128)

    def embed(self, crop_bgr) -> Optional[np.ndarray]:
        """Crop BGR -> embedding L2 crudo (np 3840) o None si es ilegible.
        V4: crops chicos (>=24x48) se suben a 64x128 antes del transform."""
        if not self.ok or crop_bgr is None or crop_bgr.size == 0:
            return None
        h, w = crop_bgr.shape[:2]
        if w < REID_FLOOR_W or h < REID_FLOOR_H:
            return None                        # ilegible: se omite, no se juzga
        if w < REID_MIN_W or h < REID_MIN_H:
            crop_bgr = cv2.resize(crop_bgr, (REID_MIN_W, REID_MIN_H),
                                  interpolation=cv2.INTER_CUBIC)
        x = self._preprocess(crop_bgr)
        with self._lock:
            e = self.sess.run(None, {"image": x})[0][0].astype(np.float64)
            e /= (np.linalg.norm(e) + 1e-12)   # el grafo NO normaliza
            if not np.isfinite(e).all() or float(np.abs(e).max()) > 3.0:
                return None
            self._sum += e
            self._n += 1
        return e

    @property
    def warm(self) -> bool:
        return self.ok and self._n >= REID_CENTER_WARMUP

    def _center(self, e: np.ndarray) -> np.ndarray:
        c = e - self._sum / max(1, self._n)
        return c / (np.linalg.norm(c) + 1e-9)

    def match(self, query: np.ndarray, gallery: list) -> float:
        if not self.warm or not gallery:
            return -1.0
        with np.errstate(all="ignore"):
            q = self._center(query)
            sims = [float(q @ self._center(g)) for g in gallery]
        sims = [s for s in sims if np.isfinite(s)]
        return max(sims) if sims else -1.0

    # ── Modo 'zona_crudo': la lógica de ZonaRestringida_ReID ────────────────
    @staticmethod
    def feat768(e: np.ndarray) -> np.ndarray:
        """Vector BNNeck global de 768-d (primeras 768 dims del 3840 — misma
        ruta del grafo que usa ZonaRestringida), re-normalizado L2. El coseno
        crudo SÍ discrimina en este espacio."""
        v = e[:768]
        return v / (np.linalg.norm(v) + 1e-12)

    @staticmethod
    def zona_score(buffer768: list, gallery768: list, thr: float):
        """Agregación de ZonaRestringida: matriz completa buffer×galería en
        coseno crudo → (media, fracción de comparaciones >= thr)."""
        if not buffer768 or not gallery768:
            return -1.0, 0.0
        B = np.stack(buffer768)
        G = np.stack(gallery768)
        with np.errstate(all="ignore"):
            S = B @ G.T
        S = S[np.isfinite(S)]
        if S.size == 0:
            return -1.0, 0.0
        return float(S.mean()), float((S >= thr).mean())

    @staticmethod
    def gallery_add(gallery: list, e: np.ndarray) -> int:
        """Mantiene <= GALLERY_SIZE vistas maximizando diversidad."""
        if len(gallery) < GALLERY_SIZE:
            gallery.append(e)
            return len(gallery) - 1
        with np.errstate(all="ignore"):
            sims = [float(e @ g) for g in gallery]
        sims = [s if np.isfinite(s) else 2.0 for s in sims]
        i = int(np.argmax(sims))
        gallery[i] = e
        return i

    def seed_names(self, db: "EmployeeDB") -> int:
        """Suma al re-centrado los embeddings de nombres AÚN NO sembrados.
        Seguro de llamar desde varias cámaras que comparten este motor: cada
        nombre se cuenta UNA sola vez en total (bookkeeping global, no por
        cámara). Devuelve cuántos nombres nuevos sembró."""
        if not self.ok:
            return 0
        with self._seed_lock:
            new_names = [n for n in db.galleries if n not in self.seeded_names]
            if not new_names:
                return 0
            for n in new_names:
                for e in db.galleries[n]:
                    self._sum += e
                    self._n += 1
                self.seeded_names.add(n)
            # anti-deadlock V4: base chica -> ciclar vistas hasta cubrir el
            # warmup (una sola vez: tras esto _n >= WARMUP y no se repite)
            if 0 < self._n < REID_CENTER_WARMUP:
                all_embs = [e for g in db.galleries.values() for e in g]
                i = 0
                while self._n < REID_CENTER_WARMUP and all_embs:
                    self._sum += all_embs[i % len(all_embs)]
                    self._n += 1
                    i += 1
            return len(new_names)


# Instancia ÚNICA por proceso (compartida entre TODAS las camaras que usan
# los mismos pesos): una sola sesion ONNX + su area de trabajo en GPU en vez
# de una por camara. embed() ya serializa internamente con self._lock, asi
# que compartir la instancia entre hilos es seguro.
_reid_registry_lock = threading.Lock()
_reid_engines: dict = {}


def get_shared_reid_engine(weights_onnx: Path) -> "ReIDEngine":
    key = str(weights_onnx)
    with _reid_registry_lock:
        eng = _reid_engines.get(key)
        if eng is None:
            eng = ReIDEngine(weights_onnx)
            _reid_engines[key] = eng
        return eng


# =============================================================================
# VEREDICTO TEMPORAL (idéntico a V3)
# =============================================================================
class PresenceVerdict:
    def __init__(self, fps: float, tolerance_seconds: float = TOLERANCE_SECONDS,
                 position_primary: bool = False,
                 rise_seconds: float = RISE_SECONDS):
        self.position_primary = position_primary
        self.tolerance_seconds = tolerance_seconds
        self.rise_step = 1.0 / max(1.0, rise_seconds * fps)
        self.decay_step = 1.0 / max(1.0, tolerance_seconds * fps)
        self._window = deque(maxlen=max(1, int(round(MAJORITY_SECONDS * fps))))
        self.presence_conf = 0.0
        self.person_in_roi = False
        self.verified_in_roi = False

    def update(self, verified_in_roi: bool, person_in_roi: bool) -> None:
        self.person_in_roi = person_in_roi
        self.verified_in_roi = verified_in_roi
        raw = person_in_roi if self.position_primary else verified_in_roi
        self._window.append(1.0 if raw else 0.0)
        if (sum(self._window) / len(self._window)) >= MAJORITY_FRAC:
            self.presence_conf = min(1.0, self.presence_conf + self.rise_step)
        else:
            self.presence_conf = max(0.0, self.presence_conf - self.decay_step)

    @property
    def state(self) -> str:
        if self.presence_conf >= ATTEND_CONFIRM:
            return "CAJERO_ATENDIENDO"
        if self.presence_conf > 0.0:
            return "TOLERANCIA"
        if self.person_in_roi and not self.position_primary:
            return "PERSONA_NO_CAJERO"
        return "DESATENDIDA"

    @property
    def tolerance_remaining_s(self) -> float:
        return self.presence_conf * self.tolerance_seconds


# =============================================================================
# BASE DE DATOS DE EMPLEADOS (formato .npz idéntico a V3/V4)
# =============================================================================
class EmployeeDB:
    def __init__(self, path: str):
        self.dir = Path(path)
        self.galleries: dict = {}       # nombre -> [emb corporal 3840-d]
        self.galleries768: dict = {}    # nombre -> [BNNeck 768-d] (modo zona)
        self.face_galleries: dict = {}  # nombre -> [emb facial 512-d]
        for f in sorted(self.dir.glob("*.npz")):
            try:
                data = np.load(f)
                gal = [e for e in data["embeddings"].astype(np.float64)]
                self.galleries[f.stem] = gal
                self.galleries768[f.stem] = [ReIDEngine.feat768(e) for e in gal]
                if "faces" in data.files and len(data["faces"]):
                    self.face_galleries[f.stem] = \
                        [e for e in data["faces"].astype(np.float64)]
            except Exception as e:  # noqa: BLE001
                print(f"[DB] no pude leer {f.name}: {e}")
        n = sum(len(g) for g in self.galleries.values())
        nf = sum(len(g) for g in self.face_galleries.values())
        if self.galleries:
            print(f"[DB] {len(self.galleries)} empleado(s), {n} vistas "
                  f"corporales + {nf} faciales: {sorted(self.galleries)}")
        else:
            print(f"[DB] VACÍA ({self.dir}/) — registra empleados desde el panel")

    def all_embeddings(self):
        for g in self.galleries.values():
            yield from g

    def best_face_match(self, fe):
        """Mejor empleado por ROSTRO (coseno crudo ArcFace). -> (nombre, sim)."""
        best_name, best = None, -1.0
        for name, gal in self.face_galleries.items():
            with np.errstate(all="ignore"):
                s = max(float(fe @ g) for g in gal)
            if np.isfinite(s) and s > best:
                best_name, best = name, s
        return best_name, best

    def face_sim(self, name, fe):
        """Similitud facial vs UN empleado específico (-1 si no tiene rostros)."""
        gal = self.face_galleries.get(name)
        if not gal:
            return -1.0
        with np.errstate(all="ignore"):
            s = max(float(fe @ g) for g in gal)
        return s if np.isfinite(s) else -1.0


# =============================================================================
# REGISTRO DE TRACKS (idéntico a V3)
# =============================================================================
class Track:
    __slots__ = ("tid", "box", "in_roi", "estado", "empleado", "best_sim",
                 "gallery", "last_seen", "last_check", "checks",
                 "last_enrich", "blocked_by", "feat_buffer", "born",
                 "last_face_check", "face_disagree", "face_banned",
                 "last_face", "last_face_t")

    def __init__(self, tid, now):
        self.tid = tid
        self.box = None
        self.in_roi = None             # ID de la zona donde esta, o None
        self.estado = "PENDIENTE"
        self.empleado = None
        self.best_sim = -1.0
        self.gallery: list = []
        self.last_seen = now
        self.last_check = -1e9
        self.checks = 0
        self.last_enrich = -1e9
        self.blocked_by = None
        self.feat_buffer: list = []    # vistas 768-d acumuladas (modo zona)
        self.born = now                # para el periodo de gracia facial
        self.last_face_check = -1e9    # cadencia propia del rostro
        self.face_disagree = 0         # desacuerdos faciales consecutivos
        self.face_banned: set = set()  # identidades REFUTADAS por rostro: el
                                       # cuerpo ya no puede reclamarlas aquí
        self.last_face = None          # último embedding facial leído (512-d)
        self.last_face_t = -1e9        # cuándo — evidencia viva por
                                       # FACE_EVIDENCE_TTL segundos


class RegistryDB:
    def __init__(self, reid: ReIDEngine, db: EmployeeDB, faces=None,
                 face_th: float = 0.35, reid_mode: str = "zona_crudo",
                 min_frac: float = MIN_FRACTION,
                 face_grace: float = FACE_GRACE_S,
                 face_required: bool = False):
        self.reid = reid
        self.db = db
        self.faces = faces              # motor facial compartido (o None)
        self.face_th = face_th
        self.reid_mode = reid_mode      # 'zona_crudo' | 'v4_recentrado' (vivo)
        self.min_frac = min_frac
        self.face_grace = face_grace    # prioridad facial al inicio del track
        self.face_required = face_required  # el cuerpo no verifica: solo rostro
        self.tracks: dict = {}
        self.pool: list = []
        self.n_matched = 0
        self.n_relinked = 0
        self.n_face_matched = 0
        self.n_face_veto = 0
        self.seen_now: set = set()

    def _best(self, e, banned=()):
        name, best = None, -1.0
        for n, g in self.db.galleries.items():
            if n in banned:
                continue
            s = self.reid.match(e, g)
            if s > best:
                name, best = n, s
        from_pool = False
        for p in self.pool:
            if p["name"] in banned:
                continue
            s = self.reid.match(e, p["gallery"])
            if s > best:
                name, best, from_pool = p["name"], s, True
        return name, best, from_pool

    def _best_zona(self, buffer768, thr, banned=()):
        """Lógica de ZonaRestringida: media de la matriz buffer×galería +
        fracción sobre el umbral, contra base ∪ pool de sesión.
        → (nombre, score, frac, es_pool)."""
        name, best, bfrac = None, -1.0, 0.0
        for n, g768 in self.db.galleries768.items():
            if n in banned:
                continue
            sc, fr = ReIDEngine.zona_score(buffer768, g768, thr)
            if sc > best:
                name, best, bfrac = n, sc, fr
        from_pool = False
        for p in self.pool:
            if p["name"] in banned:
                continue
            g768 = [ReIDEngine.feat768(x) for x in p["gallery"]]
            sc, fr = ReIDEngine.zona_score(buffer768, g768, thr)
            if sc > best:
                name, best, bfrac, from_pool = p["name"], sc, fr, True
        return name, best, bfrac, from_pool

    def _revoke(self, t, reason: str):
        """VETO FACIAL: el rostro refutó la identidad corporal del track.
        Vuelve a PENDIENTE, la identidad refutada queda VETADA para el cuerpo
        en este track (el rostro sí puede reasignarla si luego coincide), la
        galería de sesión se descarta (estaba envenenada), y se PURGA el pool
        de sesión de ese nombre: sus entradas contienen la apariencia del
        impostor — sin esto, cada track nuevo del mismo impostor se re-ligaba
        desde el pool a +0.9 y el error se auto-perpetuaba 30 minutos
        (medido en Untitled3.mov el 2026-08-16)."""
        print(f"[DB] track {t.tid}: VETO FACIAL de '{t.empleado}' — {reason}")
        self.n_face_veto += 1
        if t.empleado:
            t.face_banned.add(t.empleado)
            n_pool = len(self.pool)
            self.pool = [p for p in self.pool if p.get("name") != t.empleado]
            purged = n_pool - len(self.pool)
            if purged:
                print(f"[DB] pool: {purged} galería(s) de sesión de "
                      f"'{t.empleado}' purgadas (contaminadas)")
        t.estado = "PENDIENTE"
        t.empleado = None
        t.gallery = []
        t.checks = 0
        t.face_disagree = 0
        t.blocked_by = None

    def _face_contradicts(self, t, name, now):
        """¿La evidencia facial RECIENTE del track refuta a `name`?
        (rostro leído hace < FACE_EVIDENCE_TTL con sim < FACE_REVOKE_TH
        contra la galería facial de ese nombre)."""
        if t.last_face is None or now - t.last_face_t > FACE_EVIDENCE_TTL:
            return False
        s = self.db.face_sim(name, t.last_face)
        return 0 <= s < FACE_REVOKE_TH

    def _bound_to(self, name: str, exclude_tid: int):
        for t in self.tracks.values():
            if (t.tid != exclude_tid and t.estado == "VERIFICADO"
                    and t.empleado == name):
                return t.tid
        return None

    def update(self, dets: list, now: float, frame, match_th: float,
               in_roi_map: Optional[dict] = None) -> None:
        seen = set()
        self.seen_now = seen
        candidates: dict = {}
        for tid, box, _ in dets:
            seen.add(tid)
            t = self.tracks.get(tid)
            if t is None:
                t = self.tracks[tid] = Track(tid, now)
            t.box = box
            t.last_seen = now
            if (in_roi_map or {}).get(tid) is None:   # fuera de toda zona
                continue
            crop = None
            face_capable = self.faces is not None and bool(self.db.face_galleries)

            # ── ROSTRO: corre para TODOS los estados (vigilancia continua) ──
            # PENDIENTE/DESCONOCIDO: verifica con prioridad (y rescata).
            # VERIFICADO: audita — si el rostro refuta la identidad corporal,
            # la REVOCA (misma ropa != misma persona).
            face_hit = None
            if face_capable and now - t.last_face_check >= FACE_RECHECK_S:
                t.last_face_check = now
                crop = self._crop(frame, box)
                fe = _face_from_crop(self.faces, crop)
                if fe is not None:
                    t.last_face = fe          # evidencia viva para el cuerpo
                    t.last_face_t = now       # (ver _face_contradicts)
                    fname, fsim = self.db.best_face_match(fe)
                    if t.estado == "VERIFICADO":
                        sim_own = self.db.face_sim(t.empleado, fe)
                        if (fname is not None and fname != t.empleado
                                and fsim >= self.face_th and fsim > sim_own):
                            # rostro POSITIVO de OTRA persona: veto + reasignar
                            self._revoke(t, f"el rostro dice '{fname}' "
                                            f"({fsim:+.2f})")
                            candidates.setdefault(fname, []).append(
                                (fsim, tid, None, False, "face"))
                        elif 0 <= sim_own < FACE_REVOKE_TH:
                            t.face_disagree += 1
                            if t.face_disagree >= FACE_DISAGREE_N:
                                self._revoke(t, f"rostro visible no coincide "
                                                f"({sim_own:+.2f}) "
                                                f"x{t.face_disagree}")
                        elif sim_own >= self.face_th:
                            t.face_disagree = 0
                    elif fname is not None and fsim >= self.face_th:
                        face_hit = (fname, fsim)
                        candidates.setdefault(fname, []).append(
                            (fsim, tid, None, False, "face"))
                    elif self.face_required and t.estado == "PENDIENTE":
                        # modo ROSTRO OBLIGATORIO: un rostro visible que NO
                        # coincide con nadie consume un intento — tras
                        # DB_MAX_CHECKS la persona queda DESCONOCIDO
                        # ("PERSONA EN ZONA DE CAJERO, NO RECONOCIDA")
                        t.checks += 1
                        if t.checks >= DB_MAX_CHECKS:
                            t.estado = "DESCONOCIDO"
                            print(f"[DB] track {tid}: DESCONOCIDO (rostro "
                                  f"visible sin coincidencia en {t.checks} "
                                  f"intentos, modo rostro-obligatorio)")

            # ── CUERPO (TransReID) — métrica según reid_mode. PRIORIDAD
            #    FACIAL: durante los primeros face_grace segundos del track el
            #    cuerpo ESPERA (el rostro decide primero); si la cámara no ve
            #    rostros o no hay galerías faciales, no hay espera. ────────────
            zona = self.reid_mode == "zona_crudo"
            ready = self.reid.ok if zona else self.reid.warm
            grace_over = (not face_capable
                          or now - t.born >= self.face_grace)
            if (face_hit is None and t.estado == "PENDIENTE"
                    and not (self.face_required and face_capable)
                    and ready and grace_over and t.checks < DB_MAX_CHECKS
                    and now - t.last_check >= DB_CHECK_COOLDOWN_S):
                # (con face_required=True el cuerpo NO verifica a nadie:
                #  solo el rostro puede — incluye el re-ligado desde pool.
                #  Si la camara no puede hacer rostros (sin motor o sin
                #  galerias faciales), el requisito no es exigible y el
                #  cuerpo opera normal para no dejar la fuente ciega)
                crop = crop if crop is not None else self._crop(frame, box)
                e = self.reid.embed(crop)
                if e is not None:
                    t.last_check = now
                    if zona:
                        # ZonaRestringida: acumula evidencia y decide por
                        # MEDIA + FRACCIÓN sobre toda la matriz. Los nombres
                        # REFUTADOS por rostro quedan fuera del match corporal,
                        # y un candidato que CONTRADIGA la evidencia facial
                        # reciente del track se rechaza y se re-intenta con el
                        # siguiente mejor nombre.
                        t.feat_buffer.append(ReIDEngine.feat768(e))
                        del t.feat_buffer[:-FEAT_BUFFER_MAX]
                        banned = set(t.face_banned)
                        while True:
                            name, best, frac, from_pool = self._best_zona(
                                t.feat_buffer, match_th, banned=banned)
                            if (name is None or best < match_th
                                    or not self._face_contradicts(t, name, now)):
                                break
                            print(f"[DB] track {tid}: cuerpo propone '{name}' "
                                  f"({best:+.2f}) pero el rostro reciente lo "
                                  f"contradice — rechazado")
                            banned.add(name)
                        t.best_sim = max(t.best_sim, best)
                        if best >= match_th and frac >= self.min_frac:
                            candidates.setdefault(name, []).append(
                                (best, tid, e, from_pool, "body"))
                        elif best >= match_th * GRAY_ZONE_FACTOR:
                            pass   # zona GRIS: sigue juntando evidencia
                                   # sin consumir intento
                        else:
                            t.checks += 1
                            if t.checks >= DB_MAX_CHECKS:
                                t.estado = "DESCONOCIDO"
                                print(f"[DB] track {tid}: DESCONOCIDO "
                                      f"(zona: mejor media={t.best_sim:+.2f} "
                                      f"en {t.checks} intentos; el rostro "
                                      f"aún puede rescatarlo)")
                    else:
                        # V4: coseno re-centrado 3840-d, máximo, 1 intento —
                        # con el mismo rechazo por contradicción facial
                        banned = set(t.face_banned)
                        while True:
                            name, best, from_pool = self._best(e, banned=banned)
                            if (name is None or best < match_th
                                    or not self._face_contradicts(t, name, now)):
                                break
                            print(f"[DB] track {tid}: cuerpo propone '{name}' "
                                  f"({best:+.2f}) pero el rostro reciente lo "
                                  f"contradice — rechazado")
                            banned.add(name)
                        t.best_sim = max(t.best_sim, best)
                        if best >= match_th:
                            candidates.setdefault(name, []).append(
                                (best, tid, e, from_pool, "body"))
                        else:
                            t.checks += 1
                            if t.checks >= DB_MAX_CHECKS:
                                t.estado = "DESCONOCIDO"
                                print(f"[DB] track {tid}: DESCONOCIDO (mejor "
                                      f"sim={t.best_sim:+.2f} en {t.checks} "
                                      f"intentos; el rostro aún puede rescatarlo)")

        for name, cands in candidates.items():
            # el ROSTRO gana sobre el cuerpo; a igual vía, mayor similitud
            cands.sort(reverse=True, key=lambda c: (c[4] == "face", c[0]))
            owner = self._bound_to(name, exclude_tid=-1)
            if owner is not None:
                for best, tid, _, _, via in cands:
                    t = self.tracks[tid]
                    if t.blocked_by != owner:
                        t.blocked_by = owner
                        print(f"[DB] track {tid}: se parece a '{name}' "
                              f"({via} sim={best:+.2f}) pero ya está ligada "
                              f"al track {owner} — ignorado")
                continue
            best, tid, e, from_pool, via = cands[0]
            t = self.tracks[tid]
            t.estado = "VERIFICADO"
            t.empleado = name
            t.gallery = [e] if e is not None else []   # por rostro: la galería
            t.blocked_by = None                        # corporal la llena enrich()
            if via == "face":
                self.n_face_matched += 1
                print(f"[DB] track {tid}: EMPLEADO '{name}' por ROSTRO "
                      f"sim={best:+.2f}")
            elif from_pool:
                self.n_relinked += 1
                print(f"[DB] track {tid}: RE-LIGADO (sesión) a '{name}' "
                      f"sim={best:+.2f}")
            else:
                self.n_matched += 1
                print(f"[DB] track {tid}: EMPLEADO '{name}' sim={best:+.2f}")

        for tid in list(self.tracks.keys()):
            t = self.tracks[tid]
            if tid in seen:
                continue
            if now - t.last_seen > TRACK_DEATH_S:
                if t.estado == "VERIFICADO" and t.gallery:
                    self.pool.append({"gallery": t.gallery, "name": t.empleado,
                                      "t": now})
                    self.pool = [p for p in self.pool if now - p["t"] <= POOL_TTL_S]
                    self.pool = self.pool[-POOL_MAX:]
                del self.tracks[tid]

    @staticmethod
    def _crop(frame, box):
        # del frame 800x600 CRUDO (la máscara nunca amputa píxeles del embedding)
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = box.astype(int)
        return frame[max(0, y1):min(H, y2), max(0, x1):min(W, x2)]

    def enrich(self, frame, now: float) -> None:
        for t in self.tracks.values():
            if t.tid not in self.seen_now:
                continue
            if t.estado != "VERIFICADO" or now - t.last_enrich < ENRICH_EVERY_S:
                continue
            e = self.reid.embed(self._crop(frame, t.box))
            if e is not None:
                ReIDEngine.gallery_add(t.gallery, e)
                t.last_enrich = now


# =============================================================================
# HELPERS DE REGISTRO (enroll_employee_v4.py)
# =============================================================================
def _diverse_add(gallery: list, crops: list, e: np.ndarray, crop, k: int) -> None:
    if len(gallery) < k:
        gallery.append(e)
        crops.append(crop)
        return
    with np.errstate(all="ignore"):
        sims = [float(e @ g) for g in gallery]
    sims = [s if np.isfinite(s) else 2.0 for s in sims]
    i = int(np.argmax(sims))
    gallery[i] = e
    crops[i] = crop


def _filter_coherent(embs: list, th: float):
    """Expulsa vistas de OTRA persona por componentes conexas (ver V4)."""
    n = len(embs)
    E = np.stack(embs).astype(np.float64)
    C = E - E.mean(axis=0)
    C /= (np.linalg.norm(C, axis=1, keepdims=True) + 1e-9)
    with np.errstate(all="ignore"):
        S = C @ C.T
    np.fill_diagonal(S, -1.0)
    adj = S > th
    comp = [-1] * n
    cid = 0
    for i in range(n):
        if comp[i] >= 0:
            continue
        stack = [i]
        comp[i] = cid
        while stack:
            u = stack.pop()
            for v in range(n):
                if comp[v] < 0 and adj[u][v]:
                    comp[v] = cid
                    stack.append(v)
        cid += 1
    sizes = [comp.count(c) for c in range(cid)]
    main = int(np.argmax(sizes))
    keep = [i for i in range(n) if comp[i] == main]
    dropped = [(i, float(S[i, keep].max())) for i in range(n) if comp[i] != main]
    return keep, dropped


def _filter_coherent_faces(embs: list, th: float = FACE_COHERENCE_TH):
    """Expulsa rostros mal capturados (blur/ángulo/oclusión) de la galería
    facial. A diferencia de _filter_coherent (cuerpo): coseno CRUDO, SIN
    re-centrar — ArcFace ya discrimina en crudo (media intra-persona genuina
    ~0.35-0.95; un rostro mal capturado cae a ~0.0-0.25 de similitud PROMEDIO
    con el resto de la galería). Descarta cualquier vista cuya similitud
    promedio a las demás esté bajo `th`. -> (keep_idx, dropped_idx)"""
    n = len(embs)
    if n < 3:
        return list(range(n)), []
    E = np.stack(embs).astype(np.float64)
    E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    with np.errstate(all="ignore"):
        S = E @ E.T
    np.fill_diagonal(S, np.nan)
    avg = np.nanmean(S, axis=1)
    avg = np.where(np.isfinite(avg), avg, -1.0)
    keep = [i for i in range(n) if avg[i] >= th]
    dropped = [i for i in range(n) if avg[i] < th]
    if len(keep) < 3:   # umbral demasiado agresivo para este lote: no filtrar
        return list(range(n)), []
    return keep, dropped


# =============================================================================
# DETECTOR (contrato del framework Base)
# =============================================================================
class CashierDetector(ParamsMixin, BaseDetector):
    NAME = "CAJERO"
    ZONA_TIPOS = ("CAJERO",)   # ver Base/zonas.py

    # Parámetros ajustables EN CALIENTE por fuente desde el panel (botón
    # "⚙ Ajustes"); ver ParamsMixin (Base/params_mixin.py). El valor inicial
    # de cada uno se copia desde el flag CLI equivalente en setup().
    PARAMS = [
        dict(key="reid_mode", label="Métrica ReID (cuerpo)", type="choice",
             choices=REID_MODES,
             help="zona_crudo = lógica de ZonaRestringida: coseno crudo 768-d, "
                  "media sobre buffer de evidencia + fracción, zona gris "
                  "(umbral ~0.55). v4_recentrado = coseno re-centrado 3840-d, "
                  "máximo sobre galería (umbral ~0.20-0.30). ¡Escalas NO "
                  "comparables: ajustar el umbral al cambiar de modo!"),
        dict(key="match_th", label="Umbral ReID (cuerpo)", type="float",
             min=0.05, max=0.90, step=0.01,
             help="Similitud mínima contra la base de empleados (TransReID). "
                  "~0.55 en modo zona_crudo; ~0.20-0.30 en v4_recentrado"),
        dict(key="min_frac", label="Fracción mín. de matches (zona)", type="float",
             min=0.0, max=1.0, step=0.05,
             help="Solo modo zona_crudo: fracción de comparaciones del buffer "
                  "que deben superar el umbral para confirmar"),
        dict(key="face_th", label="Umbral ReID (rostro)", type="float",
             min=0.10, max=0.80, step=0.01,
             help="Similitud mínima de rostro (ArcFace) para verificar"),
        dict(key="face_grace", label="Prioridad facial (s)", type="float",
             min=0.0, max=15.0, step=0.5,
             help="El cuerpo ESPERA estos segundos al inicio de cada track "
                  "para que el rostro decida primero (0 = sin espera). El "
                  "rostro además audita continuamente y puede REVOCAR "
                  "verificaciones corporales equivocadas"),
        dict(key="face_required", label="Rostro OBLIGATORIO", type="bool",
             help="El cuerpo nunca verifica solo: sin confirmación facial no "
                  "hay VERIFICADO (ni desde el pool de sesión). Para cámaras "
                  "frontales donde la cara se ve bien; en cámaras sin rostros "
                  "visibles dejaría a todos sin reconocer"),
        dict(key="person_conf", label="Confianza YOLO (persona)", type="float",
             min=0.05, max=0.90, step=0.01,
             help="Confianza mínima de detección de persona"),
        dict(key="tolerance", label="Tolerancia (s)", type="float",
             min=1, max=120, step=1,
             help="Segundos de ausencia del cajero antes de DESATENDIDA. En "
                  "QUEUE_CASHIER tambien es cuanto siguen los clientes como "
                  "ATENDIENDO despues de que el cajero se va"),
        dict(key="rise_seconds", label="Confirmar atencion (s)", type="float",
             min=0.2, max=30.0, step=0.1,
             help="Presencia VERIFICADA sostenida para confirmar "
                  "CAJERO_ATENDIENDO (la subida; la bajada es Tolerancia). "
                  "En QUEUE_CASHIER: cuanto tarda el cliente en subir de "
                  "POSIBLEMENTE ATENDIENDO (morado) a ATENDIENDO (verde)"),
        dict(key="roi_overlap", label="Solape mínimo con el ROI", type="float",
             min=0.0, max=1.0, step=0.05,
             help="Fracción del bbox dentro del ROI para contar 'dentro'"),
        dict(key="yolo_margin", label="Margen de detección", type="float",
             min=0.0, max=1.0, step=0.05,
             help="Margen extra de la zona de detección alrededor del ROI"),
        dict(key="strict_roi", label="Máscara estricta del ROI", type="bool",
             help="Enmascarar en negro fuera del polígono del ROI"),
        dict(key="position_primary", label="Modo posición (ignora identidad)",
             type="bool",
             help="Basta una persona en el ROI, sin verificar quién es"),
        dict(key="alert_unknown", label="Alertar en PERSONA_NO_CAJERO",
             type="bool"),
    ]

    # ------------------------------------------------------------------ #
    def add_arguments(self, parser):
        parser.add_argument("--regions", default="",
                            help="JSON del ROI en coords 800x600 (vacío = sin "
                                 "ROI: stream limpio hasta dibujarlo en el panel)")
        parser.add_argument("--db", default=str(EMPLEADOS_DIR),
                            help="Carpeta de empleados registrados (*.npz)")
        parser.add_argument("--reid-onnx",
                            default=str(V4_DIR / "onnx" / "transreid_msmt.onnx"),
                            help="Modelo TransReID ONNX")
        parser.add_argument("--person-model",
                            default=str(V4_DIR / "yolo11s.pt"),
                            help="Modelo YOLO de personas (default yolo11s: "
                                 "punto medio precisión/VRAM para operar muchas "
                                 "cámaras; --person-model .../yolo11x.pt para el "
                                 "modelo grande de ZonaRestringida_ReID. Acepta "
                                 "también un *-pose para reactivar el filtro de "
                                 "keypoints)")
        parser.add_argument("--no-faces", action="store_true",
                            help="Desactiva la corroboración de identidad por "
                                 "ROSTRO (InsightFace)")
        parser.add_argument("--face-th", type=float, default=0.55,
                            help="Similitud facial mínima (ArcFace) para "
                                 "verificar por rostro. CALIBRADO CON DATOS "
                                 "de impostor real (2026-08-16): media +0.17, "
                                 "p95=0.52, p99=0.56 — 0.55 deja margen sobre "
                                 "el p95 medido")
        parser.add_argument("--fps", type=float, default=0.0,
                            help="FPS de la fuente para el tiempo virtual "
                                 "(0 = autodetectar; si falla usa 25)")
        parser.add_argument("--match-th", type=float, default=DB_MATCH_TH,
                            help="Umbral corporal (~0.55 en zona_crudo; "
                                 "~0.20-0.30 en v4_recentrado)")
        parser.add_argument("--reid-mode", choices=REID_MODES,
                            default="zona_crudo",
                            help="Métrica de identidad corporal (default: la "
                                 "lógica de ZonaRestringida_ReID)")
        parser.add_argument("--min-frac", type=float, default=MIN_FRACTION,
                            help="Modo zona: fracción mínima de comparaciones "
                                 "sobre el umbral para confirmar")
        parser.add_argument("--face-grace", type=float, default=FACE_GRACE_S,
                            help="Segundos de prioridad facial al inicio de "
                                 "cada track (el cuerpo espera; 0 = sin espera)")
        # FUSION 2026-08-19 (pedido del usuario): rostro obligatorio POR
        # DEFAULT. Con el fantasma del garrafon quedo demostrado que el
        # cuerpo solo (ropa+contexto) puede verificar a un objeto. La red
        # de seguridad para camaras SIN rostros visibles ya existe en el
        # runtime: si la camara no puede hacer rostros (sin motor o sin
        # galerias faciales), el requisito no es exigible y el cuerpo
        # opera normal (ver face_capable en RegistryDB.update). Ademas
        # sigue siendo ajustable en caliente por fuente (PARAMS).
        parser.add_argument("--face-required", dest="face_required",
                            action="store_true", default=True,
                            help="El cuerpo nunca verifica solo: sin "
                                 "confirmación facial no hay VERIFICADO "
                                 "(DEFAULT desde la fusión)")
        parser.add_argument("--no-face-required", dest="face_required",
                            action="store_false",
                            help="Permite verificar solo por cuerpo "
                                 "(comportamiento del despliegue original)")
        parser.add_argument("--tolerance", type=float, default=TOLERANCE_SECONDS)
        parser.add_argument("--rise-seconds", type=float, default=RISE_SECONDS,
                            help="Segundos de presencia VERIFICADA sostenida "
                                 "para confirmar CAJERO_ATENDIENDO (la subida "
                                 "del veredicto; la bajada es --tolerance). En "
                                 "QUEUE_CASHIER gobierna cuanto tarda un "
                                 "cliente en subir de POSIBLEMENTE ATENDIENDO "
                                 "a ATENDIENDO tras verificarse el cajero")
        parser.add_argument("--roi-overlap", type=float, default=ROI_OVERLAP_FRAC)
        parser.add_argument("--yolo-margin", type=float, default=ROI_DETECT_MARGIN_FRAC)
        parser.add_argument("--no-strict-roi", action="store_true")
        # FUSION 2026-08-19 (pedido del usuario): 0.5 por default. El 0.20
        # historico dejaba pasar fantasmas (garrafon a 0.51 verificado como
        # empleado); 0.5 los corta y las personas reales de estas camaras
        # detectan a 0.8+. Ajustable en caliente por fuente si una camara
        # lejana necesita menos.
        parser.add_argument("--person-conf", type=float, default=0.5)
        parser.add_argument("--kpt-min", type=float, default=KPT_MIN_MEAN,
                            help="0.15 con máscara estricta, 0.45 sin ella")
        parser.add_argument("--position-primary", action="store_true",
                            help="Basta UNA persona en el ROI (ignora identidad)")
        parser.add_argument("--alert-unknown", action="store_true",
                            help="También alertar al entrar a PERSONA_NO_CAJERO")

    # ------------------------------------------------------------------ #
    def configure(self, args):
        """Siembra aqui (y NUNCA en setup) las copias VIVAS de los flags
        ajustables en caliente. Motivo (BUG heredado del despliegue
        original, corregido en la fusion 2026-08-19): el panel re-aplica
        los parametros guardados (params_web/<fuente>.json) INMEDIATAMENTE
        despues de configure() y ANTES de setup(); si setup() volviera a
        asignarlos desde el CLI —como hacia—, cada reinicio del panel
        pisaba EN SILENCIO los ajustes del operador (person_conf guardado
        en 0.8 volvia a 0.20 y los fantasmas reaparecian). Es exactamente
        la trampa que QueueAnalytics.configure() documenta y esquiva."""
        super().configure(args)
        a = args
        self.position_primary = a.position_primary
        self.face_th = a.face_th
        self.reid_mode = a.reid_mode
        self.min_frac = a.min_frac
        self.face_grace = a.face_grace
        self.face_required = a.face_required
        self.match_th = a.match_th
        self.person_conf = a.person_conf
        self.tolerance = a.tolerance
        self.rise_seconds = a.rise_seconds
        self.roi_overlap = a.roi_overlap
        self.yolo_margin = a.yolo_margin
        self.alert_unknown = a.alert_unknown
        self.strict_roi = not a.no_strict_roi

    # ------------------------------------------------------------------ #
    def setup(self):
        from ultralytics import YOLO
        a = self.args

        # ROI opcional (sin ROI = reposo, stream limpio)
        # FUSION Queue-Cashier: las zonas viven TIPADAS en self.zonas
        # (Base/zonas.py); self.regions se DERIVA (lista de puntos alineada,
        # None = sin ROI) porque toda la maquinaria de mascaras es
        # posicional, y self._zona_ids traduce posicion -> id estable.
        self.zonas = []
        self._sincronizar_zonas()
        cargadas = None
        source_key = getattr(self, "source_key", None)
        if source_key:
            # Camino del panel fusionado: zones_web/<source_key>.json
            # (v2 tipado, o legacy que se convierte al leer) — mismo
            # patron de carga tolerante que QueueAnalytics.setup().
            zpath = ZONES_WEB_DIR / f"{source_key}.json"
            if zpath.is_file():
                try:
                    with open(zpath) as f:
                        self.zonas = _zonas.leer_payload(json.load(f), self.NAME)
                    self._sincronizar_zonas()
                    cargadas = str(zpath)
                except Exception as e:  # noqa: BLE001
                    print(f"[{self.NAME}] No pude cargar zonas de "
                          f"{zpath}: {e} — arranco sin ROI")
        if cargadas is None and a.regions:
            # Camino legacy por CLI (--regions con puntos pelados)
            with open(a.regions) as f:
                regs = json.load(f)
            self.zonas = _zonas.desde_legacy(regs or [], self.NAME)
            self._sincronizar_zonas()
            cargadas = a.regions
        print(f"[{self.NAME}] V4 | procesamiento {PROC_W}x{PROC_H} | ROI: "
              + (", ".join(f"{z['id']}={z['nombre']}" for z in self.zonas)
                 + f" ({cargadas})" if self.zonas
                 else "SIN ROI (stream limpio)"))

        # YOLO de personas + ByteTrack: instancia propia por detector (el panel
        # corre un detector por fuente; el tracker no debe compartirse).
        # Con yolo11x (sin keypoints) el filtro de pose queda inactivo — la
        # máscara estricta del ROI es la defensa anti-falsos-positivos.
        print(f"[{self.NAME}] Cargando detector de personas: {a.person_model}")
        task = "pose" if "pose" in Path(a.person_model).stem else None
        self.pose = YOLO(a.person_model, task=task)
        self._pose_lock = threading.Lock()   # runtime vs registro por fotos

        # Rostros (corroboración de identidad; compartido entre fuentes)
        self.faces = None
        if not a.no_faces:
            # PORT-MONOLITO (definido arriba en este archivo): from .face_engine import get_face_engine  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)
            self.faces = get_face_engine()

        # ReID ONNX + base de empleados. Motor COMPARTIDO entre todas las
        # camaras (misma ruta de pesos): 1 sesion ONNX en VRAM en vez de N.
        self.reid = get_shared_reid_engine(Path(a.reid_onnx))
        self.db = EmployeeDB(a.db)
        # position_primary/face_th/etc. YA viven en self (sembrados en
        # configure(), posiblemente re-escritos por set_params antes de
        # llegar aqui) — setup() solo los LEE.
        if not self.reid.ok and not self.position_primary:
            print(f"[{self.NAME}] SIN ReID: cambiando a modo POSITION-PRIMARY")
            self.position_primary = True
        self._seed_recenter()
        self.registry = RegistryDB(self.reid, self.db,
                                   faces=self.faces, face_th=self.face_th,
                                   reid_mode=self.reid_mode,
                                   min_frac=self.min_frac,
                                   face_grace=self.face_grace,
                                   face_required=self.face_required)

        # Tiempo virtual (segundos DE VIDEO)
        fps = a.fps
        # FUSION: en modo panel no existe a.source (es un flag de CLI) — el
        # panel ahora entrega self.source_path (ruta cruda de ESTA fuente),
        # asi que el autodetect tambien funciona ahi. Con videos subidos
        # (normalizados a 15 fps exactos por ffmpeg) esto elimina el sesgo
        # del default de 25: una "tolerancia de 10 s" volvia a durar 16.7 s
        # reales sin avisar. RTSP no es un archivo: cae al default como
        # siempre (los frames llegan en tiempo real y 25 aproxima bien).
        src = str(getattr(self, "source_path", "") or getattr(a, "source", ""))
        if fps <= 0 and os.path.isfile(src):
            cap = cv2.VideoCapture(src)
            f = cap.get(cv2.CAP_PROP_FPS)
            cap.release()
            if f and 1.0 < f < 240.0:
                fps = f
                print(f"[{self.NAME}] FPS autodetectado: {fps:.1f} ({src})")
        if fps <= 0:
            fps = 25.0
        self.eff_fps = fps / (1 + max(0, a.frame_skip))
        # Un veredicto POR ZONA (creados al vuelo en _verdict): varias cajas
        # en un mismo encuadre son independientes entre si.
        self._verdicts: dict = {}
        self.yolo_interval = max(1, int(round(self.eff_fps / YOLO_FPS)))
        self.enroll_interval = max(1, int(round(self.eff_fps / ENROLL_SAMPLE_FPS)))

        # (las copias vivas de los PARAMS se siembran en configure(), no
        #  aqui — ver ese metodo para el porque)
        self._roi_mask = None
        self._region_data = None   # [(bbox, submask_estricta|None)] por polígono
        # ByteTrack PROPIO por cámara: la detección corre por-región (un lote
        # de N recortes al mismo YOLO) y el rastreo se hace aparte sobre las
        # detecciones fusionadas — model.track() solo admite una imagen.
        self.tracker = self._new_tracker()
        self._dets: list = []
        self._n = 0
        self._prev_states: dict = {}   # ultimo estado visto por zona
        self._proc_frame = None
        self.counts: dict = {}
        self._enroll = None      # job de registro en curso (dict) o None
        mode = "POSITION-PRIMARY" if self.position_primary else "EMPLEADO-PRIMARIO"
        print(f"[{self.NAME}] modo={mode} | eff_fps={self.eff_fps:.1f} | "
              f"detección cada {self.yolo_interval} frame(s)")

    @staticmethod
    def _new_tracker():
        """ByteTrack independiente (los IDs son únicos a nivel proceso: el
        contador de STrack es global, así que no colisionan entre cámaras)."""
        from ultralytics.trackers.byte_tracker import BYTETracker
        from ultralytics.utils import IterableSimpleNamespace, YAML
        from ultralytics.utils.checks import check_yaml
        cfg = IterableSimpleNamespace(**YAML.load(check_yaml("bytetrack.yaml")))
        return BYTETracker(cfg)

    def _verdict(self, zona: int) -> "PresenceVerdict":
        """Veredicto de UNA zona. Se crea al vuelo con los parametros
        VIGENTES: una zona puede aparecer despues de un ajuste en caliente."""
        v = self._verdicts.get(zona)
        if v is None:
            v = self._verdicts[zona] = PresenceVerdict(
                self.eff_fps, self.tolerance,
                position_primary=self.position_primary,
                rise_seconds=self.rise_seconds)
        return v

    def _podar_verdicts(self, zonas) -> None:
        """Olvida las zonas que ya no existen. `zonas` son IDs estables:
        una zona que sobrevive a la edicion conserva su veredicto (antes,
        con indices, el poligono siguiente HEREDABA el del borrado)."""
        for z in [z for z in self._verdicts if z not in zonas]:
            self._verdicts.pop(z, None)
            self._prev_states.pop(z, None)

    def _seed_recenter(self):
        """Siembra el re-centrado con la base (motor COMPARTIDO: los nombres
        que ya sembró otra cámara no se repiten, ver ReIDEngine.seed_names)."""
        if not self.reid.ok:
            return
        added = self.reid.seed_names(self.db)
        print(f"[REID] +{added} nombre(s) sembrado(s) (motor compartido, "
              f"n={self.reid._n}, "
              f"{'operable' if self.reid.warm else 'warmup pendiente'})")

    # ------------------------------------------------------------------ #
    #  Parámetros ajustables en caliente (ParamsMixin) — propagar a las
    #  estructuras internas que guardan su PROPIA copia del valor.
    # ------------------------------------------------------------------ #
    def on_params_changed(self, changed: set):
        if "tolerance" in changed:
            for v in self._verdicts.values():
                v.tolerance_seconds = self.tolerance
                v.decay_step = 1.0 / max(1.0, self.tolerance * self.eff_fps)
        if "rise_seconds" in changed:
            for v in self._verdicts.values():
                v.rise_step = 1.0 / max(1.0, self.rise_seconds * self.eff_fps)
        if "position_primary" in changed:
            for v in self._verdicts.values():
                v.position_primary = self.position_primary
        if "face_th" in changed:
            self.registry.face_th = self.face_th
        if "reid_mode" in changed:
            self.registry.reid_mode = self.reid_mode
        if "min_frac" in changed:
            self.registry.min_frac = self.min_frac
        if "face_grace" in changed:
            self.registry.face_grace = self.face_grace
        if "face_required" in changed:
            self.registry.face_required = self.face_required
        if "yolo_margin" in changed or "strict_roi" in changed:
            # mascara y recortes por region se reconstruyen en el proximo _track()
            self._roi_mask = None
            self._region_data = None
        print(f"[{self.NAME}] parámetros actualizados: "
              + ", ".join(f"{k}={getattr(self, k, None)}" for k in sorted(changed)))

    # ------------------------------------------------------------------ #
    #  ROI editable / borrable desde el panel
    # ------------------------------------------------------------------ #
    def get_regions(self):
        if not hasattr(self, "args"):
            return None
        return list(self.regions) if self.regions else []

    def set_regions(self, regions):
        """LEGACY (fusion Queue-Cashier): wrapper del protocolo tipado.
        casar_ids() preserva el id de todo poligono cuyos puntos no
        cambiaron, para que un guardado por este camino viejo no re-baraje
        ids (y con ellos, los veredictos y el historial)."""
        try:
            nuevas = _zonas.desde_legacy(regions or [], self.NAME)
        except ValueError as e:
            print(f"[{self.NAME}] set_regions legacy: ROI invalido ({e}) "
                  f"— se ignora")
            return False
        _zonas.casar_ids(nuevas, self.zonas)
        return self.set_zonas(nuevas)

    def _sincronizar_zonas(self):
        """Deriva de self.zonas las vistas que consume la maquinaria
        interna: self.regions (puntos alineados; None = sin ROI, el
        centinela historico de este detector) y self._zona_ids
        (posicion -> id estable, para traducir lo que devuelve _box_zone).
        Invalida mascaras: se reconstruyen en el proximo _track()."""
        self.zonas = list(self.zonas or [])
        self.regions = _zonas.a_regions(self.zonas) or None
        self._zona_ids = [z["id"] for z in self.zonas]
        self._zonas_por_id = {z["id"]: z for z in self.zonas}
        self._region_data = None
        self._roi_mask = None

    def set_zonas(self, zonas):
        """Camino REAL de recepcion de zonas (panel fusionado). Los
        veredictos viven keyed por ID: una zona que sobrevive a la edicion
        CONSERVA su tolerancia acumulada y su estado previo, y una borrada
        se olvida (la poda por id ya la hace _podar_verdicts en el
        proximo frame). Solo el caso SIN zonas resetea el tracking entero
        (reposo total, mismo comportamiento historico)."""
        self.zonas = list(zonas or [])
        self._sincronizar_zonas()
        if self.regions:
            print(f"[{self.NAME}] ROI actualizado en caliente: "
                  + ", ".join(f"{z['id']}={z['nombre']}" for z in self.zonas))
        else:
            # sin ROI: reposo total y stream limpio
            self.registry.tracks.clear()
            self.registry.seen_now = set()
            self._dets = []
            self.tracker = self._new_tracker()   # rastreo desde cero
            self._verdicts.clear()
            self._prev_states.clear()
            print(f"[{self.NAME}] ROI eliminado: stream limpio, monitoreo en reposo")
        return True

    def get_zonas(self):
        return list(getattr(self, "zonas", None) or [])

    # ------------------------------------------------------------------ #
    #  Detección / tracking (coords 800x600)
    # ------------------------------------------------------------------ #
    def _detect_full(self, frame, imgsz, conf):
        """Detección full-frame SIN tracking (registro). -> boxes (n,4)"""
        with self._pose_lock:
            res = self.pose.predict(frame, imgsz=imgsz, conf=conf,
                                    iou=PERSON_IOU, classes=[0],
                                    verbose=False)[0]
        if res.boxes is None or not len(res.boxes):
            return np.empty((0, 4))
        boxes = res.boxes.xyxy.cpu().numpy()
        if res.keypoints is not None and len(res.keypoints.data) == len(boxes):
            kc = res.keypoints.data.cpu().numpy()
            if kc.shape[1] > 0:
                boxes = boxes[kc[:, :, 2].mean(axis=1) >= self.args.kpt_min]
        return boxes

    def _build_regions(self, H, W):
        """Máscara combinada (pertenencia al ROI) + un recorte POR POLÍGONO:
        cada región se procesa con su propio zoom — dos regiones alejadas ya
        no comparten un rectángulo envolvente lleno de fondo negro."""
        # Mapa de ETIQUETAS: cada pixel guarda el indice del poligono +1
        # (0 = fuera). Sigue funcionando como mascara binaria con `> 0` —de
        # ahi que _box_in_regions no cambie— y ademas permite saber EN QUE
        # zona esta cada persona. Si dos poligonos se solapan, gana el ultimo
        # dibujado. Tope de 255 zonas por el uint8 (de sobra).
        mask = np.zeros((H, W), np.uint8)
        for i, r in enumerate(self.regions):
            cv2.fillPoly(mask, [np.array(r, np.int32)], i + 1)
        self._roi_mask = mask
        self._region_data = []
        for r in self.regions:
            bb = roi_bbox([r], (H, W), self.yolo_margin)
            if bb is None:
                continue
            smask = None
            if self.strict_roi:
                bx0, by0, bx1, by1 = bb
                smask = np.zeros((by1 - by0, bx1 - bx0), np.uint8)
                pts = np.array(r, np.int32) - [bx0, by0]
                cv2.fillPoly(smask, [pts], 255)
                if self.yolo_margin > 0:
                    pbb = roi_bbox([r], (H, W), 0.0)
                    kx = 2 * max(1, int(self.yolo_margin * (pbb[2] - pbb[0]))) + 1
                    ky = 2 * max(1, int(self.yolo_margin * (pbb[3] - pbb[1]))) + 1
                    smask = cv2.dilate(smask, cv2.getStructuringElement(
                        cv2.MORPH_ELLIPSE, (kx, ky)))
            self._region_data.append((bb, smask))

    @staticmethod
    def _dedupe(boxes, confs, kmeans, iou_th=0.6):
        """Regiones solapadas pueden detectar a la MISMA persona dos veces:
        supresión codiciosa por confianza entre las detecciones fusionadas."""
        order = np.argsort(-np.asarray(confs))
        keep = []
        for i in order:
            bi = boxes[i]
            dup = False
            for j in keep:
                bj = boxes[j]
                ix1, iy1 = max(bi[0], bj[0]), max(bi[1], bj[1])
                ix2, iy2 = min(bi[2], bj[2]), min(bi[3], bj[3])
                inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                ua = ((bi[2]-bi[0])*(bi[3]-bi[1])
                      + (bj[2]-bj[0])*(bj[3]-bj[1]) - inter)
                if ua > 0 and inter / ua > iou_th:
                    dup = True
                    break
            if not dup:
                keep.append(i)
        keep = sorted(keep)
        return ([boxes[i] for i in keep], [confs[i] for i in keep],
                [kmeans[i] for i in keep])

    def _track(self, frame):
        H, W = frame.shape[:2]
        if self._roi_mask is None or self._region_data is None:
            self._build_regions(H, W)

        # ── recorte por región (zoom independiente), TODOS en un solo lote ──
        crops, offsets = [], []
        for (x0, y0, x1, y1), smask in self._region_data:
            crop = frame[y0:y1, x0:x1]
            if smask is not None:
                crop = crop.copy()
                crop[smask == 0] = 0
            crops.append(crop)
            offsets.append((x0, y0))

        boxes_all, confs_all, kmeans_all = [], [], []
        if crops:
            with self._pose_lock:
                results = self.pose.predict(crops, imgsz=PERSON_IMGSZ,
                                            conf=self.person_conf, iou=PERSON_IOU,
                                            classes=[0], verbose=False)
            for res, (x0, y0) in zip(results, offsets):
                if res.boxes is None or not len(res.boxes):
                    continue
                b = res.boxes.xyxy.cpu().numpy().copy()
                c = res.boxes.conf.cpu().numpy()
                km = (res.keypoints.data.cpu().numpy()[:, :, 2].mean(axis=1)
                      if res.keypoints is not None
                      and len(res.keypoints.data) == len(b)
                      else np.ones(len(b)))
                b[:, [0, 2]] += x0
                b[:, [1, 3]] += y0
                for i in range(len(b)):
                    if km[i] < self.args.kpt_min:
                        continue
                    boxes_all.append(b[i])
                    confs_all.append(float(c[i]))
                    kmeans_all.append(float(km[i]))
        if len(boxes_all) > 1:
            boxes_all, confs_all, kmeans_all = self._dedupe(
                boxes_all, confs_all, kmeans_all)

        # ── ByteTrack propio (se actualiza también con 0 detecciones, para
        #    que los tracks perdidos envejezcan igual que antes) ──────────────
        from ultralytics.engine.results import Boxes
        import torch as _torch
        n = len(boxes_all)
        data = np.concatenate(
            [np.asarray(boxes_all, np.float32).reshape(n, 4),
             np.asarray(confs_all, np.float32).reshape(n, 1),
             np.zeros((n, 1), np.float32)], axis=1) if n else \
            np.zeros((0, 6), np.float32)
        tracks = self.tracker.update(
            Boxes(_torch.from_numpy(data), (H, W)).numpy(), frame)

        dets = []
        for t in np.asarray(tracks).reshape(-1, 8):
            tid = int(t[4])
            src = int(t[7])                     # índice de la detección origen
            km = kmeans_all[src] if 0 <= src < n else 1.0
            dets.append((tid, t[:4].astype(np.float64), float(km)))
        return dets

    # ------------------------------------------------------------------ #
    def process_frame(self, frame, frame_idx):
        frame = _rescale(frame)              # V4: TODO en 800x600
        self._proc_frame = frame
        # Reinicio TECNICO de la fuente (bucle de video o reconexion RTSP):
        # frame_idx retrocede — misma senal y mismo motivo que
        # QueueAnalytics._manejar_reinicio_fuente. Sin esto, los veredictos
        # por zona ARRASTRAN la confianza de la pasada anterior: el video
        # reinicia con la zona vacia pero el veredicto sigue en
        # TOLERANCIA decayendo, y en el compuesto eso mantiene a los
        # clientes como ATENDIENDO frente a una caja visiblemente sola
        # (visto en Grabacion_2026-08-11_182824 el 2026-08-19). El reloj
        # interno (self._n) no retrocede, asi que solo hay que olvidar el
        # ESTADO de escena: veredictos y ultimo-estado-por-zona; los
        # tracks mueren solos por TRACK_DEATH_S.
        if frame_idx < getattr(self, "_ultimo_frame_idx", 0):
            if self._verdicts or self._prev_states:
                self._verdicts.clear()
                self._prev_states.clear()
                print(f"[{self.NAME}] Reinicio tecnico de fuente detectado "
                      f"(frame_idx retrocedio): veredictos por zona a cero.")
        self._ultimo_frame_idx = frame_idx
        now = self._n / self.eff_fps

        if self._enroll is not None:
            self._enroll_tick(frame, now)

        if not self.regions:                 # sin ROI: reposo, stream limpio
            self._n += 1
            return {"alert": False, "label": "", "score": None,
                    "boxes": [], "extra": "", "state": "SIN_ROI"}

        if self._n % self.yolo_interval == 0:
            self._dets = self._track(frame)
            mask = self._roi_mask
            if mask is None:
                in_roi_map = {tid: None for tid, _, _ in self._dets}
            else:
                # _box_zone devuelve el INDICE del poligono (1-based, es lo
                # que codifica la mascara de etiquetas); se traduce AQUI a
                # id estable y todo lo demas (veredictos, JSON, overlay)
                # trabaja con ids. zona_ids se captura junto con la mascara
                # (misma referencia de tiempo) por si set_zonas() cruza a
                # mitad de frame desde el hilo del panel.
                zona_ids = self._zona_ids
                def _a_id(idx):
                    return (zona_ids[idx - 1]
                            if idx is not None and 0 < idx <= len(zona_ids)
                            else None)
                in_roi_map = {tid: _a_id(_box_zone(box, mask, self.roi_overlap))
                              for tid, box, _ in self._dets}
            self.registry.update(self._dets, now, frame, self.match_th, in_roi_map)
            for tid, box, _ in self._dets:
                t = self.registry.tracks.get(tid)
                if t is not None:
                    t.in_roi = in_roi_map[tid]
            self.registry.enrich(frame, now)
        self._n += 1

        in_roi = [t for t in self.registry.tracks.values()
                  if t.in_roi is not None and t.tid in self.registry.seen_now]
        por_zona: dict = {}
        for t in in_roi:
            por_zona.setdefault(t.in_roi, []).append(t)

        # ── un veredicto POR ZONA: cada poligono es una caja independiente,
        #    keyed por ID ESTABLE (borrar una zona ya no corre el veredicto
        #    de las demas) ──
        zonas = list(self._zona_ids)
        self._podar_verdicts(zonas)
        varias = len(zonas) > 1
        estado_zona: dict = {}
        for z in zonas:
            ts = por_zona.get(z, [])
            v = self._verdict(z)
            v.update(any(t.estado == "VERIFICADO" for t in ts), bool(ts))
            sz = v.state
            # "PERSONA EN ZONA DE CAJERO, NO RECONOCIDA": distingue el caso
            # donde YA se intentó identificar a alguien en la zona y el
            # rostro/cuerpo lo descartó explícitamente (estado DESCONOCIDO),
            # de simplemente estar todavía verificando (PENDIENTE) — más
            # específico que el genérico "PERSONA_NO_CAJERO" para el operador
            # del panel. No implica que la persona traiga puesto ningún
            # uniforme: es solo presencia + rechazo explícito de identidad.
            if sz == "PERSONA_NO_CAJERO" and any(
                    t.estado == "DESCONOCIDO" for t in ts):
                sz = "PERSONA_NO_RECONOCIDA"
            estado_zona[z] = sz

        # el estado de la CAMARA es el PEOR de sus zonas: una caja sola no
        # debe quedar tapada por otra que si esta atendida. Con una sola zona
        # esto es exactamente el estado de antes.
        st = _peor_estado(set(estado_zona.values()))
        self.counts[st] = self.counts.get(st, 0) + 1

        # alerta por flanco POR ZONA (P4: se ignora la primera asignación de
        # estado). Basta que UNA zona entre en alerta: el snapshot es del
        # frame completo y las contiene a todas.
        alert_states = {"DESATENDIDA"}
        if self.alert_unknown:
            alert_states.add("PERSONA_NO_CAJERO")
            alert_states.add("PERSONA_NO_RECONOCIDA")
        alert = False
        for z in zonas:
            sz, prev = estado_zona[z], self._prev_states.get(z)
            if sz != prev:
                if sz in alert_states and prev is not None:
                    alert = True
                zt = f" {z}" if varias else ""
                print(f"[CAJA{zt}] t={now:7.1f}s  {prev} -> {sz}  "
                      f"conf={self._verdict(z).presence_conf:.2f}")
            self._prev_states[z] = sz

        boxes = []
        for t in self.registry.tracks.values():
            if t.box is None or now - t.last_seen > GHOST_HIDE_S:
                continue
            label = (t.empleado if t.estado == "VERIFICADO"
                     else t.estado.lower())
            conf = t.best_sim if t.best_sim > -1.0 else None
            # permanencia = vida del track. OJO: TRACK_DEATH_S corta a los 5 s
            # sin ver a la persona, asi que una oclusion larga reinicia el
            # contador con un tid nuevo. Es tiempo de ESTE track, no de la
            # persona a lo largo del turno.
            perm = _fmt_perm(now - t.born)
            zt = f" {t.in_roi}" if (varias and t.in_roi is not None) else ""
            boxes.append((*t.box.tolist(),
                          f"id{t.tid}{zt} {label} [{perm}]", conf))

        # permanencia por empleado: si dos tracks reclaman el mismo nombre
        # (religado tras oclusion) se muestra el mas antiguo de los dos
        def _perm_empleados(ts):
            d: dict = {}
            for t in ts:
                if t.estado == "VERIFICADO" and t.empleado:
                    d[t.empleado] = max(d.get(t.empleado, 0.0), now - t.born)
            return d

        # detalle POR ZONA: rotula cada poligono en draw() y es la base del
        # JSON de salida por zona
        # "zona" es el ID ESTABLE (string, ej. "z2"): es lo que hace que un
        # historial guardado siga siendo cierto despues de borrar/redibujar
        # poligonos. El nombre editable viene del registro de zonas.
        zonas_info = [{
            "zona": z,
            "nombre": self._zonas_por_id[z]["nombre"]
                      if z in self._zonas_por_id else str(z),
            "estado": estado_zona[z],
            "personas": len(por_zona.get(z, [])),
            "conf": round(self._verdict(z).presence_conf, 3),
            "empleados": [{"nombre": k, "permanencia_s": round(v, 1)}
                          for k, v in sorted(
                              _perm_empleados(por_zona.get(z, [])).items())],
        } for z in zonas]

        # nombre(s) del empleado verificado en el ROI -> banner y extra
        perm_por_nombre = _perm_empleados(in_roi)
        nombres = sorted(perm_por_nombre)
        label = _STATE_STYLE[st][1]
        if nombres:
            # solo ASCII: las fuentes de cv2 no dibujan em-dash ni acentos
            label += ": " + ", ".join(
                f"{n} ({_fmt_perm(perm_por_nombre[n])})" for n in nombres)

        extra = label
        if st == "TOLERANCIA":
            # la zona que impone el estado de la camara
            z_st = next((z for z in zonas if estado_zona[z] == st), None)
            if z_st is not None:
                extra += f" ({self._verdict(z_st).tolerance_remaining_s:.0f}s)"
        if varias:
            extra += " | " + " ".join(
                f"z{i['zona']}:{_STATE_CORTO.get(i['estado'], i['estado'])}"
                for i in zonas_info)
        extra += (f" | en ROI: {len(in_roi)} | match: {self.registry.n_matched}"
                  f" | rostro: {self.registry.n_face_matched}"
                  f" | vetos: {self.registry.n_face_veto}"
                  f" | religados: {self.registry.n_relinked}")

        # score de la camara = el de la zona PEOR (coherente con `st`)
        score = min((self._verdict(z).presence_conf for z in zonas),
                    default=0.0)
        return {"alert": alert, "label": label,
                "score": score, "boxes": boxes,
                "extra": extra, "state": st, "zonas": zonas_info}

    # ------------------------------------------------------------------ #
    #  Registro de empleados: por segmento del stream en vivo
    # ------------------------------------------------------------------ #
    def start_enroll(self, name, seconds, use_roi):
        name = re.sub(r"[^A-Za-z0-9_-]", "_", str(name)).strip("_")
        if not name or not self.reid.ok:
            return False
        if self._enroll is not None and not self._enroll.get("done"):
            return False    # ya hay una captura EN CURSO (una terminada no bloquea)
        self._enroll = {
            "id": f"{name}-{int(time.time() * 1000)}",
            "name": name,
            "seconds": max(5.0, float(seconds or 20.0)),
            "use_roi": bool(use_roi),
            "t0": None,
            "samples_e": [], "samples_c": [], "samples_f": [], "samples_fc": [],
            "skipped": 0,
            "active": True, "done": False, "ok": None,
            "msg": "capturando...", "progress": 0.0,
        }
        print(f"[ENROLL] '{name}': captura de {self._enroll['seconds']:.0f}s "
              f"iniciada ({'en el ROI' if use_roi else 'frame completo'})")
        return True

    def enroll_status(self):
        j = self._enroll
        if j is None:
            return {"active": False}
        return {"active": j["active"], "done": j["done"], "ok": j["ok"],
                "id": j["id"], "name": j["name"], "msg": j["msg"],
                "progress": round(j["progress"], 3),
                "captured": len(j["samples_e"]), "skipped": j["skipped"]}

    def _enroll_tick(self, frame, now):
        j = self._enroll
        if j is None or j["done"]:
            return
        if j["t0"] is None:
            j["t0"] = now
        j["progress"] = min(1.0, (now - j["t0"]) / j["seconds"])
        if now - j["t0"] >= j["seconds"]:
            self._finalize_enroll()
            return
        if self._n % self.enroll_interval != 0:
            return
        # candidatos: en el ROI (mismo camino del runtime, solape inclusivo)
        # o full-frame con conf permisiva — como enroll_employee_v4.py
        if j["use_roi"] and self.regions:
            if self._n % self.yolo_interval == 0 or not self._dets:
                self._dets = self._track(frame)
            mask = self._roi_mask
            cand = [box for _, box, _ in self._dets
                    if mask is not None and _box_in_regions(box, mask, 0.0)]
        else:
            cand = list(self._detect_full(frame, PERSON_IMGSZ, PERSON_CONF))
        if not cand:
            return
        if len(cand) > 1:
            # frame AMBIGUO: 2+ personas — no se captura nada de este frame
            j["skipped"] += 1
            return
        b = np.asarray(cand[0]).astype(int)
        H, W = frame.shape[:2]
        crop = frame[max(0, b[1]):min(H, b[3]), max(0, b[0]):min(W, b[2])]
        e = self.reid.embed(crop)
        if e is None:
            return
        j["samples_e"].append(e)
        j["samples_c"].append(crop)
        if self.faces is not None:
            head = _head_region(crop)
            fe = self.faces.extract(head) if head is not None else None
            if fe is not None:
                j["samples_f"].append(fe)
                j["samples_fc"].append(head)
        j["msg"] = (f"capturando... {len(j['samples_e'])} vistas"
                    f" ({len(j['samples_f'])} con rostro)")

    def _finalize_enroll(self):
        j = self._enroll
        j["active"] = False
        ok, msg = self._save_gallery(j["name"], j["samples_e"], j["samples_c"],
                                     photos_mode=False, faces=j["samples_f"],
                                     face_crops=j["samples_fc"])
        if j["skipped"]:
            msg += f" ({j['skipped']} frames ambiguos omitidos)"
        j.update(done=True, ok=ok, msg=msg, progress=1.0)
        print(f"[ENROLL] '{j['name']}': {'OK — ' if ok else 'FALLÓ — '}{msg}")

    # ------------------------------------------------------------------ #
    #  Registro de empleados: por fotografías
    # ------------------------------------------------------------------ #
    def enroll_photos(self, name, images):
        name = re.sub(r"[^A-Za-z0-9_-]", "_", str(name)).strip("_")
        if not name:
            return False, "Nombre inválido"
        if not self.reid.ok:
            return False, "ReID inactivo (sin modelo ONNX)"
        samples_e, samples_c, samples_f, samples_fc = [], [], [], []
        n_amb = n_bad = 0
        for img in images:
            if img is None or img.size == 0:
                n_bad += 1
                continue
            s = 1280.0 / max(img.shape[:2])
            if s < 1.0:
                img = cv2.resize(img, None, fx=s, fy=s,
                                 interpolation=cv2.INTER_AREA)
            boxes = self._detect_full(img, 640, PERSON_CONF_RUN)
            if not len(boxes):
                crop = img          # sin detección: se usa la foto completa
            else:
                areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
                order = np.argsort(areas)[::-1]
                if len(boxes) > 1 and areas[order[1]] > 0.5 * areas[order[0]]:
                    n_amb += 1      # 2+ personas comparables: foto ambigua
                    continue
                b = boxes[order[0]].astype(int)
                h_, w_ = img.shape[:2]
                crop = img[max(0, b[1]):min(h_, b[3]), max(0, b[0]):min(w_, b[2])]
            e = self.reid.embed(crop)
            if e is None:
                n_bad += 1
                continue
            samples_e.append(e)
            samples_c.append(crop)
            if self.faces is not None:
                head = _head_region(crop)
                fe = self.faces.extract(head) if head is not None else None
                if fe is not None:
                    samples_f.append(fe)
                    samples_fc.append(head)
        ok, msg = self._save_gallery(name, samples_e, samples_c,
                                     photos_mode=True, faces=samples_f,
                                     face_crops=samples_fc)
        if n_amb or n_bad:
            msg += f" ({n_amb} ambiguas, {n_bad} ilegibles)"
        print(f"[ENROLL-FOTOS] '{name}': {'OK — ' if ok else 'FALLÓ — '}{msg}")
        return ok, msg

    # ------------------------------------------------------------------ #
    def _save_gallery(self, name, samples_e, samples_c, photos_mode, faces=None,
                      face_crops=None):
        if len(samples_e) < ENROLL_MIN_VIEWS:
            return False, (f"solo {len(samples_e)} vistas útiles — se necesitan "
                           f">= {ENROLL_MIN_VIEWS} (¿persona muy lejos, tapada "
                           f"o siempre acompañada?)")
        # filtro de coherencia: en lotes chicos de FOTOS el centrado por lote
        # expulsaría vistas legítimas; la curación humana hace ese papel
        if photos_mode and len(samples_e) < 12:
            keep, dropped = list(range(len(samples_e))), []
        else:
            keep, dropped = _filter_coherent(samples_e, ENROLL_COHERENCE_TH)
        if len(keep) < ENROLL_MIN_VIEWS:
            return False, (f"solo {len(keep)} vistas coherentes — la ventana "
                           f"parece contaminada; repetir con el empleado solo")
        gallery, crops = [], []
        for i in keep:
            _diverse_add(gallery, crops, samples_e[i], samples_c[i], ENROLL_K)

        # galería FACIAL: filtro de coherencia propio (bug real encontrado
        # 2026-08-16: sin esto, UN solo rostro mal capturado -blur, ángulo,
        # oclusión- queda en la galería para siempre y basta que un
        # desconocido se parezca a ESE vector para verificar por rostro, sin
        # importar qué tan alto esté el umbral — el match es por MÁXIMO sobre
        # la galería, no por consenso). NO reutiliza _filter_coherent: esa
        # función re-centra pensando en TransReID (coseno crudo ~0.99 a todo);
        # en ArcFace el coseno crudo YA discrimina, re-centrar lo distorsiona.
        faces = faces or []
        if photos_mode and len(faces) < 12:
            fkeep, fdropped_idx = list(range(len(faces))), []
        else:
            fkeep, fdropped_idx = _filter_coherent_faces(faces)
        if fdropped_idx:
            print(f"[ENROLL] {len(fdropped_idx)} rostro(s) descartado(s) por "
                  f"incoherentes con el resto de la galería facial")
        face_crops = face_crops or []
        face_gallery: list = []
        face_thumbs: list = []
        for i in fkeep:
            fe = faces[i]
            fc = (cv2.resize(face_crops[i], (64, 64))
                  if i < len(face_crops) else np.zeros((64, 64, 3), np.uint8))
            if len(face_gallery) < ENROLL_K:
                face_gallery.append(fe)
                face_thumbs.append(fc)
                continue
            with np.errstate(all="ignore"):
                sims = [float(fe @ g) for g in face_gallery]
            sims = [s if np.isfinite(s) else 2.0 for s in sims]
            j = int(np.argmax(sims))
            face_gallery[j] = fe
            face_thumbs[j] = fc

        db = Path(self.args.db)
        db.mkdir(parents=True, exist_ok=True)
        thumbs = np.stack([cv2.resize(c, (48, 96)) for c in crops])
        arrays = {"embeddings": np.stack(gallery), "thumbs": thumbs}
        if face_gallery:
            arrays["faces"] = np.stack(face_gallery)
            arrays["face_thumbs"] = np.stack(face_thumbs)
        np.savez_compressed(db / f"{name}.npz", **arrays)
        # montaje de verificación visual
        try:
            out_dir = db.parent / "out"
            out_dir.mkdir(exist_ok=True)
            tiles = [cv2.resize(c, (96, 192)) for c in crops]
            montage = np.hstack(tiles)
            cv2.putText(montage, name, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 255, 255), 2, cv2.LINE_AA)
            cv2.imwrite(str(out_dir / f"enroll_{name}.png"), montage)
        except Exception:  # noqa: BLE001
            pass
        self.reload_db()
        msg = (f"{len(gallery)} vistas corporales + {len(face_gallery)} "
               f"faciales (de {len(samples_e)} muestras)")
        if dropped:
            msg += f", {len(dropped)} descartadas por incoherentes"
        if not face_gallery and self.faces is not None:
            msg += " — SIN rostro utilizable: solo se identificará por cuerpo"
        return True, msg

    def reload_db(self):
        """Recarga empleados/*.npz; el motor de ReID (compartido entre
        cámaras) evita re-sembrar nombres que ya sembró cualquier otra."""
        db2 = EmployeeDB(self.args.db)
        if self.reid.ok:
            self.reid.seed_names(db2)
        self.db = db2
        self.registry.db = db2

    # ------------------------------------------------------------------ #
    #  Administración de empleados (panel web)
    # ------------------------------------------------------------------ #
    def list_identities(self):
        import base64
        out = []
        for f in sorted(Path(self.args.db).glob("*.npz")):
            try:
                z = np.load(f)
                views = int(z["embeddings"].shape[0])
                n_faces = int(z["faces"].shape[0]) if "faces" in z.files else 0
                thumb = None
                if "thumbs" in z.files and len(z["thumbs"]):
                    ok, buf = cv2.imencode(
                        ".jpg", z["thumbs"][0],
                        [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok:
                        thumb = base64.b64encode(buf.tobytes()).decode()
                out.append({"name": f.stem, "views": views,
                            "faces": n_faces, "thumb": thumb})
            except Exception as e:  # noqa: BLE001
                print(f"[DB] no pude leer {f.name}: {e}")
        return out

    def delete_identity(self, name):
        name = re.sub(r"[^A-Za-z0-9_-]", "_", str(name)).strip("_")
        if not name:
            return False
        p = Path(self.args.db) / f"{name}.npz"
        existed = p.exists()
        try:
            p.unlink()
        except FileNotFoundError:
            pass    # otra fuente ya borró el archivo; purgar estado igual
        # purga en caliente: pool de sesión y tracks ligados a esa identidad
        self.registry.pool = [q for q in self.registry.pool
                              if q.get("name") != name]
        for t in self.registry.tracks.values():
            if t.empleado == name:
                t.estado = "PENDIENTE"
                t.empleado = None
                t.gallery = []
                t.checks = 0
                t.best_sim = -1.0
                t.last_check = -1e9
        self.reid.seeded_names.discard(name)
        self.reload_db()
        if existed:
            print(f"[DB] empleado '{name}' ELIMINADO (archivo + estado)")
        return True

    def get_identity_views(self, name):
        """Vistas individuales de cuerpo Y rostro con miniatura — para que un
        humano revise y decida qué borrar. La marca de "sospechosa" NO usa un
        umbral inventado para la UI: reutiliza las MISMAS funciones que ya
        deciden qué se descarta al registrar (_filter_coherent para cuerpo,
        _filter_coherent_faces para rostro), corridas contra la galería tal
        como está guardada HOY. Así se encontró y corrigió el bug real del
        2026-08-16 (un rostro con coherencia +0.03 dejaba pasar a cualquiera).
        Nota: cuerpo y rostro viven en escalas distintas (cuerpo re-centrado
        puede dar negativos incluso para vistas buenas) — por eso NO se usa
        un corte absoluto de coherencia para la marca visual, solo para
        mostrar el número de referencia."""
        import base64
        name = re.sub(r"[^A-Za-z0-9_-]", "_", str(name)).strip("_")
        p = Path(self.args.db) / f"{name}.npz"
        if not p.exists():
            return None
        try:
            z = np.load(p)
        except Exception:
            return None

        def thumb_b64(arr_bgr):
            ok, buf = cv2.imencode(".jpg", arr_bgr, [cv2.IMWRITE_JPEG_QUALITY, 82])
            return base64.b64encode(buf.tobytes()).decode() if ok else None

        def coherence(E, recenter):
            if len(E) < 3:
                return [None] * len(E)
            C = (E - E.mean(axis=0)) if recenter else E
            C = C / (np.linalg.norm(C, axis=1, keepdims=True) + 1e-9)
            with np.errstate(all="ignore"):
                S = C @ C.T
            np.fill_diagonal(S, np.nan)
            avg = np.nanmean(S, axis=1)
            return [None if not np.isfinite(v) else round(float(v), 3) for v in avg]

        out = {"body": [], "face": []}
        if "embeddings" in z.files:
            E = z["embeddings"].astype(np.float64)
            thumbs = z["thumbs"] if "thumbs" in z.files else None
            coh = coherence(E, recenter=True)
            _, dropped = (_filter_coherent(list(E), ENROLL_COHERENCE_TH)
                         if len(E) >= 3 else (list(range(len(E))), []))
            dropped_idx = {i for i, _ in dropped}
            for i in range(len(E)):
                out["body"].append({
                    "idx": i, "coherence": coh[i], "suspect": i in dropped_idx,
                    "thumb_b64": thumb_b64(thumbs[i]) if thumbs is not None else None,
                })
        if "faces" in z.files:
            F = z["faces"].astype(np.float64)
            thumbs_f = z["face_thumbs"] if "face_thumbs" in z.files else None
            coh = coherence(F, recenter=False)
            _, fdropped_idx = _filter_coherent_faces(list(F))
            for i in range(len(F)):
                out["face"].append({
                    "idx": i, "coherence": coh[i], "suspect": i in fdropped_idx,
                    "thumb_b64": thumb_b64(thumbs_f[i]) if thumbs_f is not None else None,
                })
        return out

    def delete_identity_view(self, name, kind, index):
        """Elimina UNA vista especifica (kind='body'|'face') de la galeria de
        `name`. Nota: el re-centrado COMPARTIDO del motor ReID (_sum/_n) no
        se recalcula retroactivamente al borrar una vista — su contribución
        se diluye sola con el uso normal; un reinicio del panel lo limpia
        del todo si hace falta."""
        name = re.sub(r"[^A-Za-z0-9_-]", "_", str(name)).strip("_")
        if kind not in ("body", "face"):
            return False
        p = Path(self.args.db) / f"{name}.npz"
        if not p.exists():
            return False
        try:
            z = np.load(p)
            arrays = {k: z[k] for k in z.files}
        except Exception:
            return False

        key, thumb_key = (("embeddings", "thumbs") if kind == "body"
                         else ("faces", "face_thumbs"))
        if key not in arrays or not (0 <= index < len(arrays[key])):
            return False
        keep = [i for i in range(len(arrays[key])) if i != index]
        arrays[key] = arrays[key][keep]
        if thumb_key in arrays:
            arrays[thumb_key] = arrays[thumb_key][keep]
        if kind == "face" and len(keep) == 0:
            arrays.pop("faces", None)
            arrays.pop("face_thumbs", None)

        np.savez_compressed(p, **arrays)
        for t in self.registry.tracks.values():
            if t.empleado == name:
                t.checks = 0   # re-evaluar con la galeria actualizada
        self.reload_db()
        print(f"[DB] '{name}': vista {kind}#{index} eliminada por revisión humana")
        return True

    # ------------------------------------------------------------------ #
    def annotate(self, frame, result, stats):
        base = self._proc_frame if self._proc_frame is not None else _rescale(frame)
        out = base.copy()
        st = result.get("state", "SIN_ROI")

        if st == "SIN_ROI":
            # stream LIMPIO: sin ROI no se dibuja nada (solo el progreso de
            # registro, si hay uno en curso)
            self._draw_enroll(out)
            return out

        overlay = out.copy()
        for r in (self.regions or []):
            pts = np.array(r, np.int32)
            cv2.fillPoly(overlay, [pts], (255, 200, 0))
            cv2.polylines(out, [pts], True, (255, 200, 0), 2)
        cv2.addWeighted(overlay, 0.15, out, 0.85, 0, out)
        # etiqueta "Zona de cajero N" (N = orden de dibujo) en cada poligono.
        # Piso de 55px: el banner superior (0-40px) se dibuja DESPUES y
        # taparia la etiqueta si el poligono empieza muy arriba del frame.
        # con varias cajas en el encuadre el banner superior solo puede
        # mostrar el PEOR estado; el rotulo de cada poligono dice cual es cual
        zinfo = {z["zona"]: z for z in (result.get("zonas") or [])}
        for zona in (getattr(self, "zonas", None) or []):
            pts = np.array(zona["puntos"], np.int32)
            cx, cy = pts[:, 0].mean(), pts[:, 1].min()
            zone_txt = zona["nombre"]
            zcolor = (255, 200, 0)
            zi = zinfo.get(zona["id"])
            if zi:
                zone_txt += f": {_STATE_CORTO.get(zi['estado'], zi['estado'])}"
                zcolor = _STATE_STYLE.get(zi["estado"], ((255, 200, 0),))[0]
            (tw, th), _ = cv2.getTextSize(zone_txt, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
            # centrado en el poligono pero ACOTADO al frame: al añadir el
            # estado el texto quedo mas largo y una zona pegada a un borde se
            # salia del cuadro. Piso de 55px arriba: el banner (0-40px) se
            # dibuja despues y taparia el rotulo.
            Hf, Wf = out.shape[:2]
            tx = int(min(max(4, cx - tw / 2), max(4, Wf - tw - 4)))
            ty = int(min(max(55, cy - 6), Hf - 4))
            cv2.putText(out, zone_txt, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, zcolor, 2, cv2.LINE_AA)

        for box in result.get("boxes") or []:
            x1, y1, x2, y2 = [int(v) for v in box[:4]]
            label = box[4] if len(box) > 4 else ""
            conf = box[5] if len(box) > 5 else None
            color = _TRACK_COLOR["PENDIENTE"]
            if "pendiente" not in label and "desconocido" not in label:
                color = _TRACK_COLOR["VERIFICADO"]
            elif "desconocido" in label:
                color = _TRACK_COLOR["DESCONOCIDO"]
            cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
            txt = label if conf is None else f"{label} {conf:+.2f}"
            cv2.putText(out, txt, (x1, max(15, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        color, _texto = _STATE_STYLE.get(st, ((60, 60, 60), st))
        h, w = out.shape[:2]
        cv2.rectangle(out, (0, 0), (w, 40), color, -1)
        score = result.get("score")
        txt = f"{self.NAME} | {result.get('label') or _texto}"
        if score is not None:
            txt += f" | conf {score:.2f}"
        cv2.putText(out, txt, (10, 27), cv2.FONT_HERSHEY_SIMPLEX,
                    0.65, self.COLOR_TEXT, 2)
        self.draw_infobar(out, stats, result.get("extra", ""))
        self._draw_enroll(out)
        return out

    def _draw_enroll(self, out):
        j = self._enroll
        if j is None or not j["active"]:
            return
        h, w = out.shape[:2]
        pct = int(100 * j["progress"])
        txt = (f"REGISTRANDO '{j['name']}' {pct}% | "
               f"{len(j['samples_e'])} vistas")
        cv2.rectangle(out, (0, h - 58), (w, h - 32), (90, 30, 90), -1)
        cv2.putText(out, txt, (10, h - 40), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (255, 255, 255), 2)

    # ------------------------------------------------------------------ #
    def teardown(self):
        if not self.counts:
            return
        total = sum(self.counts.values()) or 1
        print(f"[{self.NAME}] Resumen de estados:")
        for st, n in sorted(self.counts.items(), key=lambda x: -x[1]):
            print(f"  {st:<20} {n:>7} frames ({100.0 * n / total:.1f}%)")


if __name__ == "__main__":
    main(CashierDetector())


# ── PORT-MONOLITO · G1: restauración (ver arriba)
for _v in _g1_sin_definir:
    _os_g1.environ.pop(_v, None)
if _g1_hilos:
    try:
        _cv2_g1.setNumThreads(_g1_hilos)
    except Exception:
        pass
del _os_g1, _g1_sin_definir, _g1_hilos


# ══════ SECCIÓN VENDIDA: QueueAnalytics_Web.py (código validado 2026-08; no editar) ══════
# -*- coding: utf-8 -*-
"""
QueueAnalytics_Web.py — Deteccion de personas para analitica de filas.

ALCANCE DE ESTA VERSION (a proposito, muy acotado):
    Detecta personas en cada frame con YOLO, les asigna un ID de seguimiento
    persistente via ultralytics (model.track + ByteTrack), clasifica su
    posicion en FILA / CAJA / FUERA por punto-en-poligono, y ahora ademas
    mantiene una MAQUINA DE ESTADOS CON MEMORIA por track ID (FUERA -> EN_FILA
    -> EN_ATENCION -> ATENDIDO, con salida a DESERTOR desde EN_FILA) que mide
    con tiempo real (no frames) cuanto espera cada persona, cuanto dura su
    atencion, y registra el episodio cerrado en una lista en memoria (acotada
    a los ultimos 5000 episodios). Un expediente EN_ATENCION cuyo track deja
    de confirmarse en CAJA durante --gracia segundos tambien se cierra solo,
    como ATENDIDO (no queda huerfano indefinidamente). La etiqueta de cada
    caja muestra ID + estado + cronometro vivo, y la barra inferior agrega
    fila/caja del frame junto a las metricas de episodios cerrados (espera
    promedio, atendidos, desertores). NADA MAS.
    Ademas, cada vez que un expediente se cierra (ATENDIDO, DESERTOR,
    DESCARTADO, o INDETERMINADO si se detiene la fuente con expedientes a
    medias) se escribe una fila en una tabla Markdown de verificacion
    (--episodios-md) para poder auditar a ojo lo que la maquina de estados
    fue decidiendo, sin depender de leer los logs de consola.

    Correcciones para que los tiempos reportados sean defendibles (ver cada
    metodo para el detalle):
      - --reloj (PARTE 1): la maquina de estados puede medir tiempo con el
        reloj de pared (time.monotonic(), RTSP/webcam) o con un reloj
        derivado de frame_idx/--fps-video (archivos de video, donde el
        framework puede procesar mas lento o rapido que la duracion real
        del video). 'auto' (default) elige segun el tipo de fuente. Ver
        _reloj_ahora/_detectar_tipo_fuente.
      - --histeresis (PARTE 2): umbral mas estricto para ENTRAR a una zona
        que para seguir/salir de ella, para que una persona en el limite
        entre ZONA_FILA y ZONA_CAJA no parpadee entre estados. Ver
        _punto_en_zona.
      - Transicion FUERA -> EN_ATENCION directa (PARTE 3, desactivable con
        --sin-directo-caja): el cliente que llega a CAJA sin haber hecho
        fila abre expediente con espera=00:00 en vez de quedar sin episodio.
      - --max-atencion (PARTE 4): red de seguridad que cierra como
        DESCARTADO (fuera de los promedios) un expediente EN_ATENCION que
        lleva demasiado tiempo abierto, incluso si sigue confirmandose en
        CAJA.
      - --capacidad-caja (PARTE 5): ZONA_CAJA pasa a ser un recurso con
        cupo (default 1 persona en EN_ATENCION a la vez). Sin esto, la
        cercania fisica entre quien ya esta siendo atendido y quien se le
        pega detras en la fila se confundia con el estado de "ser
        atendido", partiendo esperas reales en dos episodios. Ver el paso
        "PARTE 5" en process_frame para el criterio de asignacion de
        puestos (continuidad > antiguedad en fila > distancia al borde
        del poligono de CAJA).
      - --min-caja (PARTE 6): corrige el criterio de ATENDIDO/DESERTOR
        para que sea consistente con --capacidad-caja. Ya NO se decide
        preguntando si el expediente llego al estado EN_ATENCION (con
        cupo limitado, alguien puede estar fisicamente en CAJA sin puesto
        asignado durante minutos); se decide con si la persona alcanzo la
        geometria CRUDA de ZONA_CAJA por al menos --min-caja segundos
        (self._actualizar_racha_zona_cruda/_desenlace_por_caja), sin
        importar si tuvo puesto. "Espera", "servicio" y "total" (la
        columna del Markdown, antes "en fila total") se derivan TODOS de
        la geometria cruda de zonas -nunca de la zona EFECTIVA ni del
        estado formal de la maquina-, incluida la primera entrada cruda a
        FILA: asi el tiempo perdido por falta de cupo no se cuenta dos
        veces, y total = espera + servicio siempre, sin excepcion. Ver
        _tiempos_geometria_cruda.
      - Deteccion de reinicio TECNICO de fuente (bucle de video o
        reconexion, PARTE 2 de acumulados de jornada): frame_idx bajando
        respecto al ultimo visto es la unica senal disponible aqui (ver
        _manejar_reinicio_fuente, llamado desde process_frame). Cierra lo
        que seguia EN_FILA/EN_ATENCION como INDETERMINADO y limpia la
        memoria de "quien esta en escena ahora", pero los acumulados de
        JORNADA (atendidos/desertores/indeterminados/episodios/alertas)
        sobreviven intactos -solo se ponen a cero con la accion explicita
        del operador, ver reiniciar_jornada()-.
    No hay agregacion por minuto/hora/dia, ni persistencia en disco/BD mas
    alla de ese Markdown, ni logica de oclusiones/fantasmas/herencia de
    IDs entre tracks distintos (un ID nuevo de ByteTrack, sea por perdida
    de track normal o por el reinicio tecnico de arriba, SIEMPRE es
    persona nueva: no hay re-identificacion por apariencia/ReID), ni
    anclaje adaptativo, ni dibujo de los poligonos sobre el frame. Eso
    viene despues.

COMO ENCAJA EN EL FRAMEWORK (para quien no lo conoce):
    El framework vive en Models/Base y aporta todo lo aburrido: leer video o
    RTSP, reconectar, redimensionar, grabar, servir MJPEG, guardar alertas y
    el panel web con multiples fuentes. Nosotros solo escribimos una clase que
    hereda de BaseDetector y rellenamos unos "hooks":

        add_arguments(parser)          -> nuestras flags de linea de comandos
        configure(args)                -> recibe los args ya parseados (la
                                          implementacion base ya guarda
                                          self.args, no hace falta tocarla)
        setup()                        -> cargar modelos UNA sola vez
        process_frame(frame, frame_idx)-> inferencia; devuelve un dict
        annotate(frame, result, stats) -> dibujado; llamamos al de la base
                                          (banner/barra inferior/cajas) y
                                          encima recoloreamos cada caja
                                          segun el estado de esa persona
        teardown()                     -> liberar recursos (no tenemos nada)

    El dict que DEBE devolver process_frame (segun ref/base_detector.py) es:

        {
            "alert": bool,   # True dispara snapshot de alerta en disco
            "label": str,    # etiqueta de esa alerta
            "score": float,  # score 0..1 o None
            "boxes": list,   # [(x1, y1, x2, y2, label, conf), ...]
            "extra": str,    # texto libre que sale en la barra inferior
        }

USO (una vez copiado a Models/areaRest/QueueAnalytics/):

    uv run QueueAnalytics_Web.py

    Los defaults operativos (--port 8035, --resize 800x600, --fps-video 15,
    --min-caja 3, --min-fila 0.5, --tracker bytetrack_queue.yaml,
    --episodios-md episodios.md, --config sources.json) ya vienen puestos en
    add_arguments()/main_web_zones(); cualquier flag se puede sobrescribir,
    ej. "uv run QueueAnalytics_Web.py --port 9000 --fps-video 30". Las
    dependencias (flask, opencv-python, ultralytics, lap) se resuelven solas
    via pyproject.toml, sin necesidad de --with.
"""

import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path   # FUSION: defaults de modelos/

import cv2
import numpy as np

# --------------------------------------------------------------------- #
#  Silenciar SOLO el warning repetitivo de "'half' is deprecated"
# --------------------------------------------------------------------- #
# self.model.track(..., half=True) (mas abajo, PARTE de inferencia) dispara
# esto una vez POR FRAME: ultralytics.utils.deprecation_warn() hace
# LOGGER.warning(...) sobre el logger "ultralytics" (no es un warning de
# Python via el modulo `warnings`, es logging estandar). El filtro descarta
# unicamente ese mensaje exacto; cualquier otro log de "ultralytics" (o de
# cualquier otro logger) sigue pasando normal.
class _SilenceHalfDeprecation(logging.Filter):
    def filter(self, record):
        return "'half' is deprecated" not in record.getMessage()


logging.getLogger("ultralytics").addFilter(_SilenceHalfDeprecation())

# --------------------------------------------------------------------- #
#  Import del framework (Models/Base) de forma RELATIVA al propio archivo
# --------------------------------------------------------------------- #
# Este archivo vive en:  Models/areaRest/QueueAnalytics/QueueAnalytics_Web.py
#
#   _AQUI    = .../Models/areaRest/QueueAnalytics
#   dirname  = .../Models/areaRest          (1 nivel arriba)
#   dirname  = .../Models                   (2 niveles arriba)
#   + "Base" = .../Models/Base              <- donde vive el framework
#
# Se hace asi (y no con rutas absolutas ni PYTHONPATH) para que la carpeta
# se pueda mover/clonar sin romper nada, igual que los demas detectores.
_AQUI = os.path.dirname(os.path.abspath(__file__))
_AQUI_PATH = Path(_AQUI)          # FUSION: para los defaults de modelos/
_BASE = os.path.join(_AQUI, "Base")
# PORT-KINICH: sin sys.path.insert — el paquete usa imports relativos (G7)
# sys.path.insert(0, _BASE)

# PORT-MONOLITO (definido arriba en este archivo): from .base_detector import BaseDetector  # noqa: E402  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)
# ZONES_WEB_DIR es la carpeta donde web_server_zones.py guarda un .json por
# fuente con las zonas dibujadas (zones_web/<source_key>.json). La importamos
# tal cual la define ese modulo -en vez de recalcularla aqui- para que, si el
# dia de manana cambia de sitio, este archivo la siga encontrando sin tocar
# nada.
# La PLATAFORMA (panel Flask) es OPCIONAL: solo hace falta para correr este
# archivo como servidor (__main__). Usado como MODELO puro (adaptadores de
# To_FullCV, contrato BaseCVModel de Full_CV), flask puede no existir en el
# entorno — el detector solo necesita ZONES_WEB_DIR para leer sus zonas
# guardadas, y esa ruta es una convencion fija (<panel>/zones_web) que se
# replica aqui sin importar el modulo del panel.
try:
    from web_server_zones import main_web_zones, ZONES_WEB_DIR  # noqa: E402
except ImportError:                       # sin flask/werkzeug: modelo puro
    main_web_zones = None
    ZONES_WEB_DIR = os.path.join(_AQUI, "zones_web")
# PARTE 2: protocolo OPCIONAL de parametros ajustables en caliente desde el
# panel (get_params/set_params, ver params_mixin.py junto a este script).
# Vive en el mismo directorio que este archivo (igual que web_server_zones),
# por eso el import es directo, sin necesidad de tocar sys.path de nuevo.
# PORT-MONOLITO (definido arriba en este archivo): from .params_mixin import ParamsMixin  # noqa: E402  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)
# Protocolo de zonas TIPADAS (fusion Queue-Cashier, ver Base/zonas.py): las
# zonas dejan de identificarse por su POSICION en la lista (regions[0]=FILA,
# etc. -borrar el primer poligono convertia la CAJA en FILA en silencio-) y
# pasan a ser {"id","tipo","nombre","puntos"} con id estable y tipo que
# decide que calculo aplica.
# PORT-MONOLITO (definido arriba en este archivo): from . import zonas as _zonas  # noqa: E402  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)


class QueueAnalytics(ParamsMixin, BaseDetector):
    # El NAME se usa en el titulo del panel, en el banner superior del video
    # y como nombre de la carpeta de alertas (ALERTAS_<NAME>).
    NAME = "QUEUE_ANALYTICS"
    ZONA_TIPOS = ("FILA", "CAJA", "AFORO")   # ver Base/zonas.py

    # Lock DE CLASE (compartido por todas las instancias/threads de este
    # mismo proceso Python). Cada fuente escribe su PROPIO
    # resultados/<clave-de-fuente>/episodios.md (ver setup() y
    # _resolver_carpeta_key_fuente), asi que en el caso normal este lock ya
    # no protege un archivo compartido entre fuentes distintas -sigue
    # sirviendo si dos fuentes terminan resolviendo la MISMA clave (mismo
    # source_key/nombre saneado) o si se pasa --episodios-md absoluto
    # apuntando al mismo archivo a proposito: abrir en "a", escribir la fila
    # completa, cerrar, todo dentro del lock, para que esas escrituras no
    # se intercalen a medias. OJO: esto solo protege escrituras DENTRO de
    # este proceso; dos procesos "uv run QueueAnalytics_Web.py" distintos
    # apuntando al mismo archivo no quedan coordinados por este lock (haria
    # falta un lock de sistema operativo tipo fcntl.flock).
    _MD_LOCK = threading.Lock()

    # Modelo YOLO COMPARTIDO entre todas las instancias/fuentes de este
    # mismo proceso (cada fuente sigue siendo su propia instancia de
    # QueueAnalytics -su propio self.expedientes, self.regions, etc.-,
    # pero todas apuntan al MISMO objeto YOLO en vez de cargar una copia
    # en VRAM por fuente). _model_lock protege UNICAMENTE la carga
    # perezosa (que dos fuentes arrancando a la vez no disparen dos
    # YOLO(...) en paralelo y se quede una copia huerfana en VRAM).
    _model_shared = None
    _model_lock = threading.Lock()

    # _infer_lock: SI protege las llamadas a model.track() (ver
    # process_frame), porque el modelo de arriba es compartido y
    # model.track(..., persist=True) guarda el estado del tracker
    # (ByteTrack) en el propio objeto predictor de ese modelo -llamarlo
    # concurrentemente desde dos fuentes corrompería/mezclaría ese estado
    # entre camaras. Este lock serializa la GPU entre fuentes: es
    # correcto, pero limita el throughput a ~1/(tiempo_inferencia)
    # fuentes a 15 FPS. Para mas, hace falta inferencia por lotes.
    _infer_lock = threading.Lock()

    @classmethod
    def _get_model(cls, model_path):
        with cls._model_lock:
            if cls._model_shared is None:
                from ultralytics import YOLO
                cls._model_shared = YOLO(model_path)
            return cls._model_shared

    # ------------------------------------------------------------------ #
    #  PARTE 2: umbrales ajustables en caliente (ParamsMixin, ver
    #  params_mixin.py). Cada entrada declara "key" (= el atributo EN VIVO
    #  self.<key> que process_frame lee cada frame, sembrado en
    #  configure() a partir de la flag CLI homonima -ver ese metodo- y
    #  sobreescribible por el panel via set_params()/PARAMS_WEB_DIR sin
    #  reiniciar el proceso). "min"/"max" los aplica el propio mixin como
    #  RECORTE (nunca error, ver set_params en params_mixin.py): ningun
    #  valor negativo para los primeros 7, capacidad_caja con piso 1, conf
    #  acotada a [0.05, 0.95] -tal como se pidio-.
    #
    # OJO: NO incluye --model/--imgsz/--anclaje/--tracker/--episodios-md/
    # --diag-bbox/--profile/--reloj/--fps-video/--sin-directo-caja: no se
    # pidieron ajustables en caliente, y varios de ellos (--anclaje,
    # --reloj) invalidarian zonas ya calibradas o mezclarian relojes
    # incompatibles a mitad de sesion si se cambiaran sin reiniciar -por
    # eso siguen siendo solo de arranque, via CLI-.
    PARAMS = [
        dict(key="min_fila", label="Min. fila (s)", type="float",
             min=0.0, step=0.5,
             help="Permanencia minima en FILA para abrir expediente"),
        dict(key="min_caja", label="Min. caja (s)", type="float",
             min=0.0, step=0.5,
             help="Permanencia minima en CAJA para contar como atencion"),
        dict(key="gracia", label="Gracia (s)", type="float",
             min=0.0, step=1.0,
             help="Tiempo sin confirmarse antes de cerrar el expediente"),
        dict(key="retener_puesto", label="Retener puesto (s)", type="float",
             min=0.0, step=0.5,
             help="Reserva del cupo de caja ante oclusion breve"),
        dict(key="max_atencion", label="Max. atencion (s)", type="float",
             min=0.0, step=10.0,
             help="Red de seguridad contra expedientes eternos (<=0 la desactiva)"),
        dict(key="histeresis", label="Histeresis (px)", type="float",
             min=0.0, step=1.0,
             help="Margen en pixeles en la frontera de zona (0 la desactiva)"),
        dict(key="objetivo_espera", label="Objetivo de espera (s)", type="float",
             min=0.0, step=5.0,
             help="Referencia para el porcentaje 'pct' de estado_cola()"),
        dict(key="conf", label="Confianza YOLO", type="float",
             min=0.05, max=0.95, step=0.01,
             help="Confianza minima de deteccion pasada a model.track()"),
        dict(key="capacidad_caja", label="Capacidad de caja", type="int",
             min=1,
             help="Personas simultaneas en EN_ATENCION"),
        dict(key="confirmar_regreso", label="Confirmar regreso (s)", type="float",
             min=0.0, step=0.5,
             help="Confirmacion sostenida en FILA para salir de DESERTOR"),
        dict(key="cooldown_atendido", label="Enfriamiento atendido (s)", type="float",
             min=0.0, step=5.0,
             help="Tiempo tras ATENDIDO en que no puede reabrir expediente"),
        dict(key="limite_aforo", label="Limite de aforo", type="int",
             min=1,
             help="Personas maximas dentro del poligono de AFORO "
                  "(la zona de tipo AFORO) antes de emitir alerta"),
    ]

    # ------------------------------------------------------------------ #
    #  1) Flags propias
    # ------------------------------------------------------------------ #
    def add_arguments(self, parser):
        """
        El framework crea el parser con sus flags generales (--port, --resize,
        --frame-skip, etc.) y luego nos deja agregar las nuestras aqui.
        Los valores parseados llegan solos a self.args via configure().
        """
        # FUSION Queue-Cashier: apunta a modelos/yolo11s.pt (copia real, mismo
        # md5 que el del despliegue del Cashier y que el de QueueAnalytics).
        # Antes era el nombre suelto "yolo11s.pt", que ultralytics resuelve
        # contra el CWD y, al no encontrarlo aqui, DESCARGABA una copia nueva
        # -pesos distintos a los validados, en silencio-.
        parser.add_argument("--model",
                            default=str(_AQUI_PATH / "modelos" / "yolo11s.pt"),
                            help="Pesos YOLO a usar (se descargan solos si no existen)")
        parser.add_argument("--conf", type=float, default=0.35,
                            help="Confianza minima para aceptar una persona")
        parser.add_argument("--imgsz", type=int, default=640,
                            help="Resolucion interna de inferencia (lado mayor)")
        parser.add_argument("--tracker", default="bytetrack_queue.yaml",
                            help="Archivo de configuracion del tracker (ByteTrack) "
                                 "que usa ultralytics en model.track(). Se busca "
                                 "primero junto a este script (para no depender "
                                 "del directorio de trabajo); si no esta ahi se "
                                 "pasa tal cual a ultralytics, que probara sus "
                                 "rutas de configuracion por defecto.")
        parser.add_argument("--anclaje", choices=["pies", "cabeza", "centro"],
                            default="pies",
                            help="Punto de la caja que representa a la persona "
                                 "para decidir en que zona esta. 'pies' "
                                 "(default) = centro del borde inferior, el "
                                 "comportamiento historico de este modelo, "
                                 "pensado para zonas dibujadas sobre el plano "
                                 "del suelo. 'cabeza' = centro del borde "
                                 "superior. 'centro' = centro geometrico de "
                                 "la caja. OJO: cambiar este valor INVALIDA "
                                 "las zonas ya dibujadas -el punto que se "
                                 "compara deja de corresponder con el plano "
                                 "para el que se calibraron- y hay que "
                                 "redibujarlas despues del cambio.")
        parser.add_argument("--min-fila", type=float, default=0.5,
                            help="Segundos MINIMOS que una persona debe "
                                 "permanecer clasificada en FILA, sin "
                                 "interrupcion, para que se le abra "
                                 "expediente. Filtra a quien solo cruza la "
                                 "zona de paso sin realmente hacer fila.")
        parser.add_argument("--gracia", type=float, default=20.0,
                            help="Segundos que se conserva abierto un "
                                 "expediente en FILA sin confirmar que la "
                                 "persona sigue ahi (porque deja de verse o "
                                 "porque pasa a FUERA) antes de darla por "
                                 "DESERTOR. Si reaparece en FILA dentro de "
                                 "este margen, el expediente continua con "
                                 "sus tiempos intactos.")
        parser.add_argument("--episodios-md", default="episodios.md",
                            help="Nombre del archivo Markdown donde se va "
                                 "anexando (append) una fila cada vez que un "
                                 "expediente se cierra, a modo de salida "
                                 "temporal de verificacion. Si es un nombre "
                                 "relativo (el caso normal) se resuelve DENTRO "
                                 "de la carpeta propia de esta fuente "
                                 "(resultados/<fuente>/, ver "
                                 "_resolver_carpeta_key_fuente en setup()), asi "
                                 "que cada fuente del panel escribe su propio "
                                 "episodios.md sin mezclarse con el de otra. Una "
                                 "ruta ABSOLUTA se respeta tal cual como override "
                                 "explicito (un unico archivo, sin aislar por "
                                 "fuente); util solo para CLI de una sola fuente.")
        parser.add_argument("--diag-bbox", action="store_true",
                            help="Modo de diagnostico TEMPORAL para decidir "
                                 "--anclaje con datos: agrega 3 columnas mas al "
                                 "Markdown de episodios con estadisticas del "
                                 "alto del bbox de cada persona (ver "
                                 "_acumular_altura y _cerrar_expediente). Off "
                                 "por defecto: no calcula ni escribe nada de "
                                 "esto si no se pasa esta flag. No implementa "
                                 "ningun anclaje adaptativo; solo junta datos.")
        parser.add_argument("--profile", action="store_true",
                            help="Instrumentacion de rendimiento TEMPORAL: "
                                 "cronometra por separado cada etapa dentro de "
                                 "process_frame (inferencia, extraccion, "
                                 "geometria, maquina de estados, etiquetas) con "
                                 "time.perf_counter(), e imprime el promedio en "
                                 "ms cada 60 frames, junto con el hueco de "
                                 "tiempo FUERA de process_frame (framework: "
                                 "annotate, JPEG, sinks). Off por defecto: no "
                                 "cronometra nada si no se pasa esta flag. Solo "
                                 "medicion, no optimiza ni cambia ningun "
                                 "resultado.")
        parser.add_argument("--reloj", choices=["auto", "video", "pared"],
                            default="auto",
                            help="Reloj que usa la maquina de estados para medir "
                                 "espera/servicio/deserciones. 'pared' = "
                                 "time.monotonic() (el comportamiento historico; "
                                 "correcto para RTSP/webcam, donde los frames "
                                 "llegan en tiempo real). 'video' = derivado de "
                                 "frame_idx y --fps-video (correcto para archivos "
                                 "de video, donde el framework puede procesar mas "
                                 "lento o mas rapido que la duracion real del "
                                 "video). 'auto' (default) elige 'video' si la "
                                 "fuente parece un archivo y 'pared' si parece un "
                                 "stream -ver _detectar_tipo_fuente para el "
                                 "detalle de esa deteccion, que es una heuristica, "
                                 "no una certeza-. OJO: los tiempos medidos con "
                                 "reloj de video NO son comparables con episodios "
                                 "registrados con reloj de pared.")
        parser.add_argument("--fps-video", type=float, default=15.0,
                            help="FPS nativos asumidos de la fuente, usados SOLO "
                                 "por el reloj de video (--reloj=video o "
                                 "auto-detectado como archivo) para derivar "
                                 "tiempo = frame_idx / --fps-video. No hay forma "
                                 "de leer este valor automaticamente desde este "
                                 "archivo (no tenemos acceso al objeto "
                                 "cv2.VideoCapture de la fuente, ver "
                                 "_reloj_ahora); si el video real no es de 30 "
                                 "FPS, hay que indicarlo aqui.")
        parser.add_argument("--histeresis", type=float, default=15.0,
                            help="Pixeles de histeresis en el borde de zona: "
                                 "umbral mas exigente para ENTRAR a una zona que "
                                 "para seguir/salir de ella, para que una persona "
                                 "parada justo en el limite entre ZONA_FILA y "
                                 "ZONA_CAJA no parpadee entre estados frame a "
                                 "frame. 0 desactiva la histeresis (vuelve al "
                                 "umbral unico de antes de este cambio). Ver "
                                 "_punto_en_zona.")
        parser.add_argument("--sin-directo-caja", action="store_true",
                            help="Desactiva la transicion directa FUERA/candidato "
                                 "-> EN_ATENCION (ver el bloque 'PARTE 3' en "
                                 "process_frame) y vuelve al comportamiento "
                                 "anterior: quien llega a CAJA sin haber abierto "
                                 "expediente en FILA no genera episodio. Flag "
                                 "solo para poder comparar antes/despues de esa "
                                 "transicion; por defecto la transicion SI esta "
                                 "activa.")
        parser.add_argument("--max-atencion", type=float, default=300.0,
                            help="Segundos maximos que se tolera un expediente en "
                                 "EN_ATENCION antes de cerrarlo por la fuerza con "
                                 "desenlace DESCARTADO (no cuenta en promedios ni "
                                 "en el total de atendidos). Red de seguridad "
                                 "contra expedientes que quedan abiertos "
                                 "indefinidamente en CAJA (ej. personal del "
                                 "establecimiento parado en esa zona) -ver el "
                                 "barrido de gracia en process_frame. <= 0 "
                                 "desactiva esta red de seguridad.")
        parser.add_argument("--capacidad-caja", type=int, default=1,
                            help="Cuantas personas pueden estar en EN_ATENCION "
                                 "a la vez en esta fuente (PARTE 5). Convierte "
                                 "ZONA_CAJA en un recurso con cupo: si hay mas "
                                 "personas geometricamente dentro de CAJA que "
                                 "--capacidad-caja, las que no consiguen puesto "
                                 "se tratan como EN_FILA (su cronometro de "
                                 "espera sigue corriendo) en vez de abrirseles "
                                 "expediente de atencion. Sin esto, la cercania "
                                 "fisica entre clientes se confunde con el "
                                 "estado de ser atendido -ver el paso "
                                 "'PARTE 5' en process_frame para el criterio "
                                 "de asignacion de puestos.")
        parser.add_argument("--min-caja", type=float, default=3.0,
                            help="Segundos MINIMOS de presencia CONTINUA en la "
                                 "geometria CRUDA de ZONA_CAJA (sin importar si "
                                 "hay puesto asignado por --capacidad-caja) para "
                                 "considerar que la persona 'alcanzo la caja' "
                                 "(PARTE 6). Filtra un cruce breve o de paso; NO "
                                 "es lo mismo que tener puesto: alguien puede "
                                 "acumular --min-caja sin puesto asignado en "
                                 "ningun momento y aun asi contar como ATENDIDO "
                                 "al cerrar su expediente. Ver "
                                 "_actualizar_racha_zona_cruda/_desenlace_por_caja.")
        parser.add_argument("--retener-puesto", type=float, default=5.0,
                            help="Segundos que un puesto de EN_ATENCION queda "
                                 "RESERVADO para su ocupante aunque no se le "
                                 "confirme en CAJA cruda ese frame (PARTE 7). Sin "
                                 "esto, una oclusion de un solo frame -el ocupante "
                                 "no aparece detectado ese frame- hace que la "
                                 "regla de continuidad no pueda protegerlo (no "
                                 "esta en la lista de candidatos de ese frame) y "
                                 "el puesto pasa al siguiente en la fila; un frame "
                                 "despues el ocupante reaparece y hay dos personas "
                                 "EN_ATENCION a la vez con --capacidad-caja=1. El "
                                 "puesto se libera y se reasigna solo si el "
                                 "ocupante lleva mas de --retener-puesto segundos "
                                 "sin confirmarse en CAJA cruda, o si su expediente "
                                 "se cierra. 0 desactiva la retencion (vuelve al "
                                 "comportamiento anterior: se libera con solo no "
                                 "aparecer en el frame). Ver self._ocupantes_caja.")
        parser.add_argument("--objetivo-espera", type=float, default=60.0,
                            help="Segundos de espera 'esperados' contra los que "
                                 "se mide el porcentaje 'pct' de cada persona en "
                                 "estado_cola() (PARTE 3, ver ese metodo). Es solo "
                                 "una referencia para pintar un panel externo "
                                 "(ej. una barra de progreso); no afecta a "
                                 "ninguna decision de la maquina de estados ni a "
                                 "los tiempos que se archivan. pct puede superar "
                                 "100 (alguien ya esperando mas de lo esperado). "
                                 "Debe ser > 0 (se usa como divisor).")
        parser.add_argument("--confirmar-regreso", type=float, default=1.0,
                            help="Segundos de confirmacion SOSTENIDA en FILA "
                                 "que exige, ademas de un simple frame, salir "
                                 "del estado visual DESERTOR y volver a "
                                 "ESPERANDO (PARTE 2). Entrar a DESERTOR sigue "
                                 "bastando con un solo frame sin confirmarse "
                                 "en FILA (asimetrico a proposito): una alerta "
                                 "que parpadea es peor que una alerta con un "
                                 "segundo de retardo. Mientras la reversion no "
                                 "se consolida, el reloj de gracia no se "
                                 "reinicia por confirmaciones sueltas -solo al "
                                 "consolidarse-. 0 vuelve a un solo frame "
                                 "tambien para salir (comportamiento previo a "
                                 "esta parte).")
        parser.add_argument("--cooldown-atendido", type=float, default=60.0,
                            help="Segundos de enfriamiento tras cerrar un "
                                 "expediente como ATENDIDO, durante los "
                                 "cuales ese track ID no puede abrir un "
                                 "expediente nuevo (PARTE 3): se le trata "
                                 "como sin expediente (gris, etiqueta '---'), "
                                 "aunque este parado en FILA o en CAJA. "
                                 "Evita que alguien recien atendido que se "
                                 "queda rondando cerca de la fila genere un "
                                 "episodio falso con una espera que no "
                                 "existio. Pasado el enfriamiento, un "
                                 "expediente nuevo SI se abre normalmente -es "
                                 "una segunda compra legitima- y queda "
                                 "marcado como 'reencolado' en el Markdown "
                                 "(ver _cerrar_expediente). 0 desactiva el "
                                 "enfriamiento (vuelve al comportamiento "
                                 "anterior: puede reencolarse de inmediato).")
        parser.add_argument("--limite-aforo", type=int, default=10,
                            help="Personas maximas toleradas dentro del "
                                 "poligono de AFORO (la zona de tipo AFORO; antes el tercer "
                                 "poligono dibujado en el editor). Conteo "
                                 "independiente de la maquina de estados "
                                 "FILA/CAJA: no abre expediente, no "
                                 "consume cupo de --capacidad-caja y no "
                                 "usa histeresis (mismo _punto_en_zona/ "
                                 "--anclaje, pero siempre con el umbral "
                                 "sin historial). Al superarse se guarda "
                                 "UNA alerta (frame completo + JSON con "
                                 "hora/fuente/limite/conteo) en "
                                 "resultados/<fuente>/alertas/aforo/; no se "
                                 "repite mientras el conteo siga por "
                                 "encima del limite y se rearma en cuanto "
                                 "baja (ver _alerta_aforo). Ajustable en "
                                 "caliente via el panel (PARAMS).")

    # ------------------------------------------------------------------ #
    #  1b) PARTE 2: siembra de umbrales ajustables ANTES de setup()
    # ------------------------------------------------------------------ #
    def configure(self, args):
        """
        La base (BaseDetector.configure) solo guarda self.args = args (ver
        el docstring del modulo: "no hace falta tocarla"); se sobreescribe
        aqui, llamando primero a super().configure(args) para conservar
        ese comportamiento intacto, y DESPUES sembrando self._estado_lock
        y las copias EN VIVO de los umbrales de PARAMS (self.min_fila,
        self.conf, etc.) a partir de la flag CLI homonima -satisface (a):
        "cada parametro conserva su flag CLI actual como valor inicial"-.

        Es IMPRESCINDIBLE hacerlo aqui y no en setup(): web_server_zones.py
        (_add_source) llama a set_params() con los valores guardados de
        una sesion anterior (params_web/<source_key>.json) INMEDIATAMENTE
        despues de configure() y ANTES de que setup() llegue a correr (que
        recien pasa mas tarde, ya dentro del thread propio de la fuente,
        ver docstring de setup()). Si estos atributos solo existieran
        desde setup():
          - esa re-aplicacion temprana fallaria (self._estado_lock ni
            siquiera existiria todavia para set_params(), ver el override
            de ese metodo mas abajo), o
          - peor, setup() pisaria DESPUES los valores ya restaurados por
            set_params() con los de la flag CLI otra vez, perdiendo en
            silencio el ajuste guardado de la sesion anterior.
        Por eso setup() NUNCA vuelve a tocar estos atributos (ver ese
        metodo): una vez sembrados aqui, solo set_params() (el panel) los
        vuelve a escribir.
        """
        super().configure(args)
        # PARTE 3 (ver estado_cola()): el MISMO candado se reutiliza aqui
        # para proteger la lectura/escritura de estos umbrales en caliente
        # (PARTE 2) -no se crea uno nuevo, ver el override de
        # get_params()/set_params() mas abajo-.
        self._estado_lock = threading.Lock()
        self.min_fila = args.min_fila
        self.min_caja = args.min_caja
        self.gracia = args.gracia
        self.retener_puesto = args.retener_puesto
        self.max_atencion = args.max_atencion
        self.histeresis = args.histeresis
        self.objetivo_espera = args.objetivo_espera
        self.conf = args.conf
        self.capacidad_caja = args.capacidad_caja
        self.confirmar_regreso = args.confirmar_regreso
        self.cooldown_atendido = args.cooldown_atendido
        self.limite_aforo = args.limite_aforo
        # FUSION Queue-Cashier: señal EXTERNA de "hay cajero en su puesto".
        #   None  -> sin señal (detector corriendo solo): comportamiento
        #            historico intacto, la caja siempre puede atender.
        #   True  -> hay un EMPLEADO VERIFICADO en la(s) zona(s) CAJERO
        #            (o en su tolerancia de ausencia breve): atencion plena.
        #   "posible" -> hay ALGUIEN en la zona CAJERO pero NO verificado
        #            (desconocido o pendiente): la atencion fisica existe
        #            (cupo y servicio corren normal) pero el estado visible
        #            es "POSIBLEMENTE ATENDIENDO" — presentacion, no
        #            maquina de estados: al verificarse el cajero, la
        #            etiqueta sube a ATENDIENDO sin perder cronometro.
        #   False -> caja DESATENDIDA: nadie puede estar "ATENDIENDO" —
        #            estar frente a una caja sin cajero es SEGUIR ESPERANDO
        #            (el tiempo cuenta como espera, no como servicio, y
        #            quien se va sin ser atendido cierra como DESERTOR).
        # La escribe queue_cashier.py ANTES de cada process_frame, desde el
        # MISMO hilo del worker (no necesita lock).
        self.atencion_disponible = None
        # Estado de zonas sembrado AQUI (no solo en setup): el compuesto
        # multi-juego entrega zonas via set_zonas() ANTES del setup
        # perezoso de cada juego. setup() ya no debe PISAR lo entregado.
        self.zonas = []
        self.regions = []
        self.zone_expand = None

    def get_params(self):
        """PARTE 2: envuelve ParamsMixin.get_params con self._estado_lock
        (el mismo candado de estado_cola(), no uno nuevo) para que el
        panel nunca lea una mezcla a medio aplicar de un set_params()
        concurrente."""
        with self._estado_lock:
            return super().get_params()

    def set_params(self, data):
        """PARTE 2: envuelve ParamsMixin.set_params con self._estado_lock
        para que TODO el lote de cambios de un mismo POST se aplique como
        una unidad atomica -process_frame toma su propio snapshot de estos
        umbrales bajo el mismo candado una vez por frame (ver el inicio de
        process_frame), asi que nunca puede ver una mezcla de valores
        viejos y nuevos entre dos parametros del mismo guardado."""
        with self._estado_lock:
            return super().set_params(data)

    # ------------------------------------------------------------------ #
    #  Carpeta de resultados por fuente (episodios.md + alertas propias)
    # ------------------------------------------------------------------ #
    _RE_CARPETA_INSEGURA = re.compile(r"[^A-Za-z0-9._-]+")

    @staticmethod
    def _sanear_nombre_carpeta(nombre):
        """
        Convierte cualquier string (nombre puesto por el operador, basename
        de una URL RTSP, source_key ya semi-saneado, etc.) en un nombre de
        carpeta seguro: solo letras/digitos/punto/guion/guion-bajo, con
        cualquier otra cosa (espacios, "/", ":", "?", credenciales de RTSP,
        etc.) colapsada a un unico "_". Recorta puntos/guiones-bajos
        sobrantes en los extremos (evita ".", "..", o nombres que empiecen
        con "_" por una URL que arrancaba con separador). Si el resultado
        queda vacio (string original vacio o compuesto solo por caracteres
        inseguros), cae a "fuente" para nunca devolver "" (que rompería
        os.path.join contra la raiz de resultados).
        """
        limpio = QueueAnalytics._RE_CARPETA_INSEGURA.sub("_", str(nombre)).strip("._")
        return limpio or "fuente"

    def _resolver_carpeta_key_fuente(self):
        """
        Nombre de carpeta estable y saneado para ESTA fuente, usado como
        resultados/<esto>/ (ver setup(), donde se guarda en
        self._resultados_fuente_dir y de ahi cuelgan episodios.md y
        alertas/desercion|aforo -ver _resolver_alert_dir).

        Se basa en self.fuente_nombre (ya resuelto en setup() ANTES de este
        punto: el nombre que puso el operador al agregar la fuente en el
        panel, o si no puso nada, os.path.basename() del source crudo -ver
        _add_source en web_server_zones.py-). Es DELIBERADO no usar
        self.source_key aqui: source_key es f"{NAME}__{secure_filename(source
        COMPLETO)}" -pensado para nombrar un archivo de ZONAS/PARAMS por
        fuente, no para ser leido por un humano-, y con un source que es una
        ruta larga (ej. un archivo subido, o webcam remota) produce un
        nombre de carpeta larguisimo e ilegible. self.fuente_nombre es lo
        que ya se muestra en el panel/Markdown y es lo que produce carpetas
        legibles tipo resultados/video_1/, resultados/video_2/... (10
        videos con 10 nombres distintos -> 10 carpetas distintas, ver la
        tarea que pidio este cambio).

        Saneamos el resultado siempre (fuente_nombre NO pasa por
        secure_filename -es libre, lo puede escribir el operador o venir de
        un basename() de RTSP con caracteres invalidos para un nombre de
        carpeta, ver el TODO en _add_source sobre URLs sin ruta final-), y
        si de casualidad fuente_nombre hubiera caido en el fallback de
        source_key (self.source_name ausente, ver setup()), le quitamos el
        prefijo "{NAME}__" por la misma razon de legibilidad.
        """
        crudo = self.fuente_nombre
        prefijo = f"{self.NAME}__"
        if crudo.startswith(prefijo):
            crudo = crudo[len(prefijo):]
        return self._sanear_nombre_carpeta(crudo)

    # ------------------------------------------------------------------ #
    #  2) Carga del modelo
    # ------------------------------------------------------------------ #
    def setup(self):
        """
        Se llama UNA sola vez por fuente, ya dentro del thread que procesa esa
        fuente (ver SourceWorker._run en web_server.py). El import de
        ultralytics sigue sin pagarse hasta que hace falta de verdad (ver
        _get_model): el arranque del panel es instantaneo y solo se carga
        torch cuando la PRIMERA fuente llega a este punto.

        OJO (cambio de esta parte): el modelo YOLO ya NO es una copia por
        fuente -self.model apunta al mismo objeto compartido de la clase
        (ver _get_model/_model_shared), cargado una unica vez la primera
        vez que cualquier fuente llega aqui-. Cada fuente conserva su
        propio self.expedientes/self.regions/etc., solo el modelo en VRAM
        se comparte.

        REGRESION corregida: self._get_model() se llama al FINAL de este
        metodo (ver el cierre, tras cargar zonas/tracker/markdown/etc.),
        no al principio. Con 6 fuentes arrancando a la vez, las 5 que no
        ganan la carrera por _model_lock quedan bloqueadas ahi mismo
        esperando turno; si esa espera fuera la PRIMERA linea del metodo,
        todo el resto de la inicializacion propia de esa fuente -en
        particular, cargar sus zonas desde zones_web/- quedaria pospuesta
        detras de la carga (lenta) del modelo compartido. Poniendolo al
        final, cada fuente completa su propia inicializacion de inmediato
        y solo la adquisicion del modelo (rapida para todas menos la
        primera, que ya viene desde disco) queda serializada al final.
        """
        # ------------------------------------------------------------ #
        #  Tracker: resolver la ruta del .yaml de configuracion
        # ------------------------------------------------------------ #
        # --tracker llega normalmente como un nombre de archivo suelto
        # (default "bytetrack_queue.yaml"). Lo buscamos primero junto a
        # este script (_AQUI) para que funcione sin importar cual sea el
        # directorio de trabajo desde el que se lanza "uv run"; si no esta
        # ahi, se lo pasamos a ultralytics tal cual (puede ser una ruta
        # absoluta que nos dieron, o el nombre de un tracker propio de
        # ultralytics como "bytetrack.yaml"/"botsort.yaml").
        tracker_arg = self.args.tracker
        candidato = os.path.join(_AQUI, tracker_arg)
        if not os.path.isabs(tracker_arg) and os.path.isfile(candidato):
            tracker_arg = candidato
        self.tracker_path = tracker_arg
        print(f"[{self.NAME}] Tracker: {self.tracker_path}")

        # ------------------------------------------------------------ #
        #  Nombre de fuente + carpeta de resultados PROPIA de esta fuente
        # ------------------------------------------------------------ #
        # Nombre de "fuente" para la columna del Markdown. Preferimos
        # self.source_name: web_server_zones.py (_add_source) ahora nos lo
        # asigna, junto a source_key, ya resuelto a algo legible (el nombre
        # que puso el operador al agregar la fuente, o si no puso nada,
        # os.path.basename() del source crudo -ver el TODO junto a esa
        # linea en _add_source sobre el caso de URLs RTSP sin ruta final-).
        # A diferencia de source_key (saneado para nombrar archivos, con
        # los separadores de ruta aplastados en "_"), source_name no pasa
        # por ese aplastado.
        #
        # Si por lo que sea no existe (detector arrancado fuera de este
        # panel, o una version vieja de web_server_zones.py que no lo
        # asigne todavia), caemos en source_key y, en ultimo caso, en NAME.
        self.fuente_nombre = (getattr(self, "source_name", None)
                               or getattr(self, "source_key", None)
                               or self.NAME)

        # Carpeta resultados/<clave-de-fuente>/ propia de ESTA fuente:
        # de aqui cuelgan tanto episodios.md como alertas/desercion|aforo
        # (ver _resolver_carpeta_key_fuente/_resolver_alert_dir). --alert-dir
        # (flag heredada del framework base, default "") sigue sirviendo
        # como override de la RAIZ si se pasa explicitamente -por ejemplo
        # para mandar todo a un disco/particion distinto-; sin ella, la
        # raiz es "resultados" junto a este script.
        raiz_resultados_arg = getattr(self.args, "alert_dir", "") or "resultados"
        raiz_resultados = (raiz_resultados_arg if os.path.isabs(raiz_resultados_arg)
                            else os.path.join(_AQUI, raiz_resultados_arg))
        self._resultados_fuente_dir = os.path.join(
            raiz_resultados, self._resolver_carpeta_key_fuente())
        os.makedirs(self._resultados_fuente_dir, exist_ok=True)
        print(f"[{self.NAME}] Resultados de esta fuente: {self._resultados_fuente_dir}")

        # ------------------------------------------------------------ #
        #  Markdown de verificacion: resolver ruta
        # ------------------------------------------------------------ #
        # --episodios-md es normalmente solo un NOMBRE de archivo
        # ("episodios.md"), que ahora se resuelve DENTRO de
        # self._resultados_fuente_dir (una fuente, un episodios.md propio,
        # nunca compartido con otra). Una ruta ABSOLUTA se sigue
        # respetando tal cual (override explicito, ver el docstring de la
        # flag) -este archivo lo vamos a CREAR nosotros si falta (ver
        # _asegurar_encabezado_md), no hace falta que exista de antemano.
        episodios_arg = self.args.episodios_md
        if os.path.isabs(episodios_arg):
            self.episodios_md_path = episodios_arg
        else:
            self.episodios_md_path = os.path.join(self._resultados_fuente_dir,
                                                    episodios_arg)
        print(f"[{self.NAME}] Episodios (Markdown): {self.episodios_md_path}")

        # ------------------------------------------------------------ #
        #  PARTE 1: reloj de fuente (--reloj), resuelto UNA vez aqui
        # ------------------------------------------------------------ #
        # No se decide por frame: si es "auto", se detecta el tipo de
        # fuente una sola vez (no deberia cambiar durante la vida de esta
        # instancia) y se fija self._reloj_modo a "video" o "pared", que
        # es lo que lee _reloj_ahora() en cada llamada a process_frame.
        if self.args.reloj == "auto":
            tipo_fuente = self._detectar_tipo_fuente()
            self._reloj_modo = "video" if tipo_fuente == "archivo" else "pared"
        else:
            self._reloj_modo = self.args.reloj
        if self._reloj_modo == "video" and self.args.fps_video <= 0:
            # --fps-video invalido (0 o negativo): _reloj_ahora haria una
            # division por cero. Nos caemos a reloj de pared en vez de
            # arrancar rota la fuente; es mejor un reloj sesgado y
            # conocido que un detector que no arranca.
            print(f"[{self.NAME}] AVISO: --fps-video={self.args.fps_video} "
                  f"no es valido (tiene que ser > 0); uso reloj de PARED "
                  f"en su lugar.")
            self._reloj_modo = "pared"
        print(f"[{self.NAME}] Reloj de la maquina de estados: "
              f"{self._reloj_modo} (--reloj={self.args.reloj})")
        if self._reloj_modo == "video":
            print(f"[{self.NAME}] AVISO: reloj de VIDEO activo "
                  f"(--fps-video={self.args.fps_video}). Los tiempos "
                  f"medidos con este reloj NO son comparables con "
                  f"episodios registrados con reloj de PARED "
                  f"(time.monotonic(), el que se usaba antes de esta "
                  f"version) -mezclar ambos en el mismo analisis daria "
                  f"conclusiones erroneas. Ver _reloj_ahora para el "
                  f"detalle de como se deriva del frame_idx.")
        # Ultimo frame_idx visto en process_frame, para que teardown()
        # (que no recibe un frame_idx nuevo) pueda seguir derivando el
        # reloj de video sin saltar hacia adelante -ver _reloj_ahora.
        self._ultimo_frame_idx_reloj = 0

        # ------------------------------------------------------------ #
        #  PARTE 2: memoria de histeresis por track ID (--histeresis)
        # ------------------------------------------------------------ #
        # track_id -> ultima zona ("FILA"/"CAJA"/"FUERA") ya resuelta CON
        # histeresis para ese track. Es memoria de la capa de GEOMETRIA
        # (que zona gano la ultima vez), separada a proposito de
        # self.candidatos_fila/self.expedientes (memoria de la capa de
        # ESTADOS): un track puede estar solo en FUERA o en CAJA sin
        # llegar nunca a ser candidato de FILA, y aun asi necesita esta
        # memoria para que la histeresis funcione en su propio borde.
        # Ver _punto_en_zona para el uso.
        self._zona_previa = {}

        # ------------------------------------------------------------ #
        #  PARTE 7: puestos de CAJA como reserva CON MEMORIA
        #  (--retener-puesto), a nivel de la fuente
        # ------------------------------------------------------------ #
        # Conjunto de track_id que ocupan AHORA MISMO un puesto de
        # EN_ATENCION en esta fuente (maximo --capacidad-caja). Es
        # memoria A NIVEL DE FUENTE (no de cada expediente por separado):
        # antes de esta parte, la asignacion de puesto se recalculaba
        # DESDE CERO cada frame mirando solo quien aparecia ese frame en
        # CAJA -si el ocupante quedaba ocluido un instante y no se le
        # detectaba, la regla de continuidad no podia protegerlo (no
        # estaba en la lista de ese frame) y el puesto pasaba al
        # siguiente; un frame despues el ocupante reaparecia y habia DOS
        # personas EN_ATENCION a la vez con capacidad 1. Con este conjunto
        # persistente, el ocupante retiene su puesto mientras su racha de
        # CAJA cruda (self.expedientes[tid]["caja_t_ultima_vez"], ver
        # _actualizar_racha_zona_cruda) no lleve mas de --retener-puesto
        # segundos sin confirmarse, o hasta que su expediente cierre (ver
        # el descarte explicito en _cerrar_expediente). Ver el paso
        # "PARTE 7" en process_frame para el purgado/asignacion completos.
        self._ocupantes_caja = set()

        # ------------------------------------------------------------ #
        #  PARTE 3: estado_cola() - snapshot seguro entre hilos
        # ------------------------------------------------------------ #
        # self._estado_lock (creado en configure(), ver ese metodo -corre
        # antes que setup(), asi que ya existe aqui-) protege estas dos
        # variables de cache (self._cache_personas/self._cache_resumen),
        # el append a self.episodios (ver _cerrar_expediente) y, desde la
        # PARTE 2, los umbrales ajustables en caliente (self.min_fila,
        # self.conf, etc., ver PARAMS y configure()). NO protege
        # self.expedientes/self.candidatos_fila en si, que solo los toca
        # el hilo propio de esta fuente (process_frame/teardown, ver
        # docstring de setup()) y por tanto no necesitan lock entre ellos.
        # estado_cola() puede llamarse desde OTRO hilo (ej. un panel
        # externo que arma su propia vista) mientras process_frame sigue
        # corriendo en el suyo: en vez de leer self.expedientes
        # directamente desde ese otro hilo (inseguro: un dict puede
        # cambiar de tamano a mitad de una iteracion externa mientras este
        # hilo hace `del self.expedientes[...]` o abre un expediente
        # nuevo), process_frame arma, al final de CADA frame, un snapshot
        # ya resuelto (ver _construir_snapshot_estado) y lo publica aqui
        # bajo el lock; estado_cola() solo copia esas dos referencias bajo
        # el mismo lock (una asignacion de puntero, practicamente
        # instantanea) y formatea/ordena FUERA del lock, para bloquear a
        # process_frame lo minimo posible. Se inicializan aqui con valores
        # vacios para que estado_cola() nunca falle si se llama antes de
        # que el primer frame termine de procesarse.
        self._cache_personas = []
        self._cache_resumen = {
            "atendiendo": 0, "en_fila": 0, "desertando": 0,
            "atendidos": 0, "desertores": 0, "indeterminados": 0,
            "tasa_desercion": 0.0, "espera_media": 0.0, "servicio_medio": 0.0,
        }

        # Si el archivo de episodios no existe todavia, lo creamos con el
        # encabezado de la tabla y una linea con fecha/parametros activos.
        # Se hace con open/write/close (sin mantenerlo abierto), igual que
        # cada fila que se anexa despues -ver _asegurar_encabezado_md-.
        self._asegurar_encabezado_md()

        # Conjunto de TODOS los track IDs distintos vistos desde que arranco
        # esta fuente (no solo los presentes en el frame actual). Un set
        # evita contar dos veces el mismo ID si reaparece tras una oclusion
        # corta que ByteTrack ya resolvio internamente (re-asociacion dentro
        # de track_buffer). Vive mientras viva el proceso/fuente: si se
        # reinicia la fuente, se reinicia tambien este conteo.
        self.seen_track_ids = set()

        # PARTE 4a: contador de llegadas, incremental y nunca se reinicia
        # ni se reutiliza (aunque un candidato se descarte sin llegar a
        # abrir expediente). Vive por fuente, igual que self.seen_track_ids.
        self._contador_llegadas = 0

        # PARTE 3: track_id -> `ahora` en que se cerro su ULTIMO episodio
        # ATENDIDO (ver _cerrar_expediente). Mientras
        # ahora_actual - valor < --cooldown-atendido, ese track_id no puede
        # abrir expediente nuevo (ver la rama "Sin expediente todavia" del
        # bucle 2). No se poda (igual que self.seen_track_ids): una entrada
        # vieja es inofensiva -la resta simplemente da un numero grande que
        # ya no cumple la condicion de cooldown- y podarla le haria perder
        # la memoria de "reencolado" si --cooldown-atendido se sube despues
        # en caliente.
        self._cooldown_hasta = {}

        # ------------------------------------------------------------ #
        #  Maquina de estados FILA/CAJA con memoria por track ID
        # ------------------------------------------------------------ #
        # self.candidatos_fila: track_id -> {"inicio": t, "ultima_vez": t,
        #   "orden": n}
        #   Personas que estan ahora mismo clasificadas en FILA pero que
        #   TODAVIA no acumulan --min-fila segundos ahi: es una zona de
        #   "cuarentena" antes de abrir expediente formal, para no contar
        #   como episodio a quien solo atraviesa la zona de fila de paso.
        #   "ultima_vez" se reutiliza tambien para descartar candidatos cuyo
        #   track ya no se ve (o dejo de estar en FILA) hace mas de
        #   --gracia segundos, para que este diccionario no crezca sin
        #   limite con IDs que nunca llegaron a nada. "orden" (PARTE 4a) es
        #   el numero de llegada asignado al crear este candidato, que se
        #   traspasa al expediente si llega a abrirse (ver mas abajo).
        self.candidatos_fila = {}

        # self.expedientes: track_id -> dict con el expediente EN CURSO
        # (solo estados EN_FILA o EN_ATENCION; en cuanto se cierra como
        # ATENDIDO o DESERTOR se saca de aqui y se archiva en
        # self.episodios). Campos por expediente:
        #   estado           -> "EN_FILA" o "EN_ATENCION"
        #   t_entrada_fila   -> cuando se abrio el expediente (= el inicio
        #                       del candidato que supero --min-fila)
        #   t_inicio_atencion-> cuando paso de EN_FILA a EN_ATENCION (None
        #                       mientras siga en EN_FILA)
        #   t_fin_atencion   -> cuando salio de CAJA (None hasta que ocurre;
        #                       es un campo interno que no pediste en el
        #                       expediente pero que hace falta para poder
        #                       calcular "servicio" al cerrar -ver docstring
        #                       de process_frame para el detalle-)
        #   t_ultima_vez     -> ultima vez CONFIRMADA en la zona que
        #                       corresponde al estado actual: en FILA
        #                       mientras el expediente sigue en EN_FILA, en
        #                       CAJA mientras sigue en EN_ATENCION (ver
        #                       comentario en el bucle principal). Es lo
        #                       que compara el barrido de gracia contra
        #                       --gracia para decidir si el expediente ya
        #                       lleva demasiado sin confirmarse y hay que
        #                       cerrarlo (ver ese barrido; el DESENLACE con
        #                       el que se cierra -ATENDIDO o DESERTOR- ya
        #                       NO se decide por este campo ni por el
        #                       estado, sino por caja_alcanzada de mas
        #                       abajo, ver _desenlace_por_caja)
        #   altura_suma/altura_max/altura_n -> SOLO si --diag-bbox esta
        #                       activo (ver _acumular_altura): estadisticos
        #                       del alto de bbox (y2-y1) de esta persona
        #                       acumulados desde que arranco como
        #                       candidato, para el diagnostico de --anclaje.
        #   caja_racha_inicio/caja_alcanzada/t_entrada_caja/
        #   caja_t_ultima_vez -> PARTE 6 (ver _actualizar_racha_zona_cruda,
        #                       llamado con prefijo="caja"): presencia
        #                       CONTINUA en la geometria CRUDA de
        #                       ZONA_CAJA, sin importar si esta persona
        #                       tiene puesto asignado por --capacidad-caja.
        #                       caja_alcanzada (bool, para siempre una vez
        #                       True) es lo que decide ATENDIDO vs DESERTOR
        #                       al cerrar (_desenlace_por_caja); t_entrada_caja
        #                       y caja_t_ultima_vez son las referencias de
        #                       inicio/fin de "servicio" (ver
        #                       _tiempos_geometria_cruda).
        #   fila_cruda_racha_inicio/fila_cruda_alcanzada/
        #   t_entrada_fila_cruda/fila_cruda_t_ultima_vez -> PARTE 6, mismo
        #                       mecanismo que los de caja pero con
        #                       prefijo="fila_cruda", zona_objetivo="FILA"
        #                       y umbral=--min-fila: presencia CONTINUA en
        #                       la geometria CRUDA de ZONA_FILA (NO la zona
        #                       EFECTIVA: alguien denegado de puesto tiene
        #                       zona efectiva "FILA" sin haber pisado
        #                       ZONA_FILA de verdad, y eso NO debe contar
        #                       aqui). t_entrada_fila_cruda es la
        #                       referencia de INICIO de "espera" -distinto
        #                       del t_entrada_fila de mas arriba, que usa
        #                       la zona efectiva y sigue sirviendo para la
        #                       maquina de estados/DESERTOR-.
        #   estado_visible     -> PARTE 1: "ESPERANDO"/"DESERTOR"/
        #                       "ATENDIENDO"/"ATENDIDO", el estado EN VIVO
        #                       que se muestra en pantalla y en
        #                       estado_cola(). Es un campo PEGAJOSO: solo
        #                       cambia por una transicion confirmada de la
        #                       maquina de estados (ver el bucle 2 en
        #                       process_frame, unico lugar que lo escribe),
        #                       nunca se recalcula desde la zona del frame
        #                       en curso al momento de mostrarlo.
        #   regreso_racha_inicio -> PARTE 2: mientras estado_visible es
        #                       "DESERTOR", instante en que arranco la
        #                       racha ACTUAL de confirmacion sostenida en
        #                       FILA (None si no hay ninguna en curso). Se
        #                       consolida -y vuelve a ESPERANDO- recien a
        #                       los --confirmar-regreso segundos; se
        #                       reinicia a None si la racha se corta antes.
        #   regreso_caja_racha_inicio -> PARTE 2 (simetria): igual que
        #                       regreso_racha_inicio, pero para la racha de
        #                       confirmacion sostenida en CAJA -exigida para
        #                       que un DESERTOR consolide EN_ATENCION, no
        #                       solo para volver a ESPERANDO por FILA-.
        #                       Racha INDEPENDIENTE: confirmar por CAJA y
        #                       por FILA son intentos de salida distintos,
        #                       cada uno se rompe (vuelve a None) en cuanto
        #                       la zona efectiva de este frame no es la suya.
        #   orden              -> PARTE 4a: numero de llegada incremental
        #                       (self._contador_llegadas), asignado al
        #                       candidato la primera vez que entra a FILA
        #                       cruda y traspasado tal cual al abrir el
        #                       expediente formal; NUNCA cambia despues.
        #                       None si el expediente abrio directo en
        #                       CAJA (nunca hizo fila, ver PARTE 3 mas
        #                       arriba en el bucle 2).
        self.expedientes = {}

        # Episodios ya cerrados (ATENDIDO, DESERTOR, DESCARTADO o
        # INDETERMINADO), en memoria, en el orden en que se cerraron.
        # deque(maxlen=5000): al
        # llegar al limite, cada append() nuevo descarta automaticamente el
        # episodio mas viejo por el otro extremo -es la politica de tamano
        # acordada, para que esta lista deje de crecer sin limite durante
        # una sesion larga. El unico uso que hace el resto del archivo de
        # self.episodios es este mismo append() (ver _cerrar_expediente);
        # no se indexa, ni se recorre, ni se hace slicing en ningun otro
        # lado, asi que el cambio de list a deque no rompe nada.
        self.episodios = deque(maxlen=5000)

        # Acumuladores para "espera promedio" en el campo extra: sumar y
        # contar en cada cierre es O(1) por episodio, mucho mas barato que
        # recorrer self.episodios entero cada frame para promediar (que
        # seria O(n) y n solo crece).
        self._suma_espera = 0.0
        self._suma_servicio = 0.0
        self._total_atendidos = 0
        self._total_desertores = 0
        # PARTE 2/3: contador de episodios INDETERMINADO (fuente detenida
        # con expediente a medias). No participa de "espera media"/
        # "deserción" -esos siguen siendo solo ATENDIDO/DESERTOR-, es un
        # contador aparte para el resumen de estado_cola()/extra.
        self._total_indeterminados = 0
        # Contador de alertas DEFINITIVAS de desercion generadas en esta
        # sesion (una por cada expediente que se cierra de verdad como
        # DESERTOR, ver _alerta_desertor_definitiva/_cerrar_expediente).
        # Es un contador APARTE de self._total_desertores (aunque hoy
        # coincidan siempre 1 a 1 por construccion) para no acoplar el
        # conteo de negocio con el conteo de alertas generadas, por si el
        # dia de manana difieren (p.ej. una alerta que falla al escribir).
        self._total_alertas_desercion = 0

        # ------------------------------------------------------------ #
        #  AFORO: conteo de personas dentro del poligono de aforo
        #  (regions[2], ver process_frame) y estado de su alerta.
        # ------------------------------------------------------------ #
        # Independiente de la maquina de estados FILA/CAJA: no abre
        # expediente ni consume cupo de --capacidad-caja (ver PARAMS/
        # --limite-aforo). self._aforo_alerta_activa es el "rearme": UNA
        # sola alerta mientras el conteo siga por encima del limite,
        # mismo patron de _alerta_desertor_definitiva pero sin track
        # especifico -es un evento de la fuente entera- (ver
        # _alerta_aforo). self._aforo_alertas_sesion se reinicia junto al
        # resto de acumulados de jornada en reiniciar_jornada().
        self._aforo_actual = 0
        self._aforo_alerta_activa = False
        self._aforo_alertas_sesion = 0

        # Acumuladores del promedio GLOBAL de --diag-bbox. A diferencia de
        # alto_medio/alto_max (que son de la persona puntual cuyo episodio
        # cierra, ver _acumular_altura/_cerrar_expediente), este promedio
        # se actualiza en CADA frame con la altura de bbox de TODO track
        # ID valido que aparezca, sin importar en que zona este clasificado
        # ni si llego a abrir expediente o candidato -si solo contara a
        # quienes generan episodio, la muestra seria una minoria y el
        # promedio poco fiable. Se actualiza en el bucle principal de
        # process_frame, no aqui.
        #
        # Es un promedio POR FUENTE: cada camara/video que se agrega desde
        # el panel crea su propia instancia de este detector (ver docstring
        # de setup()), asi que este acumulador nunca se comparte entre
        # fuentes -cada una arranca el suyo en 0-.
        #
        # OJO: incluir a las personas del fondo del encuadre sesga este
        # promedio hacia ABAJO por perspectiva (a menor distancia focal
        # efectiva por lejania, una persona lejos de la camara ocupa menos
        # pixeles de alto que una igual de cerca) -no es un defecto a
        # corregir aqui, es la razon de ser de este diagnostico: sirve para
        # ver ese sesgo, no para promediarlo fuera.
        self._diag_suma_altura_global = 0.0
        self._diag_n_altura_global = 0

        # ------------------------------------------------------------ #
        #  Instrumentacion de rendimiento (--profile): SOLO medicion
        # ------------------------------------------------------------ #
        # Acumula, en segundos, cuanto tiempo pasa process_frame en cada
        # etapa (ver process_frame para donde se cronometra cada una) y
        # cuanto pasa FUERA de process_frame entre una llamada y la
        # siguiente ("hueco_externo": todo lo que hace el framework entre
        # medio -annotate, JPEG, sinks, espera del proximo frame- y que
        # este archivo no controla ni puede optimizar). Se promedia e
        # imprime cada 60 frames (ver _imprimir_profile) y se reinicia
        # para la ventana siguiente; se inicializan siempre, aunque
        # --profile este apagado, porque no cuesta nada.
        self._profile_acumulado = {
            "inferencia": 0.0,
            "extraccion": 0.0,
            "geometria": 0.0,
            "maquina_estados": 0.0,
            "etiquetas": 0.0,
            "total": 0.0,
            "hueco_externo": 0.0,
            # Desglose interno de model.track(), tomado de
            # resultados.speed (que ultralytics reporta en ms; se
            # convierte a segundos aqui para acumular en la misma unidad
            # que el resto de "acc" y que _imprimir_profile lo multiplique
            # por 1000 igual que a todos los demas). Es un desglose de
            # "inferencia", no una medicion adicional independiente.
            "ultra_preprocess": 0.0,
            "ultra_inference": 0.0,
            "ultra_postprocess": 0.0,
        }
        self._profile_frames = 0
        self._profile_n_hueco = 0
        # Momento (time.perf_counter()) en que TERMINO el process_frame
        # anterior. None en el primer frame (todavia no hay "anterior" con
        # que comparar). La resta entre esta marca y el inicio del
        # siguiente process_frame es exactamente el hueco_externo de
        # arriba.
        self._profile_prev_fin = None

        # ------------------------------------------------------------ #
        #  Zonas: cargar lo que haya guardado de una sesion anterior
        # ------------------------------------------------------------ #
        # Convencion (ver docstring de set_zones): regions[0] = ZONA_FILA,
        # regions[1] = ZONA_CAJA. En esta version NO se usan todavia para
        # nada (no hay geometria ni tracking); solo se guardan para que
        # el paso siguiente pueda apoyarse en ellas.
        #
        # self.regions y self.zone_expand arrancan siempre inicializados
        # (aunque no haya archivo de zonas) para que el resto del codigo
        # -incluido process_frame- nunca tenga que preguntar si existen.
        # Las zonas pueden haber llegado YA (set_zonas del compuesto antes
        # de este setup perezoso): NO pisarlas — solo asegurar que los
        # atributos existan si este detector se instancio sin configure()
        # (no deberia pasar, pero un detector que arranca vacio es mejor
        # que uno que revienta).
        self.zonas = getattr(self, "zonas", None) or []
        self.regions = _zonas.a_regions(self.zonas)
        self.zone_expand = getattr(self, "zone_expand", None)

        # web_server_zones.py asigna self.source_key ANTES de llamar a
        # setup() (ver create_app._add_source en ese archivo), asi que aqui
        # ya deberia estar disponible. Con getattr(..., None) evitamos un
        # AttributeError si algun dia este detector se arranca de otra
        # forma que no pase por ese panel.
        source_key = getattr(self, "source_key", None)
        if source_key:
            zones_path = os.path.join(ZONES_WEB_DIR, f"{source_key}.json")
            if os.path.isfile(zones_path):
                try:
                    with open(zones_path) as f:
                        guardado = json.load(f)
                    # v2 tipado ("zonas") o legacy ("regions", tipos por
                    # posicion historica) — ver zonas.leer_payload.
                    self.zonas = _zonas.leer_payload(guardado, self.NAME)
                    self.regions = _zonas.a_regions(self.zonas)
                    self.zone_expand = guardado.get("zone_expand")
                    print(f"[{self.NAME}] Zonas cargadas desde disco: "
                          + ", ".join(f"{z['id']}={z['tipo']}" for z in self.zonas)
                          + f" ({zones_path})" if self.zonas
                          else f"[{self.NAME}] Archivo de zonas sin zonas "
                               f"validas ({zones_path})")
                except Exception as e:
                    # Si el JSON esta corrupto o no se puede leer, no
                    # queremos que el detector deje de arrancar por eso:
                    # seguimos sin zonas, igual que si no existiera el
                    # archivo.
                    print(f"[{self.NAME}] No se pudieron cargar zonas de "
                          f"{zones_path}: {e}")
            else:
                print(f"[{self.NAME}] Sin zonas guardadas todavia "
                      f"({zones_path} no existe), arranco sin zonas")
        else:
            # Sin source_key no hay forma de saber que archivo leer; esto
            # es normal si el detector se instancia fuera del panel web.
            print(f"[{self.NAME}] Sin source_key, arranco sin zonas")

        # ------------------------------------------------------------ #
        #  Modelo YOLO (compartido): AL FINAL a proposito -ver el OJO del
        #  docstring de arriba-, para que todo lo de esta fuente (zonas,
        #  tracker, markdown, reloj) ya haya quedado listo antes de
        #  bloquearse en _model_lock esperando su turno.
        # ------------------------------------------------------------ #
        from ultralytics import YOLO

        print(f"[{self.NAME}] Cargando modelo: {self.args.model}")
        self.model = YOLO(self.args.model)

    # ------------------------------------------------------------------ #
    #  2a) PARTE 1: reloj de fuente (--reloj)
    # ------------------------------------------------------------------ #
    def _detectar_tipo_fuente(self):
        """
        Heuristica para --reloj=auto: intenta decidir si esta fuente es un
        archivo de video (-> reloj de video) o un stream en vivo, RTSP o
        webcam (-> reloj de pared).

        INVESTIGACION (pedida en el prompt de esta tarea): ni este archivo
        ni web_server_zones.py -el otro que tengo permitido leer- le pasan
        al detector el tipo de fuente, un objeto cv2.VideoCapture, ni los
        FPS nativos. _add_source() en web_server_zones.py SOLO asigna dos
        atributos al detector: self.source_key (saneado con
        secure_filename, pensado para nombrar archivos en disco, no para
        leerse) y self.source_name (el nombre legible que puso el operador
        al agregar la fuente, o si no puso nada, os.path.basename() del
        source crudo). Ninguno de los dos declara explicitamente "esto es
        un archivo" / "esto es RTSP" / "esto es una webcam". Por eso esto
        es una INFERENCIA sobre el TEXTO del nombre, no una certeza:

          - Puramente numerico (ej. "0", "1"): indice de webcam -> "stream".
          - Termina en una extension de video conocida (secure_filename
            preserva los puntos, asi que la extension sobrevive el
            saneado de source_key; source_name tampoco la toca): archivo
            -> "archivo".
          - Cualquier otro caso (incluida una URL RTSP, que para cuando
            llega aqui ya perdio el prefijo "rtsp://" -source_key le
            aplasto los "/" a "_", y source_name, si no hay nombre puesto
            por el operador, le aplico os.path.basename() que devuelve
            solo el ultimo tramo o, si la URL no tiene ruta final, el
            propio "usuario:password@host:puerto"-) se asume "stream" por
            seguridad: tratar por error un stream en vivo como "video"
            fijaria su reloj a un frame_idx/--fps-video que podria no
            tener nada que ver con la realidad; tratar por error un
            archivo como "stream" simplemente mantiene el reloj de pared
            que ya existia antes de este cambio (sesgado con archivos,
            pero un sesgo ya conocido, no una rotura nueva).
        """
        nombre = (getattr(self, "source_name", None)
                  or getattr(self, "source_key", None) or "")
        base = os.path.basename(nombre)
        if base.isdigit():
            return "stream"
        _, ext = os.path.splitext(base)
        extensiones_video = (".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".ts")
        if ext.lower() in extensiones_video:
            return "archivo"
        return "stream"

    def _reloj_ahora(self, frame_idx):
        """
        Devuelve "ahora" segun self._reloj_modo (resuelto UNA vez en
        setup() a partir de --reloj/--fps-video, ver ese metodo y
        _detectar_tipo_fuente). Centralizado aqui para que process_frame y
        teardown() usen exactamente el mismo criterio.

        Reloj de VIDEO (self._reloj_modo == "video"):
            ahora = frame_idx / self.args.fps_video
        No tenemos acceso al objeto cv2.VideoCapture de la fuente (lo
        administra el framework, fuera de este archivo) ni a sus FPS
        nativos reales, asi que --fps-video es un valor que el OPERADOR
        debe indicar (default 30.0) en vez de algo que este codigo mida
        solo -es la alternativa que se autorizo para cuando no hay acceso
        al VideoCapture: derivar el tiempo del numero de frame y los FPS
        nativos de la fuente-.

        ASUME que frame_idx es la posicion real del frame dentro del
        video (crece 1 a 1 por cada frame del archivo, en el mismo orden
        en que se reproduciria). Si el framework aplicara --frame-skip de
        forma que frame_idx contara en cambio los frames PROCESADOS
        (saltando los que se descartan) en vez de la posicion real en el
        archivo, este reloj quedaria mal calibrado -no pude confirmar cual
        de las dos semanticas tiene frame_idx sin ver base_detector.py,
        que no esta disponible en este workspace-.

        Reloj de PARED (time.monotonic()): el que ya se usaba antes de
        este cambio. Correcto para RTSP/webcam, donde los frames llegan en
        tiempo real y el momento en que los procesamos SI corresponde al
        tiempo real transcurrido.

        frame_idx puede venir en None: lo hace teardown(), que no recibe
        un frame nuevo que procesar. En ese caso, para el reloj de video,
        se reusa el ULTIMO frame_idx visto en process_frame
        (self._ultimo_frame_idx_reloj) para no saltar el reloj hacia
        adelante artificialmente al cerrar expedientes por corte de fuente.
        """
        if self._reloj_modo == "video":
            if frame_idx is None:
                frame_idx = self._ultimo_frame_idx_reloj
            return frame_idx / self.args.fps_video
        return time.monotonic()

    # ------------------------------------------------------------------ #
    #  2b) Recepcion de zonas desde el editor del panel (hot-reload)
    # ------------------------------------------------------------------ #
    def set_zones(self, regions, zone_expand=None, opts=None):
        """
        Llamado por web_server_zones.py (endpoint POST /api/sources/<id>/zones,
        ver set_zones() alrededor de la linea 448 de ese archivo) justo despues
        de guardar las zonas en zones_web/<source_key>.json, para que el
        detector las tenga en caliente sin reiniciar el proceso.

        Argumentos EXACTOS que manda ese endpoint (no cambiar el orden ni los
        nombres):
            regions      -> lista de zonas. Cada zona es una lista de puntos
                             [x, y] en pixeles, sobre el frame ya redimensionado
                             (800x600 en este proyecto). El panel garantiza
                             minimo 2 puntos por zona y su MAX_ZONES (12); aqui NO
                             hace falta volver a validar eso.
            zone_expand   -> int o None. Cuantos pixeles "extra" se debe
                             expandir la zona de deteccion (lo usan otros
                             detectores para un margen de seguridad). Esta
                             version solo lo guarda, no lo aplica todavia.
            opts          -> dict con opciones especificas de otros modelos
                             (por ejemplo CONTADOR_V2 manda n_lines/invert/
                             axis_rot). Para QueueAnalytics no aplica nada;
                             se recibe y se ignora para no romper la llamada
                             del panel (que a veces manda este 3er argumento
                             y a veces no).

        CONVENCION DE ESTE DETECTOR (importante, ver seccion de contexto):
            El operador dibuja hasta 3 zonas, SIEMPRE en este orden:
                regions[0] -> ZONA_FILA  (donde la gente espera)
                regions[1] -> ZONA_CAJA  (mostrador/atencion)
                regions[2] -> ZONA_AFORO (conteo de ocupacion, opcional;
                                          ver --limite-aforo)
            OJO (fusion Queue-Cashier): esta convencion POSICIONAL es solo
            de ESTE camino legacy — el camino real es set_zonas(), donde
            cada zona trae su tipo y la posicion ya no significa nada. ZONA_AFORO es
            independiente de la maquina de estados FILA/CAJA: solo
            alimenta el conteo de process_frame/_alerta_aforo, nunca abre
            expediente ni consume cupo de --capacidad-caja.
        """
        # LEGACY (fusion Queue-Cashier): este metodo queda como wrapper del
        # protocolo tipado. Los puntos pelados se tipan por POSICION
        # historica (regions[0]=FILA...) y casar_ids() preserva el id de
        # toda zona cuyos puntos no cambiaron, para que un guardado por el
        # camino viejo no re-baraje ids y vuelva mentiroso el historial.
        self.zone_expand = zone_expand
        try:
            nuevas = _zonas.desde_legacy(regions or [], self.NAME)
        except ValueError as e:
            print(f"[{self.NAME}] set_zones legacy: zonas invalidas ({e}) "
                  f"— se ignoran")
            return
        _zonas.casar_ids(nuevas, self.zonas)
        self.set_zonas(nuevas)

    # ------------------------------------------------------------------ #
    #  Protocolo de zonas TIPADAS (fusion Queue-Cashier, Base/zonas.py)
    # ------------------------------------------------------------------ #
    def set_zonas(self, zonas):
        """Camino REAL de recepcion de zonas (el panel llama aqui con la
        lista canonica ya validada; set_zones legacy tambien delega aqui).
        A partir de la fusion, la FILA/CAJA/AFORO se resuelven POR TIPO
        (ver process_frame/_dibujar_zonas), nunca por posicion: borrar un
        poligono ya no puede convertir la caja en fila en silencio.

        Misma disciplina de hilos que tenia self.regions: se construyen
        las listas COMPLETAS y se asignan al final (asignacion atomica en
        CPython); process_frame toma su propia referencia local al inicio
        del frame."""
        zonas = list(zonas or [])
        self.zonas = zonas
        self.regions = _zonas.a_regions(zonas)     # vista legacy derivada
        # Aviso operativo: FILA y CAJA no deberian SOLAPARSE — CAJA tiene
        # prioridad en la clasificacion, asi que un formado parado dentro
        # del solape compite por el cupo de atencion como si estuviera en
        # la caja. Solo se avisa (el operador decide); medido por area de
        # mascaras, umbral 2%.
        try:
            zf = _zonas.por_tipo(zonas, "FILA")
            zc = _zonas.por_tipo(zonas, "CAJA")
            if zf and zc:
                mf = np.zeros((600, 800), np.uint8)
                mc = np.zeros((600, 800), np.uint8)
                cv2.fillPoly(mf, [np.array(zf["puntos"], np.int32)], 1)
                cv2.fillPoly(mc, [np.array(zc["puntos"], np.int32)], 1)
                inter = int((mf & mc).sum())
                if inter > 0.02 * max(1, int(mc.sum())):
                    print(f"[{self.NAME}] AVISO: las zonas FILA y CAJA se "
                          f"SOLAPAN ({inter} px, "
                          f"{100.0 * inter / max(1, int(mc.sum())):.0f}% de la "
                          f"CAJA). CAJA tiene prioridad: los formados dentro "
                          f"del solape compiten por el cupo de atencion. "
                          f"Recomendado: redibujar sin solape.")
        except Exception:  # noqa: BLE001  (el aviso jamas tumba el guardado)
            pass
        print(f"[{self.NAME}] set_zonas: "
              + (", ".join(f"{z['id']}={z['tipo']}" for z in zonas)
                 if zonas else "SIN zonas")
              + f" | zone_expand={getattr(self, 'zone_expand', None)}")
        return True

    def get_zonas(self):
        return list(getattr(self, "zonas", None) or [])

    def get_zones(self):
        """
        Llamado por web_server_zones.py (endpoint GET /api/sources/<id>/zones,
        ver get_zones() alrededor de la linea 430 de ese archivo) para saber
        que zonas mostrar cuando el operador abre el editor.

        Ese endpoint hace exactamente esto:

            getter = getattr(w.detector, "get_zones", None)
            regions = getter() if callable(getter) else []

        es decir: se llama SIN argumentos y debe devolver SOLO la lista de
        zonas (lo mismo que recibe set_zones en su parametro "regions"), no
        un dict con mas campos. El propio endpoint arma el resto de la
        respuesta ({"regions": ..., "zone_expand": ..., "full_frame": ...})
        leyendo self.zone_expand por fuera con getattr(), asi que no hace
        falta devolverlo aqui tambien.

        Sin este metodo, el editor cargaria vacio despues de un reinicio del
        proceso (aunque el detector ya tenga las zonas leidas del disco en
        setup()), y el operador tendria que redibujarlas a mano -con el
        riesgo de invertir el orden regions[0]=ZONA_FILA / regions[1]=
        ZONA_CAJA, que es el error mas grave posible en este modelo-.
        """
        return getattr(self, "regions", None) or []

    # ------------------------------------------------------------------ #
    #  2c) Geometria de zonas: punto-en-poligono
    # ------------------------------------------------------------------ #
    @staticmethod
    def _preparar_contorno(poligono):
        """
        Convierte una zona (lista de puntos [x, y] tal como la manda el
        editor) al formato que espera cv2.pointPolygonTest: un array numpy
        float32. Se hace UNA vez por frame y por zona (no una vez por
        persona) porque es el mismo trabajo repetido si hay varias personas
        en el mismo frame -y en una fila puede haber bastantes-.

        Devuelve None si la zona no existe todavia o no tiene puntos
        suficientes para encerrar un area (un poligono necesita minimo 3),
        para que el resto del codigo pueda tratar "sin zona" y "zona
        degenerada" exactamente igual: nadie esta dentro.
        """
        if not poligono or len(poligono) < 3:
            return None
        return np.array(poligono, dtype=np.float32)

    def _punto_en_zona(self, punto, contorno, histeresis, estaba_dentro=False):
        """
        True si `punto` (tupla x, y en pixeles del frame) cae dentro de
        `contorno`, aplicando self.zone_expand como margen y, si
        `histeresis` > 0, un umbral distinto segun `estaba_dentro`
        (PARTE 2).

        PARTE 2 (parametros en caliente): `histeresis` llega como
        PARAMETRO -ya no se lee de self.args.histeresis aqui dentro- para
        que todas las personas de un mismo frame se evaluen contra el
        MISMO valor, tomado una unica vez del snapshot que process_frame
        arma al principio bajo self._estado_lock (ver el inicio de ese
        metodo); evita ademas leer el atributo en caliente una vez por
        persona y por zona (dos veces por persona: CAJA y FILA).

        cv2.pointPolygonTest(..., measureDist=True) ya hace el trabajo caro
        (recorrer los vertices del poligono) y de paso devuelve, ademas de
        dentro/fuera, la DISTANCIA con signo en pixeles del punto al borde
        mas cercano: positiva si esta dentro, negativa si esta fuera.

        Para aplicar zone_expand SIN modificar el poligono original,
        aprovechamos justo ese numero: en vez de agrandar geometricamente el
        poligono (offset/buffer tipo Minkowski, que con poligonos concavos o
        con vertices muy juntos se vuelve delicado de hacer bien y no es
        gratis calcularlo cada frame), simplemente relajamos el umbral de
        "dentro" de 0 a -zone_expand. Un punto que esta a menos de
        zone_expand pixeles FUERA del poligono pasa a contar como dentro.
        Es geometricamente equivalente a expandir el poligono zone_expand
        pixeles hacia afuera, pero sin tocar sus vertices y sin coste extra:
        mismo recorrido O(vertices) que el test sin expandir.

        HISTERESIS (--histeresis, en pixeles): reutiliza la misma distancia
        con signo para poner DOS umbrales en vez de uno, segun si el track
        ya estaba clasificado dentro de ESTA zona en el frame anterior
        (`estaba_dentro`, ver self._zona_previa en process_frame):
          - Para ENTRAR (estaba_dentro=False): umbral MAS estricto,
            distancia >= -expand + histeresis -hay que estar un poco MAS
            adentro del margen normal para que cuente como "ya entro".
          - Para seguir DENTRO o recien SALIR (estaba_dentro=True): umbral
            MAS laxo, distancia >= -expand - histeresis -tolera estar un
            poco MAS afuera del margen normal antes de contar como fuera.
        Esto crea una banda muerta de 2*histeresis pixeles alrededor del
        borde (mas zone_expand) donde el resultado NO cambia segun de que
        lado se venia, que es justo lo que evita el parpadeo frame a frame
        de quien esta parado justo en el limite entre dos zonas. Es un
        retardo DELIBERADO en la transicion (la persona tarda un poco mas
        en "confirmarse" en la zona nueva) -preferible al parpadeo, que
        corta cronometros y genera episodios falsos. Con --histeresis=0
        ambos umbrales colapsan a -expand: se vuelve al comportamiento
        unico de antes de este cambio.
        """
        if contorno is None:
            return False
        distancia = cv2.pointPolygonTest(contorno, punto, True)
        expand = self.zone_expand or 0
        umbral = (-expand - histeresis) if estaba_dentro else (-expand + histeresis)
        return distancia >= umbral

    @staticmethod
    def _punto_de_contacto(x1, y1, x2, y2, anclaje):
        """
        Punto (x, y) de la caja que se compara contra las zonas, segun
        --anclaje. Es la UNICA funcion que decide esto; el resto del
        codigo (clasificacion FILA/CAJA/FUERA, maquina de estados) no
        cambia en absoluto sea cual sea el anclaje elegido, porque todos
        reciben el mismo tipo de punto (x, y) sin saber como se calculo.

        Los tres modos comparten la misma coordenada horizontal
        (x1+x2)/2 -el anclaje solo mueve el punto en el eje vertical-:

            "pies"   -> centro del BORDE INFERIOR: (cx, y2). Comportamiento
                        historico de este modelo (ver el paso de zonas):
                        aproxima donde pisa la persona, la referencia
                        correcta cuando las zonas estan dibujadas sobre el
                        plano del suelo. Default.
            "cabeza" -> centro del BORDE SUPERIOR: (cx, y1).
            "centro" -> centro GEOMETRICO de la caja: (cx, (y1+y2)/2).

        IMPORTANTE - cambiar --anclaje INVALIDA las zonas ya dibujadas: el
        operador calibro los poligonos de FILA/CAJA mirando donde caia el
        punto de contacto ("pies" por defecto) sobre el plano del suelo del
        frame. Si se cambia a "cabeza" o "centro" sin redibujar las zonas,
        el punto que se compara salta varias decenas de pixeles hacia
        arriba respecto a donde el operador calibro -sigue siendo un punto
        valido, pero ya no corresponde con ese plano-, y la clasificacion
        en FILA/CAJA/FUERA deja de ser fiable aunque el codigo no falle ni
        avise de nada. Las zonas SIEMPRE se deben redibujar despues de
        cambiar --anclaje.
        """
        cx = (x1 + x2) / 2.0
        if anclaje == "cabeza":
            return (cx, y1)
        if anclaje == "centro":
            return (cx, (y1 + y2) / 2.0)
        return (cx, y2)  # "pies" (default)

    # ------------------------------------------------------------------ #
    #  2d) Maquina de estados: helpers de formato y cierre de expediente
    # ------------------------------------------------------------------ #
    @staticmethod
    def _mmss(segundos):
        """
        Formatea un numero de segundos (float, puede venir de una resta de
        marcas de time.monotonic()) como mm:ss para la etiqueta y el extra.
        max(0, ...) evita mostrar negativos por algun redondeo o carrera
        minima entre lecturas de tiempo.
        """
        segundos = max(0, int(segundos))
        return f"{segundos // 60:02d}:{segundos % 60:02d}"

    @staticmethod
    def _acumular_altura(destino, y1, y2):
        """
        SOLO se llama cuando --diag-bbox esta activo (los sitios que la
        invocan ya lo comprueban antes). Acumula, dentro del dict
        `destino` (un candidato de self.candidatos_fila o un expediente de
        self.expedientes), estadisticos del alto de bbox (y2 - y1) de esta
        persona: suma (para promediar despues), maximo, y cantidad de
        frames acumulados.

        Se llama UNA vez por frame mientras el track siga siendo
        candidato o expediente, sin importar en que zona este clasificado
        ESE frame -"mientras estuvo trackeada" cubre toda la vida del
        candidato/expediente (desde que entro a FILA por primera vez hasta
        que el episodio cierra), no solo los frames en FILA-.

        destino.get(..., default) en vez de asumir que las claves ya
        existen: funciona igual la primera vez que se llama sobre un
        candidato/expediente recien creado (las siembra desde 0) que en
        las llamadas siguientes (las va acumulando).
        """
        altura = y2 - y1
        destino["altura_suma"] = destino.get("altura_suma", 0.0) + altura
        destino["altura_max"] = max(destino.get("altura_max", 0.0), altura)
        destino["altura_n"] = destino.get("altura_n", 0) + 1

    @staticmethod
    def _actualizar_racha_zona_cruda(destino, prefijo, zona_cruda_actual, zona_objetivo, umbral, ahora,
                                      frame_idx=None):
        """
        PARTE 6 (corrige el criterio de desercion tras --capacidad-caja, y
        unifica espera/servicio/total con la geometria CRUDA): actualiza,
        dentro de `destino` (un candidato de self.candidatos_fila o un
        expediente de self.expedientes), si esta persona alcanzo una zona
        de geometria CRUDA -sin importar cual sea su zona EFECTIVA (la que
        ya tiene aplicado el cupo de --capacidad-caja)- y desde cuando.

        GENERALIZADO para dos usos con la misma logica exacta, que solo
        difieren en que zona cruda miran, que umbral usan, y bajo que
        prefijo de claves guardan el resultado en `destino`:
          - CAJA (prefijo="caja", zona_objetivo="CAJA", umbral=--min-caja):
            si esta persona alcanzo ZONA_CAJA de verdad, sin importar si
            tiene puesto asignado. Ver _desenlace_por_caja.
          - FILA cruda (prefijo="fila_cruda", zona_objetivo="FILA",
            umbral=--min-fila): si esta persona alcanzo ZONA_FILA de
            verdad -en vez de haber llegado directo a CAJA con la zona
            EFECTIVA marcada "FILA" solo por denegacion de cupo, que es un
            caso distinto y no debe contar como haber hecho fila-. Ver
            _tiempos_geometria_cruda.

        DISTINCION CLAVE que motiva ambos usos: "estar en una zona"
        (geometria CRUDA, lo que mide este metodo) y "que efecto tiene eso
        en el negocio" (zona EFECTIVA -con el cupo de CAJA aplicado- o
        estado formal de la maquina) son cosas DISTINTAS desde que existe
        --capacidad-caja. Antes de esa parte coincidian siempre, y tanto
        la desercion como los tiempos se podian leer directo del estado.
        Ahora una persona puede estar fisicamente en CAJA, SIN puesto
        (zona efectiva FILA, ver PARTE 5), durante minutos enteros porque
        otra persona ocupa el unico puesto -y aun asi, para el negocio, SI
        llego a la caja-. La asignacion de cupo es una decision de
        PRESENTACION (quien se muestra como ATENDIENDO en un instante
        dado) y no debe alterar lo que se MIDE.

        Campos que deja en `destino` (con {prefijo} sustituido por "caja"
        o "fila_cruda" segun corresponda):
          {prefijo}_racha_inicio  -> cuando empezo la racha CONTINUA
                                 actual de presencia cruda en la zona
                                 objetivo. None si el ultimo frame no fue
                                 esa zona y la racha todavia no se
                                 confirmo.
          {prefijo}_alcanzada     -> True PARA SIEMPRE en cuanto esa racha
                                 supera `umbral` segundos seguidos. Evita
                                 que un cruce breve o de paso cuente como
                                 haber llegado de verdad a esa zona.
          t_entrada_{prefijo}     -> se fija UNA vez, al confirmarse
                                 {prefijo}_alcanzada: el INICIO de la
                                 racha que se confirmo, no el momento en
                                 que se confirmo. Es la referencia de
                                 "espera" al cerrar (ver
                                 _tiempos_geometria_cruda): asi el tiempo
                                 esperando por falta de cupo, DESPUES de
                                 haber llegado a la caja, no se cuenta
                                 como espera.
          {prefijo}_t_ultima_vez  -> ultima vez CONFIRMADA en la zona
                                 cruda; se sigue actualizando aunque ya
                                 este {prefijo}_alcanzada. Para CAJA, es la
                                 referencia de FIN de "servicio" al
                                 cerrar, para no inflarlo con tiempo
                                 posterior a que la persona ya se fue de
                                 la zona.

        ADITIVO (benchmark offline, runner de tools/Annotador): si se pasa
        `frame_idx`, se guarda el MISMO frame_idx (el numero de frame 0-based
        del video, ver contrato de process_frame) en paralelo a cada marca de
        tiempo de arriba, bajo el mismo nombre de clave con "t_"/"_t_"
        sustituido por "frame_"/"_frame_": frame_{prefijo}_racha_inicio,
        frame_entrada_{prefijo}, {prefijo}_frame_ultima_vez. Esto le permite a
        _cerrar_expediente exponer fila_in/fila_out/caja_in/caja_out como
        NUMERO DE FRAME exacto (capturado en el instante en que ocurre en esta
        maquina de estados), sin tener que reconstruirlo despues a partir de
        los segundos. No cambia ningun calculo de tiempo existente: si
        frame_idx es None (nadie mas que el runner offline lo pasa hoy), este
        bloque completo es un no-op.
        """
        clave_racha = f"{prefijo}_racha_inicio"
        clave_alcanzada = f"{prefijo}_alcanzada"
        clave_entrada = f"t_entrada_{prefijo}"
        clave_ultima = f"{prefijo}_t_ultima_vez"
        clave_racha_frame = f"frame_{prefijo}_racha_inicio"
        clave_entrada_frame = f"frame_entrada_{prefijo}"
        clave_ultima_frame = f"{prefijo}_frame_ultima_vez"

        if zona_cruda_actual == zona_objetivo:
            if destino.get(clave_racha) is None:
                destino[clave_racha] = ahora
                if frame_idx is not None:
                    destino[clave_racha_frame] = frame_idx
            destino[clave_ultima] = ahora
            if frame_idx is not None:
                destino[clave_ultima_frame] = frame_idx
            if (not destino.get(clave_alcanzada, False)
                    and (ahora - destino[clave_racha]) >= umbral):
                destino[clave_alcanzada] = True
                destino[clave_entrada] = destino[clave_racha]
                if frame_idx is not None:
                    destino[clave_entrada_frame] = destino.get(clave_racha_frame)
        else:
            if not destino.get(clave_alcanzada, False):
                # Racha rota antes de confirmarse el umbral: se reinicia
                # por completo, igual que hace el candidato de FILA con
                # --min-fila. Si YA se confirmo, esto no se toca: una vez
                # alcanzada la zona, queda alcanzada para siempre.
                destino[clave_racha] = None
                destino[clave_racha_frame] = None

    @staticmethod
    def _desenlace_por_caja(exp):
        """
        ATENDIDO si esta persona alcanzo la geometria CRUDA de ZONA_CAJA
        por al menos --min-caja segundos (exp["caja_alcanzada"], ver
        _actualizar_racha_zona_cruda), DESERTOR si no. Es DELIBERADAMENTE
        el mismo criterio para las tres formas en que un expediente se
        cierra sin llegar a --max-atencion (transicion explicita al salir
        de CAJA, gracia desde EN_FILA, gracia desde EN_ATENCION-huerfano):
        "estar en la zona de caja" y "tener puesto asignado" son cosas
        distintas desde que existe --capacidad-caja, y es la primera la
        que le importa al negocio -ver _actualizar_racha_zona_cruda para
        la distincion completa-.
        """
        return "ATENDIDO" if exp.get("caja_alcanzada", False) else "DESERTOR"

    @staticmethod
    def _tiempos_geometria_cruda(exp):
        """
        PARTE 6: calcula (espera, servicio, total) para un expediente que
        SI alcanzo CAJA cruda (exp["caja_alcanzada"], ver
        _actualizar_racha_zona_cruda), derivados EXCLUSIVAMENTE de la
        geometria CRUDA de zonas -nunca de la zona EFECTIVA (con el cupo
        de --capacidad-caja ya aplicado) ni del estado formal de la
        maquina (EN_FILA/EN_ATENCION)-, para que la tabla de episodios sea
        verificable a simple vista: espera + servicio = total, SIEMPRE,
        sin excepcion, porque `total` no es mas que esa misma suma:

            espera   = primera entrada CONFIRMADA a CAJA cruda
                       - primera entrada CONFIRMADA a FILA cruda
            servicio = ultima vez CONFIRMADA en CAJA cruda
                       - primera entrada CONFIRMADA a CAJA cruda
            total    = espera + servicio

        Si la persona nunca entro a FILA cruda -fue directa a CAJA, ver
        exp["fila_cruda_alcanzada"]-, espera = 0: no hay ninguna "primera
        entrada a fila" que restar.

        Es DELIBERADO que esto no use t_entrada_fila/t_inicio_atencion/
        t_fin_atencion (las marcas de la zona EFECTIVA/el estado formal
        de la maquina): la asignacion de --capacidad-caja es una decision
        de PRESENTACION (quien se muestra como ATENDIENDO en un momento
        dado) y no debe alterar lo que se MIDE. Se usa para los tres
        desenlaces donde la persona si llego a confirmarse en CAJA:
        ATENDIDO, DESCARTADO (--max-atencion) e INDETERMINADO (si
        alcanzo caja antes de que se detuviera la fuente) -ver
        _cerrar_expediente, que es quien llama a esto y quien decide,
        para cada desenlace, si corresponde llamarlo o dejar los tres
        campos en None ("no aplica", igual que ya se hacia con
        espera/servicio).
        """
        if exp.get("fila_cruda_alcanzada", False):
            espera = exp["t_entrada_caja"] - exp["t_entrada_fila_cruda"]
        else:
            espera = 0.0
        fin_caja = exp.get("caja_t_ultima_vez", exp["t_entrada_caja"])
        servicio = fin_caja - exp["t_entrada_caja"]
        return espera, servicio, espera + servicio

    @staticmethod
    def _total_por_fila_cruda(exp):
        """
        PARTE 6 (correccion): "total" para DESERTOR e
        INDETERMINADO-sin-caja -quienes NUNCA alcanzaron CAJA cruda, ver
        exp["caja_alcanzada"]/_desenlace_por_caja-. espera y servicio
        quedan en None para estos dos desenlaces (nunca llegaron a la
        caja: no hay "espera hasta la caja" ni "servicio" que medir), pero
        total NO: es el tiempo que SI pasaron en FILA cruda, la metrica
        que mide la paciencia del cliente antes de rendirse -desde
        t_entrada_fila_cruda hasta su ultima confirmacion ahi
        (fila_cruda_t_ultima_vez)-.

        FALLBACK: si fila_cruda_alcanzada nunca se confirmo -caso raro
        donde el track alterno entre FILA cruda y CAJA-denegada sin
        asentarse en ninguna el tiempo suficiente para --min-fila/
        --min-caja, aunque la zona EFECTIVA se mantuvo en "FILA" todo el
        tiempo (por eso igual se abrio expediente y luego desertor)-, se
        cae de vuelta a t_entrada_fila/t_ultima_vez -las marcas de la zona
        EFECTIVA, siempre disponibles en cualquier expediente- para no
        dejar el dato vacio sin necesidad. En el caso normal (alguien que
        genuinamente hizo fila) ambos pares de marcas coinciden casi
        exactamente, asi que el fallback no cambia el numero en la
        practica.
        """
        if exp.get("fila_cruda_alcanzada", False):
            fin_fila = exp.get("fila_cruda_t_ultima_vez", exp["t_entrada_fila_cruda"])
            return fin_fila - exp["t_entrada_fila_cruda"]
        return exp["t_ultima_vez"] - exp["t_entrada_fila"]

    def _imprimir_profile(self):
        """
        Imprime UNA linea con el promedio en milisegundos de cada etapa de
        process_frame acumulada en la ventana de --profile (60 frames), y
        reinicia los acumuladores para la siguiente ventana.

        "total" es el tiempo que process_frame paso adentro de si mismo
        (la suma de sus propias etapas, con algo de holgura por el propio
        costo de tomar las marcas de tiempo). "hueco_externo" es aparte:
        el tiempo transcurrido FUERA de process_frame, entre que termino
        una llamada y empezo la siguiente -todo lo que hace el framework
        en el medio (annotate, JPEG, sinks, esperar el proximo frame de la
        fuente) y que este archivo ni mide en detalle ni puede optimizar-.
        Si hueco_externo resulta grande frente a total, el cuello de
        botella esta FUERA de este codigo.

        self._profile_prev_fin NO se reinicia aqui a proposito: tiene que
        sobrevivir al corte de ventana para poder seguir midiendo el hueco
        externo del primer frame de la ventana siguiente contra el ultimo
        frame de esta.
        """
        acc = self._profile_acumulado
        n = self._profile_frames or 1
        n_hueco = self._profile_n_hueco or 1
        print(
            f"[{self.NAME}] PROFILE (promedio sobre {self._profile_frames} "
            f"frames, ms): "
            f"inferencia={acc['inferencia'] / n * 1000:.2f} "
            f"extraccion={acc['extraccion'] / n * 1000:.2f} "
            f"geometria={acc['geometria'] / n * 1000:.2f} "
            f"maquina_estados={acc['maquina_estados'] / n * 1000:.2f} "
            f"etiquetas={acc['etiquetas'] / n * 1000:.2f} "
            f"| total={acc['total'] / n * 1000:.2f} "
            f"| hueco_externo={acc['hueco_externo'] / n_hueco * 1000:.2f} "
            f"(fuera de process_frame, entre llamadas -framework) "
            f"| ultra_preprocess={acc['ultra_preprocess'] / n * 1000:.2f} "
            f"ultra_inference={acc['ultra_inference'] / n * 1000:.2f} "
            f"ultra_postprocess={acc['ultra_postprocess'] / n * 1000:.2f} "
            f"(desglose interno de model.track(), va dentro de inferencia)"
        )
        for clave in acc:
            acc[clave] = 0.0
        self._profile_frames = 0
        self._profile_n_hueco = 0

    def _asegurar_encabezado_md(self):
        """
        Crea self.episodios_md_path con el encabezado de la tabla y una
        linea de fecha/parametros SI el archivo todavia no existe. No hace
        nada si ya existe (para no pisar episodios de una sesion anterior:
        este archivo es acumulativo entre reinicios de la fuente, igual que
        las zonas guardadas en disco).

        El chequeo de existencia se hace DOS veces (antes y despues de
        tomar el lock): la primera vez sin lock es solo para evitar tomarlo
        en el caso normal (archivo ya existe, que sera la inmensa mayoria
        de los arranques); la segunda, ya con el lock tomado, es la que de
        verdad decide si se escribe, para que si dos fuentes de este mismo
        proceso arrancan casi al mismo tiempo con el mismo archivo no
        terminen escribiendo la cabecera dos veces.

        Si --diag-bbox esta activo, el encabezado incluye 3 columnas mas
        (ver _cerrar_expediente/_registrar_episodio_md). OJO: si ya existe
        un archivo de una sesion anterior SIN --diag-bbox y ahora se
        arranca CON la flag activa (o al reves), esta funcion no toca ese
        encabezado viejo (ver docstring de arriba: nunca reescribe un
        archivo existente) y las filas nuevas quedarian con distinta
        cantidad de columnas que las viejas, rompiendo la alineacion de la
        tabla. Al ser un modo de diagnostico temporal, la recomendacion es
        usar un --episodios-md distinto mientras se este midiendo con
        --diag-bbox, no reutilizar el archivo de produccion.
        """
        if os.path.isfile(self.episodios_md_path):
            return
        with self._MD_LOCK:
            if os.path.isfile(self.episodios_md_path):
                return
            # PARTE 3/4: "orden" y "reencolado" van despues de "total" y
            # antes de las columnas opcionales de --diag-bbox.
            columnas = ("| hora | fuente | track ID | desenlace | espera | "
                        "servicio | total | orden | reencolado |")
            separador = "|---|---|---|---|---|---|---|---|---|"
            if self.args.diag_bbox:
                columnas += " alto medio | alto max | alto prom global | muestras prom global |"
                separador += "---|---|---|---|"
            # PARTE 2: estos 6 (min-fila/gracia/histeresis/max-atencion/
            # capacidad-caja/min-caja) ahora son ajustables en caliente
            # (self.min_fila, etc., ver PARAMS/configure()); se leen de
            # ahi (no de self.args) para que, si esta fuente arranco
            # restaurando valores guardados de una sesion anterior
            # (params_web/<source_key>.json, aplicados ANTES de llegar
            # aqui -ver configure()-), el encabezado refleje los
            # parametros con los que de verdad va a arrancar a procesar,
            # no los originales de la flag CLI.
            encabezado = (
                f"# Episodios {self.NAME}\n\n"
                f"Iniciado: {datetime.now():%Y-%m-%d %H:%M:%S} | "
                f"--min-fila={self.min_fila} | --gracia={self.gracia} | "
                f"modelo={self.args.model} | tracker={self.tracker_path} | "
                f"anclaje={self.args.anclaje} | diag-bbox={self.args.diag_bbox} | "
                f"reloj={self._reloj_modo} (--reloj={self.args.reloj}, "
                f"--fps-video={self.args.fps_video}) | "
                f"histeresis={self.histeresis}px | "
                f"directo-caja={not self.args.sin_directo_caja} | "
                f"max-atencion={self.max_atencion}s | "
                f"capacidad-caja={self.capacidad_caja} | "
                f"min-caja={self.min_caja}\n\n"
                f"{columnas}\n"
                f"{separador}\n"
            )
            with open(self.episodios_md_path, "a", encoding="utf-8") as f:
                f.write(encabezado)

    def _registrar_episodio_md(self, episodio):
        """
        Anexa UNA fila al Markdown de verificacion por el episodio ya
        cerrado. Abre en modo append, escribe, cierra -sin mantener el
        archivo abierto entre llamadas ni acumular filas en memoria para
        volcarlas al final- para que un Ctrl+C a mitad de sesion no pierda
        filas que ya deberian estar escritas.

        espera/servicio/total se dejan vacios ("") solo cuando de verdad
        no aplican; se compara con "is not None" y no con un simple if,
        porque 0.0 segundos es un valor valido (espera casi nula) y no
        debe confundirse con "no aplica". PARTE 6: para ATENDIDO,
        DESCARTADO e INDETERMINADO-si-alcanzo-caja, total nunca se calcula
        por separado -es exactamente espera + servicio, ver
        _tiempos_geometria_cruda-, asi que esas filas son verificables a
        simple vista. DESERTOR e INDETERMINADO-sin-caja son la excepcion
        deliberada: espera/servicio quedan vacios (nunca llegaron a la
        caja) pero total SI trae un valor -el tiempo en FILA cruda, ver
        _total_por_fila_cruda-, que no tiene por que sumar con nada
        porque los otros dos estan vacios.

        Si --diag-bbox esta activo, `episodio` trae ademas
        "alto_medio"/"alto_max"/"alto_prom_global" (ver _cerrar_expediente,
        que es quien los calcula) y se anexan como 3 columnas mas, en el
        mismo orden que declara el encabezado (_asegurar_encabezado_md).

        PARTE 4d: "orden" (numero de llegada del expediente, vacio si
        llego directo a caja) y PARTE 3c: "reencolado" (si/no, segun si
        este track_id ya habia sido ATENDIDO antes en la misma sesion).
        """
        espera_txt = self._mmss(episodio["espera"]) if episodio["espera"] is not None else ""
        servicio_txt = self._mmss(episodio["servicio"]) if episodio["servicio"] is not None else ""
        total_txt = f"{episodio['total']:.1f}" if episodio["total"] is not None else ""
        orden_txt = str(episodio["orden"]) if episodio["orden"] is not None else ""
        reencolado_txt = "si" if episodio["reencolado"] else "no"

        fila = (
            f"| {datetime.now():%H:%M:%S} "
            f"| {self.fuente_nombre} "
            f"| {episodio['track_id']} "
            f"| {episodio['desenlace']} "
            f"| {espera_txt} "
            f"| {servicio_txt} "
            f"| {total_txt} "
            f"| {orden_txt} "
            f"| {reencolado_txt} |"
        )
        if self.args.diag_bbox:
            fila += (f" {episodio['alto_medio']:.1f} "
                     f"| {episodio['alto_max']:.1f} "
                     f"| {episodio['alto_prom_global']:.1f} "
                     f"| {episodio['alto_prom_global_n']} |")
        fila += "\n"
        with self._MD_LOCK:
            with open(self.episodios_md_path, "a", encoding="utf-8") as f:
                f.write(fila)

    # ------------------------------------------------------------------ #
    #  Alertas de DESERTOR (dos momentos: preliminar al entrar, definitiva
    #  al confirmarse tras --gracia). Ver _alerta_desertor_preliminar/
    #  _alerta_desertor_definitiva para el detalle de cada una.
    # ------------------------------------------------------------------ #
    def _resolver_alert_dir(self, tipo):
        """
        Carpeta donde se guardan las alertas de `tipo` ("desercion" o
        "aforo") de ESTA fuente: siempre
        resultados/<clave-de-fuente>/alertas/<tipo>/, colgando de
        self._resultados_fuente_dir (calculado UNA vez en setup(), ver
        _resolver_carpeta_key_fuente) -la MISMA carpeta raiz por fuente que
        usa episodios.md, para que nunca haya forma de que dos fuentes
        (o desercion/aforo entre si) terminen escribiendo en el mismo
        sitio.

        SUPUESTO (documentado, no verificado leyendo Base/ por la
        restriccion de esta tarea): el mecanismo automatico del
        framework (process_frame -> {"alert": True, ...}) solo sabe
        guardar UN snapshot del frame COMPLETO tal cual llega, sin
        recorte de bbox, sin poder distinguir "preliminar" de
        "definitiva", ni usar un frame ya viejo guardado a mano -las
        tres cosas que pide esta tarea-. Por eso esta clase escribe sus
        propios archivos DIRECTO a disco en vez de devolver "alert":
        True (que se deja en False, igual que antes).
        """
        carpeta = os.path.join(self._resultados_fuente_dir, "alertas", tipo)
        os.makedirs(carpeta, exist_ok=True)
        return carpeta

    @staticmethod
    def _recorte_seguro(frame, x1, y1, x2, y2):
        """Recorte de bbox con los indices saneados/clampeados al tamano
        real del frame, para que un bbox pegado al borde (o con
        redondeo) nunca produzca un recorte vacio o un IndexError."""
        h, w = frame.shape[:2]
        xi1 = max(0, min(w - 1, int(x1)))
        yi1 = max(0, min(h - 1, int(y1)))
        xi2 = max(xi1 + 1, min(w, int(x2)))
        yi2 = max(yi1 + 1, min(h, int(y2)))
        return frame[yi1:yi2, xi1:xi2]

    @staticmethod
    def _marcar_persona(frame, x1, y1, x2, y2, texto):
        """Copia del frame con un rectangulo rojo (mismo color que
        COLOR_DESERTOR en annotate()) y una etiqueta de texto sobre el
        bbox indicado; NUNCA modifica el frame original -ese lo sigue
        pintando annotate() por su cuenta, esto es solo para el archivo
        de alerta en disco."""
        marcado = frame.copy()
        p1, p2 = (int(x1), int(y1)), (int(x2), int(y2))
        cv2.rectangle(marcado, p1, p2, (0, 0, 255), 2)
        cv2.putText(marcado, texto, (p1[0], max(0, p1[1] - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA)
        return marcado

    def _guardar_alerta_async(self, tareas):
        """
        Ejecuta en un hilo NUEVO (daemon) la lista `tareas` de
        (ruta, funcion(ruta)) -cv2.imwrite o escritura de JSON- para que
        el I/O de disco no bloquee process_frame.

        RIESGO documentado (pedido explicitamente: si no hay mecanismo
        asincrono que reutilizar, documentar el riesgo): no hay cola ni
        limite de hilos concurrentes, cada alerta lanza su propio hilo
        efimero. Es aceptable porque una desercion es un evento poco
        frecuente frente al framerate de video (unos pocos por minuto,
        no por frame); si algun dia hiciera falta soportar cientos de
        alertas por segundo, esto necesitaria una cola con uno o pocos
        hilos trabajadores fijos en vez de un hilo por evento.
        """
        def _trabajo():
            for ruta, fn in tareas:
                try:
                    fn(ruta)
                except Exception as e:
                    print(f"[{self.NAME}] No se pudo escribir alerta "
                          f"{ruta}: {e}")
        threading.Thread(target=_trabajo, daemon=True).start()

    def _alerta_desertor_preliminar(self, track_id, exp, frame, x1, y1, x2, y2):
        """
        MOMENTO 1: se llama UNA sola vez, justo cuando exp["estado_visible"]
        pasa de "ESPERANDO" a "DESERTOR" (ver el bucle 2 de process_frame,
        unico lugar que la invoca). Guarda el frame COMPLETO de este mismo
        instante (con la persona marcada) y el recorte de su bbox, con
        nombres que dejan claro que es un aviso PRELIMINAR y de que track
        ID -la desercion todavia puede revertirse durante --gracia, ver
        _alerta_desertor_definitiva-.
        """
        carpeta = self._resolver_alert_dir("desercion")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        base_nombre = f"{self.fuente_nombre}_id{track_id}_PRELIMINAR_{ts}"
        marcado = self._marcar_persona(frame, x1, y1, x2, y2,
                                        f"ID {track_id} DESERTOR (preliminar)")
        recorte = self._recorte_seguro(frame, x1, y1, x2, y2)
        self._guardar_alerta_async([
            (os.path.join(carpeta, f"{base_nombre}_full.jpg"),
             lambda ruta, img=marcado: cv2.imwrite(ruta, img)),
            (os.path.join(carpeta, f"{base_nombre}_crop.jpg"),
             lambda ruta, img=recorte: cv2.imwrite(ruta, img)),
        ])

    def _alerta_desertor_definitiva(self, track_id, exp, episodio):
        """
        MOMENTO 2: se llama desde _cerrar_expediente, SOLO cuando la
        gracia se agoto y el expediente se cierra de verdad como
        DESERTOR (si la persona vuelve a FILA antes de --gracia, la
        maquina de estados revierte exp["estado_visible"] a "ESPERANDO"
        -ver PARTE 2 en process_frame- y este metodo nunca llega a
        llamarse para esa racha: la alerta definitiva queda cancelada
        sola, sin codigo adicional. El aviso PRELIMINAR de
        _alerta_desertor_preliminar NO se borra en ese caso -se eligio
        no borrarlo por ser lo mas simple; queda en disco como rastro de
        que hubo una alerta que luego se resolvio sola-.

        Usa exp["_ult_frame_fila"]/exp["_ult_bbox_fila"] -la copia del
        ULTIMO frame+bbox en que se confirmo a esta persona en FILA,
        conservada por process_frame cada vez que se confirma esa zona
        (ver los dos puntos donde se siembran esas claves)-, NUNCA el
        frame del instante de cierre: para entonces la persona ya no
        esta en la fila y no habria bbox valido que recortar.

        Genera un .json con los datos pedidos (track ID, hora de entrada
        a fila, hora de la ultima vez visto, tiempo total en fila,
        numero de orden de llegada, fuente) mas el frame completo y el
        recorte de ese ultimo frame guardado.

        SUPUESTO sobre "hora de entrada a fila"/"hora de la ultima vez
        visto": se reportan en el mismo reloj INTERNO que usa todo el
        resto de este archivo (self._reloj_ahora -segundos de pared o
        derivados del video, ver PARTE 1-, no un timestamp de pared
        real), para ser consistentes con t_entrada_fila/t_ultima_vez ya
        archivados en el Markdown de episodios. Se agrega ademas
        "generado_en" con datetime.now().isoformat() -hora de pared real
        del servidor- solo como referencia legible para el operador.
        """
        frame_guardado = exp.get("_ult_frame_fila")
        bbox_guardado = exp.get("_ult_bbox_fila")
        marcado = recorte = None
        if frame_guardado is not None and bbox_guardado is not None:
            fx1, fy1, fx2, fy2 = bbox_guardado
            marcado = self._marcar_persona(frame_guardado, fx1, fy1, fx2, fy2,
                                            f"ID {track_id} DESERTOR")
            recorte = self._recorte_seguro(frame_guardado, fx1, fy1, fx2, fy2)
        else:
            # Defensivo: no deberia pasar para un DESERTOR real (implica
            # haber sido confirmado en FILA al menos una vez), pero si
            # pasara preferimos guardar igual el JSON sin imagen que
            # perder la alerta entera.
            print(f"[{self.NAME}] Alerta DESERTOR definitiva de ID "
                  f"{track_id} sin frame de FILA guardado; se omiten "
                  f"las imagenes.")

        carpeta = self._resolver_alert_dir("desercion")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        base_nombre = f"{self.fuente_nombre}_id{track_id}_DESERTOR_{ts}"
        datos = {
            "track_id": track_id,
            "fuente": self.fuente_nombre,
            "t_entrada_fila": exp["t_entrada_fila"],
            "t_ultima_vez_en_fila": exp.get("_ult_t_fila", exp["t_ultima_vez"]),
            "tiempo_en_fila_segundos": episodio.get("hasta_desercion"),
            "orden_llegada": exp.get("orden"),
            "generado_en": datetime.now().isoformat(),
        }

        def _escribir_json(ruta, d=datos):
            with open(ruta, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)

        tareas = [(os.path.join(carpeta, f"{base_nombre}.json"), _escribir_json)]
        if marcado is not None:
            tareas.append((os.path.join(carpeta, f"{base_nombre}_full.jpg"),
                            lambda ruta, img=marcado: cv2.imwrite(ruta, img)))
            tareas.append((os.path.join(carpeta, f"{base_nombre}_crop.jpg"),
                            lambda ruta, img=recorte: cv2.imwrite(ruta, img)))
        self._guardar_alerta_async(tareas)

    def _alerta_aforo(self, frame, limite, conteo):
        """
        Alerta de AFORO superado (--limite-aforo): MISMO patron que
        _alerta_desertor_definitiva -frame completo escrito a disco mas un
        JSON con los datos del evento, ambos por el mismo mecanismo
        asincrono (_guardar_alerta_async)-, pero SIN track especifico: este
        no es un evento de una persona, es de la fuente entera (cuantas
        personas hay AHORA dentro del poligono de AFORO), asi que no hay
        bbox que recortar ni marcar. Se guarda en su PROPIA carpeta
        resultados/<fuente>/alertas/aforo/ (_resolver_alert_dir("aforo")),
        separada de resultados/<fuente>/alertas/desercion/, para que nunca
        se mezclen ambos tipos de alerta.

        Se llama UNA sola vez por episodio de sobre-aforo, desde
        process_frame, justo cuando personas_en_aforo cruza por encima de
        --limite-aforo (self._aforo_alerta_activa pasa de False a True);
        mientras el conteo siga por encima del limite no se vuelve a
        llamar, y se rearma solo cuando baja del limite (ver ese bloque).
        """
        carpeta = self._resolver_alert_dir("aforo")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        base_nombre = f"{self.fuente_nombre}_AFORO_{ts}"
        datos = {
            "hora": datetime.now().isoformat(),
            "fuente": self.fuente_nombre,
            "limite": limite,
            "conteo_actual": conteo,
        }

        def _escribir_json(ruta, d=datos):
            with open(ruta, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)

        marcado = frame.copy()
        tareas = [
            (os.path.join(carpeta, f"{base_nombre}.json"), _escribir_json),
            (os.path.join(carpeta, f"{base_nombre}_full.jpg"),
             lambda ruta, img=marcado: cv2.imwrite(ruta, img)),
        ]
        self._guardar_alerta_async(tareas)

    def _cerrar_expediente(self, track_id, exp, desenlace, ahora, frame_idx=None):
        """
        Cierra el expediente EN_FILA/EN_ATENCION de `track_id` con el
        desenlace dado ("ATENDIDO", "DESERTOR", "DESCARTADO" -PARTE 4,
        solo desde el barrido de gracia por --max-atencion- o
        "INDETERMINADO" -este ultimo solo desde teardown(), ver ese
        metodo-): calcula los cronometros que aplican, archiva el episodio
        en self.episodios, actualiza los acumuladores de "espera promedio"
        (SOLO para ATENDIDO: ni DESERTOR, ni DESCARTADO, ni INDETERMINADO
        cuentan ahi), escribe la linea de log con track ID/desenlace/
        tiempos, y anexa la fila correspondiente al Markdown de
        verificacion -TODOS los desenlaces se archivan y se vuelcan al
        Markdown, cuenten o no para los promedios, para no perder rastro
        de ningun expediente cerrado-. Se saca de self.expedientes aqui
        mismo para que el resto del codigo nunca vea un expediente ya
        cerrado.

        Si --diag-bbox esta activo, tambien calcula los diagnosticos de
        alto de bbox: alto_medio/alto_max son de ESTA persona puntual, a
        partir de lo que _acumular_altura fue sumando en `exp` durante
        toda la vida del candidato/expediente. alto_prom_global (y su
        conteo de muestras) NO se calculan aqui -se leen tal cual estan
        acumulados en self._diag_suma_altura_global/self._diag_n_altura_global
        en este instante, que el bucle principal de process_frame va
        actualizando en CADA frame para TODO track valido (ver el
        comentario junto a su inicializacion en setup()); esta funcion
        solo los reporta junto con el episodio que cierra.

        PARTE 6: espera/servicio/total (la columna "total" del Markdown,
        antes llamada "en fila total") se derivan de la geometria CRUDA de
        zonas, nunca de la zona EFECTIVA (con el cupo de --capacidad-caja
        ya aplicado) ni de t_entrada_fila/t_inicio_atencion/t_fin_atencion
        (las marcas del estado FORMAL de la maquina). Dos casos:
          - ATENDIDO, DESCARTADO, e INDETERMINADO-si-alcanzo-caja: los
            tres vienen JUNTOS de _tiempos_geometria_cruda, la misma
            funcion y formula para los tres desenlaces -por eso
            espera + servicio = total se cumple siempre que los tres no
            sean None, nunca se calculan por separado-.
          - DESERTOR, e INDETERMINADO-sin-caja (nunca confirmaron CAJA
            cruda): espera/servicio quedan en None -no aplica, nunca
            llegaron a la caja-, pero total NO: es el tiempo que SI
            pasaron en FILA cruda (_total_por_fila_cruda), la metrica de
            paciencia del cliente antes de rendirse/detenerse la fuente.
            Este total NO participa del invariante espera+servicio=total
            de arriba -es un dato aparte, deliberadamente-.

        PARTE 3/4: dos columnas nuevas en el episodio (y en el Markdown,
        ver _registrar_episodio_md). "orden" es el numero de llegada del
        expediente (exp["orden"], PARTE 4a; None si llego directo a caja).
        "reencolado" (PARTE 3) es True si este track_id YA habia cerrado
        un episodio ATENDIDO antes, en la misma sesion -se lee de
        self._cooldown_hasta ANTES de que este cierre (si es ATENDIDO) lo
        actualice mas abajo-, para distinguir en el analisis del Markdown
        los episodios de una segunda (o tercera...) compra legitima.
        """
        # PARTE 3: "reencolado" se calcula ANTES de tocar
        # self._cooldown_hasta mas abajo -refleja si este track_id YA
        # habia sido ATENDIDO en un episodio ANTERIOR de esta misma sesion,
        # no cuenta el episodio que se esta cerrando ahora mismo-.
        episodio = {
            "track_id": track_id,
            "desenlace": desenlace,
            "t_entrada_fila": exp["t_entrada_fila"],
            "t_inicio_atencion": exp["t_inicio_atencion"],
            "t_fin_atencion": exp["t_fin_atencion"],
            "espera": None,
            "servicio": None,
            "total": None,
            "orden": exp.get("orden"),
            "reencolado": track_id in self._cooldown_hasta,
            # ADITIVO (benchmark offline, ver tools/Annotador): mismos
            # eventos de geometria CRUDA que alimentan _tiempos_geometria_
            # cruda/_total_por_fila_cruda, pero como NUMERO DE FRAME (0-based,
            # el mismo frame_idx que recibe process_frame) en vez de segundos
            # -capturado en vivo por _actualizar_racha_zona_cruda en el
            # instante en que ocurre cada evento, nunca reconstruido despues a
            # partir de "ahora"-. None si esa zona nunca se confirmo (misma
            # semantica que t_entrada_fila_cruda/t_entrada_caja ausentes).
            # end_frame es el frame_idx de ESTE cierre de expediente (o el
            # ultimo frame_idx visto, si se cierra desde teardown()/reinicio
            # de fuente, que no reciben un frame_idx nuevo).
            "fila_in": exp.get("frame_entrada_fila_cruda"),
            "fila_out": exp.get("fila_cruda_frame_ultima_vez"),
            "caja_in": exp.get("frame_entrada_caja"),
            "caja_out": exp.get("caja_frame_ultima_vez"),
            "end_frame": frame_idx,
        }

        if desenlace == "ATENDIDO":
            espera, servicio, total = self._tiempos_geometria_cruda(exp)
            episodio["espera"] = espera
            episodio["servicio"] = servicio
            episodio["total"] = total
            self._suma_espera += espera
            self._suma_servicio += servicio
            self._total_atendidos += 1
            # PARTE 3: arranca (o renueva) el enfriamiento de este
            # track_id -mientras no pasen --cooldown-atendido segundos
            # desde AHORA, no puede abrir un expediente nuevo (ver la rama
            # "Sin expediente todavia" del bucle 2 en process_frame)-.
            self._cooldown_hasta[track_id] = ahora
            print(f"[{self.NAME}] Expediente cerrado: ID {track_id} ATENDIDO "
                  f"espera={espera:.1f}s servicio={servicio:.1f}s total={total:.1f}s")
        elif desenlace == "DESERTOR":
            # PARTE 6: DESERTOR ahora significa "nunca alcanzo la
            # geometria cruda de CAJA por --min-caja segundos" (ver
            # _desenlace_por_caja), no "nunca llego a EN_ATENCION" -que ya
            # no es lo mismo desde que existe --capacidad-caja: alguien
            # puede estar fisicamente en CAJA, sin puesto, y no por eso
            # desertar-. espera/servicio quedan en None -no aplica: nunca
            # llego a la caja, no hay "espera hasta la caja" ni "servicio"
            # que medir-.
            #
            # CORRECCION: total NO queda vacio. Es el tiempo que SI paso
            # en FILA cruda -la paciencia real del cliente antes de
            # rendirse-, via _total_por_fila_cruda (desde
            # t_entrada_fila_cruda hasta su ultima confirmacion en FILA
            # cruda). No participa del invariante espera+servicio=total
            # (ese invariante solo aplica a los desenlaces donde los tres
            # campos vienen de la MISMA formula, ver
            # _tiempos_geometria_cruda); aqui espera/servicio estan vacios
            # a proposito y total es un dato aparte.
            #
            # Usamos t_ultima_vez (la ultima vez CONFIRMADA en FILA, ver
            # comentario de self.expedientes en setup()) y no `ahora`, para
            # que hasta_desercion mida cuanto duro realmente en el sistema
            # hasta que se le dejo de ver/paso a FUERA, sin inflar ese
            # tiempo con los --gracia segundos que tuvimos que dejar pasar
            # para confirmar que no iba a volver. hasta_desercion es un
            # campo interno (no se escribe al Markdown, "total" es el que
            # se ve ahi) que en el caso normal coincide con "total".
            hasta_desercion = exp["t_ultima_vez"] - exp["t_entrada_fila"]
            episodio["hasta_desercion"] = hasta_desercion
            episodio["total"] = self._total_por_fila_cruda(exp)
            self._total_desertores += 1
            print(f"[{self.NAME}] Expediente cerrado: ID {track_id} DESERTOR "
                  f"hasta_desercion={hasta_desercion:.1f}s")
            # Alerta DEFINITIVA (MOMENTO 2): la gracia se agoto de verdad,
            # este cierre YA NO se revierte. Se genera aqui, con el
            # episodio ya armado (episodio["hasta_desercion"] disponible
            # justo arriba) y ANTES de que exp se descarte (self.expedientes
            # todavia no se toca en este metodo hasta el "del" de mas abajo,
            # asi que exp["_ult_frame_fila"]/_ult_bbox_fila siguen aqui).
            self._alerta_desertor_definitiva(track_id, exp, episodio)
            self._total_alertas_desercion += 1
        elif desenlace == "DESCARTADO":
            # PARTE 4, red de seguridad --max-atencion: el expediente
            # llevaba EN_ATENCION mas tiempo del limite, casi siempre
            # porque nunca se le vio salir de CAJA (ver el barrido de
            # gracia en process_frame para el motivo completo: personal
            # del establecimiento parado en la zona, la solucion principal
            # es geometrica). Se archiva y se vuelca al Markdown para dejar
            # rastro, pero -a proposito- NO se suma a self._suma_espera ni
            # a self._total_atendidos ni a ningun otro acumulador de
            # promedios: no es una atencion real confirmada, es un
            # descarte de seguridad. PARTE 6: misma formula que ATENDIDO
            # (antes usaba t_inicio_atencion/`ahora`, ya no); "servicio"
            # aqui es cuanto llevaba en CAJA cruda hasta la ultima vez que
            # se confirmo ahi, no necesariamente hasta el instante del
            # descarte.
            #
            # Guarda defensiva con caja_alcanzada: en la practica, para
            # llegar a EN_ATENCION la zona EFECTIVA tuvo que ser "CAJA"
            # (lo que implica zona CRUDA tambien "CAJA"), asi que con los
            # valores por defecto (--max-atencion=300s >> --min-caja=3s)
            # caja_alcanzada siempre esta confirmado para cuando dispara
            # esta rama. Pero si alguien configura --max-atencion MENOR
            # que --min-caja, DESCARTADO podria dispararse antes de
            # confirmarse -sin esta guarda, _tiempos_geometria_cruda
            # reventaria pidiendo t_entrada_caja, que todavia no existe-.
            if exp.get("caja_alcanzada", False):
                espera, servicio, total = self._tiempos_geometria_cruda(exp)
                episodio["espera"] = espera
                episodio["servicio"] = servicio
                episodio["total"] = total
                servicio_txt = f"{servicio:.1f}s"
            else:
                servicio_txt = "N/D (--max-atencion < --min-caja: aun no se confirmaba CAJA)"
            print(f"[{self.NAME}] Expediente cerrado: ID {track_id} "
                  f"DESCARTADO (supero --max-atencion="
                  f"{self.max_atencion}s) servicio_hasta_descarte="
                  f"{servicio_txt}")
        else:
            # INDETERMINADO: se detuvo la fuente con este expediente a
            # medias (ver teardown()). No cuenta como ATENDIDO ni como
            # DESERTOR para los acumuladores del extra -no sabemos de
            # verdad como iba a terminar-, pero se archiva y se vuelca al
            # Markdown igual para no perder el rastro de esa persona.
            # PARTE 6: misma formula que ATENDIDO/DESCARTADO, pero SOLO si
            # de verdad alcanzo CAJA cruda antes del corte -espera/
            # servicio/total vienen juntos de _tiempos_geometria_cruda,
            # respetando el invariante espera+servicio=total-.
            #
            # CORRECCION: si todavia NO alcanzo CAJA cruda (se detuvo la
            # fuente mientras seguia en fila), espera/servicio quedan en
            # None -no aplica, igual que en DESERTOR- pero total NO: es el
            # tiempo que llevaba en FILA cruda hasta ese momento, via
            # _total_por_fila_cruda (mismo tratamiento que DESERTOR,
            # porque este caso es literalmente "la fuente se detuvo justo
            # cuando hubiera desertado" -no sabemos si iba a llegar a la
            # caja o a desertar, pero la paciencia acumulada hasta ahi es
            # el mismo dato en ambos casos).
            if exp.get("caja_alcanzada", False):
                espera, servicio, total = self._tiempos_geometria_cruda(exp)
                episodio["espera"] = espera
                episodio["servicio"] = servicio
                episodio["total"] = total
            else:
                episodio["total"] = self._total_por_fila_cruda(exp)
            # PARTE 2/3: contador aparte (no participa de "espera media" ni
            # de "deserción", que siguen siendo solo sobre ATENDIDO/
            # DESERTOR) para poder mostrarlo en el resumen de
            # estado_cola()/extra sin mezclarlo con esos otros dos.
            self._total_indeterminados += 1
            print(f"[{self.NAME}] Expediente cerrado: ID {track_id} INDETERMINADO "
                  f"(fuente detenida con expediente abierto)")

        if self.args.diag_bbox:
            # altura_n deberia ser siempre >= 1 (se siembra en el mismo
            # frame en que se crea el candidato, ver process_frame), pero
            # el "or 1" evita una division por cero si por lo que sea
            # llegara en 0.
            altura_n = exp.get("altura_n", 0) or 1
            alto_medio = exp.get("altura_suma", 0.0) / altura_n
            episodio["alto_medio"] = alto_medio
            episodio["alto_max"] = exp.get("altura_max", 0.0)

            # Promedio GLOBAL: no se suma nada aqui, se lee tal cual esta
            # acumulado en self._diag_suma_altura_global/
            # self._diag_n_altura_global (ver el bucle principal de
            # process_frame, que los actualiza en CADA frame para TODO
            # track valido, no solo al cerrar episodios). Se reporta junto
            # con n_global -cuantas muestras lo respaldan- para poder
            # juzgar si el promedio es fiable o todavia hay poca muestra.
            n_global = self._diag_n_altura_global
            episodio["alto_prom_global"] = (
                (self._diag_suma_altura_global / n_global) if n_global else 0.0
            )
            episodio["alto_prom_global_n"] = n_global

        # PARTE 3: self.episodios lo puede leer estado_cola() desde OTRO
        # hilo (ver self._estado_lock en setup()); el append en si (mutar
        # el deque) se protege con el mismo lock para que ese otro hilo
        # nunca vea el deque a medio modificar. El resto de este metodo
        # (self.expedientes, self._suma_espera, etc.) NO necesita el lock:
        # solo los toca el hilo propio de esta fuente.
        with self._estado_lock:
            self.episodios.append(episodio)
        self._registrar_episodio_md(episodio)
        del self.expedientes[track_id]

        # PARTE 7c: el expediente se cierra (por CUALQUIER desenlace, no
        # solo por dejar EN_ATENCION de forma "normal"): si retenia un
        # puesto de CAJA, se libera aqui mismo, incondicionalmente. Sin
        # esto, alguien que cerro su expediente (p.ej. por DESCARTADO via
        # --max-atencion) seguiria "reservando" el puesto en
        # self._ocupantes_caja para siempre, bloqueando ese cupo a todo el
        # mundo aunque ya no exista ningun expediente detras de ese ID.
        self._ocupantes_caja.discard(track_id)

    def _construir_snapshot_estado(self, ahora):
        """
        PARTE 2/3 (y PARTE 1/4, ver mas abajo): recorre self.expedientes
        UNA vez (ya resuelto para este frame: se llama despues del bucle 2
        y del barrido de gracia/--max-atencion en process_frame, ver mas
        abajo) y devuelve (personas, resumen):

          personas -> lista de {"id", "estado", "segundos", "orden",
                      "posicion"} por cada expediente ABIERTO ahora mismo
                      (EN_FILA o EN_ATENCION).
          resumen  -> dict con los contadores en vivo (atendiendo/en_fila/
                      desertando -las MISMAS 3 categorias FORMALES de
                      siempre: "atendiendo" incluye tanto ATENDIENDO como
                      la ventana de gracia ATENDIDO, ambas son
                      estado=="EN_ATENCION"- mas los acumulados de
                      episodios cerrados en la sesion (atendidos/
                      desertores/indeterminados/tasa_desercion/
                      espera_media/servicio_medio).

        Quien llama a esto (process_frame) es el UNICO que debe hacerlo:
        publica el resultado en self._cache_personas/self._cache_resumen
        bajo self._estado_lock para que estado_cola() (PARTE 3, puede
        llamarse desde otro hilo) lo lea sin tocar self.expedientes.

        PARTE 1 (estado pegajoso): "estado" y "segundos" YA NO se derivan
        aqui de forma independiente (antes esta funcion usaba su PROPIA
        formula, `t_ultima_vez == ahora`, distinta de la que usaba el
        bucle 2 -`zona`-, dos calculos del "mismo" dato que en teoria
        podian discrepar). Ahora simplemente se LEE exp["estado_visible"],
        el campo unico y pegajoso que la maquina de estados en el bucle 2
        actualiza -ver el comentario extenso ahi-. Esto tambien vuelve
        automatica la REGLA DEL ROJO (un expediente EN_FILA que ya alcanzo
        --min-caja nunca se pinta de rojo, PARTE 6/PARTE 1): esa regla ya
        esta aplicada en el momento en que estado_visible se decidio, no
        hace falta repetirla aqui.

        PARTE 4b/c: "orden" es el numero de llegada del expediente (fijo
        de por vida, ver donde se asigna: creacion del candidato de FILA).
        "posicion" es el puesto de este expediente entre TODOS los que
        estan actualmente EN_FILA (formal), ordenados por "orden"
        ascendente -1 es el primero de la cola-; None para quien esta
        EN_ATENCION (ya no esta "en la cola"). Se calcula en una segunda
        pasada, una vez que ya se sabe quienes estan EN_FILA.
        """
        personas = []
        atendiendo = en_fila = desertando = 0
        en_fila_para_ordenar = []  # [(orden, track_id), ...] solo EN_FILA
        for track_id, exp in self.expedientes.items():
            estado_txt = exp["estado_visible"]
            if (self.atencion_disponible is False
                    and estado_txt in ("ATENDIENDO", "ATENDIDO")):
                estado_txt = "ESPERANDO"   # ver bucle 2 (caja desatendida)
            # tiempo_en_fila/alerta_activa: SOLO tienen valor para DESERTOR
            # (ver docstring de estado_cola() para el porque) -en el resto
            # de los estados quedan en None/False, igual que "posicion"
            # queda en None para quien no esta EN_FILA.
            tiempo_en_fila = None
            alerta_activa = False
            if estado_txt == "ESPERANDO":
                segundos = ahora - exp["t_entrada_fila"]
                en_fila += 1
            elif estado_txt == "DESERTOR":
                segundos = ahora - exp["t_ultima_vez"]
                desertando += 1
                # Tiempo que de verdad paso EN FILA antes de dejar de
                # confirmarse (misma formula que "hasta_desercion" en
                # _cerrar_expediente): se CONGELA en t_ultima_vez, no sigue
                # creciendo con `ahora` mientras corre la cuenta de
                # --gracia -ese tiempo de gracia no es tiempo de fila real.
                tiempo_en_fila = exp["t_ultima_vez"] - exp["t_entrada_fila"]
                alerta_activa = True
            elif estado_txt == "ATENDIENDO":
                segundos = ahora - exp["t_inicio_atencion"]
                atendiendo += 1
            else:  # "ATENDIDO"
                segundos = ahora - exp["t_ultima_vez"]
                atendiendo += 1
            if exp["estado"] == "EN_FILA":
                en_fila_para_ordenar.append((exp["orden"], track_id))
            # FUSION: el remapeo a "POSIBLEMENTE ATENDIENDO" va AQUI, ya
            # calculados segundos y contadores con el vocabulario base —
            # solo cambia el string que ve el panel/API, nada mas.
            if (estado_txt == "ATENDIENDO"
                    and self.atencion_disponible == "posible"):
                estado_txt = "POSIBLEMENTE ATENDIENDO"
            personas.append({
                "id": track_id, "estado": estado_txt, "segundos": segundos,
                "orden": exp.get("orden"), "posicion": None,
                "tiempo_en_fila": tiempo_en_fila, "alerta_activa": alerta_activa,
            })

        # PARTE 4b: se ordena por "orden" ascendente (el numero de llegada
        # mas chico = el primero en sumarse a la cola) y se asigna 1..N.
        en_fila_para_ordenar.sort(key=lambda par: par[0])
        posiciones = {tid: i + 1 for i, (_orden, tid) in enumerate(en_fila_para_ordenar)}
        for p in personas:
            if p["id"] in posiciones:
                p["posicion"] = posiciones[p["id"]]

        # "deserción": porcentaje de DESERTOR sobre el total de episodios
        # cerrados, EXCLUYENDO DESCARTADO e INDETERMINADO del calculo (tal
        # como se pidio) -self._total_atendidos/self._total_desertores ya
        # excluyen a esos dos por construccion, ver _cerrar_expediente-.
        total_cerrados_desercion = self._total_atendidos + self._total_desertores
        tasa_desercion = (
            (self._total_desertores / total_cerrados_desercion * 100.0)
            if total_cerrados_desercion else 0.0
        )
        espera_media = (
            (self._suma_espera / self._total_atendidos) if self._total_atendidos else 0.0
        )
        servicio_medio = (
            (self._suma_servicio / self._total_atendidos) if self._total_atendidos else 0.0
        )
        resumen = {
            "atendiendo": atendiendo,
            "en_fila": en_fila,
            "desertando": desertando,
            "atendidos": self._total_atendidos,
            "desertores": self._total_desertores,
            "indeterminados": self._total_indeterminados,
            "tasa_desercion": tasa_desercion,
            "espera_media": espera_media,
            "servicio_medio": servicio_medio,
            # Alertas de desercion (definitivas) generadas en la sesion,
            # ver _alerta_desertor_definitiva/_total_alertas_desercion.
            "alertas_desercion": self._total_alertas_desercion,
            # AFORO (ver process_frame/--limite-aforo): conteo crudo
            # actual, limite vigente, si la alerta esta activa AHORA
            # MISMO (superado y sin rearmar) y cuantas alertas de aforo
            # se generaron en la sesion (ver _alerta_aforo).
            "aforo_actual": self._aforo_actual,
            "aforo_limite": self.limite_aforo,
            "aforo_alerta_activa": self._aforo_alerta_activa,
            "aforo_alertas_sesion": self._aforo_alertas_sesion,
        }
        return personas, resumen

    def estado_cola(self):
        """
        PARTE 3: metodo PUBLICO (Python directo, NO expone ningun endpoint
        HTTP) para que un panel EXTERNO pinte el estado de la cola sin
        depender de leer el Markdown de episodios ni el campo `extra`.

        Seguro de llamar desde OTRO hilo mientras process_frame sigue
        corriendo en el suyo: no toca self.expedientes/self.candidatos_fila
        directamente (ver _construir_snapshot_estado), solo copia, bajo
        self._estado_lock, el snapshot que process_frame ya dejo listo al
        final de su ultimo frame procesado (self._cache_personas/
        self._cache_resumen) y los ultimos episodios de self.episodios.
        Formatear/ordenar pasa FUERA del lock a proposito, para soltarlo
        cuanto antes y no demorar al hilo que si esta procesando frames.

        Devuelve:
          {
            "personas": [{"id", "estado", "segundos", "pct", "orden",
                          "posicion", "tiempo_en_fila", "alerta_activa"},
                         ...],
            "resumen": {..., "alertas_desercion", "aforo_actual",
                        "aforo_limite", "aforo_alerta_activa",
                        "aforo_alertas_sesion"},
            "ultimos": [{"id", "desenlace", "espera", "servicio"}, ...],
            "parametros": {"min_fila", "min_caja", "gracia",
                           "retener_puesto", "max_atencion", "histeresis",
                           "objetivo_espera", "conf", "capacidad_caja",
                           "confirmar_regreso", "cooldown_atendido"},
          }

        "tiempo_en_fila"/"alerta_activa" (nuevos): SOLO tienen valor
        distinto de None/False para estado=="DESERTOR" -tiempo_en_fila es
        cuanto rato SI paso confirmandose en FILA antes de dejar de
        verse (se congela, no sigue creciendo con la cuenta de gracia);
        alerta_activa es simplemente True para que un panel externo sepa
        que pintar el marco rojo/cronometro sin tener que repetir la
        logica de "que estado cuenta como alerta". Ver
        _alerta_desertor_preliminar/_alerta_desertor_definitiva para las
        alertas que se escriben a disco (ALERTAS_<NAME>/<fuente>/) en los
        dos momentos (al entrar a DESERTOR y al confirmarse tras
        --gracia); "resumen.alertas_desercion" cuenta cuantas de esas
        alertas DEFINITIVAS se generaron en la sesion.

        "personas" solo incluye expedientes ABIERTOS (EN_FILA/EN_ATENCION),
        ordenados por "segundos" descendente. "pct" es "segundos" como
        porcentaje de self.objetivo_espera (puede superar 100; es solo una
        referencia para un panel, no cambia ningun calculo interno).
        "orden" (PARTE 4a) es el numero de llegada del expediente, fijo de
        por vida (None si llego directo a CAJA, nunca hizo fila). "posicion"
        (PARTE 4b) es el puesto en la cola AHORA MISMO entre los EN_FILA,
        1 = el primero (None para quien esta EN_ATENCION -ya no esta en la
        cola-); ver _construir_snapshot_estado para el calculo completo.
        "ultimos" son los 5 episodios cerrados mas recientes, del mas
        reciente al mas viejo. "parametros" (PARTE 2) son los valores
        ACTIVOS ahora mismo de los umbrales ajustables en caliente -para
        que el panel pueda mostrarlos junto al resto del estado, ver
        PARAMS/get_params()/set_params()-.
        """
        # PARTE 2 (requisito c): "parametros" se lee bajo el MISMO lock,
        # junto con el resto del snapshot -son los mismos 9 atributos que
        # process_frame toma como snapshot al inicio de cada frame (ver
        # PARAMS/configure()), asi que reflejan el ultimo valor aplicado
        # por set_params(), este haya llegado por el panel o por la
        # restauracion de params_web/<source_key>.json al arrancar.
        with self._estado_lock:
            personas_snapshot = list(self._cache_personas)
            resumen_snapshot = dict(self._cache_resumen)
            ultimos_snapshot = list(self.episodios)[-5:]
            objetivo = self.objetivo_espera
            parametros_snapshot = {
                "min_fila": self.min_fila,
                "min_caja": self.min_caja,
                "gracia": self.gracia,
                "retener_puesto": self.retener_puesto,
                "max_atencion": self.max_atencion,
                "histeresis": self.histeresis,
                "objetivo_espera": self.objetivo_espera,
                "conf": self.conf,
                "capacidad_caja": self.capacidad_caja,
                "confirmar_regreso": self.confirmar_regreso,
                "cooldown_atendido": self.cooldown_atendido,
            }

        personas = sorted(
            (
                {
                    "id": p["id"],
                    "estado": p["estado"],
                    "segundos": round(p["segundos"], 1),
                    "pct": round(p["segundos"] / objetivo * 100) if objetivo > 0 else 0,
                    "orden": p["orden"],
                    "posicion": p["posicion"],
                    "tiempo_en_fila": (round(p["tiempo_en_fila"], 1)
                                       if p["tiempo_en_fila"] is not None else None),
                    "alerta_activa": p["alerta_activa"],
                }
                for p in personas_snapshot
            ),
            key=lambda p: p["segundos"],
            reverse=True,
        )

        # list(self.episodios)[-5:] deja los 5 mas recientes en orden
        # ASCENDENTE (el mas viejo de los 5 primero); reversed() los deja
        # del mas reciente al mas viejo, mas natural para un panel de
        # "actividad reciente".
        ultimos = [
            {
                "id": ep["track_id"],
                "desenlace": ep["desenlace"],
                "espera": ep["espera"],
                "servicio": ep["servicio"],
            }
            for ep in reversed(ultimos_snapshot)
        ]

        return {
            "personas": personas,
            "resumen": resumen_snapshot,
            "ultimos": ultimos,
            "parametros": parametros_snapshot,
        }

    # ------------------------------------------------------------------ #
    #  2c) Reinicio TECNICO de la fuente (bucle de video/reconexion) -
    #      distinto del reinicio DELIBERADO del operador, ver
    #      reiniciar_jornada() mas abajo
    # ------------------------------------------------------------------ #
    def _manejar_reinicio_fuente(self):
        """
        Se llama SOLO desde process_frame, cuando detecta que frame_idx
        bajo respecto al ultimo visto (ver el comentario junto a esa
        comparacion, un poco mas abajo) -la unica senal de "la fuente se
        reabrio desde cero" disponible aqui dentro, sin que el framework
        (Models/Base, fuera de este archivo) nos avise explicitamente-.
        Esto pasa tanto al terminar un archivo de video en --loop-files
        como al agotar los reintentos de una reconexion RTSP y reabrir el
        stream: en ambos casos el framework arranca un lector nuevo cuyo
        contador de frame vuelve a 0, y ByteTrack -que no sabe nada de
        "reinicios de fuente"- muy probablemente reasigna IDs nuevos a
        partir de ahi (su historial de tracks no sobrevive un corte tan
        largo). Es decir: es exactamente el mismo caso que un track que se
        pierde para siempre (ver el comentario de LIMITE DE LA PERMANENCIA
        junto a la maquina de estados), asi que no hace falta re-identificar
        a nadie: las personas que sigan en escena van a aparecer de nuevo,
        con un ID nuevo, como candidatos/expedientes nuevos.

        Lo que SI hay que resolver a mano es la "memoria" que quedaba
        colgando de la vuelta ANTERIOR:
          - self.expedientes: cualquiera que siguiera EN_FILA/EN_ATENCION
            se cierra aqui como INDETERMINADO -mismo desenlace y mismo
            mecanismo que usa teardown() para una fuente que se detiene de
            verdad (ver ese metodo), con el MISMO reloj que veniamos usando
            (self._reloj_ahora(None), que en modo "video" todavia lee el
            ULTIMO frame_idx bueno -self._ultimo_frame_idx_reloj-, porque
            quien llama a este metodo lo hace ANTES de pisarlo con el
            frame_idx nuevo, ya bajo). Sin este cierre explicito quedarian
            huerfanos para siempre: el barrido de gracia normal compara
            (ahora - t_ultima_vez) contra --gracia, y `ahora` esta a punto
            de saltar hacia ATRAS (reloj de video) -esa resta daria
            NEGATIVA, nunca mayor que --gracia, y esos expedientes jamas
            se cerrarian solos ni soltarian su puesto de --capacidad-caja-.
          - self.candidatos_fila/self._zona_previa/self._ocupantes_caja/
            self.seen_track_ids: memoria de "quien esta AHORA MISMO en
            escena", atada a IDs que no van a volver a aparecer (ver
            arriba). Se limpian aqui mismo; no generan episodio (los
            candidatos en cuarentena de --min-fila nunca lo generaron,
            igual que cuando expiran por --gracia en el barrido normal).

        Lo que a proposito NO se toca (PARTE 2 del pedido: el panel
        ACUMULA, no se reinicia con la fuente) son los acumulados de
        JORNADA: self.episodios (ya se le acaba de sumar las filas
        INDETERMINADO de arriba, que siguen sin contar para la "espera
        media" ni la "tasa de desercion", igual que en teardown()),
        self._total_atendidos/_total_desertores/_total_indeterminados/
        _total_alertas_desercion, self._suma_espera/_suma_servicio,
        self._contador_llegadas (si se reiniciara, el numero de orden de
        alguien que en este instante ya esta en pantalla desde ANTES del
        corte quedaria mas alto que el de un recien llegado tras el corte,
        y la cola se veria en el orden equivocado) y self._cooldown_hasta
        (sus claves son track_id viejos que no van a repetirse; dejarlas
        es inofensivo, purgarlas no aporta nada). Estos acumulados solo se
        ponen a cero con una accion EXPLICITA del operador, ver
        reiniciar_jornada().
        """
        ahora_cierre = self._reloj_ahora(None)
        for track_id, exp in list(self.expedientes.items()):
            self._cerrar_expediente(track_id, exp, "INDETERMINADO", ahora_cierre,
                                     self._ultimo_frame_idx_reloj)
        self.candidatos_fila.clear()
        self._zona_previa.clear()
        self._ocupantes_caja.clear()
        self.seen_track_ids.clear()
        print(f"[{self.NAME}] Reinicio tecnico de fuente detectado "
              f"(frame_idx retrocedio: bucle de video o reconexion). "
              f"Estado en vivo recalculado desde cero; acumulados de "
              f"jornada intactos.")

    def reiniciar_jornada(self):
        """
        PARTE 3: boton "Reiniciar jornada" del panel (ver
        web_server_zones.py, endpoint POST
        /api/sources/<id>/reiniciar-jornada, que llama a este metodo tal
        cual). Accion DELIBERADA del OPERADOR -lo opuesto, a proposito, del
        reinicio TECNICO de _manejar_reinicio_fuente() de arriba, que
        existe justo para que estos mismos acumulados NUNCA se toquen
        solos por un bucle de video o una reconexion-.

        Pone a cero TODOS los acumulados de JORNADA: contadores
        (atendidos/desertores/indeterminados), medias (_suma_espera/
        _suma_servicio, de donde salen "espera media"/"servicio medio" en
        estado_cola()), la lista de ultimos episodios cerrados
        (self.episodios) y el contador de alertas de desercion de la
        sesion (_total_alertas_desercion).

        Deliberadamente NO toca nada del estado EN VIVO -los expedientes
        de quien esta ahora mismo en escena siguen intactos, con sus
        cronometros corriendo como si nada; a alguien que en este instante
        esta EN_FILA o EN_ATENCION no se le corta el expediente ni se le
        reinicia el tiempo de espera solo porque el operador apreto este
        boton-. Tampoco toca self._contador_llegadas (el numero de orden
        de quien sigue en escena depende de que este contador no vuelva a
        empezar en 1, o la cola se veria en un orden incoherente) ni
        self._cooldown_hasta/self.seen_track_ids (bookkeeping de tracks,
        no una metrica de negocio que "la jornada" deba mostrar en cero).

        Mutaciones bajo self._estado_lock: self.episodios y estos
        acumuladores los puede leer estado_cola() en cualquier momento
        desde OTRO hilo (el de Flask, quien expone el endpoint que llama a
        este metodo), a diferencia del resto de self.expedientes/etc. que
        solo toca el hilo propio de esta fuente -ver el mismo candado en
        _cerrar_expediente/setup()-. self._cache_resumen no se refresca
        aqui mismo a proposito: el proximo process_frame (a menos de un
        segundo de distancia en el peor caso normal) ya lo vuelve a
        publicar con estos acumulados en cero, sin necesidad de duplicar
        aqui la construccion del snapshot.
        """
        with self._estado_lock:
            self.episodios.clear()
            self._suma_espera = 0.0
            self._suma_servicio = 0.0
            self._total_atendidos = 0
            self._total_desertores = 0
            self._total_indeterminados = 0
            self._total_alertas_desercion = 0
            self._aforo_alertas_sesion = 0
        print(f"[{self.NAME}] Jornada reiniciada por el operador: "
              f"acumulados de sesion puestos a cero (expedientes en curso "
              f"intactos).")

    # ------------------------------------------------------------------ #
    #  3) Inferencia por frame
    # ------------------------------------------------------------------ #
    def process_frame(self, frame, frame_idx):
        """
        Recibe el frame ya redimensionado por el framework (--resize 800x600)
        como imagen BGR de OpenCV, y el indice del frame.

        Filtramos a la clase 0 del dataset COCO (= "person") directamente en
        la llamada con classes=[0]: es mas barato que detectar todo y descartar
        despues, porque YOLO ya se salta el resto en el post-proceso (NMS).

        half=True usa precision FP16, que en GPU va mas rapido y ocupa menos
        VRAM. En CPU ultralytics lo ignora con un aviso, asi que es seguro
        dejarlo puesto siempre.

        verbose=False evita que ultralytics imprima una linea por frame y
        ensucie el log del servidor.

        Usamos model.track(...) en vez de model.predict(...) para que
        ultralytics mantenga un tracker (ByteTrack, configurado via
        self.tracker_path) entre llamadas y le asigne a cada persona un ID
        estable mientras siga siendo la "misma" segun ese tracker. persist=True
        es imprescindible: le dice a ultralytics que NO reinicie el estado del
        tracker en cada llamada, sino que lo conserve de un frame al siguiente
        (sin esto, cada frame empezaria IDs desde cero y el tracking no
        serviria de nada).

        Si --profile esta activo, este metodo queda instrumentado de punta a
        punta con time.perf_counter() (ver los bloques `if perfil:` mas abajo,
        y _imprimir_profile para el reporte cada 60 frames). Es SOLO medicion:
        ningun `if perfil:` cambia un resultado, solo cronometra tiempo ya
        gastado por el codigo que estaba ahi de antes.
        """
        # --profile: SOLO medicion (ver setup() para donde se guardan los
        # acumuladores y _imprimir_profile para donde se reportan). `perfil`
        # se lee una vez en una variable local para no repetir el
        # getattr/atributo en cada chequeo de mas abajo.
        perfil = self.args.profile
        if perfil:
            t_inicio = time.perf_counter()
            if self._profile_prev_fin is not None:
                # Hueco FUERA de process_frame: todo lo que paso desde que
                # TERMINAMOS la llamada anterior hasta que EMPEZAMOS esta
                # -el framework corriendo annotate/JPEG/sinks/lectura del
                # proximo frame en el medio-. Es el dato mas importante:
                # si este numero es grande frente a "total", el cuello de
                # botella no esta en este archivo.
                self._profile_acumulado["hueco_externo"] += (
                    t_inicio - self._profile_prev_fin
                )
                self._profile_n_hueco += 1
            t0 = time.perf_counter()

        # ------------------------------------------------------------ #
        #  PARTE 2: snapshot de los umbrales ajustables en caliente
        # ------------------------------------------------------------ #
        # Se toman TODOS de una vez, bajo self._estado_lock (el MISMO
        # candado de estado_cola()/PARTE 3, ver requisito (f): "reutiliza
        # el mecanismo de bloqueo... no anadas uno nuevo"), en variables
        # LOCALES que se usan durante TODO este frame en vez de leer
        # self.<atributo> una y otra vez. Dos motivos:
        #   1) Seguridad entre hilos: set_params() (el panel, PARTE 2)
        #      puede escribir estos atributos desde OTRO hilo en cualquier
        #      momento; sin esto, dos lecturas del MISMO atributo dentro
        #      del mismo frame podrian, en teoria, ver valores de dos
        #      escrituras distintas si un set_params() cae justo en medio.
        #   2) Consistencia dentro del frame: si un POST cambia varios
        #      umbrales a la vez (ej. min_fila Y min_caja juntos), este
        #      frame los ve TODOS con el valor viejo o TODOS con el nuevo,
        #      nunca una mezcla -mas fuerte incluso que lo pedido, pero es
        #      lo que sale natural de tomar el snapshot una sola vez-.
        # Los expedientes/candidatos abiertos NO se tocan aqui (requisito
        # (b)): el valor nuevo simplemente se usa a partir de este frame
        # en adelante, sobre el mismo self.expedientes/self.candidatos_fila
        # de siempre.
        with self._estado_lock:
            min_fila = self.min_fila
            min_caja = self.min_caja
            gracia = self.gracia
            retener_puesto = self.retener_puesto
            max_atencion = self.max_atencion
            histeresis = self.histeresis
            conf = self.conf
            capacidad_caja = self.capacidad_caja
            confirmar_regreso = self.confirmar_regreso
            cooldown_atendido = self.cooldown_atendido
            limite_aforo = self.limite_aforo
        # FUSION: la señal de cajero presente se lee UNA vez por frame.
        # None (sin señal, modo standalone) equivale a True.
        atencion_ok = self.atencion_disponible is not False

        # self.model es una instancia YOLO propia de esta fuente (ver
        # setup(): self.model = YOLO(self.args.model) por fuente), asi que
        # no hace falta serializar esta llamada entre fuentes distintas.
        resultados = self.model.track(
            frame,
            classes=[0],              # solo personas (clase 0 de COCO)
            conf=conf,
            imgsz=self.args.imgsz,
            half=True,
            persist=True,              # conserva el estado del tracker entre frames
            tracker=self.tracker_path,
            verbose=False,
        )[0]

        if perfil:
            self._profile_acumulado["inferencia"] += time.perf_counter() - t0
            # resultados.speed viene en milisegundos (preprocess/inference/
            # postprocess) segun lo mide ultralytics adentro de model.track();
            # se acumula aparte, en segundos, como desglose informativo de
            # "inferencia" (que sigue siendo el wall-clock completo de la
            # llamada, medido arriba). No reemplaza a "inferencia".
            speed = resultados.speed
            self._profile_acumulado["ultra_preprocess"] += speed["preprocess"] / 1000.0
            self._profile_acumulado["ultra_inference"] += speed["inference"] / 1000.0
            self._profile_acumulado["ultra_postprocess"] += speed["postprocess"] / 1000.0
            t0 = time.perf_counter()

        # resultados.boxes.id trae un tensor con el track ID de cada caja, EN
        # EL MISMO ORDEN que resultados.boxes. Puede venir None entero (no por
        # caja) cuando el tracker todavia no confirmo ningun track para este
        # frame -tipicamente en los primeros frames tras arrancar la fuente-;
        # en ese caso tratamos a todas las cajas del frame como "sin ID" en
        # vez de dejar que reviente con AttributeError/TypeError mas abajo.
        ids_tensor = resultados.boxes.id
        if ids_tensor is not None:
            track_ids = ids_tensor.int().tolist()
        else:
            track_ids = [None] * len(resultados.boxes)

        if perfil:
            # La conversion de arriba (ids_tensor -> track_ids) cuenta como
            # "extraccion"; dentro del bucle de mas abajo se le suma el
            # resto de la extraccion (x1,y1,x2,y2,conf por caja).
            self._profile_acumulado["extraccion"] += time.perf_counter() - t0
            t0 = time.perf_counter()

        # ------------------------------------------------------------ #
        #  Zonas: preparar los contornos UNA vez por frame (no por persona)
        # ------------------------------------------------------------ #
        # Convencion del proyecto (ver docstring de set_zones):
        #   regions[0] = ZONA_FILA, regions[1] = ZONA_CAJA.
        # Puede haber 0, 1 o 2 zonas cargadas; _preparar_contorno() devuelve
        # None para la que falte, y _punto_en_zona() ya sabe tratar None como
        # "nadie esta dentro", asi que el resto del codigo no necesita ramas
        # if/else por cada combinacion posible.
        # FUSION Queue-Cashier: los contornos se resuelven POR TIPO contra
        # las zonas tipadas (Base/zonas.py), ya no por posicion en la
        # lista. zonas.normalizar garantiza a lo sumo UNA de cada tipo.
        # Referencia local UNA vez por frame (misma disciplina que los
        # umbrales de arriba): set_zonas() puede reasignar self.zonas
        # desde el hilo de Flask a mitad de frame.
        zonas_frame = getattr(self, "zonas", None) or []
        _z_fila = _zonas.por_tipo(zonas_frame, "FILA")
        _z_caja = _zonas.por_tipo(zonas_frame, "CAJA")
        _z_aforo = _zonas.por_tipo(zonas_frame, "AFORO")
        contorno_fila = self._preparar_contorno(_z_fila["puntos"]) if _z_fila else None
        contorno_caja = self._preparar_contorno(_z_caja["puntos"]) if _z_caja else None
        contorno_aforo = self._preparar_contorno(_z_aforo["puntos"]) if _z_aforo else None

        if perfil:
            # Preparar los contornos cuenta como "geometria" (es trabajo
            # previo a la pertenencia a zonas); el resto de la geometria
            # -punto de contacto y pointPolygonTest por persona- se suma
            # dentro del bucle de mas abajo.
            self._profile_acumulado["geometria"] += time.perf_counter() - t0

        # Tiempo para toda la maquina de estados (NO numero de frame): el
        # FPS varia (carga de la GPU, --frame-skip, tamano de la fuente,
        # etc.), asi que "N frames de espera" no es una unidad estable
        # para medir minutos de fila. Se toma UNA vez por frame para que
        # todas las personas de este mismo frame comparen contra el mismo
        # instante.
        #
        # PARTE 2 (panel que acumula, no se reinicia con la fuente):
        # frame_idx SOLO puede bajar respecto al ultimo visto si el
        # framework reabrio la fuente desde cero -video_source.py arranca
        # su contador de frame en 0 cada vez que se lo llama de nuevo,
        # tanto al terminar un archivo y repetirlo en bucle como al
        # agotar los reintentos de un stream RTSP caido y reabrirlo;
        # DENTRO de una misma pasada frame_idx nunca baja, ni con
        # --frame-skip ni con una reconexion corta que se resuelve sin
        # reabrir la fuente-. Es la unica senal de "reinicio tecnico"
        # disponible en este archivo (el framework no nos avisa
        # explicitamente), y hay que actuar ANTES de pisar
        # self._ultimo_frame_idx_reloj con el frame_idx nuevo (ya bajo):
        # ver _manejar_reinicio_fuente para el detalle completo de que
        # cierra/limpia y, sobre todo, que NO toca.
        if frame_idx < self._ultimo_frame_idx_reloj:
            self._manejar_reinicio_fuente()

        # PARTE 1: el reloj concreto (pared o video) lo decide
        # self._reloj_ahora(), ya resuelto en setup() segun --reloj (ver
        # ese metodo para el detalle completo). Guardamos frame_idx para
        # que teardown() -que no recibe un frame_idx nuevo- pueda seguir
        # derivando el reloj de video sin saltar hacia adelante.
        ahora = self._reloj_ahora(frame_idx)
        self._ultimo_frame_idx_reloj = frame_idx

        # El framework espera las cajas como tuplas
        # (x1, y1, x2, y2, label, conf) en coordenadas de pixel del frame que
        # le pasamos; draw_boxes() en base_detector.py las convierte a int y
        # las dibuja con la etiqueta y la confianza encima.
        #
        # PARTE 5 (--capacidad-caja) obliga a partir en DOS lo que antes
        # era un solo bucle: primero hay que saber la zona de TODAS las
        # personas del frame (bucle 1: extraccion + geometria, identico a
        # como funcionaba antes) antes de poder decidir quien de las que
        # cayeron en CAJA ocupa de verdad un puesto de atencion (paso
        # intermedio, ver mas abajo) y recien despues correr la maquina de
        # estados de cada una con su zona ya corregida (bucle 2). No se
        # puede saber quien gana el cupo sin haber visto antes a TODOS los
        # candidatos del frame a la vez.
        boxes = []
        # ADITIVO (benchmark offline, ver tools/Annotador): mismos datos por
        # persona que `boxes`, pero estructurados (track_id/bbox/zona/estado
        # como campos propios) en vez de incrustados en el string de
        # `etiqueta`. Nadie mas los lee hoy (ni annotate() ni el panel), asi
        # que agregar esta clave al dict que devuelve process_frame no
        # cambia ningun comportamiento existente.
        tracks = []
        cajas_info = []
        personas_en_aforo = 0  # AFORO: conteo crudo, ver mas abajo
        for b, track_id in zip(resultados.boxes, track_ids):
            if perfil:
                t0 = time.perf_counter()
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            conf = float(b.conf[0])
            if track_id is not None:
                # Solo contamos como "identidad nueva vista" cuando SI hay
                # ID: una deteccion sin ID (ver comentario de arriba) no debe
                # inflar el contador de personas distintas, porque no sabemos
                # todavia si es alguien nuevo o alguien que ya veniamos
                # siguiendo y que el tracker aun no confirmo.
                self.seen_track_ids.add(track_id)
                id_txt = f"ID {track_id}"
                if self.args.diag_bbox:
                    # Acumulador GLOBAL de --diag-bbox: TODO track valido
                    # de este frame, sin importar zona ni si tiene
                    # candidato/expediente -ver el comentario junto a su
                    # inicializacion en setup() para el porque.
                    self._diag_suma_altura_global += (y2 - y1)
                    self._diag_n_altura_global += 1
            else:
                id_txt = "sin ID"
            if perfil:
                self._profile_acumulado["extraccion"] += time.perf_counter() - t0
                t0 = time.perf_counter()

            # Punto de contacto segun --anclaje (default "pies" = centro
            # del borde inferior, el comportamiento historico). Ver
            # _punto_de_contacto para el detalle de cada modo y el aviso
            # de que cambiar --anclaje invalida las zonas ya dibujadas.
            punto_contacto = self._punto_de_contacto(x1, y1, x2, y2, self.args.anclaje)

            # PARTE 2 (histeresis): le pasamos a _punto_en_zona si el
            # track YA estaba clasificado dentro de CADA zona en el frame
            # anterior (self._zona_previa, memoria de la capa de
            # geometria -ver setup()-), para que el umbral de ENTRAR sea
            # mas estricto que el de seguir/salir. Sin ID (zona_previa_id
            # None) no hay continuidad posible entre frames, asi que
            # siempre evalua como si "no estaba dentro" -umbral estricto-,
            # que es el comportamiento seguro por defecto.
            zona_previa_id = self._zona_previa.get(track_id) if track_id is not None else None

            # AFORO: mismo punto de contacto y misma _punto_en_zona (con el
            # mismo zone_expand) que FILA/CAJA, pero SIN histeresis
            # (histeresis=0.0 fijo, no el snapshot `histeresis` de arriba):
            # el pedido es un conteo crudo por frame, no una clasificacion
            # con memoria -no hay "estaba_dentro" que darle continuidad,
            # asi que ambos umbrales de _punto_en_zona colapsan al mismo
            # valor (-zone_expand), ver ese metodo-. No abre expediente, no
            # consume cupo de --capacidad-caja: es un conteo aparte.
            if self._punto_en_zona(punto_contacto, contorno_aforo, 0.0):
                personas_en_aforo += 1

            # CAJA tiene prioridad sobre FILA si el punto cayera dentro de
            # las dos zonas a la vez (solape accidental al dibujarlas).
            # `histeresis` es el snapshot tomado al inicio de process_frame
            # (PARTE 2), no self.args.histeresis directo -ver _punto_en_zona-.
            if self._punto_en_zona(punto_contacto, contorno_caja, histeresis,
                                    estaba_dentro=(zona_previa_id == "CAJA")):
                zona = "CAJA"
            elif self._punto_en_zona(punto_contacto, contorno_fila, histeresis,
                                      estaba_dentro=(zona_previa_id == "FILA")):
                zona = "FILA"
            else:
                zona = "FUERA"

            if track_id is not None:
                # Actualiza la memoria de histeresis para el PROXIMO frame
                # de este mismo track. Solo tiene sentido con ID: sin el,
                # no hay forma de saber si el "proximo frame" es la misma
                # persona.
                self._zona_previa[track_id] = zona

            if perfil:
                self._profile_acumulado["geometria"] += time.perf_counter() - t0

            cajas_info.append({
                "track_id": track_id, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                "conf": conf, "id_txt": id_txt, "zona": zona,
                "punto_contacto": punto_contacto,
            })

        # ------------------------------------------------------------ #
        #  AFORO: alerta por limite superado (UNA por episodio, ver
        #  --limite-aforo). self._aforo_alerta_activa es el rearme: si ya
        #  esta activa no se re-emite mientras siga superado; se rearma
        #  (vuelve a False) en cuanto el conteo baja del limite, para que
        #  la PROXIMA vez que se supere genere una alerta nueva.
        # ------------------------------------------------------------ #
        self._aforo_actual = personas_en_aforo
        if personas_en_aforo > limite_aforo:
            if not self._aforo_alerta_activa:
                self._aforo_alerta_activa = True
                self._aforo_alertas_sesion += 1
                self._alerta_aforo(frame, limite_aforo, personas_en_aforo)
        else:
            self._aforo_alerta_activa = False

        # ------------------------------------------------------------ #
        #  PARTE 5: --capacidad-caja - CAJA como recurso con cupo
        # ------------------------------------------------------------ #
        # Sin este paso, la cercania FISICA entre dos clientes (el
        # siguiente en la fila pegandose a quien ya esta siendo atendido)
        # se confunde con el ESTADO de "ser atendido": ambos quedarian
        # geometricamente DENTRO de ZONA_CAJA y ambos terminarian
        # EN_ATENCION, aunque una caja solo pueda atender a
        # --capacidad-caja personas a la vez (default 1). Este paso
        # decide, entre TODOS los track_id que cayeron en zona CAJA este
        # frame, cuales de verdad ocupan un puesto y cuales se tratan como
        # si siguieran EN_FILA -mismo track, misma posicion fisica, pero
        # sin puesto: su cronometro de espera sigue corriendo en vez de
        # abrirseles o mantenerseles un expediente de atencion-.
        #
        # Prioridad para ocupar puesto (en este orden):
        #   1. Continuidad: quien YA estaba EN_ATENCION conserva su puesto
        #      pase lo que pase con el resto -nunca se le quita el estado
        #      porque otra persona se acerque; este es justo el problema
        #      que motiva esta parte (espera real de 41s partida en
        #      41s + 15s por perder el expediente a mitad de atencion)-.
        #   2. De los puestos que queden libres, quien lleve MAS tiempo
        #      esperando en fila: t_entrada_fila mas antiguo si ya tiene
        #      expediente EN_FILA, o el inicio de su candidato en
        #      cuarentena de --min-fila si todavia no lo completo -es el
        #      orden natural de una cola.
        #   3. Si empatan, o si ninguno de los que compiten viene de fila
        #      (ej. alguien que aparece directo en CAJA sin historial),
        #      gana quien este mas ADENTRO del poligono de CAJA: mayor
        #      distancia con signo de cv2.pointPolygonTest -la misma
        #      funcion que ya usa _punto_en_zona para decidir pertenencia
        #      a zona; aqui solo LEEMOS ese numero para desempatar, no se
        #      cambia en nada el criterio de pertenencia en si-.
        zona_efectiva = {}
        en_caja = [c for c in cajas_info if c["zona"] == "CAJA" and c["track_id"] is not None]

        if perfil:
            t0 = time.perf_counter()

        # ------------------------------------------------------------ #
        #  PARTE 7: --retener-puesto - el puesto es una RESERVA CON
        #  MEMORIA (self._ocupantes_caja, a nivel de fuente), no una
        #  decision recalculada desde cero cada frame
        # ------------------------------------------------------------ #
        # Antes de esta parte, "quien ya tiene puesto" se recalculaba cada
        # frame mirando solo quien aparecia ESE frame en zona CAJA con
        # estado EN_ATENCION. Si el ocupante quedaba OCLUIDO un solo frame
        # -no detectado, ni siquiera aparece en cajas_info/en_caja ese
        # frame-, la regla de continuidad no podia protegerlo (no estaba
        # en la lista de ESE frame) y el puesto pasaba al siguiente
        # candidato; un frame despues el ocupante reaparecia y HABIA DOS
        # personas EN_ATENCION a la vez con --capacidad-caja=1.
        #
        # self._ocupantes_caja es un conjunto PERSISTENTE (vive en self,
        # no se recalcula desde cero cada frame): el ocupante retiene su
        # puesto mientras su racha en CAJA cruda
        # (exp["caja_t_ultima_vez"], ver _actualizar_racha_zona_cruda) no
        # lleve mas de --retener-puesto segundos sin confirmarse, AUNQUE
        # no aparezca en el frame actual -es justo lo que protege contra
        # la oclusion de un solo frame de arriba-.
        #
        # Primero se PURGAN los puestos vencidos. Corre SIEMPRE, incluso
        # si nadie mas esta en CAJA este frame (en_caja podria estar
        # vacio): el ocupante ocluido no aparece en cajas_info y aun asi
        # hay que decidir, por tiempo transcurrido, si sigue reservado.
        for _tid in list(self._ocupantes_caja):
            _exp_ocupante = self.expedientes.get(_tid)
            if _exp_ocupante is None or _exp_ocupante["estado"] != "EN_ATENCION":
                # Deberia haberse liberado ya en _cerrar_expediente (ver
                # el discard ahi); esto es una red de seguridad por si
                # algun otro camino lo dejo inconsistente.
                self._ocupantes_caja.discard(_tid)
                continue
            _ultima_confirmacion = _exp_ocupante.get(
                "caja_t_ultima_vez", _exp_ocupante.get("t_inicio_atencion", ahora)
            )
            if (ahora - _ultima_confirmacion) > retener_puesto:
                # Vencio la retencion: no se confirmo en CAJA cruda
                # durante mas de --retener-puesto segundos (tipicamente
                # porque su track se perdio/ocluyo). Se libera el puesto
                # para que pueda reasignarse.
                self._ocupantes_caja.discard(_tid)

        if not atencion_ok:
            # FUSION: caja DESATENDIDA — se cierra el cupo por completo. Los
            # puestos retenidos se liberan (cuando el cajero vuelva, la
            # prioridad normal reasigna: quien mas lleva esperando primero)
            # y todo el que este geometricamente en CAJA se trata como
            # EN FILA: su cronometro de espera sigue corriendo y ningun
            # expediente puede abrirse ni sostenerse como EN_ATENCION.
            self._ocupantes_caja.clear()
            for c in en_caja:
                zona_efectiva[c["track_id"]] = "FILA"
        elif en_caja and contorno_caja is not None:
            cupos_libres = max(0, capacidad_caja - len(self._ocupantes_caja))

            def _clave_prioridad_caja(c):
                tid = c["track_id"]
                exp = self.expedientes.get(tid)
                candidato = self.candidatos_fila.get(tid)
                if exp is not None and exp["estado"] == "EN_FILA":
                    tiempo_espera = exp["t_entrada_fila"]
                elif candidato is not None:
                    tiempo_espera = candidato["inicio"]
                else:
                    tiempo_espera = None
                distancia = cv2.pointPolygonTest(contorno_caja, c["punto_contacto"], True)
                # (0, ...) ordena ANTES que (1, ...): quien tiene historial
                # de espera compite primero que quien no tiene ninguno.
                # Empates (o sin historial) se resuelven por -distancia,
                # que ordena de mayor a menor distancia real (mas adentro
                # primero) al ordenar la lista de forma ascendente.
                return (0 if tiempo_espera is not None else 1,
                        tiempo_espera if tiempo_espera is not None else 0.0,
                        -distancia)

            # PARTE 7a: los candidatos a puesto nuevo son quienes estan en
            # CAJA este frame y NO retienen ya uno (self._ocupantes_caja,
            # ya purgado arriba). Los que SI retienen no compiten -ni
            # falta hace, ya tienen su lugar asegurado por continuidad-.
            candidatos_a_puesto = [c for c in en_caja if c["track_id"] not in self._ocupantes_caja]
            candidatos_a_puesto.sort(key=_clave_prioridad_caja)
            # PARTE 7a/7d: se agregan al conjunto PERSISTENTE (no a un set
            # local de "admitidos de este frame"), para que el proximo
            # frame los seguidos ya vengan con puesto retenido. Nunca se
            # admiten mas de cupos_libres, asi que
            # len(self._ocupantes_caja) <= --capacidad-caja se mantiene
            # por construccion (ver tambien la verificacion explicita de
            # mas abajo).
            self._ocupantes_caja.update(
                c["track_id"] for c in candidatos_a_puesto[:cupos_libres]
            )

            for c in en_caja:
                if c["track_id"] not in self._ocupantes_caja:
                    # Sin puesto: se trata como si siguiera EN_FILA -no se
                    # le abre ni se le mantiene expediente de atencion por
                    # estar fisicamente dentro de CAJA sin cupo libre.
                    zona_efectiva[c["track_id"]] = "FILA"

        # PARTE 7d: verificacion EXPLICITA de que nunca haya mas
        # expedientes EN_ATENCION que --capacidad-caja. Corre SIEMPRE (no
        # solo cuando hay gente en CAJA este frame): es un invariante que
        # deberia cumplirse SIEMPRE por construccion (el purgado de arriba
        # nunca deja mas de --capacidad-caja ocupantes, y las admisiones
        # nuevas respetan cupos_libres); si algun dia se viola, es una
        # senal de un bug real en esta seccion o en _cerrar_expediente, y
        # preferimos gritarlo por consola en vez de dejarlo pasar en
        # silencio -que es justo el bug que esta PARTE 7 vino a resolver.
        if len(self._ocupantes_caja) > capacidad_caja:
            print(f"[{self.NAME}] ALERTA: {len(self._ocupantes_caja)} "
                  f"puestos ocupados ({sorted(self._ocupantes_caja)}) > "
                  f"--capacidad-caja={capacidad_caja}. Esto NO "
                  f"deberia pasar; revisar _actualizar_racha_zona_cruda/"
                  f"_cerrar_expediente/esta seccion (PARTE 5/6/7).")

        if perfil:
            # Se suma a "geometria": este paso es una extension de la
            # pertenencia a zona (decide QUIEN de los que estan
            # geometricamente en CAJA cuenta de verdad como tal), no
            # una etapa nueva de las que reporta --profile.
            self._profile_acumulado["geometria"] += time.perf_counter() - t0

        # ---------------------------------------------------------- #
        #  Maquina de estados (solo para cajas CON track ID: sin ID no
        #  hay identidad que seguir de un frame al siguiente, ver (e)
        #  del paso anterior de tracking). Cada persona usa su zona
        #  EFECTIVA (con el cupo de CAJA de la PARTE 5 ya aplicado),
        #  nunca la zona geometrica cruda calculada en el bucle de arriba.
        # ---------------------------------------------------------- #
        for c in cajas_info:
            track_id = c["track_id"]
            x1, y1, x2, y2 = c["x1"], c["y1"], c["x2"], c["y2"]
            conf, id_txt = c["conf"], c["id_txt"]
            zona = zona_efectiva.get(track_id, c["zona"])
            # FUSION: zona CRUDA "de negocio" para las rachas de PARTE 6.
            # Con la caja desatendida, estar geometricamente en CAJA se
            # MIDE como estar en FILA: la racha de servicio (caja) se pausa
            # -el reloj de "servicio" solo corre con cajero presente- y la
            # racha de fila cruda avanza -ese tiempo es ESPERA real y entra
            # al calculo de espera/total de _tiempos_geometria_cruda-.
            # caja_alcanzada ya confirmada no se revoca (quien YA fue
            # atendido, atendido queda); solo deja de acumular servicio.
            zona_negocio = c["zona"]
            if zona_negocio == "CAJA" and not atencion_ok:
                zona_negocio = "FILA"

            if perfil:
                t0 = time.perf_counter()
            timer_txt = None
            estado_txt = None
            if track_id is not None:
                exp = self.expedientes.get(track_id)
                if exp is not None:
                    if self.args.diag_bbox:
                        # Se acumula ANTES de decidir la transicion (y por
                        # tanto antes de un posible cierre mas abajo), para
                        # que el frame actual quede incluido en las
                        # estadisticas de esta persona aunque este mismo
                        # frame sea el que cierra el expediente.
                        self._acumular_altura(exp, y1, y2)
                    # PARTE 6: se actualiza con la zona CRUDA (c["zona"]),
                    # NO con `zona` (la EFECTIVA, con el cupo de CAJA ya
                    # aplicado): "alcanzo la caja"/"alcanzo la fila" tiene
                    # que reflejar la geometria real, sin importar si esta
                    # persona tiene puesto asignado. Ver
                    # _actualizar_racha_zona_cruda. Se actualizan las DOS
                    # rachas (caja y fila cruda) en cada expediente, por si
                    # -caso raro pero posible- vuelve a pisar la ZONA_FILA
                    # real mas adelante en su vida.
                    self._actualizar_racha_zona_cruda(exp, "caja", zona_negocio, "CAJA", min_caja, ahora, frame_idx)
                    self._actualizar_racha_zona_cruda(exp, "fila_cruda", zona_negocio, "FILA", min_fila, ahora, frame_idx)
                    # Ya hay expediente abierto: decidir transicion segun
                    # la zona EFECTIVA de ESTE frame.
                    #
                    # PARTE 1 (el estado es "pegajoso"): a partir de aqui,
                    # exp["estado_visible"] (ESPERANDO/DESERTOR/ATENDIENDO/
                    # ATENDIDO -el mismo vocabulario en vivo de siempre) es
                    # un CAMPO DEL EXPEDIENTE, no un calculo que se rehace
                    # cada vez que hace falta mostrar algo. Este es el
                    # UNICO lugar donde exp["estado_visible"] cambia, y
                    # solo lo hace ante una transicion CONFIRMADA de zona
                    # EFECTIVA o el cierre del expediente (ver
                    # _cerrar_expediente); el bloque de mas abajo (donde
                    # antes se recalculaba de nuevo a partir de `zona`, y
                    # _construir_snapshot_estado, donde se recalculaba por
                    # TERCERA vez con una formula distinta,
                    # `t_ultima_vez == ahora`) ahora solo LEEN este campo.
                    # Antes de esta correccion, tres calculos
                    # independientes del "mismo" dato podian, en teoria,
                    # discrepar entre si, y ninguno sobrevivia una
                    # fluctuacion de deteccion de un solo frame en el
                    # limite del poligono: el color y la etiqueta se
                    # recalculaban de cero cada frame, produciendo
                    # parpadeo. Ahora el estado es una propiedad de la
                    # HISTORIA del expediente -sobrevive a fluctuaciones de
                    # deteccion- y no del frame actual.
                    #
                    # LIMITE DE LA PERMANENCIA (verificacion pedida sobre
                    # TODAS las transiciones, no solo DESERTOR): esta
                    # "memoria" vive en self.expedientes[track_id], asi que
                    # dura EXACTAMENTE lo que dura ese track_id para
                    # ByteTrack -mientras el tracker lo siga considerando la
                    # misma persona (aunque no se detecte durante algunos
                    # frames, ver el "else" de zona=="FUERA" mas abajo, que
                    # a proposito NO toca estado_visible), el estado
                    # ESPERANDO/DESERTOR/ATENDIENDO/ATENDIDO no se mueve solo
                    # por eso-. Si ByteTrack pierde el track de verdad (sale
                    # de cuadro mas tiempo del que retiene su track_buffer)
                    # y esa misma persona vuelve a aparecer, se le asigna un
                    # ID NUEVO: para este codigo es, por construccion, una
                    # persona DISTINTA -un expediente nuevo desde cero, sin
                    # memoria del anterior-. Esto es DELIBERADO y ACEPTADO:
                    # recuperar la identidad a traves de un cambio de ID
                    # requeriria re-identificacion por apariencia (ReID,
                    # comparar embeddings visuales entre tracks), que este
                    # archivo NO implementa por su costo (un modelo extra
                    # corriendo por frame) y que queda fuera del alcance
                    # acotado de esta version (ver el docstring del modulo).
                    if exp["estado"] == "EN_FILA":
                        if zona == "CAJA":
                            if exp["estado_visible"] == "DESERTOR":
                                # PARTE 2 (simetria con la reversion
                                # DESERTOR->ESPERANDO de la rama "FILA" de
                                # abajo): un DESERTOR tampoco se "blanquea"
                                # como ATENDIENDO con un solo roce geometrico
                                # en CAJA (perspectiva, solape en una fila
                                # apretada, o un cupo libre momentaneo de
                                # --capacidad-caja) -exige la MISMA
                                # confirmacion SOSTENIDA de --confirmar-
                                # regreso segundos que exige volver a
                                # ESPERANDO por FILA. Racha INDEPENDIENTE de
                                # regreso_racha_inicio (esa es especifica de
                                # confirmar por FILA): confirmar por CAJA y
                                # confirmar por FILA son intentos de salida
                                # de DESERTOR distintos, no deben compartir
                                # cronometro si el track alterna entre
                                # ambas zonas sin consolidar ninguna. Este
                                # frame la zona es CAJA (no FILA): se rompe
                                # cualquier racha de regreso por FILA en
                                # curso (misma logica ININTERRUMPIDA de
                                # PARTE 2c, ver el "else" de zona=="FUERA"
                                # mas abajo).
                                exp["regreso_racha_inicio"] = None
                                if exp.get("regreso_caja_racha_inicio") is None:
                                    exp["regreso_caja_racha_inicio"] = ahora
                                if (ahora - exp["regreso_caja_racha_inicio"]) >= confirmar_regreso:
                                    # Se CONSOLIDA: recien AHORA pasa de
                                    # verdad a EN_ATENCION (mismo criterio
                                    # que la consolidacion FILA->ESPERANDO:
                                    # t_ultima_vez NO se toca aqui, el
                                    # barrido de gracia de mas abajo sigue
                                    # contando desde el ultimo instante
                                    # confirmado antes del abandono
                                    # original hasta este mismo momento).
                                    exp["estado"] = "EN_ATENCION"
                                    exp["t_inicio_atencion"] = ahora
                                    exp["estado_visible"] = "ATENDIENDO"
                                    exp["regreso_racha_inicio"] = None
                                    exp["regreso_caja_racha_inicio"] = None
                                # Si todavia no se consolido, el expediente
                                # sigue EN_FILA/DESERTOR tal cual estaba: no
                                # se toca "estado" ni "estado_visible", igual
                                # que la rama FILA de abajo mientras su
                                # propia racha de regreso no se consolida.
                            else:
                                # EN_FILA --entra a CAJA--> EN_ATENCION
                                # (transicion CONFIRMADA: ver "b" de la
                                # PARTE 1). Esto NO cambia: un solo frame
                                # sigue bastando cuando no se venia de
                                # DESERTOR -la reversion ASIMETRICA de
                                # arriba es SOLO para ese caso.
                                exp["estado"] = "EN_ATENCION"
                                exp["t_inicio_atencion"] = ahora
                                exp["estado_visible"] = "ATENDIENDO"
                                exp["regreso_racha_inicio"] = None
                        elif zona == "FILA":
                            if exp["estado_visible"] == "DESERTOR":
                                # PARTE 2: reversion ASIMETRICA. Entrar a
                                # DESERTOR basta con un solo frame sin
                                # confirmarse (ver la rama "else" de mas
                                # abajo); SALIR y volver a ESPERANDO exige
                                # --confirmar-regreso segundos SOSTENIDOS de
                                # confirmacion en FILA, no uno solo. Motivo
                                # a documentar: una alerta que parpadea es
                                # peor que una alerta con un segundo de
                                # retardo -el operador no puede confiar en
                                # algo que aparece y desaparece-.
                                # Este frame la zona es FILA (no CAJA): se
                                # rompe cualquier racha de regreso por CAJA
                                # en curso (misma simetria de arriba).
                                exp["regreso_caja_racha_inicio"] = None
                                if exp.get("regreso_racha_inicio") is None:
                                    exp["regreso_racha_inicio"] = ahora
                                if (ahora - exp["regreso_racha_inicio"]) >= confirmar_regreso:
                                    # Se CONSOLIDA el regreso: recien AHORA
                                    # se resetea el reloj de gracia
                                    # (t_ultima_vez) y se vuelve a
                                    # ESPERANDO. Mientras no se consolide,
                                    # t_ultima_vez NO se toca (ver el
                                    # "else" de abajo, y el requisito c de
                                    # la PARTE 2): el reloj de gracia no se
                                    # reinicia por confirmaciones sueltas,
                                    # solo cuando la reversion se consolida.
                                    exp["estado_visible"] = "ESPERANDO"
                                    exp["t_ultima_vez"] = ahora
                                    exp["regreso_racha_inicio"] = None
                                # Si todavia no se consolido, t_ultima_vez
                                # sigue CONGELADO en su ultimo valor
                                # confirmado antes del abandono original:
                                # el barrido de gracia de mas abajo sigue
                                # contando desde ahi, no desde esta
                                # confirmacion suelta.
                            else:
                                # Caso normal (ya estaba ESPERANDO): sigue
                                # confirmadamente en fila, se refresca
                                # t_ultima_vez, que es justo lo que compara
                                # el barrido de gracia de mas abajo para
                                # decidir si hay que declarar DESERTOR.
                                exp["t_ultima_vez"] = ahora
                                # Alertas DESERTOR (momento 2): se conserva
                                # SIEMPRE que se confirma FILA el frame+bbox
                                # de ESTE instante, para poder usarlo como
                                # "ultimo frame visto en fila" si mas
                                # adelante esta persona termina desertando
                                # (ver _alerta_desertor_definitiva). frame.copy()
                                # es una copia en memoria (no I/O a disco),
                                # rapida y acotada -se pisa cada vez, nunca
                                # se acumula mas de una por expediente-.
                                exp["_ult_frame_fila"] = frame.copy()
                                exp["_ult_bbox_fila"] = (x1, y1, x2, y2)
                                exp["_ult_t_fila"] = ahora
                        else:
                            # zona == "FUERA": no tocamos t_ultima_vez a
                            # proposito (salvo, como se explico arriba,
                            # mientras dura una racha de regreso sin
                            # consolidar). Si no vuelve a FILA dentro de
                            # --gracia, el barrido de abajo lo cierra como
                            # DESERTOR (o ATENDIDO si caja_alcanzada, ver
                            # _desenlace_por_caja) usando ese ultimo
                            # instante confirmado.
                            if exp["estado_visible"] == "ESPERANDO":
                                # PARTE 1/2(a): UN SOLO frame sin confirmarse
                                # en FILA basta para ENTRAR a DESERTOR -es
                                # asimetrico a proposito con la salida, ver
                                # arriba-. REGLA DEL ROJO: el rojo senala
                                # ABANDONO -alguien que se va SIN haber
                                # llegado a la caja- y por eso NUNCA puede
                                # aplicarse a quien ya alcanzo la geometria
                                # CRUDA de CAJA (--min-caja,
                                # exp["caja_alcanzada"], ver
                                # _actualizar_racha_zona_cruda). Este caso es
                                # posible aun estando formalmente EN_FILA, si
                                # --capacidad-caja le nego puesto mientras
                                # seguia parado fisicamente en CAJA (PARTE 5).
                                if not exp.get("caja_alcanzada", False):
                                    exp["estado_visible"] = "DESERTOR"
                                    # Alerta DESERTOR, MOMENTO 1 (preliminar):
                                    # se dispara aqui porque esta linea SOLO
                                    # se ejecuta en la transicion
                                    # ESPERANDO->DESERTOR (el "if" de arriba
                                    # ya filtro estado_visible=="ESPERANDO"),
                                    # nunca en frames siguientes donde ya
                                    # seguia en DESERTOR -ver el "else" de
                                    # mas abajo, que es el que corre
                                    # despues-. Usa el frame y el bbox de
                                    # ESTE mismo frame (la persona esta
                                    # geometricamente en zona=="FUERA" pero
                                    # sigue siendo detectada, por eso hay
                                    # bbox valido que marcar).
                                    self._alerta_desertor_preliminar(
                                        track_id, exp, frame, x1, y1, x2, y2)
                                # si caja_alcanzada es True, se queda en
                                # ESPERANDO (no hay un "ATENDIDO" que
                                # mostrar aqui: nunca tuvo t_inicio_atencion,
                                # no hubo servicio formal que confirmar);
                                # el barrido de gracia de mas abajo decidira
                                # su cierre, que sera ATENDIDO via
                                # _desenlace_por_caja, nunca DESERTOR.
                            else:
                                # Ya estaba en DESERTOR: se rompe cualquier
                                # racha de regreso en curso (PARTE 2c: exige
                                # confirmacion SOSTENIDA e ININTERRUMPIDA;
                                # una confirmacion suelta que luego se
                                # corta no cuenta, hay que empezar de cero).
                                # Zona "FUERA" tampoco es FILA ni CAJA: se
                                # rompen AMBAS rachas de regreso en curso.
                                exp["regreso_racha_inicio"] = None
                                exp["regreso_caja_racha_inicio"] = None
                    elif exp["estado"] == "EN_ATENCION":
                        # Simetrico a EN_FILA de arriba (entrar/salir de la
                        # ventana de gracia basta con un solo frame en
                        # ambos sentidos): la reversion ASIMETRICA de la
                        # PARTE 2 se pidio UNICAMENTE para el par
                        # DESERTOR/ESPERANDO, no para este. "Sale de CAJA"
                        # ya NO cierra el expediente de inmediato (esto
                        # viene de una correccion anterior, sin cambios en
                        # esta parte): tiene la misma ventana de --gracia
                        # que EN_FILA (ver el barrido de gracia mas abajo,
                        # que ya comparaba (ahora - t_ultima_vez) >
                        # --gracia como red de seguridad para tracks
                        # perdidos del todo, y ahora tambien es el camino
                        # normal para quien se ve alejarse de CAJA estando
                        # todavia en cuadro). Mientras dura esa ventana se
                        # conserva VERDE (ver annotate(): salir de la caja
                        # es un EXITO, no una alerta) y la etiqueta pasa a
                        # "ATENDIDO" en vez de "ATENDIENDO".
                        if zona == "CAJA":
                            exp["t_ultima_vez"] = ahora
                            exp["estado_visible"] = "ATENDIENDO"
                        else:
                            exp["estado_visible"] = "ATENDIDO"

                    if exp is not None:
                        # PARTE 1(a): el estado mostrado y el cronometro se
                        # LEEN de exp["estado_visible"] -nunca se recalculan
                        # aqui a partir de `zona`-. La maquina de estados de
                        # arriba es el UNICO lugar que decide; este bloque
                        # (y _construir_snapshot_estado) solo formatean.
                        #
                        # OJO de vocabulario: "DESERTOR"/"ATENDIDO" aqui son
                        # etiquetas EN VIVO -no son lo mismo que los
                        # desenlaces ya ARCHIVADOS del mismo nombre en el
                        # Markdown/episodio (ver _desenlace_por_caja/
                        # _cerrar_expediente), que recien se deciden cuando
                        # el expediente CIERRA. Comparten la palabra a
                        # proposito (es el mismo concepto de negocio en dos
                        # momentos distintos), pero nunca se confunden en
                        # el codigo: unas viven solo en `boxes`/annotate(),
                        # las otras solo en self.episodios/el `.md`.
                        estado_txt = exp["estado_visible"]
                        # FUSION (pedido 2026-08-19): con la caja
                        # DESATENDIDA nadie se muestra ATENDIENDO ni
                        # "ATENDIDO" (la etiqueta de gracia): TODOS "En
                        # espera". Presentacion + contadores; el expediente
                        # formal no se toca (si el cajero vuelve dentro de
                        # la gracia, la atencion continua donde iba).
                        if (self.atencion_disponible is False
                                and estado_txt in ("ATENDIENDO", "ATENDIDO")):
                            estado_txt = "ESPERANDO"
                        if estado_txt == "ESPERANDO":
                            tiempo = ahora - exp["t_entrada_fila"]
                        elif estado_txt == "DESERTOR":
                            tiempo = ahora - exp["t_ultima_vez"]
                        elif estado_txt == "ATENDIENDO":
                            tiempo = ahora - exp["t_inicio_atencion"]
                        else:  # "ATENDIDO"
                            tiempo = ahora - exp["t_ultima_vez"]
                        timer_txt = self._mmss(tiempo)
                else:
                    # PARTE 3: enfriamiento tras ser ATENDIDO. Se comprueba
                    # ANTES de mirar zona/candidato: mientras dure, este
                    # track_id se trata exactamente como "sin expediente
                    # posible" (ni candidato de fila ni directo a caja), sin
                    # importar en que zona este -exactamente la misma rama
                    # "else" final (gris, etiqueta "---") que ya cubre a
                    # cualquiera sin expediente-. self._cooldown_hasta
                    # guarda, por track_id, el `ahora` en que se cerro su
                    # ULTIMO episodio ATENDIDO (ver _cerrar_expediente); se
                    # compara contra `cooldown_atendido` -el valor EN VIVO
                    # del snapshot de este frame, no el de cuando se cerro
                    # el episodio- para que un ajuste en caliente de
                    # --cooldown-atendido surta efecto de inmediato incluso
                    # sobre alguien que ya esta a mitad de su enfriamiento.
                    en_cooldown = (
                        track_id in self._cooldown_hasta
                        and (ahora - self._cooldown_hasta[track_id]) < cooldown_atendido
                    )
                    if en_cooldown:
                        # Se descarta cualquier candidato en curso (por si
                        # habia entrado a la cuarentena de --min-fila justo
                        # antes de que el enfriamiento se activara) y no se
                        # abre nada nuevo: motivo -alguien recien atendido
                        # que se queda rondando cerca de la fila NO debe
                        # generar un episodio falso con una espera que no
                        # existio-.
                        self.candidatos_fila.pop(track_id, None)
                    # Sin expediente todavia (y sin cooldown activo): solo
                    # nos interesa si esta candidateando a FILA
                    # (--min-fila) o si hay que descartar un candidato que
                    # se salio antes de tiempo.
                    elif zona == "FILA":
                        candidato = self.candidatos_fila.get(track_id)
                        if candidato is None:
                            # PARTE 4a: numero de llegada incremental,
                            # asignado al entrar a FILA cruda por primera
                            # vez -este es exactamente ese momento: la
                            # PRIMERA vez que este track_id aparece como
                            # candidato nuevo-. Si este candidato se
                            # descarta mas tarde sin llegar a --min-fila y
                            # el mismo track_id vuelve a entrar a FILA
                            # despues, se le asigna un numero NUEVO (mas
                            # alto): es, de verdad, un intento distinto y
                            # mas tardio de sumarse a la cola.
                            self._contador_llegadas += 1
                            self.candidatos_fila[track_id] = {
                                "inicio": ahora, "ultima_vez": ahora,
                                "orden": self._contador_llegadas,
                            }
                            if self.args.diag_bbox:
                                self._acumular_altura(self.candidatos_fila[track_id], y1, y2)
                            # PARTE 6: con zona CRUDA (c["zona"]), no con
                            # `zona` (aqui coinciden -esta rama solo se
                            # entra cuando la efectiva YA es "FILA"-, pero
                            # si la efectiva es "FILA" por denegacion de
                            # cupo, c["zona"] puede ser "CAJA": es
                            # justamente el caso que --min-caja/--min-fila
                            # necesitan ver para acumular sus rachas de
                            # geometria cruda). Se actualizan las DOS
                            # rachas: puede que este candidato en realidad
                            # sea alguien que llego directo a CAJA (racha
                            # de fila cruda nunca se confirma, espera=0
                            # al cerrar, ver _tiempos_geometria_cruda) o
                            # alguien genuinamente en ZONA_FILA (ambas
                            # rachas avanzan igual, sin diferencia).
                            self._actualizar_racha_zona_cruda(self.candidatos_fila[track_id], "caja", zona_negocio, "CAJA", min_caja, ahora, frame_idx)
                            self._actualizar_racha_zona_cruda(self.candidatos_fila[track_id], "fila_cruda", zona_negocio, "FILA", min_fila, ahora, frame_idx)
                        else:
                            candidato["ultima_vez"] = ahora
                            if self.args.diag_bbox:
                                self._acumular_altura(candidato, y1, y2)
                            self._actualizar_racha_zona_cruda(candidato, "caja", zona_negocio, "CAJA", min_caja, ahora, frame_idx)
                            self._actualizar_racha_zona_cruda(candidato, "fila_cruda", zona_negocio, "FILA", min_fila, ahora, frame_idx)
                            if ahora - candidato["inicio"] >= min_fila:
                                # Supero la permanencia minima: se abre
                                # expediente formal, con t_entrada_fila
                                # igual al instante en que EMPEZO a estar
                                # en fila (no al instante en que se
                                # confirmo el umbral), para no recortarle
                                # tiempo de espera ya vivido.
                                self.expedientes[track_id] = {
                                    "estado": "EN_FILA",
                                    "t_entrada_fila": candidato["inicio"],
                                    "t_inicio_atencion": None,
                                    "t_fin_atencion": None,
                                    "t_ultima_vez": ahora,
                                    # PARTE 1: arranca ESPERANDO (el estado
                                    # visible es del expediente, no de la
                                    # zona del frame -ver la maquina de
                                    # estados mas abajo, unico lugar donde
                                    # este campo cambia despues-).
                                    "estado_visible": "ESPERANDO",
                                    # PARTE 2: sin racha de regreso en curso
                                    # todavia (nunca estuvo en DESERTOR).
                                    "regreso_racha_inicio": None,
                                    # PARTE 4a: se traspasa el numero de
                                    # llegada asignado al candidato cuando
                                    # se creo (ver mas abajo); NO cambia
                                    # durante el resto de la vida de este
                                    # expediente.
                                    "orden": candidato.get("orden"),
                                }
                                if self.args.diag_bbox:
                                    # Se traspasan las estadisticas ya
                                    # acumuladas por el candidato (incluido
                                    # este mismo frame) al expediente
                                    # nuevo, para que "mientras estuvo
                                    # trackeada" cubra desde que entro a
                                    # FILA por primera vez, no solo desde
                                    # que se abrio el expediente formal.
                                    self.expedientes[track_id]["altura_suma"] = candidato.get("altura_suma", 0.0)
                                    self.expedientes[track_id]["altura_max"] = candidato.get("altura_max", 0.0)
                                    self.expedientes[track_id]["altura_n"] = candidato.get("altura_n", 0)
                                # PARTE 6: se traspasan tambien las rachas
                                # de geometria CRUDA ya acumuladas por el
                                # candidato -pudo haber tocado CAJA sin
                                # puesto, o ya venia confirmando FILA cruda
                                # de verdad, incluso antes de completar
                                # --min-fila-, para no perder ese progreso
                                # al abrir el expediente formal.
                                for _campo_caja in ("caja_racha_inicio", "caja_alcanzada",
                                                     "t_entrada_caja", "caja_t_ultima_vez",
                                                     "fila_cruda_racha_inicio", "fila_cruda_alcanzada",
                                                     "t_entrada_fila_cruda", "fila_cruda_t_ultima_vez",
                                                     # ADITIVO (benchmark offline): espejo en frame_idx de
                                                     # las 4 marcas de arriba (ver _actualizar_racha_zona_cruda).
                                                     "frame_caja_racha_inicio", "frame_entrada_caja",
                                                     "caja_frame_ultima_vez",
                                                     "frame_fila_cruda_racha_inicio", "frame_entrada_fila_cruda",
                                                     "fila_cruda_frame_ultima_vez"):
                                    if _campo_caja in candidato:
                                        self.expedientes[track_id][_campo_caja] = candidato[_campo_caja]
                                del self.candidatos_fila[track_id]
                                # Alertas DESERTOR (momento 2): se siembra
                                # ya desde la apertura del expediente -este
                                # frame confirma FILA por primera vez como
                                # expediente formal-, para que exista un
                                # "ultimo frame visto en fila" incluso si
                                # este mismisimo track jamas vuelve a pasar
                                # por la rama de refresco normal antes de
                                # desertar (ver el otro punto donde se
                                # siembran estas mismas claves).
                                self.expedientes[track_id]["_ult_frame_fila"] = frame.copy()
                                self.expedientes[track_id]["_ult_bbox_fila"] = (x1, y1, x2, y2)
                                self.expedientes[track_id]["_ult_t_fila"] = ahora
                                # Recien se abrio el expediente EN_FILA en
                                # ESTE frame: ya corresponde mostrar
                                # ESPERANDO (antes se llamaba "FILA"; ver
                                # vocabulario unificado mas abajo en la
                                # construccion de `etiqueta`).
                                estado_txt = "ESPERANDO"
                                timer_txt = self._mmss(ahora - candidato["inicio"])
                        # NOTA: mientras el candidato NO supera --min-fila
                        # (todavia en cuarentena, sin expediente formal) no
                        # se fija estado_txt/timer_txt aqui a proposito: la
                        # etiqueta debe mostrar "---" durante la cuarentena
                        # (ver el vocabulario unificado mas abajo), aunque
                        # el candidato siga acumulando su tiempo hacia
                        # --min-fila con total normalidad -esto NO cambia
                        # la logica de --min-fila, solo deja de mostrarse.
                    elif zona == "CAJA" and not self.args.sin_directo_caja:
                        # PARTE 3: llega a CAJA sin expediente EN_FILA
                        # previo -ni siquiera paso por la cuarentena de
                        # --min-fila, o estaba en cuarentena sin
                        # completarla (el "pop" de mas abajo, en el caso
                        # contrario, ya descartaba ese candidato)-. Es el
                        # cliente que llega cuando no hay fila: antes de
                        # esta parte, este caso no generaba expediente, no
                        # aparecia en el Markdown y su etiqueta quedaba en
                        # "---" -y ademas sesgaba el promedio de espera al
                        # alza, porque solo se registraban los clientes que
                        # SI esperaron.
                        #
                        # Abrimos el expediente YA en EN_ATENCION, con
                        # t_entrada_fila = t_inicio_atencion = ahora (estos
                        # dos campos ya casi no se usan para calcular
                        # tiempos -ver PARTE 6/_tiempos_geometria_cruda-,
                        # se mantienen para la maquina de estados/DESERTOR).
                        # La "espera" real de este cliente se calcula desde
                        # t_entrada_fila_cruda, que para este caso NUNCA se
                        # confirma (llego directo a CAJA, jamas piso
                        # ZONA_FILA de verdad -ver el requisito "si nunca
                        # entro a FILA cruda, espera = 0" en
                        # _tiempos_geometria_cruda-): la racha de fila
                        # cruda que arrancamos mas abajo con zona_cruda=
                        # "CAJA" nunca coincide con su zona_objetivo=
                        # "FILA", asi que fila_cruda_alcanzada se queda en
                        # False para siempre en este expediente, tal como
                        # corresponde.
                        #
                        # NO se aplica --min-fila aqui: ese umbral filtra a
                        # quien solo CRUZA la zona de fila sin hacer fila
                        # de verdad; aca no hay ninguna fila que cruzar, el
                        # cliente fue directo al mostrador.
                        self.candidatos_fila.pop(track_id, None)  # por si habia uno en curso
                        self.expedientes[track_id] = {
                            "estado": "EN_ATENCION",
                            "t_entrada_fila": ahora,
                            "t_inicio_atencion": ahora,
                            "t_fin_atencion": None,
                            "t_ultima_vez": ahora,
                            # PARTE 1: arranca ATENDIENDO directamente (nunca
                            # paso por ESPERANDO/DESERTOR).
                            "estado_visible": "ATENDIENDO",
                            "regreso_racha_inicio": None,
                            # PARTE 4a: SIN numero de llegada -nunca entro a
                            # FILA cruda, el numero se asigna especificamente
                            # a quien de verdad hizo fila (ver el
                            # comentario junto a self._contador_llegadas).
                            "orden": None,
                        }
                        if self.args.diag_bbox:
                            self._acumular_altura(self.expedientes[track_id], y1, y2)
                        # PARTE 6: arranca la racha de caja cruda de este
                        # expediente recien creado (c["zona"] es "CAJA"
                        # aqui: la efectiva solo llega a serlo si la cruda
                        # tambien lo es, ver el paso de capacidad). Tambien
                        # se llama la de fila cruda -sera un no-op que
                        # nunca confirma, ver arriba-, por consistencia con
                        # los demas puntos donde se actualizan ambas.
                        self._actualizar_racha_zona_cruda(self.expedientes[track_id], "caja", zona_negocio, "CAJA", min_caja, ahora, frame_idx)
                        self._actualizar_racha_zona_cruda(self.expedientes[track_id], "fila_cruda", zona_negocio, "FILA", min_fila, ahora, frame_idx)
                        estado_txt = "ATENDIENDO"
                        timer_txt = self._mmss(0)
                    else:
                        # zona == "FUERA", o zona == "CAJA" con
                        # --sin-directo-caja (comportamiento anterior a la
                        # PARTE 3, para poder comparar): se descarta el
                        # candidato si habia uno en curso. No se abre
                        # expediente -no genera episodio, y la etiqueta de
                        # esa persona muestra "---" (ver mas abajo)-.
                        self.candidatos_fila.pop(track_id, None)

            if perfil:
                self._profile_acumulado["maquina_estados"] += time.perf_counter() - t0
                t0 = time.perf_counter()

            # Vocabulario UNIFICADO de la etiqueta: solo estado con
            # historia, nunca la posicion cruda en el frame. Con
            # expediente abierto (EN_FILA/EN_ATENCION) se muestra
            # ESPERANDO/ATENDIENDO + cronometro; sin expediente abierto
            # -este de "sin ID", o con ID pero FUERA/CAJA sin haber hecho
            # fila, o todavia en cuarentena de --min-fila- se muestra "---"
            # sin cronometro. La clasificacion interna `zona`
            # (FILA/CAJA/FUERA) sigue calculandose exactamente igual mas
            # arriba y sigue siendo la que alimenta toda la maquina de
            # estados; lo unico que cambio aqui es que la etiqueta ya no
            # la expone, para no mezclar los dos vocabularios.
            # FUSION: atencion por alguien SIN VERIFICAR en la zona
            # CAJERO -> el "ATENDIENDO" en vivo baja a "POSIBLEMENTE
            # ATENDIENDO". Solo presentacion (por frame, sin memoria):
            # cupo, rachas y desenlaces archivados no cambian.
            if (estado_txt == "ATENDIENDO"
                    and self.atencion_disponible == "posible"):
                estado_txt = "POSIBLEMENTE ATENDIENDO"
            if estado_txt is not None:
                etiqueta = f"{id_txt} {estado_txt} {timer_txt}"
            else:
                etiqueta = f"{id_txt} ---"
            boxes.append((x1, y1, x2, y2, etiqueta, conf))
            tracks.append({
                "track_id": track_id,
                "bbox": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                "conf": round(conf, 4),
                "zona": zona,
                "zona_cruda": c["zona"],
                "estado": estado_txt,
            })
            if perfil:
                self._profile_acumulado["etiquetas"] += time.perf_counter() - t0

        if perfil:
            t0 = time.perf_counter()

        # ------------------------------------------------------------ #
        #  Barrido de gracia: cierra los expedientes cuya ultima vez
        #  confirmada en su zona correspondiente ya supero --gracia,
        #  aplica la red de seguridad --max-atencion (PARTE 4), y poda
        #  candidatos abandonados antes de llegar a --min-fila.
        #
        #  PARTE 6: el desenlace de un cierre por gracia (EN_FILA o
        #  EN_ATENCION huerfano) YA NO se asume por el estado en que
        #  estaba el expediente -eso dejo de ser correcto en cuanto
        #  existe --capacidad-caja, porque alguien EN_FILA puede llevar
        #  rato fisicamente en CAJA sin puesto asignado-. Se decide con
        #  _desenlace_por_caja: ATENDIDO si de verdad alcanzo la geometria
        #  cruda de CAJA por --min-caja segundos, DESERTOR si no,
        #  independientemente de si el estado formal llego a EN_ATENCION.
        # ------------------------------------------------------------ #
        # Corre una vez por frame sobre TODOS los expedientes/candidatos
        # abiertos (no solo los detectados en este frame): es la unica
        # forma de detectar a alguien que "deja de verse" del todo, porque
        # ese track ID simplemente no vuelve a aparecer en resultados.boxes.
        # El costo es proporcional a personas EN CURSO (nunca a los frames
        # ya procesados), asi que se mantiene barato indefinidamente.
        for track_id, exp in list(self.expedientes.items()):
            if exp["estado"] == "EN_FILA" and (ahora - exp["t_ultima_vez"]) > gracia:
                self._cerrar_expediente(track_id, exp, self._desenlace_por_caja(exp), ahora, frame_idx)
            elif exp["estado"] == "EN_ATENCION":
                # PARTE 4, red de seguridad --max-atencion: se comprueba
                # PRIMERO y con prioridad sobre el barrido de gracia de
                # abajo, porque debe cerrar el expediente aunque el track
                # siga confirmandose en CAJA todo el tiempo (--gracia solo
                # actua cuando DEJA de confirmarse; --max-atencion actua
                # aunque nunca deje de confirmarse). Motivo: es una red de
                # seguridad contra expedientes que quedan abiertos
                # indefinidamente -por ejemplo, personal del
                # establecimiento parado dentro de ZONA_CAJA durante horas-.
                # La solucion PRINCIPAL a ese problema es geometrica
                # (trazar ZONA_CAJA cubriendo solo el lado del cliente,
                # no el del mostrador donde se para el personal); esta
                # flag es solo el ultimo resguardo. max_atencion
                # <= 0 desactiva esta red de seguridad.
                if (max_atencion > 0
                        and (ahora - exp["t_inicio_atencion"]) > max_atencion):
                    exp["t_fin_atencion"] = ahora
                    self._cerrar_expediente(track_id, exp, "DESCARTADO", ahora, frame_idx)
                elif (ahora - exp["t_ultima_vez"]) > gracia:
                    # Un expediente EN_ATENCION cuyo track dejo de confirmarse
                    # en CAJA (se perdio en el tracker, salio de cuadro sin que
                    # se viera el frame de "sale de caja") quedaria abierto
                    # para siempre sin este barrido: la unica otra forma de
                    # cerrarlo es la transicion explicita de mas arriba
                    # (zona != CAJA), que nunca llega a dispararse si el track
                    # simplemente desaparece. PARTE 6: el desenlace se decide
                    # con _desenlace_por_caja (casi siempre ATENDIDO en este
                    # caso, porque llegar a EN_ATENCION ya implica haber
                    # estado en CAJA cruda; en la practica solo daria
                    # DESERTOR si tuvo puesto por menos de --min-caja
                    # segundos antes de perderse, un caso raro).
                    #
                    # t_fin_atencion se fija en t_ultima_vez -la ultima vez
                    # CONFIRMADA en CAJA- y no en `ahora`, para que "servicio"
                    # (ver _cerrar_expediente) no se infle con los --gracia
                    # segundos que esperamos antes de dar por cerrado el
                    # expediente.
                    exp["t_fin_atencion"] = exp["t_ultima_vez"]
                    self._cerrar_expediente(track_id, exp, self._desenlace_por_caja(exp), ahora, frame_idx)

        for track_id, candidato in list(self.candidatos_fila.items()):
            if (ahora - candidato["ultima_vez"]) > gracia:
                # Nunca llego a abrir expediente (no llego a --min-fila) y
                # ya no se le ve en fila: se descarta en silencio, sin
                # episodio ni log -es justamente lo que --min-fila esta
                # disenado para filtrar-.
                del self.candidatos_fila[track_id]

        # PARTE 2/3: self.expedientes ya quedo completamente resuelto para
        # este frame (bucle 2 + barrido de gracia/--max-atencion + poda de
        # candidatos, todo arriba); es el momento exacto de construir el
        # snapshot "personas AHORA en cada estado" (ver
        # _construir_snapshot_estado para el criterio completo) y
        # publicarlo bajo self._estado_lock para que estado_cola() (PARTE
        # 3) lo pueda leer desde otro hilo. Se reutiliza `resumen_actual`
        # tal cual mas abajo para armar el "extra" (PARTE 2), asi ambas
        # salidas -la barra inferior y estado_cola()- muestran siempre los
        # mismos numeros, nunca calculados dos veces por separado.
        personas_actuales, resumen_actual = self._construir_snapshot_estado(ahora)
        with self._estado_lock:
            self._cache_personas = personas_actuales
            self._cache_resumen = resumen_actual

        if perfil:
            # Se suma a "maquina_estados" (no es una etapa aparte): el
            # enunciado la reporta junto con las transiciones por caja de
            # mas arriba, como "maquina de estados y barrido de gracia".
            self._profile_acumulado["maquina_estados"] += time.perf_counter() - t0
            t0 = time.perf_counter()

        resultado = {
            # alert siempre False: esta version solo observa, no dispara nada.
            # (si fuera True, el framework guardaria un snapshot en
            #  ALERTAS_QUEUE_ANALYTICS/ y sumaria al contador del panel)
            "alert": False,
            "label": "",
            "score": None,
            "boxes": boxes,
            "tracks": tracks,
            # "extra" se concatena a la barra inferior, detras de FPS/frame/
            # fuente. PARTE 2: reemplaza por completo el campo anterior
            # (que mostraba fila_count/caja_count, el conteo CRUDO por
            # frame de la clasificacion de ZONA). Los tres primeros numeros
            # -atendiendo/en fila/desertando- son ahora sobre EXPEDIENTES
            # (personas con historia, PARTE 1), no sobre zona cruda: un
            # candidato todavia en cuarentena de --min-fila no cuenta aqui
            # (nunca abrio expediente), y "desertando" es exactamente quien
            # se muestra con la etiqueta/caja roja nueva de la PARTE 1. El
            # resto son acumulados de episodios ya CERRADOS en la sesion.
            "extra": f"atendiendo: {resumen_actual['atendiendo']} | "
                     f"en fila: {resumen_actual['en_fila']} | "
                     f"desertando: {resumen_actual['desertando']} | "
                     f"atendidos: {resumen_actual['atendidos']} | "
                     f"desertores: {resumen_actual['desertores']} | "
                     f"deserción: {resumen_actual['tasa_desercion']:.1f}% | "
                     f"espera media: {self._mmss(resumen_actual['espera_media'])}",
        }

        if perfil:
            # "etiquetas" tambien incluye esta construccion del dict de
            # retorno, no solo la etiqueta por caja de mas arriba -es la
            # misma etapa que pide el enunciado: "construccion de
            # etiquetas y del dict de retorno".
            self._profile_acumulado["etiquetas"] += time.perf_counter() - t0
            self._profile_acumulado["total"] += time.perf_counter() - t_inicio
            self._profile_frames += 1
            if self._profile_frames >= 60:
                self._imprimir_profile()
            # Marca de FIN de este process_frame: se toma como ULTIMA
            # accion antes de salir (mas cerca posible del return real),
            # para que el hueco_externo del PROXIMO frame (ver el inicio
            # de este metodo) incluya la menor porcion posible de nuestro
            # propio codigo por error de medicion.
            self._profile_prev_fin = time.perf_counter()

        return resultado

    # ------------------------------------------------------------------ #
    #  4) Cierre de la fuente
    # ------------------------------------------------------------------ #
    def teardown(self):
        """
        Se llama al detener esta fuente (Ctrl+C, quitarla desde el panel,
        etc.). Cualquier expediente que siga EN_FILA o EN_ATENCION en ese
        momento se cierra aqui con desenlace INDETERMINADO -no es ATENDIDO
        porque nunca se le vio salir de CAJA, ni DESERTOR porque no llego a
        agotarse --gracia- y se vuelca igual al Markdown, para no perder en
        silencio el episodio de alguien que estaba a mitad de fila o de
        atencion justo cuando se corto el proceso.

        list(...) sobre self.expedientes.items() porque _cerrar_expediente
        borra entradas de ese diccionario; iterar sobre una copia evita el
        "dictionary changed size during iteration" que darian el for/while
        directamente sobre self.expedientes.

        PARTE 1: usa self._reloj_ahora(None) -no time.monotonic() directo-
        para cerrar con el MISMO reloj (pared o video) que uso
        process_frame durante toda la vida de estos expedientes; frame_idx
        en None hace que, si el reloj es de video, se reuse el ultimo
        frame_idx visto en vez de saltar el reloj hacia adelante.
        """
        ahora = self._reloj_ahora(None)
        for track_id, exp in list(self.expedientes.items()):
            self._cerrar_expediente(track_id, exp, "INDETERMINADO", ahora,
                                     self._ultimo_frame_idx_reloj)

        # PARTE 3: refresca el snapshot de estado_cola() al cerrar la
        # fuente -sin esto, quedaria congelado en lo que mostraba el
        # ULTIMO process_frame antes de detenerse (personas que este mismo
        # teardown() ya cerro arriba, ver _construir_snapshot_estado: solo
        # itera sobre self.expedientes, que ahora deberia haber quedado
        # vacio)-. self.expedientes ya esta vacio en este punto (todos los
        # que quedaban abiertos se cerraron arriba), asi que "personas"
        # queda en lista vacia; el "resumen" si refleja los
        # atendidos/desertores/indeterminados finales de la sesion.
        personas_finales, resumen_final = self._construir_snapshot_estado(ahora)
        with self._estado_lock:
            self._cache_personas = personas_finales
            self._cache_resumen = resumen_final

    # ------------------------------------------------------------------ #
    #  4b) Overlay: colorear cada caja segun el estado de esa persona
    # ------------------------------------------------------------------ #
    def annotate(self, frame, result, stats):
        """
        INVESTIGACION (pedida en el prompt): el contrato de `boxes` que
        devuelve process_frame es (x1, y1, x2, y2, label, conf) -6
        elementos fijos, ver el propio comentario de process_frame y el
        docstring del modulo ("draw_boxes() en base_detector.py las
        convierte a int y las dibuja con la etiqueta y la confianza
        encima")-. No hay ningun campo de color en esa tupla, y
        base_detector.py no esta disponible en este workspace para revisar
        si draw_boxes()/annotate() admiten alguna otra via de indicar
        color por caja (por parametro, por config, etc.); dado que el
        contrato de datos que SI conozco (esta tupla) no deja lugar para
        eso, asumo que no la admiten.

        VIA ELEGIDA: dejar que BaseDetector.annotate() haga TODO su
        trabajo normal -banner superior, barra inferior con FPS/frame/
        fuente/extra, Y el dibujo por defecto de las cajas- llamando a
        super().annotate(), y DESPUES volver a dibujar SOLO el rectangulo
        de cada caja (mismas coordenadas, mismo grosor de linea) con el
        color que corresponde a su estado, encima del que ya dibujo la
        base. Ventajas de esta via sobre reimplementar todo el overlay
        nosotros:
          - No necesito conocer (ni replicar) como arma la base el banner
            ni la barra inferior: quedan intactos, con su FPS y su
            "extra" tal cual los pinta la base -que es exactamente lo que
            pide el prompt ("para no perder el FPS ni el campo extra").
          - No duplico texto de etiqueta/confianza: seguimos dejando que
            la base los dibuje una sola vez; nosotros solo repasamos el
            contorno del rectangulo.
        Costo aceptado: si el grosor de linea que usa la base para las
        cajas fuera distinto al que uso aqui (2 px), podria asomar un
        borde fino del color original de la base en el canto -riesgo bajo
        y puramente cosmetico, no afecta al FPS ni al contenido-.

        super().annotate(...) puede seguir dos convenciones que no puedo
        verificar sin ver la base: dibujar sobre `frame` IN-PLACE (y
        devolver None) o devolver un frame nuevo/distinto. Cubrimos las
        dos: dibujamos sobre lo que haya devuelto si no es None, o sobre
        el `frame` original en caso contrario, y devolvemos ese mismo
        valor para no romper el contrato con quien haya llamado a
        annotate() (probablemente el propio framework, para servir el
        JPEG por MJPEG).

        NO toca la logica de estados, la geometria ni los cronometros:
        `result["boxes"]` ya viene armado tal cual lo dejo process_frame;
        aqui solo se LEE la etiqueta de cada caja para decidir un color,
        nunca se recalcula nada.
        """
        salida = super().annotate(frame, result, stats)
        lienzo = salida if salida is not None else frame

        # Poligonos de zona SIEMPRE visibles (no dependen de que el editor
        # de zonas este abierto): se dibujan en todos y cada uno de los
        # frames servidos por el stream. Ver _dibujar_zonas.
        self._dibujar_zonas(lienzo)

        # Colores en BGR (convencion de OpenCV, no RGB).
        #
        # CRITERIO DE FONDO (PARTE 1, correccion): color saturado = persona
        # con expediente ABIERTO (algo que seguir); gris = ruido de fondo,
        # sin expediente. El rojo especificamente senala ABANDONO -por
        # eso NUNCA puede aplicarse a "ATENDIDO"/"ATENDIENDO" (quien ya
        # alcanzo la caja, ver el bloque de la etiqueta en process_frame
        # para la regla completa)-; "ATENDIENDO" y "ATENDIDO" comparten el
        # MISMO verde a proposito (el segundo es solo la ventana de
        # gracia del primero, sigue siendo un exito, no una alerta).
        COLOR_ESPERANDO = (0, 191, 255)    # ambar/amarillo
        COLOR_ATENDIENDO = (0, 170, 0)     # verde
        COLOR_DESERTOR = (0, 0, 255)       # rojo
        # "sin expediente" (etiqueta "---": sin ID, o con ID pero FUERA/
        # CAJA/FILA sin haber generado episodio, o en cuarentena de
        # --min-fila, ver process_frame) era naranja; PARTE 1 (Problema
        # B) lo cambia a GRIS porque el naranja competia visualmente con
        # el ambar de ESPERANDO y con el rojo de DESERTOR -tres colores
        # calidos casi indistinguibles de un vistazo-. El gris es neutro
        # y deja los colores saturados reservados para los tres estados
        # que de verdad importan seguir.
        COLOR_SIN_EXPEDIENTE = (128, 128, 128)  # gris
        # FUSION: atencion fisica por alguien SIN VERIFICAR en la zona
        # CAJERO (etiqueta "POSIBLEMENTE ATENDIENDO"): morado — ni el verde
        # de exito ni el ambar de espera; incertidumbre a resolver
        # (registrar al cajero, o esperar a que el rostro lo verifique).
        COLOR_POSIBLE = (255, 0, 200)  # morado

        for caja in result.get("boxes", []):
            x1, y1, x2, y2, etiqueta, _conf = caja
            # La etiqueta siempre tiene la forma "<id_txt de 2 palabras>
            # <estado> [cronometro]" o "<id_txt> ---" (ver process_frame:
            # id_txt es "ID <n>" o "sin ID", ambos de 2 palabras), asi que
            # el 3er token es siempre el estado o "---", sin importar si
            # hay ID o no. "DESERTOR"/"ATENDIDO" aqui son etiquetas EN
            # VIVO (PARTE 1: expediente sin confirmarse este frame, dentro
            # del margen de --gracia) -no los desenlaces ya archivados del
            # mismo nombre, que nunca pasan por `boxes`/annotate()-.
            tokens = etiqueta.split()
            estado_tok = tokens[2] if len(tokens) > 2 else "---"
            if estado_tok == "POSIBLEMENTE":
                color = COLOR_POSIBLE
            elif estado_tok == "ESPERANDO":
                color = COLOR_ESPERANDO
            elif estado_tok in ("ATENDIENDO", "ATENDIDO"):
                color = COLOR_ATENDIENDO
            elif estado_tok == "DESERTOR":
                color = COLOR_DESERTOR
            else:
                color = COLOR_SIN_EXPEDIENTE
            cv2.rectangle(lienzo, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)

            # Centroide (punto de contacto segun --anclaje, la MISMA
            # funcion que usa process_frame para decidir la zona de esta
            # persona -ver _punto_de_contacto-, recalculada aqui a partir
            # del bbox porque `boxes` no trae el punto ya resuelto) del
            # color de su estado actual, para poder ver a ojo si cae
            # dentro o fuera de los poligonos dibujados arriba.
            px, py = self._punto_de_contacto(x1, y1, x2, y2, self.args.anclaje)
            cv2.circle(lienzo, (int(px), int(py)), 4, color, -1, cv2.LINE_AA)

        return salida

    def _dibujar_zonas(self, lienzo):
        """
        Dibuja los 3 poligonos de zona (self.regions, ver set_zones) sobre
        `lienzo` IN-PLACE: relleno translucido (alpha ~0.25) + contorno
        solido, uno de cada color fijo por convencion de este proyecto
        (regions[0]=FILA naranja, regions[1]=CAJA verde, regions[2]=AFORO
        rojo -ver docstring de set_zones/--limite-aforo-). Se llama desde
        annotate() en TODOS los frames, sin condicion: no depende de que
        el editor de zonas (web_server_zones.py) este abierto en ese
        instante, a diferencia de la vista previa que solo se ve dentro
        del propio editor.

        Zonas que todavia no existen (menos de 3 puntos, o directamente
        no dibujadas) simplemente no se pintan -mismo criterio de
        "ausente" que ya usa _preparar_contorno para el resto del
        archivo-.
        """
        # FUSION Queue-Cashier: se dibujan las zonas TIPADAS, con el color
        # del registro de tipos (Base/zonas.py) — ya no por posicion.
        zonas_dib = getattr(self, "zonas", None) or []
        alpha = 0.15
        for z in zonas_dib:
            if len(z["puntos"]) < 3:
                continue
            color = _zonas.TIPOS[z["tipo"]]["color"]
            pts = np.array(z["puntos"], dtype=np.int32)
            overlay = lienzo.copy()
            cv2.fillPoly(overlay, [pts], color)
            cv2.addWeighted(overlay, alpha, lienzo, 1 - alpha, 0, dst=lienzo)
            cv2.polylines(lienzo, [pts], True, color, 1, cv2.LINE_AA)


# --------------------------------------------------------------------- #
#  5) Arranque del panel web
# --------------------------------------------------------------------- #
if __name__ == "__main__":
    # main_web recibe la CLASE, no una instancia: el servidor instancia un
    # detector nuevo por cada fuente que se agregue desde el navegador, para
    # que cada camara tenga su propio modelo y su propio estado.
    if main_web_zones is None:
        sys.exit("El panel necesita flask/werkzeug (uv run panel.py los "
                 "resuelve); como MODELO puro este archivo solo se importa.")
    main_web_zones(QueueAnalytics)


# ══════ SECCIÓN VENDIDA: queue_cashier_core.py (código validado 2026-08; no editar) ══════
# -*- coding: utf-8 -*-
"""
queue_cashier.py — Detector COMPUESTO: filas y cajero conviviendo en la
MISMA camara, con VARIOS JUEGOS de fila-caja-aforo (carriles).

Composicion, no reescritura: esta clase NO reimplementa ningun analisis.
Instancia UN CashierDetector (que ya maneja N zonas CAJERO) y UN
QueueAnalytics POR JUEGO de zonas FILA/CAJA/AFORO, y reparte las zonas por
tipo y por `puesto` (Base/zonas.py):

    CAJERO                    -> al CashierDetector (todas juntas)
    FILA/CAJA/AFORO puesto=1  -> al QueueAnalytics del juego "1"
    FILA/CAJA/AFORO puesto=2  -> al QueueAnalytics del juego "2"  ...etc

Cada juego es un analisis de filas COMPLETO e independiente (su maquina de
estados, su cupo de caja, sus expedientes, su carpeta resultados/<fuente>-J2/)
— exactamente el codigo validado de QueueAnalytics, sin tocar.

REGLA FUSIONADA POR JUEGO: la señal "hay cajero" de cada juego sale SOLO de
las zonas CAJERO de SU puesto; una zona CAJERO sin puesto es COMODIN (su
presencia habilita todos los juegos). Asi, en una camara con dos mostradores,
el cajero del puesto 1 no hace que los clientes del puesto 2 figuren como
atendidos.

COSTO ASUMIDO: cada juego corre su PROPIA inferencia YOLO full-frame (el
tracker de ultralytics vive dentro de la instancia del modelo y no puede
compartirse sin mezclar tracks). Con 1 juego + cajero son 2 inferencias por
fuente (igual que antes); cada juego extra suma una. En la RTX 4090 de
pruebas alcanza para pocas fuentes multi-juego; para decenas, habria que
refactorizar QueueAnalytics para aceptar detecciones externas (no trivial).

Los juegos nuevos que aparecen al guardar zonas se ARRANCAN PEREZOSAMENTE en
el hilo del worker (cargar su YOLO toma unos segundos; mejor ahi que en el
hilo del panel).
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# PORT-KINICH: sin sys.path.insert — el paquete usa imports relativos (G7)
# sys.path.insert(0, str(HERE / "Base"))

# PORT-MONOLITO (definido arriba en este archivo): from .base_detector import BaseDetector  # noqa: E402  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)
# PORT-MONOLITO (definido arriba en este archivo): from .params_mixin import ParamsMixin  # noqa: E402  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)
# PORT-MONOLITO (definido arriba en este archivo): from . import zonas as _zonas  # noqa: E402  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)

# PORT-MONOLITO (definido arriba en este archivo): from .QueueAnalytics_Web import QueueAnalytics  # noqa: E402  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)
# PORT-MONOLITO (definido arriba en este archivo): from .cashier_detector import CashierDetector, ZONES_WEB_DIR  # noqa: E402  # PORT-KINICH: import relativo (G7: sin nombres genéricos en sys.modules)

_TIPOS_FILA = ("FILA", "CAJA", "AFORO")
_SUMABLES = ("atendiendo", "en_fila", "desertando", "atendidos",
             "desertores", "indeterminados")


class QueueCashier(ParamsMixin, BaseDetector):
    NAME = "QUEUE_CASHIER"
    ZONA_TIPOS = _TIPOS_FILA + ("CAJERO",)

    # Los parametros de AMBOS en un solo panel. Un cambio de parametro de
    # filas se re-enruta a TODOS los juegos (mismos umbrales por camara —
    # si algun dia un carril necesita otros, es trabajo nuevo).
    PARAMS = QueueAnalytics.PARAMS + CashierDetector.PARAMS
    _KEYS_FILA = frozenset(p["key"] for p in QueueAnalytics.PARAMS)
    _KEYS_CAJERO = frozenset(p["key"] for p in CashierDetector.PARAMS)

    def __init__(self):
        self.cajero = CashierDetector()
        self.filas = {}              # puesto ("1","2",...) -> QueueAnalytics
        self._filas_por_arrancar = set()   # puestos con setup() pendiente
        self.zonas = []
        self._args = None

    # ------------------------------------------------------------------ #
    #  Plumbing del framework
    # ------------------------------------------------------------------ #
    def add_arguments(self, parser):
        QueueAnalytics().add_arguments(parser)
        self.cajero.add_arguments(parser)

    def configure(self, args):
        super().configure(args)
        self._args = args
        self.cajero.configure(args)
        # El juego "1" existe SIEMPRE (aunque no tenga zonas): es el
        # representante de los parametros de filas en el panel y conserva
        # el comportamiento del compuesto de un solo juego.
        self._crear_fila("1")

    def _crear_fila(self, puesto):
        inst = QueueAnalytics()
        inst.configure(self._args)
        self.filas[puesto] = inst
        self._filas_por_arrancar.add(puesto)
        return inst

    def _preparar_fila(self, inst, puesto):
        """Identidad de fuente + setup (PEREZOSO: se llama desde el hilo
        del worker; cargar YOLO aqui no congela el panel)."""
        base = getattr(self, "source_name", None) or self.NAME
        inst.source_name = base if puesto == "1" else f"{base}-J{puesto}"
        inst.source_path = getattr(self, "source_path", "")
        inst.setup()
        print(f"[{self.NAME}] juego {puesto}: analisis de filas ARRANCADO "
              f"(resultados como '{inst.source_name}')")

    def setup(self):
        for sub in (self.cajero,):
            sub.source_name = getattr(self, "source_name", None) or self.NAME
            sub.source_path = getattr(self, "source_path", "")
        self.cajero.setup()
        # el juego "1" arranca aqui mismo (hilo del worker); los demas, al
        # aparecer sus zonas
        self._preparar_fila(self.filas["1"], "1")
        self._filas_por_arrancar.discard("1")

        source_key = getattr(self, "source_key", None)
        if source_key:
            zpath = ZONES_WEB_DIR / f"{source_key}.json"
            if zpath.is_file():
                try:
                    with open(zpath) as f:
                        self.set_zonas(_zonas.leer_payload(json.load(f), self.NAME))
                except Exception as e:  # noqa: BLE001
                    print(f"[{self.NAME}] No pude cargar zonas de {zpath}: "
                          f"{e} — arranco sin zonas")
        print(f"[{self.NAME}] compuesto listo | juegos de filas: "
              f"{sorted(self.filas)} | cajero: "
              f"{'ON' if self._zonas_cajero() else 'off'}")

    def teardown(self):
        for inst in self.filas.values():
            try:
                inst.teardown()
            except Exception as e:  # noqa: BLE001
                print(f"[{self.NAME}] teardown de un juego fallo: {e}")
        self.cajero.teardown()

    # ------------------------------------------------------------------ #
    #  Zonas: reparto por tipo y por juego
    # ------------------------------------------------------------------ #
    def _zonas_cajero(self):
        return [z for z in self.zonas if z["tipo"] == "CAJERO"]

    def set_zonas(self, zonas):
        self.zonas = list(zonas or [])
        self.cajero.set_zonas(self._zonas_cajero())
        grupos = _zonas.agrupar_puestos(self.zonas)
        for puesto, zs in grupos.items():
            inst = self.filas.get(puesto) or self._crear_fila(puesto)
            inst.set_zonas(zs)
        # juegos que se quedaron sin zonas: se vacian (sus expedientes se
        # cierran solos por gracia en cuanto la geometria desaparece) pero
        # la instancia se conserva — si el juego vuelve, retoma su jornada
        for puesto, inst in self.filas.items():
            if puesto not in grupos:
                inst.set_zonas([])
        print(f"[{self.NAME}] zonas repartidas — juegos: "
              + (", ".join(f"J{p}:{len(zs)}" for p, zs in sorted(grupos.items()))
                 or "ninguno")
              + f" | cajero: {len(self._zonas_cajero())}")
        return True

    def get_zonas(self):
        return list(self.zonas)

    def get_zones(self):
        return _zonas.a_regions(self.zonas)

    # ------------------------------------------------------------------ #
    #  Parametros: filas -> TODOS los juegos; cajero -> cajero
    # ------------------------------------------------------------------ #
    def get_params(self):
        return self.filas["1"].get_params() + self.cajero.get_params()

    def set_params(self, data):
        data = data or {}
        dfila = {k: v for k, v in data.items() if k in self._KEYS_FILA}
        applied, errors = {}, {}
        for inst in self.filas.values():
            r = inst.set_params(dfila)
            applied.update(r.get("applied", {}))
            errors.update(r.get("errors", {}))
        rc = self.cajero.set_params({k: v for k, v in data.items()
                                     if k in self._KEYS_CAJERO})
        applied.update(rc.get("applied", {}))
        errors.update(rc.get("errors", {}))
        return {"applied": applied, "errors": errors}

    # ------------------------------------------------------------------ #
    #  Hooks del panel
    # ------------------------------------------------------------------ #
    def estado_cola(self):
        activos = {p: inst for p, inst in self.filas.items()
                   if p not in self._filas_por_arrancar}
        if len(activos) == 1:
            return next(iter(activos.values())).estado_cola()
        datas = {p: inst.estado_cola() for p, inst in sorted(activos.items())}
        base = datas.get("1") or next(iter(datas.values()))
        resumen = dict(base.get("resumen") or {})
        personas = []
        for p, d in datas.items():
            for per in d.get("personas") or []:
                per = dict(per)
                per["juego"] = p
                personas.append(per)
            if p == "1" or not isinstance(d.get("resumen"), dict):
                continue
            for k in _SUMABLES:
                if k in resumen and k in d["resumen"]:
                    resumen[k] = resumen.get(k, 0) + d["resumen"][k]
        salida = dict(base)
        salida["personas"] = personas
        salida["resumen"] = resumen
        salida["juegos"] = sorted(datas)
        return salida

    def reiniciar_jornada(self):
        for inst in self.filas.values():
            inst.reiniciar_jornada()
        return True

    def start_enroll(self, *a, **k):
        return self.cajero.start_enroll(*a, **k)

    def enroll_status(self):
        return self.cajero.enroll_status()

    def enroll_photos(self, *a, **k):
        return self.cajero.enroll_photos(*a, **k)

    def reload_db(self):
        return self.cajero.reload_db()

    def list_identities(self):
        return self.cajero.list_identities()

    def delete_identity(self, name):
        return self.cajero.delete_identity(name)

    def get_identity_views(self, name):
        return self.cajero.get_identity_views(name)

    def delete_identity_view(self, *a, **k):
        return self.cajero.delete_identity_view(*a, **k)

    # ------------------------------------------------------------------ #
    #  Señal "hay cajero" POR JUEGO
    # ------------------------------------------------------------------ #
    def _senal_por_juego(self, rc, puesto):
        """None = sin regla (este juego no tiene cajeros que le apliquen);
        True = verificado presente; "posible" = alguien sin verificar;
        False = nadie. Una zona CAJERO sin puesto es comodin (aplica a
        todos los juegos); con puesto, solo a su juego."""
        if not self._zonas_cajero() or rc.get("state") == "SIN_ROI":
            return None
        puesto_de = {z["id"]: z.get("puesto") for z in self._zonas_cajero()}
        rel = [i for i in (rc.get("zonas") or [])
               if puesto_de.get(i.get("zona")) in (None, puesto)]
        if not rel:
            return None
        if any(i.get("estado") in ("CAJERO_ATENDIENDO", "TOLERANCIA")
               for i in rel):
            return True
        if any(i.get("personas", 0) > 0 for i in rel):
            return "posible"
        return False

    # ------------------------------------------------------------------ #
    #  Por frame
    # ------------------------------------------------------------------ #
    def process_frame(self, frame, frame_idx):
        # cajero PRIMERO: su resultado alimenta la señal de cada juego
        rc = self.cajero.process_frame(frame, frame_idx)

        resultados = {}
        for puesto in sorted(self.filas):
            inst = self.filas[puesto]
            if not inst.zonas:            # juego sin zonas: no corre YOLO
                continue
            if puesto in self._filas_por_arrancar:
                self._preparar_fila(inst, puesto)   # perezoso, hilo worker
                self._filas_por_arrancar.discard(puesto)
            inst.atencion_disponible = self._senal_por_juego(rc, puesto)
            resultados[puesto] = inst.process_frame(frame, frame_idx)

        if not resultados:
            merged = dict(rc)
        else:
            cajero_activo = rc.get("state") != "SIN_ROI"
            boxes = []
            tracks = []
            extras = []
            for p, rq in sorted(resultados.items()):
                boxes.extend(rq.get("boxes") or [])
                for t in rq.get("tracks") or []:
                    t = dict(t)
                    t["juego"] = p
                    tracks.append(t)
                senal = self.filas[p].atencion_disponible
                marca = ("[SIN CAJERO] " if senal is False else
                         "[CAJERO S/VERIF] " if senal == "posible" else "")
                pref = f"J{p} " if len(resultados) > 1 else ""
                extras.append(f"{pref}{marca}{rq.get('extra', '')}")
            merged = {
                "alert": any(r.get("alert") for r in resultados.values())
                         or bool(rc.get("alert")),
                "label": rc.get("label") if cajero_activo
                         else next(iter(resultados.values())).get("label", ""),
                "score": rc.get("score") if cajero_activo
                         else next(iter(resultados.values())).get("score"),
                "boxes": boxes + list(rc.get("boxes") or []),
                "extra": ("FILAS " + " ‖ ".join(extras)
                          + (f"  ||  CAJERO {rc.get('extra', '')}"
                             if cajero_activo else "")),
                "state": rc.get("state"),
                "zonas": rc.get("zonas"),
                "tracks": tracks,
            }
        merged["_filas"] = resultados
        merged["_cajero"] = rc
        return merged

    # ------------------------------------------------------------------ #
    #  Overlay fusionado
    # ------------------------------------------------------------------ #
    # POSIBLEMENTE va primero: el match es por substring y "POSIBLEMENTE
    # ATENDIENDO" tambien contiene "ATENDIENDO".
    _COLOR_FILA = {"POSIBLEMENTE": (255, 0, 200),
                   "ESPERANDO": (0, 191, 255), "ATENDIENDO": (0, 170, 0),
                   "ATENDIDO": (0, 170, 0), "DESERTOR": (0, 0, 255)}

    def _dibujar_cajas_fila(self, lienzo, boxes):
        import cv2
        for caja in boxes:
            x1, y1, x2, y2 = [int(v) for v in caja[:4]]
            etiqueta = caja[4] if len(caja) > 4 else ""
            color = (128, 128, 128)
            for palabra, c in self._COLOR_FILA.items():
                if palabra in etiqueta:
                    color = c
                    break
            cv2.rectangle(lienzo, (x1, y1), (x2, y2), color, 2)
            cv2.putText(lienzo, etiqueta, (x1, max(15, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

    def annotate(self, frame, result, stats):
        rqs, rc = result.get("_filas") or {}, result.get("_cajero")
        cajero_activo = rc is not None and rc.get("state") != "SIN_ROI"
        if not rqs:
            return self.cajero.annotate(frame, rc, stats)
        if not cajero_activo and len(rqs) == 1:
            # un solo juego y sin cajero: overlay historico de filas intacto
            p = next(iter(rqs))
            return self.filas[p].annotate(frame, rqs[p], stats)
        # base: lienzo del cajero (con SIN_ROI devuelve el frame limpio, que
        # tambien sirve de base); encima, zonas y cajas de CADA juego
        lienzo = self.cajero.annotate(frame, rc, stats)
        for p, rq in sorted(rqs.items()):
            self.filas[p]._dibujar_zonas(lienzo)
            self._dibujar_cajas_fila(lienzo, rq.get("boxes") or [])
        self.cajero.draw_infobar(lienzo, stats, result.get("extra", ""))
        return lienzo


if __name__ == "__main__":
    from runner import main
    main(QueueCashier())


# PORT-MONOLITO: `nucleo()`/`adaptador` (del paquete original) = este módulo.
def nucleo():
    return _sys_mono.modules[__name__]

adaptador = _sys_mono.modules[__name__]


# ══════ SECCIÓN VENDIDA: adaptador.py (código validado 2026-08; no editar) ══════
# -*- coding: utf-8 -*-
"""Traducción núcleo↔Kinich: flags con pesos por env, zonas fuente→lienzo
800x600 y cajas de vuelta a píxeles de la fuente, y emisores de eventos
derivados del análisis (el flag `alert` del núcleo no sirve: False
incondicional en filas, y el del cajero pierde el arranque en frío)."""
# PORT-MONOLITO: __future__ va una sola vez, al inicio del archivo

import argparse
import os
from typing import Any

# PORT-MONOLITO (definido arriba en este archivo): from . import BYTETRACK_QUEUE_YAML

# Lienzo de calibración del núcleo: se analiza en 800x600 y las cajas se
# re-escalan al frame FUENTE (el overlay de Kinich normaliza contra la fuente).
CANVAS_W, CANVAS_H = 800, 600

# Pesos por env, SIEMPRE con ruta (un nombre pelado haría que ultralytics
# descargara otra copia en silencio).
YOLO_WEIGHTS = os.environ.get("KINICH_COPPEL_YOLO", "weights/yolo11s.pt")
REID_WEIGHTS = os.environ.get("KINICH_COPPEL_REID", "weights/transreid_msmt.onnx")
#: Galería de empleados (*.npz): dato mutable, al volumen /data.
GALLERY_DIR = os.environ.get("KINICH_COPPEL_GALLERY", "/data/empleados")
#: Bitácora del análisis (episodios.md + JPG): /tmp efímero por default;
#: persistirla es decisión explícita del operador.
RESULTADOS_DIR = os.environ.get("KINICH_COPPEL_RESULTADOS", "/tmp/kinich-coppel")

TIPOS_FILA = ("FILA", "CAJA", "AFORO")


def construir_args(core, fps: float) -> argparse.Namespace:
    """Flags del framework Base (defaults validados) + pesos/rutas de Kinich.
    Reloj de video a `fps`: Kinich ya pacea los frames a target_fps."""
    parser = argparse.ArgumentParser(add_help=False)
    core.add_arguments(parser)
    args, _ = parser.parse_known_args([])
    # flags del runner viejo que los detectores leen del namespace
    args.alert_dir = RESULTADOS_DIR
    args.frame_skip = 0
    args.source = ""
    # pesos y datos (B4)
    if hasattr(args, "model"):
        args.model = YOLO_WEIGHTS
    if hasattr(args, "person_model"):
        args.person_model = YOLO_WEIGHTS
    if hasattr(args, "reid_onnx"):
        args.reid_onnx = REID_WEIGHTS
    if hasattr(args, "db"):
        args.db = GALLERY_DIR
    if hasattr(args, "tracker"):
        args.tracker = str(BYTETRACK_QUEUE_YAML)
    # reloj
    if hasattr(args, "reloj"):
        args.reloj = "video"
        args.fps_video = float(fps)
    if hasattr(args, "fps"):
        args.fps = float(fps)
    return args


def aplicar_knobs(core, config: dict[str, Any], claves: frozenset[str]) -> None:
    """Knobs de sesión → set_params. SIEMPRE después de setup() (los hooks
    tocan estado que setup crea)."""
    hot = {k: v for k, v in config.items() if k in claves}
    if hot:
        core.set_params(hot)


def mapear_zonas(zonas_cfg: list, normalized: bool, w: int, h: int,
                 default_tipo: str | None = None) -> list[dict]:
    """Zonas del config ({tipo, points, carril?, nombre?} o lista de puntos)
    → protocolo tipado del núcleo, escaladas al lienzo. Coordenadas en px del
    frame FUENTE, o 0..1 si normalized."""
    sx = CANVAS_W if normalized else CANVAS_W / float(w)
    sy = CANVAS_H if normalized else CANVAS_H / float(h)
    out = []
    for z in zonas_cfg or []:
        if isinstance(z, dict):
            tipo = str(z.get("tipo") or z.get("type") or default_tipo or "").upper()
            pts = z.get("points") or z.get("polygon") or z.get("puntos") or []
            carril = z.get("carril", z.get("puesto"))
            nombre = z.get("nombre") or z.get("name") or None
        else:
            tipo, pts, carril, nombre = (default_tipo or "").upper(), z, None, None
        if not tipo or len(pts) < 3:
            continue
        out.append({
            "id": None, "tipo": tipo, "nombre": nombre,
            "puntos": [[int(p[0] * sx), int(p[1] * sy)] for p in pts],
            "puesto": str(carril) if carril not in (None, "") else None,
        })
    return out


def escalar_box(box, w: int, h: int) -> tuple[int, int, int, int]:
    """Caja del lienzo 800x600 → píxeles del frame fuente."""
    sx, sy = w / float(CANVAS_W), h / float(CANVAS_H)
    x1, y1, x2, y2 = (float(v) for v in box[:4])
    return (int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))


# ═══════════════════════════════════════════════════════════════════════════
# Emisores de eventos (B3): derivados de las señales del análisis, nunca del
# flag `alert` del núcleo.
# ═══════════════════════════════════════════════════════════════════════════
class EmisorCajero:
    """Eventos por zona CAJERO: transición a alerta suena de inmediato; zona
    que NACE alertada suena UNA vez tras la tolerancia (el upstream jamás
    alertaba ahí); al limpiar emite `*_OFF`. -> (type, nombre, estado, conf)."""

    def __init__(self, tolerance_s: float, fps: float, alert_unknown: bool):
        self.tol_frames = max(1.0, tolerance_s * fps)
        self.alert_states = {"DESATENDIDA"}
        if alert_unknown:
            self.alert_states |= {"PERSONA_NO_CAJERO", "PERSONA_NO_RECONOCIDA"}
        self._prev: dict[str, str] = {}
        self._fria_t0: dict[str, int] = {}
        self._alertada: dict[str, str] = {}   # zona -> type emitido (para el _OFF)

    @staticmethod
    def _tipo(estado: str) -> str:
        return {"DESATENDIDA": "CAJA_DESATENDIDA",
                "PERSONA_NO_CAJERO": "PERSONA_NO_CAJERO",
                "PERSONA_NO_RECONOCIDA": "PERSONA_NO_RECONOCIDA"}.get(estado, "CAJERO")

    def tick(self, zonas_info: list[dict], idx: int) -> list[tuple[str, str, str, float]]:
        out = []
        vistos = set()
        for zi in zonas_info or []:
            zid = str(zi.get("zona"))
            estado = str(zi.get("estado") or "")
            nombre = str(zi.get("nombre") or zid)
            conf = float(zi.get("conf") or 0.0)
            vistos.add(zid)
            prev = self._prev.get(zid)
            if estado not in self.alert_states:
                if zid in self._alertada:      # flanco de bajada
                    out.append((self._alertada.pop(zid) + "_OFF", nombre, estado, conf))
                self._prev[zid] = estado
                self._fria_t0.pop(zid, None)
                continue
            if zid in self._alertada:          # ya sonó, sigue en alerta
                self._prev[zid] = estado
                continue
            if prev is None or prev == "_FRIA":
                t0 = self._fria_t0.setdefault(zid, idx)
                if idx - t0 < self.tol_frames:
                    self._prev[zid] = "_FRIA"
                    continue
            out.append((self._tipo(estado), nombre, estado, conf))
            self._alertada[zid] = self._tipo(estado)
            self._prev[zid] = estado
            self._fria_t0.pop(zid, None)
        for zid in list(self._prev):
            if zid not in vistos:
                self._prev.pop(zid, None)
                self._fria_t0.pop(zid, None)
                self._alertada.pop(zid, None)
        return out


class EmisorFilas:
    """Eventos de una instancia QueueAnalytics: DESERCION (contador de
    definitivas), AFORO_EXCEDIDO/_OFF (flancos) y EPISODIO (hecho cerrado:
    desenlace/espera/servicio). -> (type, extra)."""

    def __init__(self):
        self._desercion_prev: int | None = None
        self._aforo_prev = False
        self._cerrados_prev: int | None = None

    def tick(self, estado_cola: dict) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        resumen = estado_cola.get("resumen") or {}
        n = resumen.get("alertas_desercion")
        if isinstance(n, int):
            if self._desercion_prev is not None and n > self._desercion_prev:
                for _ in range(n - self._desercion_prev):
                    out.append(("DESERCION", {}))
            self._desercion_prev = n
        aforo = bool(resumen.get("aforo_alerta_activa"))
        if aforo and not self._aforo_prev:
            out.append(("AFORO_EXCEDIDO",
                        {"aforo": resumen.get("aforo_actual"),
                         "limite": resumen.get("aforo_limite")}))
        elif self._aforo_prev and not aforo:
            out.append(("AFORO_EXCEDIDO_OFF",
                        {"aforo": resumen.get("aforo_actual"),
                         "limite": resumen.get("aforo_limite")}))
        self._aforo_prev = aforo
        cerrados = sum(int(resumen.get(k) or 0)
                       for k in ("atendidos", "desertores", "indeterminados"))
        if self._cerrados_prev is not None and cerrados > self._cerrados_prev:
            nuevos = cerrados - self._cerrados_prev
            # "ultimos" viene del más reciente al más viejo
            for u in (estado_cola.get("ultimos") or [])[:nuevos]:
                out.append(("EPISODIO", {
                    "track_id": u.get("id"),
                    "desenlace": u.get("desenlace"),
                    "espera_s": u.get("espera"),
                    "servicio_s": u.get("servicio"),
                }))
        self._cerrados_prev = cerrados
        return out


# ══════ SECCIÓN DEL MODELO (adaptador al contrato Kinich) ══════
# -*- coding: utf-8 -*-
"""QueueCashierDetector — el cruce filas↔cajero Coppel por carril, contrato Kinich.

La señal que la revisión de integración pidió rescatar: N análisis de filas
COMPLETOS (uno por carril) + un vigilante de cajero, con las señales CRUZADAS
por carril — la atención de cada carril sale SOLO de sus zonas CAJERO (sin
carril = comodín), así el cajero del carril 1 no hace que los clientes del 2
figuren como atendidos. Lógica intacta (compuesto del panel fusionado,
validado 2026-08; igualdad 120/120 verificada contra el original). OJO: el
`combo` de Kinich comparte decode pero NO cruza señales — este modelo sí.

Eliminado y cambios: los de los dos modelos hermanos (ver sus docstrings);
además el `carril` es campo de primera clase del schema, y los eventos de
filas se consultan POR CARRIL (el resumen fusionado no suma deserciones ni
aforo). Costo heredado y medido: N carriles = N+1 inferencias YOLO
full-frame por frame — el modelo caro de la familia.
"""
import logging
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from kinich.contract import Capability, CVModel, Detection, Event, ModelInfo, StreamSession

# PORT-MONOLITO (todo definido arriba): from ._coppel_core import adaptador, nucleo

log = logging.getLogger(__name__)

# PORT-MONOLITO: fps de consumo configurable por despliegue. 10 es el default
# económico; con video de 15 fps, KINICH_QC_FPS=15 consume todos los cuadros
# (misma cadencia que el panel original) a cambio de más CPU/GPU.
TARGET_FPS = float(os.getenv("KINICH_QC_FPS", "10"))
KNOBS = frozenset({
    # filas (se re-enrutan a TODOS los carriles)
    "min_fila", "min_caja", "gracia", "retener_puesto", "max_atencion",
    "histeresis", "objetivo_espera", "conf", "capacidad_caja",
    "confirmar_regreso", "cooldown_atendido", "limite_aforo",
    # cajero
    "reid_mode", "match_th", "min_frac", "face_th", "face_grace",
    "face_required", "person_conf", "tolerance", "rise_seconds",
    "roi_overlap", "yolo_margin", "strict_roi", "position_primary",
    "alert_unknown",
})

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "zones": {
            "type": "array",
            "description": (
                "Zonas tipadas: {tipo: FILA|CAJA|AFORO|CAJERO, points: "
                "[[x, y], ...] (>=3, en PÍXELES del frame del stream, o [0,1] "
                "si zones_normalized), carril?: int|str, nombre?: str}. UNA "
                "FILA/CAJA/AFORO por carril (varios carriles = varios valores "
                "de carril); CAJERO las que hagan falta — una zona CAJERO sin "
                "carril es COMODÍN y habilita todos los carriles."),
            "items": {
                "type": "object",
                "required": ["tipo", "points"],
                "properties": {
                    "tipo": {"type": "string",
                             "enum": ["FILA", "CAJA", "AFORO", "CAJERO"]},
                    "points": {"type": "array", "minItems": 3,
                               "items": {"type": "array", "minItems": 2, "maxItems": 2,
                                          "items": {"type": "number"}}},
                    "carril": {"type": ["integer", "string"]},
                    "nombre": {"type": "string"},
                },
            },
        },
        "zones_normalized": {"type": "boolean", "default": False},
        "capacidad_caja": {"type": "integer", "default": 1, "minimum": 1},
        "min_fila": {"type": "number", "default": 0.5},
        "min_caja": {"type": "number", "default": 3.0},
        "gracia": {"type": "number", "default": 20.0},
        "limite_aforo": {"type": "integer", "default": 10},
        "conf": {"type": "number", "default": 0.35},
        "tolerance": {"type": "number", "default": 10,
                      "description": "s sin cajero antes de CAJA_DESATENDIDA; también cuánto siguen los clientes como atendiendo al irse"},
        "rise_seconds": {"type": "number", "default": 1.5},
        "person_conf": {"type": "number", "default": 0.5},
        "position_primary": {"type": "boolean", "default": False},
        "face_required": {"type": "boolean", "default": True},
        "alert_unknown": {"type": "boolean", "default": False},
        "resumen_cada_s": {"type": "number", "default": 0,
                           "description": "cada cuántos s emitir RESUMEN con los contadores fusionados (0 = nunca)"},
    },
}


class _Session(StreamSession):
    def __init__(self, config: dict[str, Any]):
        nc = nucleo()
        self._zmod = nc._zonas
        self.core = nc.QueueCashier()
        self.core.configure(adaptador.construir_args(self.core, TARGET_FPS))
        self.core.source_name = f"kinich-{uuid.uuid4().hex[:8]}"
        self.core.source_path = ""
        self.core.setup()
        adaptador.aplicar_knobs(self.core, config, KNOBS)
        self._zonas_cfg = config.get("zones") or []
        self._normalized = bool(config.get("zones_normalized"))
        self._zmod.normalizar(
            adaptador.mapear_zonas(self._zonas_cfg, True, 1, 1), self.core.NAME)
        self._pendientes = True
        self._knobs_cfg = config
        self._idx = 0
        self._em_cajero = adaptador.EmisorCajero(
            tolerance_s=float(getattr(self.core.cajero, "tolerance", 10.0)),
            fps=TARGET_FPS,
            alert_unknown=bool(getattr(self.core.cajero, "alert_unknown", False)))
        self._em_filas: dict[str, adaptador.EmisorFilas] = {}
        self._resumen_cada = float(config.get("resumen_cada_s") or 0)
        self._ultimo_resumen = None

    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]:
        h, w = frame.shape[:2]
        if self._pendientes:
            zonas = self._zmod.normalizar(
                adaptador.mapear_zonas(self._zonas_cfg, self._normalized, w, h),
                self.core.NAME)
            self.core.set_zonas(zonas)
            # los carriles creados por set_zonas nacen con defaults de
            # fábrica: heredarles los knobs del operador (regla del port)
            adaptador.aplicar_knobs(self.core, self._knobs_cfg, KNOBS)
            self._pendientes = False
        canvas = frame if (w, h) == (adaptador.CANVAS_W, adaptador.CANVAS_H) else \
            cv2.resize(frame, (adaptador.CANVAS_W, adaptador.CANVAS_H))
        idx = self._idx
        self._idx += 1
        r = self.core.process_frame(canvas, idx)

        events: list[Event] = []
        # cajero: por zona, con frío y _OFF; cajas al frame FUENTE
        rc = r.get("_cajero") or {}
        disparos = self._em_cajero.tick(rc.get("zonas") or [], idx)
        if disparos:
            dets = []
            for box in (rc.get("boxes") or []):
                try:
                    x1, y1, x2, y2 = adaptador.escalar_box(box, w, h)
                    label = str(box[4]) if len(box) > 4 else "persona"
                    sc = float(box[5]) if len(box) > 5 and box[5] is not None else 0.0
                    dets.append(Detection(label=label, score=max(0.0, sc),
                                          bbox=(x1, y1, x2, y2)))
                except (TypeError, ValueError):
                    continue
            for tipo, nombre, estado, conf in disparos:
                events.append(Event(type=tipo, score=1.0, ts=ts, detections=dets,
                                    extra={"zona": nombre, "estado": estado,
                                           "presencia": round(conf, 3)}))
        # filas: POR CARRIL (el resumen fusionado no suma deserciones/aforo)
        activos = {p: inst for p, inst in self.core.filas.items()
                   if p not in self.core._filas_por_arrancar and inst.zonas}
        varios = len(activos) > 1
        for carril, inst in sorted(activos.items()):
            emisor = self._em_filas.setdefault(carril, adaptador.EmisorFilas())
            try:
                ec = inst.estado_cola()
            except Exception:  # noqa: BLE001
                continue
            for tipo, extra in emisor.tick(ec):
                if varios:
                    extra = dict(extra, carril=carril)
                events.append(Event(type=tipo, score=1.0, ts=ts, extra=extra))
        if self._resumen_cada > 0 and (self._ultimo_resumen is None
                                       or ts - self._ultimo_resumen >= self._resumen_cada):
            self._ultimo_resumen = ts
            try:
                ec = self.core.estado_cola()
                resumen = dict(ec.get("resumen") or {})
                resumen["estado_cajero"] = rc.get("state")
                if ec.get("juegos"):
                    resumen["carriles"] = ec["juegos"]
                events.append(Event(type="RESUMEN", score=0.0, ts=ts, extra=resumen))
            except Exception:  # noqa: BLE001
                pass
        return events

    def close(self) -> None:
        try:
            self.core.teardown()
        except Exception:  # noqa: BLE001
            log.exception("teardown de queue-cashier falló")


class QueueCashierDetector(CVModel):
    info = ModelInfo(
        name="queue-cashier",
        version="1.0",
        task=("Filas y cajero CRUZADOS por carril en la misma cámara: espera/"
              "servicio/deserción/aforo por carril + presencia e identidad del "
              "cajero; un cliente en caja sin cajero no cuenta como atendido"),
        capabilities={Capability.STREAM},
        event_types=["DESERCION", "AFORO_EXCEDIDO", "AFORO_EXCEDIDO_OFF",
                     "EPISODIO", "CAJA_DESATENDIDA", "CAJA_DESATENDIDA_OFF",
                     "PERSONA_NO_CAJERO", "PERSONA_NO_CAJERO_OFF",
                     "PERSONA_NO_RECONOCIDA", "PERSONA_NO_RECONOCIDA_OFF",
                     "RESUMEN"],
        config_schema=CONFIG_SCHEMA,
        target_fps=TARGET_FPS,
    )

    def load(self) -> None:
        nucleo()
        if not Path(adaptador.YOLO_WEIGHTS).is_file():
            raise FileNotFoundError(
                f"faltan los pesos {adaptador.YOLO_WEIGHTS} (KINICH_COPPEL_YOLO)")
        if not Path(adaptador.REID_WEIGHTS).is_file():
            log.warning("sin %s: identidad del cajero INACTIVA, opera por "
                        "presencia (KINICH_COPPEL_REID)", adaptador.REID_WEIGHTS)

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        return _Session(config or {})
