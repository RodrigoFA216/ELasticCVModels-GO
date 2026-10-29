# Persistencia y transacciones

SQLite nuevo: WAL, synchronous=FULL, foreign_keys=ON, busy_timeout=5000, una conexión escritora y lock de controlador. La migración inicial vive en `internal/store/schema.sql`. El esquema registra tenants, credenciales, cámaras, operaciones, ejecuciones, reservas, workers, eventos, destinos, entregas/intentos, horarios/ocurrencias, medios cifrados, retención y auditoría.

Cámara, ejecución, reserva, evento y entrega usan FK compuestas con entorno/tenant. Un índice parcial admite un solo productor activo por cámara. El mismo comando persiste efecto, operación y auditoría juntos; un error revierte todo. Las reservas se confirman antes de enviar `open`.

Fuentes, tokens recuperables e imágenes/clips se cifran con AES-256-GCM y entorno/tenant como AAD. La clave de 32 bytes está en master.key, fuera de SQLite; permisos 0600. Las credenciales de control sólo se almacenan como hash. Los medios temporales se eliminan después del cierre confirmado.

`-command backup -backup-path /ruta/nueva.sqlite` usa VACUUM INTO y requiere controlador detenido por el lock exclusivo. Respaldar master.key en almacén seguro separado. Restaurar ambos en directorio nuevo, ejecutar integrity y arrancar para reconciliar; nunca sobrescribir un almacén con eventos nuevos durante retorno. La suite restaura, descifra y conserva una cámara deshabilitada.
