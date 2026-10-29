# queue-cashier v1.0

Filas y cajero CRUZADOS por carril en la misma cámara: espera/servicio/deserción/aforo por carril + presencia e identidad del cajero; un cliente en caja sin cajero no cuenta como atendido

## Contrato

- Capacidades: stream.
- Eventos conservados: DESERCION, AFORO_EXCEDIDO, AFORO_EXCEDIDO_OFF, EPISODIO, CAJA_DESATENDIDA, CAJA_DESATENDIDA_OFF, PERSONA_NO_CAJERO, PERSONA_NO_CAJERO_OFF, PERSONA_NO_RECONOCIDA, PERSONA_NO_RECONOCIDA_OFF, RESUMEN.
- Cadencia máxima declarada de referencia: 10.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`zones`, `zones_normalized`, `capacidad_caja`, `min_fila`, `min_caja`, `gracia`, `limite_aforo`, `conf`, `tolerance`, `rise_seconds`, `person_conf`, `position_primary`, `face_required`, `alert_unknown`, `resumen_cada_s`

## Activos e aislamiento

Adaptador: `queue_cashier.QueueCashierDetector`.
Código de referencia SHA256: `28e7f3f88e06e595b70c74e6ae25710a54290f683e574b84027451ad04e39ee3`.
Variables de activos/rutas detectadas estáticamente: `KINICH_COPPEL_GALLERY`, `KINICH_COPPEL_REID`, `KINICH_COPPEL_RESULTADOS`, `KINICH_COPPEL_YOLO`, `KINICH_QC_FPS`.
Símbolos globales del módulo inventariados: `BYTETRACK_QUEUE_YAML`, `TIPOS`, `TIPOS_POR_DETECTOR`, `LEGACY_POR_POSICION`, `_zonas`, `FACE_MIN_CROP`, `FACE_DET_SCORE`, `_engine`, `_engine_lock`, `_g1_sin_definir`, `HERE`, `V4_DIR`, `EMPLEADOS_DIR`, `ZONES_WEB_DIR`, `PERSON_CONF`, `PERSON_CONF_RUN`, `PERSON_IOU`, `PERSON_IMGSZ`, `YOLO_FPS`, `KPT_MIN_MEAN`, `ROI_DETECT_MARGIN_FRAC`, `TRACK_DEATH_S`, `GHOST_HIDE_S`, `STRICT_ROI`, `ROI_OVERLAP_FRAC`, `REID_INPUT`, `REID_MEAN`, `REID_STD`, `REID_CENTER_WARMUP`, `GALLERY_SIZE`, `ENRICH_EVERY_S`, `POOL_TTL_S`, `POOL_MAX`, `DB_MATCH_TH`, `DB_CHECK_COOLDOWN_S`, `DB_MAX_CHECKS`, `REID_MODES`, `FEAT_BUFFER_MAX`, `MIN_FRACTION`, `GRAY_ZONE_FACTOR`, `FACE_RECHECK_S`, `FACE_REVOKE_TH`, `FACE_DISAGREE_N`, `FACE_GRACE_S`, `FACE_EVIDENCE_TTL`, `ENROLL_K`, `ENROLL_SAMPLE_FPS`, `ENROLL_COHERENCE_TH`, `FACE_COHERENCE_TH`, `ENROLL_MIN_VIEWS`, `_STATE_STYLE`, `_TRACK_COLOR`, `_STATE_CORTO`, `_PRIORIDAD_ESTADO`, `_reid_registry_lock`, `_reid_engines`, `_AQUI`, `_AQUI_PATH`, `_BASE`, `HERE`, `_TIPOS_FILA`, `_SUMABLES`, `adaptador`, `YOLO_WEIGHTS`, `REID_WEIGHTS`, `GALLERY_DIR`, `RESULTADOS_DIR`, `TIPOS_FILA`, `log`, `TARGET_FPS`, `KNOBS`, `CONFIG_SCHEMA`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
