"""Preproceso de imágenes para CLIP, en lote y sobre la GPU.

QUÉ SUSTITUYE

Cuatro detectores (fire, crash, carparts, robbery) hacían lo mismo:

    prep = torch.stack([preprocess(im) for im in pil_images]).to(DEVICE)

Una comprensión de lista que aplica el `Compose` de torchvision a UNA imagen
PIL a la vez, en CPU y en serie. Medido con cProfile sobre video real de la
cámara: 311 llamadas, 1,707 s, o **5,5 ms por imagen**. En `fire-detector` eso
era el 29 % de sus 90 ms por fotograma.

Y antes de llegar aquí, quien llama ya convirtió cada fotograma a PIL con
`cv2.resize` + `cvtColor` + `Image.fromarray`, para que el `Compose` lo vuelva
a redimensionar. Se redimensiona dos veces, de una en una, en el peor sitio.

Aquí se sube el lote entero a la GPU en una transferencia —medida a 54,7 GB/s,
un lote de 60 fotogramas cuesta 0,8 ms— y se hacen ahí el cambio de canales, la
escala y la normalización.

POR QUÉ SE VERIFICA Y NO SE CONFÍA

Estos detectores llevan umbrales CALIBRADOS contra las salidas de CLIP. Un
preproceso que difiera aunque sea poco mueve las similitudes y desafina la
calibración sin que nada falle: seguirían detectando, peor.

QUÉ PROTEGE Y QUÉ NO, sin adornos:

- En ejecución, lo único que se comprueba es `disponible`, y eso sólo dice que
  se supo PARSEAR el `Compose` — no que la salida coincida. Si no se pudo
  parsear, se cae al camino por imagen de siempre, que es más lento y nunca
  incorrecto: fallar aquí es fallar hacia el lado seguro.
- La equivalencia de la SALIDA se comprueba fuera de línea, en
  `tests/test_preproceso.py`, sobre vídeo real y con el `Compose` real de
  open_clip. Ahí es donde hay que mirar antes de tocar nada de este fichero.
- `equivalente_a()` es la herramienta para esa comprobación. No se llama desde
  el runtime a propósito: exige construir la referencia PIL imagen por imagen,
  que es justo el coste que este módulo existe para quitar.
"""
from __future__ import annotations

import logging

import cv2
import numpy as np
import torch
import torch.nn.functional as F

log = logging.getLogger(__name__)

# El lado al que los llamadores de `ksm_select_key_frames*` aplastaban el
# fotograma antes de construir el PIL. Es un 224 escrito a mano en los cuatro
# detectores, no el tamano de entrada del CLIP: coinciden hoy y no tienen por
# que seguir coincidiendo. Cambiarlo cambia lo que ve el modelo.
LADO_APLASTADO = 224


def _leer_compose(preprocess) -> tuple[int, list[float], list[float]] | None:
    """Saca (tamaño, media, desviación) del `Compose` de open_clip.

    Se LEE en vez de codificarse a mano: si open_clip cambia sus constantes o
    alguien usa otra variante de CLIP, un valor fijo aquí daría embeddings
    sutilmente equivocados. Si no se reconoce la estructura, se devuelve None y
    quien llama se queda con el camino de siempre.
    """
    try:
        tam = None
        media = desv = None
        for t in getattr(preprocess, "transforms", []):
            nombre = type(t).__name__
            if nombre in ("Resize", "RandomResizedCrop") and tam is None:
                s = getattr(t, "size", None)
                tam = s if isinstance(s, int) else (s[0] if s else None)
            elif nombre == "CenterCrop":
                s = getattr(t, "size", None)
                tam = s if isinstance(s, int) else (s[0] if s else tam)
            elif nombre == "Normalize":
                media, desv = list(t.mean), list(t.std)
        if tam is None or media is None or desv is None:
            return None
        return int(tam), media, desv
    except Exception:  # noqa: BLE001
        return None


