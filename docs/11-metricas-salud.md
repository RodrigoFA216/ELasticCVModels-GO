# Métricas y salud

Healthz confirma el proceso HTTP. Readyz comprueba SQLite con timeout de un segundo; no cae por una RTSP o receptor. La API capacity informa cuota/reserva del propietario y hardware_certified=false. Workers sólo lista IDs/ejecuciones/estado del ámbito, sin PID, argumentos ni fuentes.

Metrics requiere admin y usa etiquetas de cardinalidad fija: solicitudes, errores y suscripciones. Entregas, ejecuciones, operaciones y auditoría permiten diagnosticar el dominio afectado. Un fallo de fuente se conserva como reason; un cierre incierto permanece stopping y retiene reserva.

No hay exportador DCGM/NVML certificado dentro de esta entrega. Antes de usar perfiles reales, integrar la observación del dispositivo y suspensión de admisión cuando su contabilidad sea incierta. El presupuesto local prueba autoridad de reservas; no acredita memoria física libre en CUDA.
