# pulso v1.0

Modelo sintético de diagnóstico: ignora el video y emite una detección centrada N s / calla M s (sin GPU ni pesos)

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: CONTEO, CONTEO_OFF.
- Cadencia máxima declarada de referencia: 2.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`on_s`, `off_s`, `cadencia`, `tipo`, `fraccion`

## Activos e aislamiento

Diagnóstico sintético, sin GPU/pesos. Estado temporal independiente por instancia. Pruebas: flujo continuo 181s, cambios de composición y 100 ciclos. Esto no mide precisión de detectores.
