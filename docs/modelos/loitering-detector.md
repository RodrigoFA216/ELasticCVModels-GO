# loitering-detector v2.0

Detección de merodeo en CCTV (YOLO track + reglas espacio-temporales por trayectoria + RF)

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: MERODEO, MERODEO_OFF.
- Cadencia máxima declarada de referencia: 15.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`detect_scale`, `profile`, `loitering_threshold`, `activation_threshold`, `min_frames_activate`, `min_frames_deactivate`, `trajectory_points`, `stride_frames`, `min_walking_distance`, `anchor_radius`, `min_stationary_area`, `use_advanced_rules`, `yolo_conf`, `yolo_iou`

## Activos e aislamiento

Adaptador: `loitering.LoiteringDetector`.
Código de referencia SHA256: `76dd8a4f41e0d692d57a62cc11ff6852c5ff2e23035afe98c0a16e39a13f9d08`.
Variables de activos/rutas detectadas estáticamente: `KINICH_LOITERING_RF`, `KINICH_LOITERING_WEIGHTS`.
Símbolos globales del módulo inventariados: `log`, `WEIGHTS`, `RF_PATH`, `TRACKER_YAML`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
