# ppe-detector v1.1

PPE Detector — EPP por persona (casco/chaleco/botas/guantes x color), zero-shot

## Contrato

- Capacidades: image, stream, clip.
- Eventos conservados: EPP_FALTA, EPP_FALTA_OFF, TOLERANCIA, TOLERANCIA_OFF, CUMPLIMIENTO.
- Cadencia máxima declarada de referencia: 10.0 FPS; no es una medición del motor nuevo.
- Coordenadas: bbox en píxeles del frame con geometría/reloj explícitos. Cada sesión tiene member_id/generación propios.

## Configuración

Esquema completo en models/manifest/catalog.json. Parámetros declarados:

`casco`, `chaleco`, `botas`, `guantes`, `conf_person`, `zonas`, `zone_zoom`, `reid`, `reid_match_th`, `vote_window_s`, `refresh_s`, `alert_after_s`, `tol_pct`, `min_personas_pct`, `stats_every_s`, `color_min_frac`, `min_colored_frac`, `sat_min`, `val_min`

## Activos e aislamiento

Adaptador: `ppe_detector.PPEDetectorModel`.
Código de referencia SHA256: `5ea3891510020fceea018a3ec943f29827d20f1e15c780e5dcc6f16a8c5b5dec`.
Variables de activos/rutas detectadas estáticamente: `KINICH_PPE_COMFY`, `KINICH_PPE_REID`, `KINICH_PPE_SAM3_CKPT`, `KINICH_PPE_YOLO`.
Símbolos globales del módulo inventariados: `log`, `_AQUI`, `COMFY_DIR`, `SAM3_CKPT`, `YOLO_WEIGHTS`, `REID_WEIGHTS`, `CATALOGO`, `COLORES`, `_ITEM_SCHEMA`, `CONFIG_SCHEMA`, `_DEFAULT_ITEMS`, `_REID_MEAN`, `_REID_STD`, `_GALLERY_SIZE`.
El inventario es estático: constantes/pesos pueden ser inmutables, pero el núcleo SAM, prompts, galerías, trackers, caches y escritores requieren ensayo por sesión. Un proceso por cámara separa tenants; globals incompatibles en miembros de una misma cámara se rechazan.

## Validación pendiente

Estado: adaptador y código presentes; carga, paridad y rendimiento reales NO aceptados.
Faltan pesos/versiones/digests aprobados, dependencias runtime y clips etiquetados positivos, negativos y de transición. Reproducir referencia/nuevo con la misma entrada/reloj/config y tolerancias congeladas; comprobar secuencia, conteos, OFF, bbox y score. Medir warmup y escenas densas antes de publicar un perfil.
Cualquier backend ausente falla explícitamente; no se sustituye con pulso.
