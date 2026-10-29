# Composición, cadencia y batches

La cámara contiene una composición de 1–16 miembros con ID propio, modelo/version/config, FPS y generación. Combo es esta estructura, no otro detector que anida productores. Se permiten duplicados de modelos con IDs y estado separados; globals incompatibles se rechazan antes del commit.

Cada miembro procesa el frame más reciente con un hilo propio, una inferencia en vuelo y FPS dentro del catálogo. No existe una cola de frames que crezca con un modelo lento. Cada resultado lleva miembro y generación; Go comprueba que sigan vigentes.

AB→ABC→AC conserva objetos y generaciones de A/C. Cambiar config/FPS de un miembro es estructural: la nueva sesión sustituye sólo a ese miembro. No se afirma cambio caliente semántico de un plugin que aún no lo valida. Durante preparing o una mutación aún no observada se devuelve conflicto.

El commit retira B antes de pedir cleanup. Un fallo de carga conserva estado de control explícito y termina el dominio afectado; la operación no se declara aplicada silenciosamente. La reserva de solape persiste hasta confirmación del worker. Batch=1: no hay una promesa de velocidad por agrupar modelos.

Los núcleos globales SAM/EPP, queue-cashier y corridor-counter se restringen a un miembro global por cámara. Sus cambios activos se rechazan con runtime_restart_required: detener y crear ejecución nueva evita reutilizar un prompt/galería global anterior. Esta restricción explícita debe levantarse sólo con aislamiento/adaptación y pruebas reales del plugin.
