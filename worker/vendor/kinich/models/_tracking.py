"""BYTETracker por sesión, tolerante al drift de API de ultralytics.

Compartido por crash/fight/carparts: instancian un tracker propio por stream
(en vez de YOLO.track(persist=True), que guarda estado en el objeto YOLO
compartido y mezclaría track_ids entre streams). Importa perezosamente porque
ultralytics.trackers arrastra `lap`.
"""


def make_bytetracker(tracker_yaml: str = "bytetrack.yaml"):
    from ultralytics.trackers.byte_tracker import BYTETracker
    from ultralytics.utils import IterableSimpleNamespace
    from ultralytics.utils.checks import check_yaml

    try:
        from ultralytics.utils import yaml_load          # ultralytics <= 8.3
    except ImportError:
        from ultralytics.utils import YAML               # 8.4+: movido a YAML.load
        yaml_load = YAML.load

    cfg = IterableSimpleNamespace(**yaml_load(check_yaml(tracker_yaml)))
    try:
        return BYTETracker(cfg, frame_rate=30)           # firma antigua
    except TypeError:
        return BYTETracker(cfg)                           # 8.4+: solo (args)