class PreprocesoGPU:
    """Reemplazo por lotes del `Compose` de CLIP. `None` si no se pudo leer."""

    def __init__(self, preprocess, device) -> None:
        leido = _leer_compose(preprocess)
        self.disponible = leido is not None
        if not self.disponible:
            log.warning(
                "preproceso de CLIP no reconocido (%s): se usa el camino por "
                "imagen en CPU", type(preprocess).__name__,
            )
            return
        self.tam, media, desv = leido
        self.device = device
        self._media = torch.tensor(media, device=device).view(1, 3, 1, 1)
        self._desv = torch.tensor(desv, device=device).view(1, 3, 1, 1)

    def desde_bgr(self, frames_bgr: list[np.ndarray], cuadrar: bool = False) -> torch.Tensor:
        """Lista de fotogramas BGR de OpenCV -> lote listo para `encode_image`.

        Recibe BGR y no PIL a proposito: asi el llamador se ahorra tambien el
        `cvtColor` y el `Image.fromarray`.

        `cuadrar` decide la GEOMETRIA, y no es un detalle de gusto: cambia lo
        que CLIP llega a ver.

        - `True` aplasta a un cuadrado. Es lo que hacian los llamadores que
          traian su propio `cv2.resize(f, (224, 224))` antes del PIL; para
          ellos el `Resize`/`CenterCrop` del `Compose` ya no hacia nada.
        - `False` (por defecto) reproduce el `Compose` desnudo: lado corto a
          `tam` conservando la proporcion, y recorte al centro. Es lo que
          recibian los fotogramas que se pasaban a resolucion de camara.

        Confundirlas recorta el encuadre o lo deforma sin que nada falle: los
        detectores seguirian corriendo, con la calibracion desafinada.

        POR QUE EL REDIMENSIONADO CUADRADO SE QUEDA EN CPU

        Medido en un Mac M-series sobre 60 fotogramas de 1280x720: hacerlo en
        `F.interpolate` costaba 129,8 ms contra 18,5 ms del camino original.
        `cv2.resize` es SIMD y son 6,0 ms de esos 18,5; lo caro era el resto
        --PIL, `ToTensor`, `Normalize`, imagen por imagen-- y ESO es lo que se
        va al lote. De paso el bus transporta 9 MB en vez de 166: se sube ya
        reducido, no a resolucion de camara.
        """
        if cuadrar:
            # `cv2.resize` INTER_LINEAR es exactamente lo que hacia el
            # llamador: reproducirlo con `F.interpolate` da otra imagen
            # (bilineal filtrada contra sin filtrar). Asi la diferencia con el
            # camino viejo es cero, no "pequena".
            #
            # Y se aplasta a LADO_APLASTADO, no a `self.tam`. Parece lo mismo
            # porque hoy `tam` vale 224, pero no lo es: el llamador tenia el
            # 224 escrito a mano y despues venia el `Compose`, que con otro
            # CLIP --`ViT-L-14-336` sale con solo poner KINICH_FIRE_CLIP_MODEL--
            # reescalaba ese cuadrado de 224 a 336. Aplastar directo a 336 se
            # salta ese paso y da otra imagen. Ademas el respaldo por CPU de
            # los cuatro detectores tambien lleva el 224 a mano: usar `tam`
            # aqui hacia que el camino rapido y su propio respaldo no
            # coincidieran, en silencio.
            lote = np.stack([
                f if f.shape[:2] == (LADO_APLASTADO, LADO_APLASTADO)
                else cv2.resize(f, (LADO_APLASTADO, LADO_APLASTADO))
                for f in frames_bgr
            ])
            lote = torch.from_numpy(lote).to(self.device, non_blocking=True)
            lote = lote[..., [2, 1, 0]].permute(0, 3, 1, 2).float().div_(255.0)
            if lote.shape[-2:] == (self.tam, self.tam):
                # El caso de siempre (tam == 224): el `Compose` no hacia nada
                # mas, asi que aqui se termina.
                return lote.sub_(self._media).div_(self._desv)
            # Con otro CLIP falta el `Resize`/`CenterCrop`; como la entrada ya
            # es cuadrada, se reduce a un reescalado limpio a `tam`.
            lote = F.interpolate(lote, size=(self.tam, self.tam), mode="bicubic",
                                 align_corners=False, antialias=self.tam < LADO_APLASTADO)
            return lote.sub_(self._media).div_(self._desv)

        # Camino del `Compose` desnudo. Aqui los lotes son chicos --un
        # fotograma suelto, o parches de 80x80-- asi que subir en crudo no
        # pesa, y se gana reproducir el filtrado de PIL, que `cv2` no tiene.
        lote = torch.from_numpy(np.ascontiguousarray(np.stack(frames_bgr)))
        lote = lote.to(self.device, non_blocking=True)
        lote = lote[..., [2, 1, 0]].permute(0, 3, 1, 2).float().div_(255.0)

        alto, ancho = lote.shape[-2:]
        # Mismo entero que `Resize(int)` de torchvision: trunca, no redondea.
        # En 320x240 la diferencia es 298 contra 299 pixeles de ancho, y ese
        # pixel corre el recorte al centro y desalinea la imagen entera:
        # medido sobre Robbery0002, el coseno caia a 0,9893.
        corto, largo = min(alto, ancho), max(alto, ancho)
        nuevo_largo = int(self.tam * largo / corto)
        destino = (self.tam, nuevo_largo) if alto <= ancho else (nuevo_largo, self.tam)
        if (alto, ancho) != destino:
            # PIL filtra al reducir; `antialias=True` es su equivalente.
            lote = F.interpolate(lote, size=destino, mode="bicubic",
                                 align_corners=False, antialias=True)
        if lote.shape[-2:] != (self.tam, self.tam):
            top = int(round((lote.shape[-2] - self.tam) / 2.0))
            izq = int(round((lote.shape[-1] - self.tam) / 2.0))
            lote = lote[..., top:top + self.tam, izq:izq + self.tam]

        return lote.sub_(self._media).div_(self._desv)


