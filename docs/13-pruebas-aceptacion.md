# Pruebas y aceptación

La suite Go ejecuta aislamiento entre tres tenants y otro entorno, comandos idempotentes, revisión concurrente, hash/cifrado, scopes, FK compuestas, rollback, respaldo/restauración, proceso Python real, composición AB→ABC→AC, fencing, ACK perdido tras commit, callback recuperable, gate justo, DST, stop bloqueado, revocación SSE y retención. Las pruebas Python cubren leases retirados, framing/límites/versiones, sesiones sintéticas separadas y presencia estática de las 14 fábricas.

Evidencia local del 21/10/2026:

- Ensayo continuo: 181s, 181 eventos confirmados con pulso.
- 100 ciclos: 100 arranques y paradas, cero reservas finales.
- Imagen y clip: runtime Python 3.12/OpenCV, plugin sintético CPU; el test habilita IMAGE sólo para el diagnóstico.
- Fuente, positivos/negativos/transiciones de detectores reales: **pendientes**.
- Carreras, contratos, binario y smoke: ver archivos de evidencia en `docs/evidencia` y `ESTADO.md`.

Los 181s y 100 ciclos no equivalen al ensayo de 24h ni a medición de fugas CUDA. No se certificó FPS/p95 de LAN, tolerancia de bboxes, mezcla de modelos ni capacidad de MIG. La matriz de criterios conserva cada limitación.

El arnés `scripts/compare_model_events.py` compara trazas de referencia y nuevo runtime con metadatos idénticos de modelo/peso/clip/config/precisión/shape/hardware. Exige tolerancias aprobadas de tiempo, score y bbox y campos semánticos extra explícitos; una diferencia entre verificando/cumpliendo falla. Produce paridad de traza, sin afirmar precisión. Aún faltan trazas reales etiquetadas de las 14 analíticas.
