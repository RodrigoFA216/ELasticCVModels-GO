# Instalación y operación

## Instalación limpia

Compilar con Go 1.27.3 (`make build`). Python base no requiere dependencias para synthetic/pulso; OpenCV es necesario para RTSP/medios. Los extras reales de worker/pyproject.toml requieren Python 3.12 y un lock propio del perfil CUDA. No se incluye un lock GPU inventado ni pesos.

Ejecutar `kinichd -command init -state /directorio/privado -environment qa -tenant tenant-a`. La credencial inicial se guarda en archivo 0600, no en stdout. Arrancar con las mismas rutas y `-worker-dir /ruta/worker`. Escucha loopback por defecto. TLS y acceso de BFF se configuran en el despliegue interno.

Agregar tenant con controlador detenido: `-command tenant -environment qa -tenant tenant-b`. No puede sobrescribir uno existente. Las cuotas iniciales son 64 cámaras, 4 ejecuciones, 64 pendientes y memory-mib de despliegue. Los tokens de uso normal se rotan por API sin detener control.

## Diagnóstico y cierre

Consultar operación y ejecución por UUID. Pending/stopping no implica salida. Ver capacity para reservas y deliveries para receptor. No liberar una reserva manualmente porque una señal o HTTP respondió. Si el worker anterior ya desapareció, el reconciliador confirma cleanup; si PID/identidad es incierto, conservar pendiente e inspeccionar el dominio de procesos en el host.

El ejemplo systemd en packaging no se instala automáticamente. KillMode=control-group y TimeoutStopSec=20 aseguran dominio de hijos al reiniciar en Linux. Aprovisionar usuario, rutas de escritura, pesos y acceso al dispositivo según perfil. No correr dos controladores con la misma base ni añadir réplicas SQLite.

## Respaldo y restauración

Detener el servicio y verificar hijos cerrados. Crear SQLite nuevo con `-command backup -backup-path /seguro/nuevo.sqlite`. Guardar master.key por separado en un vault/respaldo privado. Copiar base y clave a directorio nuevo como kinich.sqlite y master.key, permisos 0600, ejecutar `-command integrity` y arrancar para reconciliar. Probar lectura, cámara disabled y replay antes del corte. Los pesos y contadores model-state requieren respaldo de su directorio por separado.

## Release

`make release` produce tar local con binario, runtime, contratos, catálogo, documentación y manifest SHA. Excluye .state, secretos y pesos. El paquete local macOS no es una imagen CUDA ni una instalación productiva certificada. Para Linux, compilar con GOOS/GOARCH adecuados y validar en el destino.
