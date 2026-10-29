# robbery-detector v3.0

Detección de robo con violencia en CCTV (tracker YOLO + heurísticas de par + CLIP temporal V3)

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: ROBO, ROBO_OFF.
- Cadencia máxima declarada de referencia: 10.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`detect_scale`, `robbery_alert_threshold`, `robbery_consec_min`, `area_variation_thr`

## Activos e aislamiento

Adaptador: `robbery.RobberyDetector`.
Código de referencia SHA256: `2c84e841cbe7977b59d37973ebe3e10ca725bd3736f7e1d47c4d264cc8c03f06`.
Variables de activos/rutas detectadas estáticamente: `KINICH_ROBBERY_CLIP_MODEL`, `KINICH_ROBBERY_WEIGHTS`.
Símbolos globales del módulo inventariados: `log`, `DEVICE`, `WEIGHTS`, `CLIP_MODEL_NAME`, `PERSON_CONF`, `PAIR_CROP_SIZE`, `KEY_FRAMES_K`, `GAMMA1`, `GAMMA2`, `GAMMA3`, `ALERT_THRESHOLD`, `SMOOTH_SIGMA`, `SMOOTH_HISTORY`, `KF_SIGMOID_CENTER`, `KF_SIGMOID_SCALE`, `TC_SIM_LOW`, `TC_SIM_HIGH`, `IMG_SIZE`, `WIN_SMALL`, `WIN_MID`, `WIN_LARGE`, `BUFFER_SIZE`, `AREA_VARIATION_THR`, `AREA_HISTORY_LEN`, `PROX_SCALE_FACTOR`, `PROX_MIN_PX`, `PROX_MAX_PX`, `FRAME_PCT_CAP`, `CLOSING_RATIO_REF`, `CLOSING_RATIO_MIN`, `CLOSING_RATIO_MAX`, `CLOSING_HISTORY_LEN`, `MOTION_THR_FRAC`, `MOTION_THR_MIN`, `MOTION_HISTORY_LEN`, `ASYMMETRY_RATIO`, `SCALE_REF`, `EVENT_COOLDOWN`, `CROP_PAD_PX`, `ROBBERY_TTL`, `ROBBERY_CONSEC_MIN`, `SCORE_DECAY`, `ROBBERY_POS_PROMPTS`, `ROBBERY_NEG_PROMPTS`, `ROBBERY_KSM_QUERY`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
