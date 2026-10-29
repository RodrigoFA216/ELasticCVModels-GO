# API /v2

Contrato de rutas y entradas en `openapi.json`. Autenticación `Authorization: Bearer kg_…`. Los campos environment/tenant de recursos son de salida; las entradas no eligen dueño. Recursos ajenos devuelven 404. Credencial inválida: 401; scope insuficiente: 403.

Las mutaciones requieren `Idempotency-Key` de 8–120 caracteres. La identidad se conserva dentro del entorno/tenant; reutilizar la clave con método, ruta, revisión o cuerpo diferente produce 409. La comparación del JSON es canónica por objetos. Las operaciones se conservan sin purga automática en esta versión.

Cambiar cámara, composición, miembro, disconnect/reactivate u horario requiere `If-Match: "revision"`. 428 si falta, 412 si está obsoleta. La clave debe permanecer igual al reintentar una operación cuyo resultado HTTP se perdió.

Las mutaciones devuelven una operación con `resource_id`, `state`, `request_id` y resultado. 202 acredita intención durable. Consultar `/v2/operations/{id}` y `/v2/executions/{id}`. La creación de credenciales devuelve `{operation,key}`: el secreto sólo aparece la primera vez; un replay devuelve key vacío.

Los horarios PATCH reciben una configuración completa. DELETE desactiva y conserva ocurrencias históricas. La fuente sólo se recibe en alta/rotación y no aparece en respuestas. Una predicción recibe base64 <=400 KiB; imagen/clip debe ser una capacidad declarada por el modelo. Los clips se limitan a 60s/1800 frames.
