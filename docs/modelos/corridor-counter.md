# corridor-counter v3.0

Entradas y salidas por un corredor de conteo (box + escalera de líneas, reglas anti-oclusión R1-R5, guardián de luz); DENTRO persistente por contador_id. No es aforo instantáneo (eso es people-counter)

## Contrato

- Capacidades: stream.
- Eventos conservados: ENTRADA, SALIDA, CONTEO_PAUSADO, CONTEO_PAUSADO_OFF, RESUMEN.
- Cadencia máxima declarada de referencia: 10.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`box`, `n_lines`, `invert`, `axis_rot`, `full_frame`, `dentro_inicial`, `reset_conteo`, `contador_id`, `resumen_cada_s`

## Activos e aislamiento

Adaptador: `corridor_counter.CorridorCounterDetector`.
Código de referencia SHA256: `ed801a8938f0a229abd6910a687f416d660ff8508d97b4d0aae9949c6ae9c657`.
Variables de activos/rutas detectadas estáticamente: `FULLCV_COUNTERS_DIR`, `FULLCV_DEBUG_SAVE`, `KINICH_COUNT_COUNTERS`, `KINICH_COUNT_FPS`, `KINICH_COUNT_YOLO`.
Símbolos globales del módulo inventariados: `COORDS_NORM`, `COORDS_PIXELS`, `COORDS_EMPTY`, `COORDS_INVALID`, `COORDS_DEGENERATE`, `BOX_POINTS`, `DEGENERATE_MSG`, `_MODULE_DIR`, `ROOT`, `_YOLO_WEIGHTS`, `_YOLO_WEIGHTS_PARIDAD`, `_ENV_PESOS`, `_TRACKER_LOCAL`, `TRACKER_YAML`, `EVENTS_DIR`, `_DEBUG_SAVE`, `N_LINES`, `DETECT_CLASSES`, `CONF_NORMAL`, `CONF_BAJA_LUZ`, `LUZ_OSCURO`, `LUZ_BAJA`, `LUZ_EMA`, `MIN_TRACK_AGE`, `TRACK_TTL`, `PENDING_TTL`, `PENDING_MATCH_P`, `CROP_PAD`, `CROP_TOP_X`, `CROP_MIN`, `C_BOX_BORDE`, `C_BOX_FILL`, `C_ESCALERA`, `C_AFUERA`, `C_ADENTRO`, `C_PERSONA`, `C_PROG_IN`, `C_PROG_OUT`, `C_TXT`, `C_LUZ`, `_INFOBAR_H`, `_SHA_PESOS`, `log`, `TARGET_FPS`, `PESOS_ENV`, `COUNTERS_BASE`, `CONFIG_SCHEMA`, `META_FULLCV`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
