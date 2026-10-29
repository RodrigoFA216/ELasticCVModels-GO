from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import uuid

from .plugins import create

MAX_MESSAGE = 1024 * 1024
PROTOCOL = 1


class Lease:
    def __init__(self):
        self.deadline = 0.
        self.retired = False
        self.lock = threading.Lock()

    def renew(self, milliseconds):
        with self.lock:
            now = time.monotonic()
            if self.retired or (self.deadline and now >= self.deadline):
                self.retired = True
                return False
            self.deadline = now + milliseconds / 1000
            return True

    def valid(self):
        with self.lock:
            if self.retired or time.monotonic() >= self.deadline:
                self.retired = True
                return False
            return True

    def retire(self):
        with self.lock:
            self.retired = True


class Runtime:
    def __init__(self):
        self.worker_id = os.environ['KINICH_WORKER_ID']
        self.execution_id = os.environ['KINICH_EXECUTION_ID']
        self.generation = os.environ['KINICH_GENERATION']
        self.camera_id = os.environ['KINICH_CAMERA_ID']
        self.lease = Lease()
        self.output_lock = threading.Lock()
        self.protocol_output = sys.stdout
        self.session = None
        self.pending = {}
        self.permits = {}
        self.permit_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.shutdown = threading.Event()
        self.capacity = threading.BoundedSemaphore(128)
        self.publisher = threading.Thread(target=self.publish, daemon=True)
        self.publisher.start()

    def send(self, kind, **fields):
        message = dict(v=PROTOCOL, kind=kind, worker_id=self.worker_id,
                       execution_id=self.execution_id, generation=self.generation, **fields)
        encoded = json.dumps(message, separators=(',', ':'), allow_nan=False)
        if len(encoded.encode()) > MAX_MESSAGE:
            raise ValueError('message_too_large')
        with self.output_lock:
            self.protocol_output.write(encoded + '\n')
            self.protocol_output.flush()

    def event(self, member, geometry, data):
        while self.lease.valid() and not self.shutdown.is_set():
            if self.capacity.acquire(timeout=.1):
                break
        else:
            return
        if not self.lease.valid():
            self.capacity.release()
            return
        event_id = str(uuid.uuid4())
        event = dict(id=event_id, camera_id=self.camera_id, member_id=member['member_id'],
                     member_generation=member['generation'], model=member['model'], frame=geometry, **data)
        with self.pending_lock:
            self.pending[event_id] = (event, 0.)

    def publish(self):
        while not self.shutdown.wait(.05):
            with self.pending_lock:
                items = list(self.pending.items())
            for event_id, (event, last) in items:
                if time.monotonic() - last < 1:
                    continue
                if not self.lease.valid():
                    return
                try:
                    self.send('event', event=event)
                except (BrokenPipeError, ValueError):
                    self.lease.retire()
                    return
                with self.pending_lock:
                    if event_id in self.pending:
                        self.pending[event_id] = (event, time.monotonic())

    def acquire(self, member, stop):
        key = (member['member_id'], member['generation'])
        waiting = dict(signal=threading.Event(), token='')
        with self.permit_lock:
            self.permits[key] = waiting
        self.send('inference_request', member_id=key[0], member_generation=key[1])
        while self.lease.valid() and not stop.is_set() and not self.shutdown.is_set():
            if waiting['signal'].wait(.05):
                with self.permit_lock:
                    self.permits.pop(key, None)
                return waiting['token']
        return ''

    def acknowledge(self, event_id):
        with self.pending_lock:
            if self.pending.pop(event_id, None) is not None:
                self.capacity.release()

    def handle(self, message):
        allowed = {'v', 'kind', 'worker_id', 'execution_id', 'generation', 'camera_id', 'command_id',
                   'revision', 'lease_ms', 'member_id', 'member_generation', 'permit_id', 'members', 'source', 'media', 'mode', 'event_id', 'accepted'}
        if set(message) - allowed or message.get('v') != PROTOCOL:
            raise ValueError('protocol_mismatch')
        if (message.get('worker_id'), message.get('execution_id'), message.get('generation')) != (
                self.worker_id, self.execution_id, self.generation):
            raise ValueError('identity_mismatch')
        kind = message['kind']
        if kind in ('open', 'renew'):
            lease_ms = message.get('lease_ms')
            if type(lease_ms) is not int or not 100 <= lease_ms <= 30000:
                raise ValueError('invalid_lease')
        if kind == 'open':
            if self.session or not self.lease.renew(message['lease_ms']):
                raise ValueError('session_not_openable')
            self.session = Session(self, message)
            self.session.start()
        elif kind == 'renew':
            if not self.lease.renew(message['lease_ms']):
                self.send('error', reason='lease_expired')
            else:
                self.send('status', state='lease_renewed')
        elif kind == 'configure':
            if not self.session or not self.lease.valid():
                raise ValueError('session_unavailable')
            self.session.configure(message['members'])
            self.send('status', state='configured', revision=message['revision'])
        elif kind == 'permit':
            key = (message['member_id'], message['member_generation'])
            with self.permit_lock:
                waiting = self.permits.get(key)
                if waiting:
                    waiting['token'] = message.get('permit_id', '')
                    waiting['signal'].set()
        elif kind == 'ack':
            self.acknowledge(message['event_id'])
        elif kind == 'stop':
            self.lease.retire()
            if self.session:
                self.session.close()
            self.send('closed')
            return False
        else:
            raise ValueError('unknown_command')
        return True

    def run(self):
        self.send('hello')
        try:
            while True:
                raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
                if not raw:
                    break
                if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
                    raise ValueError('message_too_large')
                if not self.handle(json.loads(raw)):
                    break
        except Exception:
            # Nunca imprimir excepciones de plugins: pueden contener RTSP o tokens.
            with contextlib.suppress(BrokenPipeError):
                self.send('error', reason='worker_protocol_or_runtime_error')
        finally:
            self.lease.retire()
            self.shutdown.set()
            if self.session:
                self.session.close()


