# fardeo-detector v2.0

Detección de fardeo (hurto en retail) — YOLO-pose + heurística mano-ROI + X-CLIP windowed

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: FARDEO, FARDEO_OFF.
- Cadencia máxima declarada de referencia: 24.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`regions`, `fardeo_alert_threshold`, `proximity_px`

## Activos e aislamiento

Adaptador: `fardeo.FardeoDetector`.
Código de referencia SHA256: `9585b100b3c38ce55d05ddbfb99e7b658623d7f272ea9c2b00fce40f3f88d027`.
Variables de activos/rutas detectadas estáticamente: `KINICH_FARDEO_WEIGHTS`, `KINICH_FARDEO_XCLIP_MODEL`.
Símbolos globales del módulo inventariados: `log`, `DEVICE`, `WEIGHTS`, `XCLIP_MODEL_ID`, `XCLIP_NUM_FRAMES`, `SEG_XCLIP`, `HALF`, `WINDOW_POST`, `WINDOW_WEIGHTS`, `WINDOW_TIMEOUT`, `MIN_PRE_FRAMES`, `MAX_PRE_FRAMES`, `PROXIMITY_PX`, `CROP_PAD`, `KP_CONF`, `KF_SIGMOID_CENTER`, `KF_SIGMOID_SCALE`, `ALERT_THRESHOLD`, `SMOOTH_SIGMA`, `SMOOTH_HISTORY`, `FARDEO_TTL`, `POS_PROMPTS`, `NEG_PROMPTS`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
