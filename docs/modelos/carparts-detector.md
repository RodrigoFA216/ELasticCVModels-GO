# carparts-detector v4.0

Detección de robo de autopartes en CCTV (YOLO track + ancla + CLIP anomaly)

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: ROBO, ROBO_OFF.
- Cadencia máxima declarada de referencia: 20.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`alert_threshold`, `segment_len`

## Activos e aislamiento

Adaptador: `carparts.CarPartsDetector`.
Código de referencia SHA256: `bd52aabfc6543dbe8b01ec078cc7776be6f0a1d369610d5b671d14e10e180c13`.
Variables de activos/rutas detectadas estáticamente: `KINICH_CARPARTS_CLIP_MODEL`, `KINICH_CARPARTS_WEIGHTS`.
Símbolos globales del módulo inventariados: `log`, `DEVICE`, `WEIGHTS`, `CLIP_MODEL_NAME`, `TRACKER_YAML`, `CLASS_PERSON`, `CLASS_VEHICLES`, `REF_W`, `REF_H`, `CP_PERIMETRO_PX_REF`, `CP_PAD_SOLO_REF`, `CP_PAD_GRUPO_REF`, `CP_GRUPO_DIST_PX_REF`, `SEGMENT_LEN`, `KEY_FRAMES_K`, `GAMMA1`, `GAMMA2`, `GAMMA3`, `ALERT_THRESHOLD`, `SMOOTH_SIGMA`, `SMOOTH_HISTORY`, `TC_SIM_LOW`, `TC_SIM_HIGH`, `IMG_SIZE`, `WIN_SMALL`, `WIN_MID`, `WIN_LARGE`, `CROP_FREEZE_FRAMES`, `CROP_CONFIRM_FRAMES`, `CROP_GENEROUS_PAD_REF`, `CROP_MOVE_TH_PX_REF`, `CROP_MAX_W_FRAC`, `CROP_MAX_H_FRAC`, `ANCHOR_CONFIRM_FRAMES`, `ANCHOR_MISS_MAX`, `_MODEL_PARAMS`, `_DEFAULT_MODEL_PARAMS`, `KSM_QUERY`, `POS_PROMPTS`, `NEG_PROMPTS`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
