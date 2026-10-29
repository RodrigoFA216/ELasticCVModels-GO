# fight-detector v1.0

Detección de peleas en CCTV (YOLO track + zonas por par + CLIP mosaico temporal)

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: FIGHT, FIGHT_OFF.
- Cadencia máxima declarada de referencia: 20.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`fight_set_score_th`, `fight_consec_min`, `zone_max`, `fight_min_movement`

## Activos e aislamiento

Adaptador: `fight.FightDetector`.
Código de referencia SHA256: `de82b795de67198d4e2e9ab5a4e315581a75086bc84338328777c8e914f3ba8a`.
Variables de activos/rutas detectadas estáticamente: `KINICH_FIGHT_CLIP_MODEL`, `KINICH_FIGHT_WEIGHTS`.
Símbolos globales del módulo inventariados: `log`, `DEVICE`, `WEIGHTS`, `CLIP_MODEL_NAME`, `TRACKER_YAML`, `CLASS_PERSON`, `F_DIST_MAX_LIMIT`, `F_MIN_IOU_CRITICAL`, `F_STICKY_FRAMES`, `F_PAD_PIXELS`, `F_PAD_CLOSE`, `F_MIN_MOVEMENT_FOR_CLIP`, `F_BUFFER_SIZE`, `F_SET_SCORE_TH`, `F_CONSEC_MIN`, `F_SMOOTH_FACTOR`, `F_FAST_TRACK_TH`, `KSM_SELECT`, `CLIP_FRAME_SIZE`, `MOSAIC_COLS`, `MOSAIC_ROWS`, `CROWD_PERSON_TH`, `ZONE_MAX`, `ZONE_IDLE_MAX`, `ZONE_MIN_PAIR_SCORE`, `STAB_EMA_ALPHA`, `STAB_IOU_TH`, `STAB_PENDING_MAX`, `FIGHT_POS_PROMPTS`, `FIGHT_NEG_PROMPTS`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
