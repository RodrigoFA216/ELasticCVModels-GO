import copy
import importlib.util
from pathlib import Path
import unittest

script = Path(__file__).resolve().parents[2]/'scripts/compare_model_events.py'
spec = importlib.util.spec_from_file_location('compare_model_events', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ParityTests(unittest.TestCase):
    def fixture(self):
        metadata = {key:'frozen' for key in ['model','model_version','weights_sha256','clip_sha256','config_sha256','precision','hardware_id']}
        metadata['frame_shape']=[640,360]
        trace = dict(metadata=metadata, events=[dict(model='ppe-detector', member_id='a', type='TOLERANCIA', state='verificando', frame=dict(media_time=1), extra=dict(count=2), detections=[dict(label='person', score=.9, bbox=[1,2,30,40])])])
        policy = dict(max_time_error_s=.1, max_score_error=.01, max_bbox_error_px=1, approved_by='fixture-only', extra_fields=['count'])
        return trace, copy.deepcopy(trace), policy

    def test_parity(self):
        a,b,p=self.fixture()
        self.assertEqual(module.compare(a,b,p)['events_compared'],1)

    def test_unknown_is_not_compliance(self):
        a,b,p=self.fixture();b['events'][0]['state']='cumpliendo'
        with self.assertRaises(ValueError):module.compare(a,b,p)

    def test_different_hardware_and_boxes_rejected(self):
        a,b,p=self.fixture();b['metadata']['hardware_id']='other'
        with self.assertRaises(ValueError):module.compare(a,b,p)
        a,b,p=self.fixture();b['events'][0]['detections'][0]['bbox'][0]=20
        with self.assertRaises(ValueError):module.compare(a,b,p)
