# whitegas v1.0

Fugas de gas blanco por segmentación de texto (SAM 3.1 INT4 W4A4, prompt configurable, sin tracking ni vetos WATER/CLOUD); FUGA_GAS con confirmación sostenida y máscara→bbox+área. No es el fire nativo (fuego/humo por CNN)

## Contrato

- Capacidades: stream.
- Eventos conservados: FUGA_GAS, FUGA_GAS_OFF, RESUMEN.
- Cadencia máxima declarada de referencia: 5.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`prompt`, `threshold`, `confirm_s`, `off_s`, `resumen_cada_s`

## Activos e aislamiento

Adaptador: `whitegas.WhiteGasKinichDetector`.
Código de referencia SHA256: `5dbf58d518287a63a9fd656d631d88386d99ca80d37979ae3b2a3545f28b6b6b`.
Variables de activos/rutas detectadas estáticamente: `KINICH_GAS_CKPT`, `KINICH_GAS_COMFY`, `KINICH_GAS_FPS`.
Símbolos globales del módulo inventariados: `_AQUI`, `_COMFY`, `CKPT_PATH`, `DEFAULT_PROMPT`, `DEFAULT_THRESHOLD`, `INTERNAL_RESOLUTION`, `WHITEGAS_NUM_RUNTIMES`, `MASK_COLOR_BGR`, `MASK_ALPHA`, `BBOX_COLOR_BGR`, `_STATS_ENABLED`, `_STATS_LOG_LOCK`, `_stats_log_fh`, `log`, `TARGET_FPS`, `_PROMPT_CARGADO`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
