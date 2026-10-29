# Protocolo privado NDJSON v1

Transporte efectivo: un objeto JSON por línea en pipes heredados. Límite de línea: 1 MiB. El esquema ejecutable está en `proto/worker/v1/message.schema.json`. `worker.proto` sólo documenta identidad para una ampliación binaria; no se presenta como gRPC generado ni como transporte actual.

Go fija worker/execution/camera/generation antes del arranque. `hello` debe coincidir y negociar v1 en <=3s. No se abre fuente ni modelo antes de `open`. Fuente, imagen y configuración se envían por stdin; no por argv. stdout pertenece al protocolo, stderr de dependencias se descarta para evitar trazas con secretos.

`open` concede 6000ms. Go renueva cada 1500ms; el worker usa reloj monotónico, responde estado y retira para siempre la concesión expirada. El watchdog fuerza salida si vence. Las señales escalan stop → SIGTERM → SIGKILL dentro del grupo exacto. El supervisor verifica salida real, no el mensaje `closed`.

Cada forward pide `inference_request`. Go asigna un `permit` global con identidad de miembro/generación. `inference_done` devuelve el permiso; la salida comprobada también libera permisos retenidos. Solicitudes pendientes se reparten entre tenants. Un proceso detenido no libera memoria ni permisos por timeout optimista.

Cada evento conserva UUID y cuerpo hasta ACK posterior al commit. Hasta 128 eventos pendientes por worker; si se llena, se pausa la producción. Un ACK rechazado retira un evento de una generación ya inválida. El contrato durable comienza al persistir en Go.
