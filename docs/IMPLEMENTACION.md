# Ejecución del plan KGC

Fuente: Kinich_Plan_Construccion_Desde_Cero_Go_2026-10-15.md. Inicio: 2026-10-21, America/Mexico_City.

## Secuencia y entregables

1. Z00: congelar catálogo de 16 entradas y procedencia local; inventariar los 14 plugins, estado global, dependencias y casos pendientes. Mantener el contrato VMS del 08/10 como referencia histórica.
2. Z01: módulo Go, runtime Python independiente, protocolo local versionado con límites, API /v2 y pruebas sin CUDA.
3. Z02–Z03: credenciales scoped con hash, cifrado de fuentes/tokens, SQLite nueva, relaciones compuestas, auditoría, revisiones e idempotencia transaccional y recuperación.
4. Z04–Z05: supervisor por cámara, identidad/generación, leases monotónicos, cierre comprobado, captura única, reconciliación, disconnect persistente y horarios con zona IANA.
5. Z06–Z07: reservas antes del warmup, cuotas por tenant, cola con reparto, composición mutable por miembro, buffers acotados y un trabajo por miembro. Batch=1.
6. Z08: eventos con ACK posterior a commit, outbox persistente, reintentos, replay/SSE y overlay autorizados, predicciones con la misma admisión y métricas sin secretos.
7. Z09: cliente Go VMS de servidor y traducción de eventos con identidad estable; no modificar otros repositorios ni atribuir eventos por RTSP.
8. Z10: ejecutar pruebas Go con detector de carreras, Python, integración de procesos, reinicio, aislamiento y ciclos. Registrar evidencia reproducible y límites.
9. Z11: comandos de instalación, respaldo/restauración, release local y manual de canario/retorno. Despliegue y aceptación de otra persona requieren entorno real.

## Criterio de avance

Implementado significa que existe código y prueba local. Validado QA de modelos exige pesos/versiones/clips positivos, negativos y transiciones aprobadas. Desplegado exige evidencia del entorno. Ninguna etapa recibe esos estados por generación de código.

## Decisiones iniciales

- HTTP y procesos con biblioteca estándar Go; SQLite con driver puro Go.
- IPC NDJSON v1 por pipes privados, mensajes <=1 MiB, sin fuentes en argv ni endpoint Python en LAN.
- Un worker por cámara/ejecución: dominio de fallo menor y costos residentes por proceso. Toda capacidad real requiere medir este perfil.
- Catálogo y adaptadores Python presentes; perfiles reales no medidos se rechazan. Pulso es diagnóstico explícito.
- No ejecutar ni migrar producción durante esta construcción local.
