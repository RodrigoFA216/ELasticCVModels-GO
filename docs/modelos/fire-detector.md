# fire-detector v2.0

Detección de fuego y humo en CCTV (YOLO + CV clásico + CLIP temporal)

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: FUEGO, FUEGO_OFF, HUMO, HUMO_OFF.
- Cadencia máxima declarada de referencia: 10.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`detect_scale`, `fire_alert_threshold`, `fire_consec_min`

## Activos e aislamiento

Adaptador: `fire.FireSmokeDetector`.
Código de referencia SHA256: `a5a3f0aeab572016c025b4292e5a1cf686a510589213418ac4ccf14ea3b42de8`.
Variables de activos/rutas detectadas estáticamente: `KINICH_FIRE_CLIP_MODEL`, `KINICH_FIRE_WEIGHTS`.
Símbolos globales del módulo inventariados: `log`, `DEVICE`, `WEIGHTS`, `CLIP_MODEL_NAME`, `CLIP_CROP_SIZE`, `FIRE_REF_W`, `FIRE_HISTORY_LEN`, `FIRE_PRE_CROP_CONF`, `FIRE_ZOOM_CONF`, `FIRE_CROP_PAD_REF`, `FIRE_MASK_MIN_AREA_REF`, `FIRE_MOTION_THR`, `FIRE_MIN_BBOX_AREA_REF`, `FIRE_FLICKER_FRAMES`, `FIRE_FLICKER_MIN_STD`, `FIRE_FLICKER_SIZE`, `FIRE_FLICKER_RESET_DIST_REF`, `FIRE_SAT_PATCH_SIZE`, `FIRE_SAT_STD_MIN`, `FIRE_CLIP_BUFFER_SIZE`, `FIRE_KEY_FRAMES_K`, `FIRE_CONSEC_MIN`, `FIRE_TTL`, `FIRE_ALERT_THRESHOLD`, `FIRE_GAMMA1`, `FIRE_GAMMA2`, `FIRE_GAMMA3`, `FIRE_KF_SIGMOID_CENTER`, `FIRE_KF_SIGMOID_SCALE`, `FIRE_TC_SIM_LOW`, `FIRE_TC_SIM_HIGH`, `FIRE_SMOOTH_SIGMA`, `FIRE_SMOOTH_HISTORY`, `FIRE_IMG_SIZE`, `FIRE_WIN_SMALL`, `FIRE_WIN_MID`, `FIRE_WIN_LARGE`, `PATCH_GRID_COLS`, `PATCH_GRID_ROWS`, `PATCH_PX_RATIO_TH`, `PATCH_PAD_RATIO`, `PATCH_MOG2_HISTORY`, `PATCH_MOG2_THRESH`, `SMOKE_HSV_DARK_LO`, `SMOKE_HSV_DARK_HI`, `SMOKE_HSV_LIGHT_LO`, `SMOKE_HSV_LIGHT_HI`, `SMOKE_HISTORY_LEN`, `SMOKE_CLIP_BUFFER_SIZE`, `SMOKE_CLIP_STRIDE`, `SMOKE_CLIP_DIFF_TH`, `SMOKE_CLIP_MIN_HITS`, `SMOKE_CLIP_SET_SCORE_TH`, `SMOKE_CLIP_TOPK`, `SMOKE_CONSEC_MIN`, `SMOKE_TTL`, `FIRE_POS_PROMPTS`, `FIRE_NEG_PROMPTS`, `SMOKE_POS_PROMPTS`, `SMOKE_NEG_PROMPTS`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
