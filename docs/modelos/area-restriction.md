# area-restriction v1.0

Intrusión en zonas restringidas (YOLO + ByteTrack por zona + hit-test 3 reglas)

## Contrato

- Capacidades: stream, clip.
- Eventos conservados: INTRUSION, INTRUSION_OFF.
- Cadencia máxima declarada de referencia: 10.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`zones`, `polygon`, `scene`, `min_bbox_zone_ratio`, `zone_y_tolerance`, `conf`, `hit_point`, `exit_grace_frames`, `crop_pad`

## Activos e aislamiento

Adaptador: `area_restriction.AreaRestrictionDetector`.
Código de referencia SHA256: `b995a7ce6fea5823f09d3ded0a4fc85190cacfbd5aeff1bc00c1a38233b117cc`.
Variables de activos/rutas detectadas estáticamente: `KINICH_AREAREST_WEIGHTS`.
Símbolos globales del módulo inventariados: `log`, `WEIGHTS`, `TRACKER_YAML`, `DETECT_CONF`, `DETECT_CLASSES`, `HIT_POINT`, `SCENE_PROFILES`, `DEFAULT_SCENE`, `EXIT_GRACE_FRAMES`, `CROP_PAD`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
