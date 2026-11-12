"""Exercise the complete control path against one owned disposable container."""
import argparse
import copy
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import uuid

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
from backend.agent import Agent, deploy_lock, load_config, make_handler as agent_handler
from backend.app import Application, make_handler as app_handler
from backend.engine import Engine, EngineError
from backend.http_support import Server

parser = argparse.ArgumentParser()
parser.add_argument('--config', required=True)
parser.add_argument('--context', default='desktop-linux')
args = parser.parse_args()
config = load_config(args.config)
engine = Engine(config['docker_socket'])
if engine.call('GET', '/info')['ID'] != config['daemon_id']:
    raise SystemExit('Unexpected Docker daemon')
name = 'cs-viewer-smoke-' + uuid.uuid4().hex[:12]
docker = ['docker', '--context', args.context]
if subprocess.check_output([*docker, 'info', '--format', '{{.ID}}'], text=True).strip() != config['daemon_id']:
    raise SystemExit('CLI context and configured Engine socket do not refer to the same daemon')
scratch = repo / '.runtime/smoke'
scratch.mkdir(parents=True, exist_ok=True)
container_id = None
servers = []
try:
    with tempfile.TemporaryDirectory(dir=scratch) as temporary:
        with deploy_lock(config['lock_path']):
            container_id = subprocess.check_output([*docker, 'run', '-d', '--name', name, '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--label', 'com.cs-viewer.smoke=' + name, 'platform/python314:ubuntu26.04-20261109-r2',
                'python3', '-B', '-c', 'import time; time.sleep(300)'], text=True).strip()
        local = {**config, 'state_path': str(Path(temporary) / 'state.db'), 'services': [
            {'name': name, 'module': 'smoke', 'role': 'worker', 'names': [name]}], 'probes': {}, 'bases': {}}
        agent = Agent(local, engine=engine)
        adapter = Server(('127.0.0.1', 0), agent_handler(agent));servers.append(adapter)
        threading.Thread(target=adapter.serve_forever, daemon=True).start()
        app = Application('http://127.0.0.1:' + str(adapter.server_port))
        web = Server(('127.0.0.1', 0), app_handler(app));servers.append(web)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        def call(path, body):
            connection = http.client.HTTPConnection('127.0.0.1', web.server_port, timeout=45)
            connection.request('POST', path, json.dumps(body), {'Content-Type': 'application/json'})
            response = connection.getresponse();data = json.load(response);headers = dict(response.getheaders());status = response.status
            connection.close()
            if status != 200:
                raise RuntimeError((status, data))
            return data, headers
        results = []
        for action, before, after in [('restart', 'running', 'running'), ('stop', 'running', 'exited'), ('start', 'exited', 'running')]:
            request = {'id': container_id, 'action': action, 'expected_state': before, 'confirmation': name, 'request_id': str(uuid.uuid4())}
            result, _ = call('/api/action', request)
            assert result['status'] == 'succeeded', result
            inspected = engine.inspect(container_id)
            assert inspected['State']['Status'] == after
            if action == 'restart':
                started = inspected['State']['StartedAt'];call('/api/action', request)
                assert engine.inspect(container_id)['State']['StartedAt'] == started, 'Duplicate restart executed'
            results.append({'action': action, 'state': after, 'status': result['status']})
        print(json.dumps({'direct_control': results, 'idempotent_restart': True, 'production_containers_changed': False}))
        agent.store.db.close()
finally:
    for server in reversed(servers):
        server.shutdown();server.server_close()
    if not container_id:
        try:
            candidate = engine.inspect(name)
            if candidate['Config'].get('Labels', {}).get('com.cs-viewer.smoke') == name:
                container_id = candidate['Id']
        except EngineError:
            pass
    if container_id:
        with deploy_lock(config['lock_path']):
            raw = engine.inspect(container_id)
            if raw['Id'] != container_id or raw['Config']['Labels'].get('com.cs-viewer.smoke') != name:
                raise RuntimeError('Disposable container ownership changed; cleanup refused')
            subprocess.run([*docker, 'rm', '-f', container_id], check=True, stdout=subprocess.DEVNULL)
