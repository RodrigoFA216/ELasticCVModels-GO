"""Compara trazas reales bajo tolerancias congeladas; no sustituye etiquetado ni banco GPU."""
import argparse
import json
import math
from pathlib import Path


def compare(reference, actual, policy):
    required = ['model', 'model_version', 'weights_sha256', 'clip_sha256', 'config_sha256', 'precision', 'frame_shape', 'hardware_id']
    for field in required:
        if not reference['metadata'].get(field) or reference['metadata'][field] != actual['metadata'].get(field):
            raise ValueError('Referencia incompatible: ' + field)
    for field in ['max_time_error_s', 'max_score_error', 'max_bbox_error_px', 'approved_by', 'extra_fields']:
        if field not in policy:
            raise ValueError('Tolerancia no congelada: ' + field)
    for field in ['max_time_error_s','max_score_error','max_bbox_error_px']:
        value=policy[field]
        if type(value) not in (int,float) or not math.isfinite(value) or value<0:
            raise ValueError('Tolerancia inválida: '+field)
    if not policy['approved_by']:
        raise ValueError('Falta aprobación de tolerancias')
    def near(a, b, tolerance, label):
        if type(a) not in (int, float) or type(b) not in (int, float) or not math.isfinite(a) or not math.isfinite(b) or abs(a-b) > tolerance:
            raise ValueError('Paridad fallida: ' + label)
    left, right = reference['events'], actual['events']
    if len(left) != len(right):
        raise ValueError('Cantidad de eventos distinta')
    for index, (expected, observed) in enumerate(zip(left, right)):
        for key in ['model', 'member_id', 'type', 'state']:
            if expected.get(key) != observed.get(key):
                raise ValueError(f'Evento {index}: {key} distinto')
        near(expected['frame']['media_time'], observed['frame']['media_time'], policy['max_time_error_s'], 'reloj')
        for key in policy['extra_fields']:
            if key not in expected.get('extra', {}) or expected['extra'][key] != observed.get('extra', {}).get(key):
                raise ValueError(f'Evento {index}: extra.{key} distinto/ausente')
        first, second = expected.get('detections', []), observed.get('detections', [])
        if len(first) != len(second):
            raise ValueError('Cantidad de detecciones distinta')
        for a, b in zip(first, second):
            if a.get('label') != b.get('label'):
                raise ValueError('Etiqueta distinta')
            near(a['score'], b['score'], policy['max_score_error'], 'score')
            if len(a['bbox']) != 4 or len(b['bbox']) != 4:
                raise ValueError('BBox inválida')
            for x, y in zip(a['bbox'], b['bbox']):
                near(x, y, policy['max_bbox_error_px'], 'bbox')
    return {'result': 'trace_parity_passed', 'model': reference['metadata']['model'], 'events_compared': len(left), 'accuracy_certified': False}


def main():
    parser = argparse.ArgumentParser()
    for name in ['reference', 'actual', 'policy']:
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    result = compare(*(json.loads(getattr(args, name).read_text()) for name in ['reference', 'actual', 'policy']))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