def equivalente_a(gpu: PreprocesoGPU, preprocess, frames_bgr, cuadrar=False,
                  min_coseno=0.999, max_dif=0.35) -> tuple[bool, float, float]:
    """¿El camino en lote da lo mismo que el `Compose` original?

    Devuelve `(pasa, coseno mínimo por imagen, diferencia máxima absoluta)`.

    SE MIRAN LAS DOS COSAS, y por motivos distintos:

    - El COSENO es lo que de verdad importa: CLIP proyecta a una dirección, y
      un preproceso que conserve la dirección conserva las similitudes contra
      las que están calibrados los umbrales.
    - Pero el coseno solo no basta. Sobre 150 mil elementos en float32, dos
      tensores IDÉNTICOS ya dan 0,99995 por acumulación —se han visto valores
      por encima de 1, que es imposible salvo por ruido— así que la banda entre
      «idéntico» y «distinto» es estrecha, y un error real cabe dentro: cambiar
      bicúbico por bilineal pasa con 0,99959. Por eso va también un tope a la
      diferencia máxima absoluta, que ese caso sí rompe.

    `max_dif` es 0,35 y no 0,05 porque 0,05 era inalcanzable: PIL y
    `F.interpolate` no interpolan idéntico, y en la ampliación de 80 a 224 que
    hace `compute_pc` la diferencia normal ya llega a 0,29. Un tope por debajo
    de eso no marca un fallo, marca siempre.

    `cuadrar` debe ser el MISMO que use el llamador real. Comprobar con el
    valor equivocado compara dos geometrías distintas y siempre falla.
    """
    from PIL import Image

    ref = torch.stack([
        preprocess(Image.fromarray(cv2.cvtColor(
            cv2.resize(f, (LADO_APLASTADO, LADO_APLASTADO)) if cuadrar else f,
            cv2.COLOR_BGR2RGB)))
        for f in frames_bgr
    ]).to(gpu.device)
    got = gpu.desde_bgr(frames_bgr, cuadrar=cuadrar)
    dif = (ref - got).abs().max().item()
    cos = torch.nn.functional.cosine_similarity(
        ref.flatten(1).float(), got.flatten(1).float(), dim=1).min().item()
    return (cos >= min_coseno and dif <= max_dif), cos, dif


_cache: dict[int, PreprocesoGPU] = {}


def obtener(preprocess, device) -> PreprocesoGPU:
    """`PreprocesoGPU` reutilizado por transform. Leer el `Compose` y crear los
    tensores de media/desviación cuesta; hacerlo por fotograma sería cambiar un
    gasto por otro."""
    clave = id(preprocess)
    if clave not in _cache:
        _cache[clave] = PreprocesoGPU(preprocess, device)
    return _cache[clave]
