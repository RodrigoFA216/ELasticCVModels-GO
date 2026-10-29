# combo v1.0

Pipeline: corre varios modelos sobre el mismo stream (un solo decode)

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: *.
- Cadencia máxima declarada de referencia: 10.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`models`, `configs`

## Activos e aislamiento

La API nueva representa combo mediante composición. Este ID sólo aparece como entrada de referencia no ejecutable; no importa el manager combo anterior.
