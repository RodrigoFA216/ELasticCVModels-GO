# people-counter v1.0

Conteo de personas por patch-zoom (grid + YOLO por patch + NMS global)

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: CONTEO.
- Cadencia máxima declarada de referencia: 5.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`scene`, `conf`, `nms_iou`, `rows`, `cols`, `overlap`, `smooth_frames`

## Activos e aislamiento

Adaptador: `people_counter.PeopleCounter`.
Código de referencia SHA256: `69d4b853565ed73f06fe601d13cd94b85bf5bec6ffd4491b5cb23a47e7fc2612`.
Variables de activos/rutas detectadas estáticamente: `KINICH_PEOPLE_WEIGHTS`.
Símbolos globales del módulo inventariados: `log`, `WEIGHTS`, `DETECT_CLASSES`, `GRID_ROWS`, `GRID_COLS`, `PATCH_OVERLAP`, `PATCH_IMGSZ`, `SMOOTH_FRAMES`, `SCENE_PROFILES`, `DEFAULT_SCENE`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
