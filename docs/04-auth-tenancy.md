# Autenticación y tenancy

El principal es `(environment, tenant_id, key_id, scopes)`. Scopes: read, control, events, predict, admin; `*` sólo se usa en bootstrap/administración explícita. Una llave no puede conceder scopes que no tiene.

Se generan 32 bytes criptográficos aleatorios y se almacena SHA-256 del token completo. Cada llave tiene caducidad y revocación persistentes. La consulta del hash no enumera tenants. Callback y control usan credenciales/audiencias distintas: el callback no empieza por kg_ y no se acepta para control.

REST, predicciones, SSE y administración usan el mismo middleware. SSE revalida antes de cada bloque y evento; el ensayo local verifica el corte después de revocar. Hay 64 suscripciones simultáneas como límite global y escrituras de 2s para consumidores lentos.

Rotación: emitir una credencial nueva con scopes mínimos, guardarla en el secreto del BFF, comprobarla y revocar el ID anterior con Idempotency-Key. No guardar tokens en navegador ni fuente en auditoría. La llave de cifrado del despliegue se respalda por separado.
