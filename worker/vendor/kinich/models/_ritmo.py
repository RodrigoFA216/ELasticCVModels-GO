"""Reparto de fotogramas entre los miembros de un combo.

QUÉ RESUELVE

El pacing por miembro ya evita que un modelo de 1 fps corra diez veces por
segundo. Pero deja un problema que no se ve hasta que se mide: **los miembros
caros caen en el mismo fotograma**.

Con el decode a 10 fps y los costes medidos en la partición de 96 GB, si
`parking-monitor` (1053 ms, 1 de cada 10 fotogramas) coincide con
`people-counter` (229 ms, 1 de cada 2), ese fotograma cuesta 2163 ms mientras
los vecinos cuestan 881. El operador no ve un promedio: ve un tirón.

Este módulo hace dos cosas, y son distintas:

  1. `cada_n` — cuántos fotogramas se salta cada miembro. Sale de los fps que
     el modelo declara, y se puede FORZAR por configuración para bajar el coste
     de un detector caro sin sacarlo del combo.
  2. La FASE — en cuál de esos N fotogramas cae. Es lo que separa a los caros
     para que no se apilen, y no cuesta nada: es elegir un número.

El coste de cada miembro NO se declara, se OBSERVA. Un detector tarda distinto
según la escena —`fire-detector` tiene un pre-filtro HSV que descarta el
fotograma antes de tocar YOLO si no hay nada naranja—, así que una tabla fija
mentiría en cuanto cambie la cámara.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

#: Peso de la última medición en la media móvil del coste. Bajo a propósito:
#: interesa la tendencia, no el fotograma raro que tardó por un pico del nodo.
_SUAVIZADO = 0.15

#: Cada cuántos fotogramas se recalculan las fases. Ni en cada uno —el reparto
#: cambia despacio y recalcular cuesta— ni tan tarde que una escena nueva
#: quede repartida con los costes de la anterior.
_REVISAR_CADA = 300


class Ritmo:
    """Decide qué miembros corren en cada fotograma, y reparte los caros.

    No conoce modelos ni sesiones: recibe nombres y devuelve nombres. Eso lo
    hace probable sin GPU, que es la razón de que viva aparte de `combo.py`.
    """

    def __init__(self, cada_n: dict[str, int]) -> None:
        #: nombre -> cada cuántos fotogramas le toca (>= 1)
        self._cada_n = {k: max(1, int(v)) for k, v in cada_n.items()}
        #: nombre -> en cuál de esos N fotogramas cae
        self._fase = {k: 0 for k in self._cada_n}
        #: nombre -> coste medio observado, en ms
        self._coste: dict[str, float] = {}
        self._n = 0
        self._candado = threading.Lock()
        self._repartir()

    # ── lo que usa el combo ──────────────────────────────────────────────

    def toca(self, nombre: str) -> bool:
        """¿Le toca a este miembro en el fotograma actual?"""
        n = self._cada_n.get(nombre, 1)
        return (self._n - self._fase.get(nombre, 0)) % n == 0

    def avanzar(self) -> None:
        """Un fotograma más. Se llama UNA vez por fotograma, no por miembro."""
        self._n += 1
        if self._n % _REVISAR_CADA == 0 and self._coste:
            self._repartir()

    def observar(self, nombre: str, ms: float) -> None:
        """Registra lo que tardó un miembro. Media móvil exponencial."""
        with self._candado:
            previo = self._coste.get(nombre)
            self._coste[nombre] = ms if previo is None else (
                previo * (1 - _SUAVIZADO) + ms * _SUAVIZADO
            )

    # ── el reparto ───────────────────────────────────────────────────────

    def _repartir(self) -> None:
        """Asigna a cada miembro la fase que aplana más el pico.

        Voraz por coste descendente: el más caro elige primero, y cada
        siguiente se coloca donde menos suba el máximo. No busca el óptimo
        global —es un problema de empaquetado— pero con diez miembros y una
        ventana corta la diferencia no paga la complejidad.

        ponytail: voraz sobre la ventana del `cada_n` mayor. Si algún día hay
        cientos de miembros, aquí es donde entra algo más listo.
        """
        with self._candado:
            ventana = max(self._cada_n.values(), default=1)
            carga = [0.0] * ventana
            # Sin costes observados todavía, el orden es el de configuración:
            # da un reparto razonable en los primeros fotogramas.
            orden = sorted(
                self._cada_n,
                key=lambda k: self._coste.get(k, 0.0),
                reverse=True,
            )
            for nombre in orden:
                n = self._cada_n[nombre]
                coste = self._coste.get(nombre, 1.0)
                mejor_fase, mejor_pico = 0, None
                for fase in range(n):
                    pico = max(
                        carga[i] + (coste if (i - fase) % n == 0 else 0.0)
                        for i in range(ventana)
                    )
                    if mejor_pico is None or pico < mejor_pico:
                        mejor_fase, mejor_pico = fase, pico
                self._fase[nombre] = mejor_fase
                for i in range(ventana):
                    if (i - mejor_fase) % n == 0:
                        carga[i] += coste

    # ── diagnóstico ──────────────────────────────────────────────────────

    def reparto(self) -> dict[str, object]:
        """Qué está haciendo, para poder mirarlo desde fuera."""
        ventana = max(self._cada_n.values(), default=1)
        carga = [0.0] * ventana
        for nombre, n in self._cada_n.items():
            for i in range(ventana):
                if (i - self._fase[nombre]) % n == 0:
                    carga[i] += self._coste.get(nombre, 0.0)
        return {
            "fotograma": self._n,
            "cada_n": dict(self._cada_n),
            "fase": dict(self._fase),
            "coste_ms": {k: round(v, 1) for k, v in self._coste.items()},
            "pico_ms": round(max(carga), 1) if carga else 0.0,
            "valle_ms": round(min(carga), 1) if carga else 0.0,
        }


def cada_n_desde_fps(target_fps: float, decode_fps: float, forzado: int | None = None) -> int:
    """De «este modelo quiere 5 fps» a «córrelo 1 de cada 2 fotogramas».

    `forzado` gana siempre: es la palanca del operador para bajar el coste de
    un detector caro sin sacarlo del combo. Un modelo que pide 24 fps sobre un
    decode de 10 no obtiene 24 — obtiene todos los fotogramas, que es cuanto
    hay.
    """
    if forzado is not None:
        return max(1, int(forzado))
    if target_fps <= 0 or decode_fps <= 0:
        return 1
    return max(1, round(decode_fps / min(target_fps, decode_fps)))