class Session:
    def __init__(self, runtime, message):
        self.runtime, self.message = runtime, message
        self.done = threading.Event()
        self.frame_condition = threading.Condition()
        self.latest = None
        self.frame_number = 0
        self.input_complete = False
        self.members = {}
        self.member_lock = threading.Lock()
        self.workspace = tempfile.TemporaryDirectory(prefix='kinich-worker-')
        self.closed = False
        self.capture = None
        self.decoder = threading.Thread(target=self.decode, daemon=True)
        self.control = threading.Thread(target=self.initialize, daemon=True)

    def start(self):
        self.control.start()

    def initialize(self):
        try:
            self.configure(self.message['members'])
            if not self.runtime.lease.valid():
                return
            self.decoder.start()
        except Exception:
            self.runtime.send('error', reason='model_initialization_failed')
            self.done.set()

    def configure(self, members):
        if not 1 <= len(members) <= 16 or len({m['member_id'] for m in members}) != len(members):
            raise ValueError('invalid_members')
        candidates = {}
        try:
            for member in members:
                previous = self.members.get(member['member_id'])
                if previous and previous['member']['generation'] == member['generation']:
                    candidates[member['member_id']] = previous
                else:
                    # Impide que un print de una biblioteca corrompa el canal de protocolo.
                    with contextlib.redirect_stdout(sys.stderr):
                        plugin = create(member, self.workspace.name)
                    candidates[member['member_id']] = dict(member=member, plugin=plugin, stop=threading.Event(), thread=None)
        except Exception:
            for key, state in candidates.items():
                if self.members.get(key) is not state:
                    state['plugin'].close()
            raise
        retired = [s for k, s in self.members.items() if candidates.get(k) is not s]
        for state in retired:
            state['stop'].set()
        with self.member_lock:
            self.members = candidates
        for state in retired:
            if state['thread']:
                state['thread'].join()
            state['plugin'].close()
        for state in candidates.values():
            if state['thread'] is None:
                state['thread'] = threading.Thread(target=self.infer, args=(state,), daemon=True)
                state['thread'].start()

    def decode(self):
        source = self.message.get('source', '')
        synthetic = source.startswith('synthetic://')
        mode = self.message.get('mode', 'stream')
        clip_path = None
        try:
            if not synthetic:
                import cv2
                media = self.message.get('media', '')
                if media:
                    raw = base64.b64decode(media, validate=True)
                    if mode == 'image':
                        import numpy as np
                        frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if frame is None:
                            raise ValueError('invalid_image')
                        self.put_frame(frame, frame.shape[1], frame.shape[0], 0.)
                        self.runtime.send('ready')
                        # Una pasada por miembro; el controlador cierra después del estado completed.
                        for state in list(self.members.values()):
                            state['thread'].join()
                        self.drain_events()
                        self.runtime.send('status', state='completed')
                        return
                    fd, clip_path = tempfile.mkstemp(suffix='.mp4', dir=self.workspace.name)
                    with os.fdopen(fd, 'wb') as f:
                        f.write(raw)
                    source = clip_path
                self.capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG,
                    [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000])
                attempts = 0
                while not self.capture.isOpened() and not self.done.is_set() and self.runtime.lease.valid():
                    attempts += 1
                    self.runtime.send('status', state='source_reconnecting')
                    if attempts >= 6:
                        raise ValueError('source_unavailable')
                    self.done.wait(min(2 ** attempts, 8))
                    self.capture.release()
                    self.capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG,
                        [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000])
            ready = False
            while not self.done.is_set() and self.runtime.lease.valid():
                if synthetic:
                    frame, w, h, media_time = None, 640, 360, time.monotonic()
                    self.done.wait(.05)
                else:
                    ok, frame = self.capture.read()
                    if not ok:
                        if mode == 'clip':
                            self.input_complete = True
                            with self.frame_condition:
                                self.frame_condition.notify_all()
                            for state in list(self.members.values()):
                                state['thread'].join()
                            self.drain_events()
                            self.runtime.send('status', state='completed')
                            return
                        raise ValueError('source_read_failed')
                    h, w = frame.shape[:2]
                    media_time = self.capture.get(cv2.CAP_PROP_POS_MSEC) / 1000 if mode=="clip" else time.monotonic()
                if mode=='clip':
                    if self.frame_number>=1800 or media_time>60:
                        raise ValueError('clip_duration_limit')
                    self.done.wait(.033)
                self.put_frame(frame, w, h, media_time)
                if not ready:
                    self.runtime.send('ready')
                    ready = True
        except Exception:
            self.runtime.send('error', reason='source_unavailable')
        finally:
            if self.capture is not None:
                self.capture.release()
            if clip_path:
                Path(clip_path).unlink(missing_ok=True)

    def drain_events(self):
        while self.runtime.lease.valid() and not self.done.is_set():
            with self.runtime.pending_lock:
                if not self.runtime.pending:
                    return
            self.done.wait(.05)

    def put_frame(self, frame, w, h, media_time):
        if not 1 <= w <= 16384 or not 1 <= h <= 16384 or w*h > 16_777_216:
            raise ValueError('frame_too_large')
        geometry = dict(id=str(uuid.uuid4()), capturedAt=dt.datetime.now(dt.timezone.utc).isoformat(),
                        media_time=media_time, width=w, height=h, source_width=w, source_height=h,
                        transform=[1., 0., 0., 0., 1., 0.])
        with self.frame_condition:
            self.frame_number += 1
            self.latest = (self.frame_number, frame, geometry)
            self.frame_condition.notify_all()

    def infer(self, state):
        seen = 0
        member = state['member']
        try:
            while not self.done.is_set() and not state['stop'].is_set() and self.runtime.lease.valid():
                with self.frame_condition:
                    self.frame_condition.wait_for(lambda: self.input_complete or (self.latest and self.latest[0] != seen), timeout=.1)
                    latest = self.latest
                if self.input_complete and (not latest or latest[0] == seen):
                    return
                if not latest or latest[0] == seen:
                    continue
                seen, frame, geometry = latest
                started = time.monotonic()
                token = self.runtime.acquire(member, state['stop'])
                if not token:
                    return
                if not self.runtime.lease.valid():
                    self.runtime.send('inference_done', permit_id=token)
                    return
                try:
                    with contextlib.redirect_stdout(sys.stderr):
                        events = state['plugin'].process(frame, geometry)
                finally:
                    self.runtime.send('inference_done', permit_id=token)
                if self.runtime.lease.valid() and not state['stop'].is_set() and not self.done.is_set():
                    for event in events:
                        self.runtime.event(member, geometry, event)
                if self.message.get('mode') == 'image':
                    return
                state['stop'].wait(max(0., 1/member['fps'] - (time.monotonic()-started)))
        except Exception:
            self.runtime.send('error', reason='inference_failed')

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.done.set()
        with self.frame_condition:
            self.frame_condition.notify_all()
        if self.control.is_alive() and self.control is not threading.current_thread():
            self.control.join()
        if self.decoder.is_alive() and self.decoder is not threading.current_thread():
            self.decoder.join()
        for state in list(self.members.values()):
            state['stop'].set()
            if state['thread']:
                state['thread'].join()
            with contextlib.suppress(Exception):
                state['plugin'].close()
        self.workspace.cleanup()


def main():
    os.umask(0o077)
    # El padre retira el pipe al morir. El watchdog cubre nativo bloqueado y stdin inactivo.
    runtime = Runtime()
    def watchdog():
        while not runtime.shutdown.wait(.1):
            if runtime.lease.deadline and not runtime.lease.valid():
                runtime.send('error', reason='lease_expired')
                os._exit(70)
    threading.Thread(target=watchdog, daemon=True).start()
    runtime.run()


if __name__ == '__main__':
    main()
