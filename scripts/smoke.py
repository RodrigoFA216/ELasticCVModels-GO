"""Arranque real de binario desde estado vacío; no usa datos/secretos del usuario."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT/'bin/kinichd'
with tempfile.TemporaryDirectory(prefix='kinich-smoke-') as state:
    subprocess.run([str(BINARY),'-command','init','-state',state,'-environment','qa','-tenant','smoke'],check=True,stdout=subprocess.DEVNULL)
    key=json.loads((Path(state)/'credential-qa-smoke.json').read_text())['key']
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    process=subprocess.Popen([str(BINARY),'-state',state,'-worker-dir',str(ROOT/'worker'),'-allow-synthetic','-listen',f'127.0.0.1:{port}'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    def request(method,path,data=None,revision=None,key_id=None):
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}
        if revision:headers['If-Match']=str(revision)
        if key_id:headers['Idempotency-Key']=key_id
        req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(data).encode() if data is not None else None,headers=headers,method=method)
        with urllib.request.urlopen(req,timeout=3) as result:return result.status,json.load(result)
    def wait(fn):
        end=time.monotonic()+8
        while time.monotonic()<end:
            try:
                if fn():return
            except (OSError,urllib.error.URLError):pass
            time.sleep(.05)
        raise AssertionError('smoke timed out')
    try:
        wait(lambda:request('GET','/readyz')[0]==200)
        _,op=request('POST','/v2/cameras',dict(code='SMOKE',name='Smoke',source='synthetic://smoke',members=[dict(member_id='p',model='pulso',config=dict(on_s=1,off_s=1))]),key_id='smoke-camera-001')
        camera_id=op['resource_id']
        _,op=request('POST',f'/v2/cameras/{camera_id}/executions',dict(queue_seconds=60),key_id='smoke-start-001')
        execution_id=op['resource_id']
        wait(lambda:request('GET',f'/v2/executions/{execution_id}')[1]['state']=='running')
        wait(lambda:len(request('GET',f'/v2/events?execution_id={execution_id}')[1]['events'])>0)
        request('POST',f'/v2/cameras/{camera_id}/disconnect',revision=1,key_id='smoke-disconnect-001')
        wait(lambda:request('GET',f'/v2/executions/{execution_id}')[1]['state']=='cancelled')
        assert request('GET',f'/v2/cameras/{camera_id}')[1]['enabled'] is False
        assert request('GET','/v2/capacity')[1]['reserved_mib']==0
    finally:
        process.send_signal(signal.SIGTERM)
        output,error=process.communicate(timeout=10)
        assert process.returncode==0, error
        assert key not in output+error
    print('Smoke real HTTP: init, auth, cámara, Python, evento, disconnect y cierre confirmado: OK')
