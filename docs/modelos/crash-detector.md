# crash-detector v1.0

Detección de choques vehiculares en CCTV (YOLO track + heurísticas de proximidad + CLIP temporal)

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: CHOQUE, CHOQUE_OFF.
- Cadencia máxima declarada de referencia: 10.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`detect_scale`, `crash_alert_threshold`, `trigger_frames`, `vehicle_min_conf`

## Activos e aislamiento

Adaptador: `crash.CrashDetector`.
Código de referencia SHA256: `400e1f2f386d629abc3ba52cb808411be122dc4b846e7aae3a3162744d3e7139`.
Variables de activos/rutas detectadas estáticamente: `KINICH_CRASH_CLIP_MODEL`, `KINICH_CRASH_WEIGHTS`.
Símbolos globales del módulo inventariados: `log`, `DEVICE`, `WEIGHTS`, `CLIP_MODEL_NAME`, `TRACKER_YAML`, `_VEHICLE_CLASSES`, `_VEHICLE_MIN_CONF`, `_ALL_TRACK_CLASSES`, `C_CAR_PAD_PX`, `C_TRIGGER_FRAMES`, `C_BUFFER_SIZE`, `C_MOTION_THRESHOLD`, `C_CAR_PAIR_IOU_THR`, `C_STATIONARY_THR_FRAMES`, `C_REAR_ANGLE_TOL`, `C_REAR_DIST_MULT`, `C_CRASH_TTL`, `C_CRASH_MAX_FRAMES`, `C_CLOSING_HISTORY`, `C_CLOSING_DROP_RATIO`, `C_MOTION_HISTORY_LEN`, `C_CLOSING_MIN_FRAMES`, `KEY_FRAMES_K`, `GAMMA1`, `GAMMA2`, `GAMMA3`, `ALERT_THRESHOLD`, `SMOOTH_SIGMA`, `SMOOTH_HISTORY`, `KF_SIGMOID_CENTER`, `KF_SIGMOID_SCALE`, `TC_SIM_LOW`, `TC_SIM_HIGH`, `IMG_SIZE`, `WIN_SMALL`, `WIN_MID`, `WIN_LARGE`, `CRASH_POS_PROMPTS`, `CRASH_NEG_PROMPTS`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
