# Arquitectura

`cmd/kinichd` conecta paquetes `internal/auth`, `store`, `api`, `reconcile`, `admission`, `events`, `schedule`, `supervisor` e `integrations/vms`. HTTP, procesos, contextos y sincronización usan biblioteca estándar. SQLite y JSON Schema son las dos dependencias directas.

Un controlador posee el archivo SQLite mediante un lock exclusivo. Un worker corresponde a una cámara/ejecución; la propiedad está fijada al arrancarlo. Los miembros comparten un decode, con sesión y generación propias. Los costos residentes se contabilizan por miembro y por proceso, conservadoramente.

El flujo es intención transaccional → cola → reserva → proceso registrado → handshake → apertura → primer frame → estado running. Una detección es una dimensión distinta. Stop retira resultados antes de limpiar; el terminal y la liberación ocurren tras verificar salida del grupo de procesos.

La elección de worker por cámara reduce el dominio de fallo frente al worker por tenant del esquema inicial. Requiere medir su mayor duplicación de pesos/contextos. Se distribuyen permisos de inferencia globales entre tenants; no hay batch compartido ni múltiples controladores.
