# Eventos, replay y entregas

Go reconstruye owner/camera/execution/worker/generation desde el vínculo de proceso. El plugin aporta member/generation/model, reloj/geometría y resultado; no el destino. Evento y entrega elegible se confirman juntos. El ACK se emite después de commit. Una repetición del UUID/cuerpo confirmado conserva ACK incluso tras retirar la sesión; cambiar contenido produce conflicto.

La secuencia SQLite es creciente; puede tener huecos de otros tenants. El cursor HMAC incorpora propietario y filtro de ejecución. Replay/SSE consulta siempre eventos persistidos con sequence > cursor, evitando un hueco al pasar a vivo. La conexión sólo mantiene lotes de hasta 100 eventos y tiene escritura acotada. Overlay es descartable y no borra historial; devuelve available=false si la geometría/generación ya no corresponde o tiene más de 5s.

Los callbacks conservan URL/token/revisión al crear la entrega; rotar el destino no reenvía backlog a otro propietario. HTTPS por defecto, sin proxy del entorno ni redirects. Se resuelve y valida cada dirección en cada conexión; loopback/link-local/metadata/redes especiales se rechazan. LAN requiere CIDR explícito, y HTTP requiere flag separado.

2xx confirma entrega; 408/429/5xx/fallo de red reintentan con backoff exponencial, hasta 12 intentos. Otros 3xx/4xx son dead-letter. Reintento administrativo conserva evento y revisión del destino. Los intentos guardan status/fecha sin respuesta ni token.

Retención de eventos: 30 días, lotes de 1000/hora. Se preservan eventos con entrega pendiente o dead-letter. Un cursor anterior al piso purgado devuelve 410 y requiere replay nuevo. Operaciones, auditoría e intenciones no se purgan automáticamente. Controlar disco y dimensionar esa política antes de producción.
