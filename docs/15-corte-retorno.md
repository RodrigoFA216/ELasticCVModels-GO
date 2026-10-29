# Corte y retorno operativo

1. Verificar propiedad de cada cámara por UUID/VMS; poner casos ambiguos en cuarentena. No importar por coincidencia de RTSP.
2. Validar release, pesos, perfil de capacidad, respaldo y receptor VMS v2 con dos tenants. Probar deduplicación e incompletos antes del canario.
3. Detener productor anterior de las cámaras elegidas, comprobar su cierre y registrar ventana de corte. Sólo después activar el productor nuevo.
4. Observar estado, eventos confirmados, entrega y grabación finalizada por separado. Si fallan SLO acordados, detener el productor nuevo y comprobar limpieza antes de volver al anterior.
5. Conservar eventos/medios nuevos y conciliar por identidades. No restaurar una copia vieja encima del almacén que ya recibió eventos nuevos. El histórico mantiene propietario original.

No se ejecutó un canario, importación ni retorno contra infraestructura existente desde esta construcción local. Requiere las evidencias de Z09/Z10 y participación del operador del VMS.
