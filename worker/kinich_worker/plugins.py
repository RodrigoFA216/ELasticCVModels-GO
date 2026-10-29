from __future__ import annotations

import importlib
import hashlib
import os
import time

FACTORIES = {
    'fire-detector': ('fire', 'FireSmokeDetector'),
    'fight-detector': ('fight', 'FightDetector'),
    'robbery-detector': ('robbery', 'RobberyDetector'),
    'crash-detector': ('crash', 'CrashDetector'),
    'fardeo-detector': ('fardeo', 'FardeoDetector'),
    'carparts-detector': ('carparts', 'CarPartsDetector'),
    'people-counter': ('people_counter', 'PeopleCounter'),
    'area-restriction': ('area_restriction', 'AreaRestrictionDetector'),
    'loitering-detector': ('loitering', 'LoiteringDetector'),
    'parking-monitor': ('parking', 'ParkingMonitor'),
    'corridor-counter': ('corridor_counter', 'CorridorCounterDetector'),
    'queue-cashier': ('queue_cashier', 'QueueCashierDetector'),
    'whitegas': ('whitegas', 'WhiteGasKinichDetector'),
    'ppe-detector': ('ppe_detector', 'PPEDetectorModel'),
}


class Pulso:
    def __init__(self, config):
        self.config = config
        self.started = time.monotonic()
        self.previous = None
        self.last_second = -1

    def process(self, frame, geometry):
        c = self.config
        elapsed = time.monotonic() - self.started
        on = elapsed % (c.get('on_s', 10) + c.get('off_s', 10)) < c.get('on_s', 10)
        edge = on != self.previous
        self.previous = on
        cadence = c.get('cadencia', 'flanco')
        second = int(elapsed)
        send = edge or (on and (cadence == 'fotograma' or (cadence == 'segundo' and second != self.last_second)))
        self.last_second = second
        if not send:
            return []
        w, h = geometry['width'], geometry['height']
        fraction = c.get('fraccion', .5)
        box = [w * (1-fraction)/2, h * (1-fraction)/2, w * (1+fraction)/2, h * (1+fraction)/2]
        return [{'type': c.get('tipo', 'CONTEO') + ('' if on else '_OFF'),
                 'state': 'detected' if on else 'clear', 'score': 1.,
                 'detections': [{'label': 'pulso', 'score': 1., 'bbox': box}] if on else [],
                 'extra': {'synthetic': True}}]

    def close(self):
        pass


class NativeAdapter:
    """Sólo modelos de visión: no importa la API, registry ni managers legacy."""
    def __init__(self, model, config, workspace, member_id, version):
        module, factory = FACTORIES[model]
        loaded = importlib.import_module('kinich.models.' + module)
        factory_type = getattr(loaded, factory)
        self.model = factory_type()
        if self.model.info.name != model or self.model.info.version != version:
            raise RuntimeError('model_identity_mismatch')
        # Pesos/config de entorno se aprovisionan fuera del repositorio.
        self.model.load()
        cfg = dict(config)
        if model == 'corridor-counter':
            cfg['contador_id'] = hashlib.sha256(member_id.encode()).hexdigest()
        self.session = self.model.create_session(cfg)

    def process(self, frame, geometry):
        result = []
        for event in self.session.process_frame(frame, geometry['media_time']) or []:
            data = event.model_dump(mode='json')
            result.append({'type': data['type'], 'score': data['score'], 'detections': [d for d in data.get('detections', []) if d.get('bbox')],
                           'extra': data.get('extra', {}), 'state': data.get('extra', {}).get('state', '')})
        return result

    def close(self):
        self.session.close()
        self.model.unload()


def create(member, workspace):
    if member['model'] == 'pulso':
        return Pulso(member['config'])
    if os.environ.get('KINICH_ALLOW_REAL_MODELS') != '1':
        raise RuntimeError('real_models_disabled')
    if member['model'] not in FACTORIES:
        raise RuntimeError('unknown_model')
    return NativeAdapter(member['model'], member['config'], workspace, member['member_id'], member['version'])
