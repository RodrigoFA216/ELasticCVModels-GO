"""Verificación estática del contrato y procedencia; no importa CUDA."""
import ast
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
api = json.loads((root/'docs/openapi.json').read_text())
assert api['openapi'] == '3.1.0'
assert len(api['paths']) >= 25
catalog = json.loads((root/'models/manifest/catalog.json').read_text())
assert len(catalog) == 16
assert (root/'models/manifest/catalog.json').read_bytes() == (root/'internal/catalog/catalog.json').read_bytes()
for item in json.loads((root/'models/manifest/provenance.json').read_text()):
    file = root/item['file']
    assert hashlib.sha256(file.read_bytes()).hexdigest() == item['sha256'], file
    ast.parse(file.read_text())
for file in (root/'worker/kinich_worker').glob('*.py'):
    ast.parse(file.read_text())
protocol = json.loads((root/'proto/worker/v1/message.schema.json').read_text())
assert {'permit','inference_request','inference_done'}.issubset(protocol['properties']['kind']['enum'])
for path, methods in api['paths'].items():
    for method, op in methods.items():
        if path.startswith('/v2') or path == '/metrics':
            assert op.get('security'), (method, path)
print('Contratos, catálogo de 16 entradas, protocolo y SHA de plugins: OK')
