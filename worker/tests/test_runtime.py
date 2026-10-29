import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from kinich_worker.plugins import FACTORIES, Pulso, create
from kinich_worker.runtime import Lease, MAX_MESSAGE

ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def test_lease_cannot_be_resurrected(self):
        lease = Lease()
        with patch('kinich_worker.runtime.time.monotonic', return_value=100):
            self.assertTrue(lease.renew(1000))
            self.assertTrue(lease.valid())
        with patch('kinich_worker.runtime.time.monotonic', return_value=101):
            self.assertFalse(lease.valid())
            self.assertFalse(lease.renew(1000))
        with patch('kinich_worker.runtime.time.monotonic', return_value=100):
            self.assertFalse(lease.valid())

    def test_pulso_sessions_do_not_share_temporal_state(self):
        geometry = dict(width=640, height=360)
        first, second = Pulso({}), Pulso({})
        self.assertEqual(first.process(None, geometry)[0]['type'], 'CONTEO')
        self.assertEqual(first.process(None, geometry), [])
        self.assertEqual(second.process(None, geometry)[0]['type'], 'CONTEO')

    def test_real_models_fail_closed(self):
        with patch.dict(os.environ, {'KINICH_ALLOW_REAL_MODELS': '0'}):
            with self.assertRaisesRegex(RuntimeError, 'real_models_disabled'):
                create(dict(model='whitegas', config={}, member_id='x'), '/tmp')

    def test_all_fourteen_factories_exist_without_importing_gpu(self):
        self.assertEqual(len(FACTORIES), 14)
        for model, (module, factory) in FACTORIES.items():
            text = (ROOT / 'vendor' / 'kinich' / 'models' / (module + '.py')).read_text()
            tree = ast.parse(text)
            names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
            self.assertIn(factory, names, model)
            self.assertNotIn('from kinich.app', text)
            self.assertNotIn('from kinich.stream', text)

    def worker(self, raw):
        env = dict(os.environ, PYTHONPATH=str(ROOT), KINICH_WORKER_ID='w',
                   KINICH_EXECUTION_ID='e', KINICH_CAMERA_ID='c', KINICH_GENERATION='g')
        result = subprocess.run([sys.executable, '-m', 'kinich_worker.runtime'], input=raw,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_bad_version_rejected_before_open(self):
        messages = self.worker(json.dumps(dict(v=2, kind='open', worker_id='w', execution_id='e', generation='g')).encode() + b'\n')
        self.assertEqual([m['kind'] for m in messages], ['hello', 'error'])

    def test_unknown_fields_and_oversized_message(self):
        for raw in [json.dumps(dict(v=1, kind='renew', worker_id='w', execution_id='e', generation='g', tenant_id='other')).encode() + b'\n', b'x' * (MAX_MESSAGE + 1)]:
            messages = self.worker(raw)
            self.assertEqual(messages[-1]['kind'], 'error')


if __name__ == '__main__':
    unittest.main()
