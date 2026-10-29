# Kinich Go / Python

Motor nuevo de control en **Go**; captura y servicios de modelos en **Python**. El repositorio implementa API `/v2`, SQLite nueva, credenciales scoped, fuentes cifradas, supervisión, leases, composiciones, admisión, horarios, eventos durables, replay/SSE, outbox y predicciones. No importa los managers ni la API anterior.

El diagnóstico `pulso` funciona sin CUDA. Los adaptadores y el código de las **14 analíticas** están incluidos con SHA de procedencia; requieren sus pesos, dependencias y perfiles medidos antes de habilitarse. `combo` se expresa con miembros de una composición.

## Arranque local

Requiere Go 1.27.3 y Python >=3.11. En este equipo el compilador se preparó en `/Users/rodrigo/.local/share/kinich-toolchain/go1.27.3/go/bin/go`.

```sh
cd /Users/rodrigo/Documents/kinich-go
export PATH="/Users/rodrigo/.local/share/kinich-toolchain/go1.27.3/go/bin:$PATH"
make build
./bin/kinichd -command init -environment development -tenant local
./bin/kinichd -allow-synthetic
```

`init` crea `.state/master.key`, la base y una credencial en `.state/credential-development-local.json`, todos privados. El secreto no aparece en logs. El servidor escucha en `127.0.0.1:8090`. La fuente `synthetic://camera` sólo está permitida al activar `-allow-synthetic`.

Para usar RTSP, instalar OpenCV en el entorno Python elegido y pasar `-python /ruta/venv/bin/python`. Para modelos reales, seguir [workers y modelos](docs/06-workers-modelos.md) y [capacidad](docs/08-capacidad-cuotas.md).

## Verificación

```sh
make check
make long        # 181 segundos de ejecución continua
make cycles      # 100 arranques/paradas
make check-cv CV_PYTHON=/ruta/python-con-opencv
```

[Resultados y límites](docs/13-pruebas-aceptacion.md), [seguimiento Z00–Z11](docs/IMPLEMENTACION.md), [matriz de aceptación](docs/matriz-aceptacion.csv), [OpenAPI](docs/openapi.json), [instalación y restauración](docs/14-instalacion-operacion.md).

## Estado de entrega

Implementación local verificable. La aceptación productiva completa del plan sigue pendiente: paridad de las 14 analíticas con clips etiquetados y pesos autorizados, mediciones en GPU/MIG objetivo, integración del receptor VMS, ensayo de 24 h, canario y retorno. Los ensayos sintéticos no acreditan precisión ni capacidad de GPU.
