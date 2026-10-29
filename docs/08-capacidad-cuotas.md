# Capacidad y cuotas

Presupuesto global `-memory-mib`, límite `-max-workers` y cuotas por tenant de cámaras, cola, ejecuciones y memoria. El CLI tenant crea alcance propio; no se permite escoger tenant por header o body. La admisión es transaccional, anterior a warmup.

Costo conservador por ejecución: suma por miembro de residente + sesión + pico warmup. No se descuenta memoria por compartir nombre de modelo: cada miembro tiene instancia Python. La modificación activa reserva costo anterior + nuevo hasta ACK configured, luego reduce al nuevo. Si no cabe el solape, se rechaza y conserva íntegra la composición anterior.

Dos permisos globales de forward en vuelo. El gate reparte primero entre tenants, con una petición por miembro; el worker no ejecuta otro forward sin permiso. La cola inicial rota el tenant atendido. Reservas stopping permanecen ocupadas. El permiso de un proceso muerto sólo se devuelve después de confirmar salida.

Pulso usa un presupuesto sintético CPU de 16 MiB por sesión para probar contabilidad; no es una medición de VRAM. No existe perfil productivo certificado. RAM de decode, contextos, fragmentación y escenas densas deben incluirse en el perfil medido antes de habilitar modelos reales. Esta primera instalación no autoescala GPUs ni comparte batch entre tenants.
