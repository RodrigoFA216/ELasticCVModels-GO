# Alcance y referencia

Proyecto nuevo en `Documents/kinich-go`. El plan original se conserva en `reference/plan-original.md` sin modificar el documento del usuario. No se migró SQLite legacy ni se tocaron otros repositorios.

El catálogo de referencia contiene 14 analíticas, `pulso` y `combo`. Proviene del fixture VMS de 2026-10-09 contrastado por el plan del 15/10. El código de inferencia proviene de la referencia local del 15/10, **no de una nueva lectura productiva del 21/10**. Cada archivo reutilizado tiene SHA en `models/manifest/provenance.json`.

Responsabilidades: Go autoriza, persiste, admite y supervisa. Python captura y ejecuta modelos. VMS conserva usuarios, sucursales, alertas y medios. El navegador consume VMS/BFF; no recibe llaves ni RTSP.

Fuera de aceptación local: pesos y clips etiquetados autorizados, SAM/ComfyUI del despliegue, paridad real, banco CUDA/MIG, receptor VMS v2 desplegado y ensayo de 24h. Los bloqueos por criterio están en `matriz-aceptacion.csv`.
