# parking-monitor v2.0

Ocupación de cajones de estacionamiento y verificación del vehículo registrado (YOLO + ResNet50 + color HSV + placa OCR)

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: OCUPADO, LIBRE, VEHICULO_OK, ALERTA_VEHICULO.
- Cadencia máxima declarada de referencia: 1.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`spots`, `spots_normalized`, `conf`, `iou_occ_thr`, `verify`, `feat_sim_thr`, `buffer_size`, `verify_every`

## Activos e aislamiento

Adaptador: `parking.ParkingMonitor`.
Código de referencia SHA256: `deece16a4befce8dfbbcc09273f968dc0a328eac825565eb8c41cdec18de3e5a`.
Variables de activos/rutas detectadas estáticamente: `KINICH_PARKING_PLATE_WEIGHTS`, `KINICH_PARKING_WEIGHTS`.
Símbolos globales del módulo inventariados: `log`, `DEVICE`, `WEIGHTS`, `PLATE_WEIGHTS`, `VEHICLE_CLS`, `PLATE_CONF`, `YOLO_CONF`, `IOU_OCC_THR`, `ROI_PAD`, `BUFFER_SIZE`, `FEAT_SIM_THR`, `COLOR_THR`, `PLATE_THR`, `VERIFY_EVERY`, `CAR_CROP_PAD`, `_COLOR_HSV`, `_PLATE_RE`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
