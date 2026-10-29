# Estado de construcción — 2026-10-21

El repositorio es una implementación local nueva en Go con servicios de modelos en Python. No se marca el plan productivo como terminado.

| Bloque | Estado | Evidencia / pendiente principal |
|---|---|---|
| KGC-Z00 | PARCIAL | docs/00-alcance-referencia.md; models/manifest/provenance.json. Faltan lectura productiva actual y clips/activos aprobados |
| KGC-Z01 | IMPLEMENTADO_LOCAL | scripts/check_contracts.py; worker/tests/test_runtime.py. Transporte NDJSON documentado; proto binario sólo esquema de identidad |
| KGC-Z02 | IMPLEMENTADO_LOCAL | TestOwnershipIdempotencyAndRevisions; TestAuthAndEncryptedSecrets; TestSSERevocationAndScope. Aceptación productiva requiere política de credenciales del BFF |
| KGC-Z03 | IMPLEMENTADO_LOCAL | TestRollbackBackupAndRestart; TestQuotaUnknownProfilesAndForeignKeys; TestCredentialCreationIsIdempotentWithoutPersistingSecret. Restauración local en directorio vacío; falta ensayo por otro operador |
| KGC-Z04 | PARCIAL | TestLostEventACKAndProcessCrash; TestUnconfirmedStopKeepsReservation; worker/tests/test_runtime.py. Falta adaptación/validación semántica real de las 14 analíticas y SIGKILL del controlador en host objetivo |
| KGC-Z05 | PARCIAL | TestContinuousWorkerCompositionAndDisconnect; TestSchedulesDSTAndDisabledOccurrences; TestLongContinuous. Falta piloto RTSP/NVR real con dos tenants; pérdida de lectura finaliza sesión con fallo explícito |
| KGC-Z06 | PARCIAL | TestGateFairnessAndOwnership; TestQuotaUnknownProfilesAndForeignKeys. Falta perfil GPU/MIG real, telemetría física y FPS útil por escena |
| KGC-Z07 | PARCIAL | TestContinuousWorkerCompositionAndDisconnect. Batch=1; sustitución estructural por miembro. Faltan paridad y costo SAM/EPP reales |
| KGC-Z08 | IMPLEMENTADO_LOCAL | TestEventsOutboxAtomicityReplayAndFencing; TestOutboxRetryKeepsPayloadAndDestination; TestRetentionExpiresCursorAndPreservesPendingDelivery; internal/events/security_test.go. Callback probado con transporte controlado; falta receptor real aprobado y banco DNS/red |
| KGC-Z09 | PARCIAL | internal/integrations/vms; docs/12-vms-alertas-grabaciones.md. VMS existente no se modificó ni desplegó; faltan alerta/evidencia/grabación reales |
| KGC-Z10 | PARCIAL | TestLongContinuous; TestHundredCycles; docs/evidencia. Faltan 24h, analíticas reales, carga CUDA y certificación SLO |
| KGC-Z11 | PARCIAL | scripts/release.sh; scripts/smoke.py; packaging/kinich.service. Faltan instalación destino, canario, retorno y aceptación de otro operador |

La matriz CSV contiene todos los AC/CL del plan. IMPLEMENTADO_LOCAL no equivale a VALIDADO_QA ni DESPLEGADO.

## Verificaciones ejecutadas

- `make check`: go vet, Go con detector de carreras, nueve pruebas Python y comprobación de contratos/procedencia.
- Proceso Python real: composición, límites globales, aislamiento, ACK perdido, SIGKILL y stop bloqueado.
- Ejecución continua de 181s y 181 eventos; 100 ciclos sin reservas finales.
- Imagen/clip con OpenCV y plugin CPU sintético; no prueba precisión de EPP ni otro detector.
- Binario y smoke HTTP desde base vacía verificados en macOS. El binario Linux amd64 se comprueba por compilación; ejecución Linux/CUDA pendiente.
- Los archivos de `docs/evidencia` conservan resultados sin secretos. El código se entrega local, sin push ni despliegue.
