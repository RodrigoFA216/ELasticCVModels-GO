# Cámaras, estados y horarios

UUID y propietario son inmutables. Code es visible y único por tenant/entorno; la URL no es identidad. Enabled se persiste. La revisión cambia al editar fuente/configuración/habilitación.

Ejecución: queued → preparing → running → stopping → completed/cancelled/failed/expired. Queued puede terminar sin abrir procesos. queue_seconds sólo vence la espera inicial: una cámara registrada no tiene límite de tres minutos. El ensayo local mantuvo 181s continuos.

Disconnect cambia enabled=false y retira generaciones en la misma transacción; stop sólo retira esa ejecución. Reactivate permite solicitudes nuevas y ocurrencias futuras; no revive IDs. La rotación de fuente retira la ejecución anterior para evitar mezclar geometrías/relojes.

Captura OpenCV conserva dimensiones de origen, transformación identidad, reloj de medio y capturedAt UTC. No hay resize oculto. Fuentes que no abren tienen reintentos acotados de apertura y fallo local; la pérdida de lectura finaliza con limpieza y se puede iniciar otra ejecución. La disponibilidad de una cámara no determina readyz global.

Horarios diarios con zona IANA, días 0–6 y política skip. La ocurrencia usa fecha/hora local: en un fold DST se ejecuta una vez; una hora inexistente se omite. No se reproduce backlog después de reinicio. Disabled y solapamiento producen ocurrencia omitida, sin nueva ejecución.
