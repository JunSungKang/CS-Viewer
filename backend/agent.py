"""Local macOS adapter. No endpoint exposes arbitrary Docker or filesystem access."""
import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit
import uuid

from .engine import Engine, EngineError, resource_usage
from .http_support import APIError, Handler, Server
from .store import Store


def host_memory():
    try:
        total = int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], timeout=2))
        text = subprocess.check_output(['vm_stat'], text=True, timeout=2)
        page = int(re.search(r'page size of (\d+) bytes', text)[1])
        counts = dict((k, int(v)) for k, v in re.findall(r'^([^:\n]+):\s+(\d+)\.', text, re.M))
        available = sum(counts.get(k, 0) for k in ('Pages free', 'Pages inactive', 'Pages speculative')) * page
        return {'total': total, 'used_estimate': max(0, total - available)}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return {'total': None, 'used_estimate': None}


def probe(module, settings):
    result = {'module': module, 'http_ms': None, 'tcp_ms': None, 'status': None, 'error': None}
    connection = None
    try:
        kind = http.client.HTTPSConnection if settings.get('scheme') == 'https' else http.client.HTTPConnection
        connection = kind(settings['host'], settings['port'], timeout=3)
        start = time.monotonic()
        connection.connect()
        result['tcp_ms'] = round((time.monotonic() - start) * 1000, 1)
        connection.request('GET', settings.get('path', '/'), headers={'User-Agent': 'CS-Viewer/1', 'Connection': 'close'})
        response = connection.getresponse()
        result['status'] = response.status
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError('body_limit')
        result['http_ms'] = round((time.monotonic() - start) * 1000, 1)
        if response.status >= 400:
            result['error'] = 'HTTP ' + str(response.status)
    except (OSError, http.client.HTTPException, ValueError):
        result['error'] = result['error'] or '연결 실패 또는 응답 제한 초과'
    finally:
        if connection:
            connection.close()
    return result


@contextlib.contextmanager
def deploy_lock(path, timeout=10):
    with open(path, 'a+') as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise APIError(409, '다른 운영 작업이 진행 중입니다. 상태를 확인한 뒤 다시 요청하세요.')
                time.sleep(.05)
        try:
            if os.fstat(handle.fileno()).st_ino != os.stat(path).st_ino:
                raise APIError(409, '운영 잠금 파일이 변경되었습니다.')
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Agent:
    def __init__(self, config, engine=None, store=None):
        self.config = config
        self.engine = engine or Engine(config['docker_socket'])
        self.store = store or Store(config['state_path'])
        self.snapshot_lock = threading.Lock()
        self.collection_lock = threading.Lock()
        self.snapshot = {'collected_at': None, 'error': '첫 수집을 기다리는 중입니다.', 'containers': [], 'services': [], 'images': [], 'bases': []}
        self.image_cache = {}
        self.modules = set(s['module'] for s in config['services'])
        self.aliases = {}
        for service in config['services']:
            for name in service['names']:
                if name in self.aliases:
                    raise ValueError('중복 컨테이너 이름 설정')
                self.aliases[name] = service

    def verify_daemon(self):
        info = self.engine.call('GET', '/info')
        if info['ID'] != self.config['daemon_id']:
            raise EngineError('설정한 운영 Docker daemon과 다릅니다.')
        return info

    def image(self, image_id):
        if image_id not in self.image_cache:
            raw = self.engine.call('GET', '/images/' + quote(image_id, safe='') + '/json')
            self.image_cache[image_id] = {'id': raw['Id'], 'tags': raw.get('RepoTags') or [],
                'digests': raw.get('RepoDigests') or [], 'layers': raw.get('RootFS', {}).get('Layers', []),
                'size': raw.get('Size'), 'architecture': raw.get('Architecture')}
        return self.image_cache[image_id]

    def container(self, listed, service):
        raw = self.engine.inspect(listed['Id'])
        name = raw['Name'].lstrip('/')
        if name not in service['names']:
            raise EngineError('수집 중 컨테이너가 변경되었습니다.')
        state = raw.get('State', {})
        ports = []
        for target, bindings in (raw.get('NetworkSettings', {}).get('Ports') or raw.get('HostConfig', {}).get('PortBindings') or {}).items():
            for binding in bindings or []:
                ports.append({'host': binding.get('HostIp', ''), 'port': binding.get('HostPort', ''), 'container': target})
        result = {'id': raw['Id'], 'name': name, 'display_name': service['name'], 'module': service['module'],
                  'role': service['role'], 'control': service.get('control') is True, 'critical': service.get('critical') is True,
                  'state': state.get('Status'), 'health': (state.get('Health') or {}).get('Status'),
                  'started_at': state.get('StartedAt'), 'restart_count': raw.get('RestartCount', 0), 'ports': ports,
                  'oom_killed': bool(state.get('OOMKilled')), 'last_exit_at': state.get('FinishedAt'),
                  'mounts': [{k: m.get(k) for k in ('Type', 'Name', 'Source', 'Destination', 'RW')} for m in raw.get('Mounts', [])],
                  'image_id': raw['Image'], 'cpu_percent': None, 'memory_bytes': None, 'telemetry_error': None}
        if result['state'] == 'running':
            try:
                result.update(resource_usage(self.engine.call('GET', '/containers/' + raw['Id'] + '/stats?stream=false&one-shot=true', 3)))
            except EngineError:
                result['telemetry_error'] = '리소스 수집 실패'
        return result

    def collect(self):
        with self.collection_lock:
            self._collect()

    def _collect(self):
        try:
            info = self.verify_daemon()
            listed = self.engine.call('GET', '/containers/json?all=true')
            managed = [(c, self.aliases[n.lstrip('/')]) for c in listed for n in c.get('Names', []) if n.lstrip('/') in self.aliases]
            # Docker returns one canonical name; de-duplicate any additional aliases.
            managed = list({c['Id']: (c, s) for c, s in managed}.values())
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                containers = list(pool.map(lambda item: self.container(*item), managed))
                probes = list(pool.map(lambda item: probe(*item), self.config.get('probes', {}).items()))
            images, bases, image_errors = [], [], []
            for image_id in dict.fromkeys(c['image_id'] for c in containers):
                try:
                    images.append(self.image(image_id))
                except EngineError:
                    image_errors.append('일부 이미지 계층을 읽을 수 없습니다.')
            for title, ref in self.config.get('bases', {}).items():
                try:
                    bases.append({'title': title, **self.image(ref)})
                except EngineError:
                    image_errors.append(title + ' 기반 이미지를 읽을 수 없습니다.')
            for item in probes:
                members = [c for c in containers if c['module'] == item['module']]
                item.update(running=sum(c['state'] == 'running' for c in members),
                            total=sum(s['module'] == item['module'] for s in self.config['services']),
                            restarts=sum(c['restart_count'] for c in members), oom_history=any(c['oom_killed'] for c in members),
                            unhealthy=any(c['health'] == 'unhealthy' for c in members),
                            cpu_percent=round(sum(c['cpu_percent'] for c in members), 1) if members and all(c['cpu_percent'] is not None for c in members) else None,
                            memory_bytes=sum(c['memory_bytes'] for c in members) if members and all(c['memory_bytes'] is not None for c in members) else None)
            self.store.sample(probes)
            missing = [s['name'] for s in self.config['services'] if not any(c['name'] in s['names'] for c in containers)]
            data = {'collected_at': time.time(), 'error': None, 'containers': containers, 'services': probes,
                    'images': images, 'bases': bases, 'image_errors': image_errors, 'missing': missing,
                    'host_memory': host_memory(), 'daemon': {'id': info['ID'], 'name': info.get('Name'),
                    'version': info.get('ServerVersion'), 'architecture': info.get('Architecture'), 'cpus': info.get('NCPU'),
                    'memory_total': info.get('MemTotal')}, 'interval': self.config.get('interval', 10)}
            with self.snapshot_lock:
                self.snapshot = data
        except Exception as exc:
            with self.snapshot_lock:
                self.snapshot = {**self.snapshot, 'error': str(exc) if isinstance(exc, EngineError) else '수집기 오류: 운영 로그를 확인하세요.'}

    def state(self):
        with self.snapshot_lock:
            state = dict(self.snapshot)
        state['audit'] = self.store.audit()
        state['trends'] = {s['module']: self.store.history(s['module']) for s in state['services']}
        return state

    def action(self, body):
        action, target = body.get('action'), body.get('id')
        if action not in ('start', 'stop', 'restart') or not isinstance(target, str) or not re.fullmatch('[0-9a-f]{64}', target):
            raise APIError(400, '작업과 실제 컨테이너 ID를 확인하세요.')
        try:
            request_id = str(uuid.UUID(body['request_id']))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise APIError(400, '유효한 작업 ID가 필요합니다.') from exc
        fingerprint = hashlib.sha256(json.dumps({k: body.get(k) for k in ('id', 'action', 'expected_state', 'confirmation')}, sort_keys=True).encode()).hexdigest()
        with deploy_lock(self.config['lock_path']):
            try:
                previous = self.store.begin(request_id, fingerprint, 'operator', target, action)
            except ValueError as exc:
                raise APIError(409, str(exc)) from exc
            if previous:
                return {k: v for k, v in previous.items() if k != 'fingerprint'}
            executed = False
            try:
                self.verify_daemon()
                raw = self.engine.inspect(target)
                service = self.aliases.get(raw['Name'].lstrip('/'))
                if raw['Id'] != target or not service or service.get('control') is not True:
                    raise APIError(403, '제어 허용 목록의 컨테이너가 아닙니다.')
                if body.get('confirmation') != service['name']:
                    raise APIError(400, '확인 이름이 일치하지 않습니다.')
                state = raw['State']['Status']
                if state != body.get('expected_state') or (action == 'start' and state not in ('exited', 'created')) or (action != 'start' and state != 'running'):
                    raise APIError(409, '컨테이너 상태가 변경되었습니다. 새로 조회하세요.')
                executed = True
                self.engine.action(target, action)
                deadline = time.monotonic() + 10
                expected = 'exited' if action == 'stop' else 'running'
                verified = False
                while time.monotonic() < deadline:
                    after = self.engine.inspect(target)
                    health = (after['State'].get('Health') or {}).get('Status')
                    if after['State']['Status'] == expected and (action == 'stop' or health in (None, 'healthy')):
                        verified = action != 'restart' or after['State'].get('StartedAt') != raw['State'].get('StartedAt')
                        if verified:
                            break
                    time.sleep(.25)
                result = self.store.finish(request_id, 'succeeded' if verified else 'unknown',
                    '실제 상태 확인 완료' if verified else '명령 응답 후 정상 상태 확인 시간 초과. 현재 상태를 다시 조회하세요.')
            except APIError as exc:
                self.store.finish(request_id, 'rejected', exc.message)
                raise
            except Exception as exc:
                self.store.finish(request_id, 'unknown' if executed else 'failed',
                                  '실제 상태를 다시 확인하세요.' if executed else 'Docker 연결 확인 실패')
                raise APIError(503, 'Docker 요청 결과를 확정할 수 없습니다. 작업 이력과 현재 상태를 확인하세요.') from exc
        # Probe collection never holds the operation lock after the change has finished.
        self.collect()
        return {k: v for k, v in result.items() if k != 'fingerprint'}


def make_handler(agent, token):
    class AgentHandler(Handler):
        def route(self):
            authorization = self.headers.get('Authorization', '')
            if not hmac.compare_digest(authorization, 'Bearer ' + token):
                raise APIError(401, '에이전트 인증 실패')
            url = urlsplit(self.path)
            if self.command == 'GET' and url.path == '/state' and not url.query:
                self.send(200, agent.state())
            elif self.command == 'GET' and url.path == '/healthz' and not url.query:
                state = agent.state()
                ready = state.get('collected_at') and not state.get('error') and time.time() - state['collected_at'] < 60
                self.send(200 if ready else 503, {'ready': bool(ready)})
            elif self.command == 'POST' and url.path == '/action' and not url.query:
                self.send(200, agent.action(self.json_body()))
            else:
                raise APIError(404, '지원하지 않는 요청')

        def do_GET(self):
            self.run_route(self.route)

        def do_POST(self):
            self.run_route(self.route)
    return AgentHandler


def load_config(path):
    config = json.loads(Path(path).read_text())
    root = Path(config['workspace_root']).resolve()
    for key in ('lock_path', 'state_path', 'token_file'):
        destination = Path(config[key]).resolve()
        if not destination.is_relative_to(root):
            raise ValueError(key + ' must stay in the workspace')
    for service in config['services']:
        if not all(isinstance(service.get(key), str) and re.fullmatch('[a-z0-9]+(?:-[a-z0-9]+)*', service[key]) for key in ('module', 'role', 'name')):
            raise ValueError('Invalid service metadata')
        if any(key in service and type(service[key]) is not bool for key in ('control', 'critical')):
            raise ValueError('Control and critical flags must be JSON booleans')
        if not isinstance(service.get('names'), list) or not service['names'] or any(not isinstance(name, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*', name) for name in service['names']):
            raise ValueError('Invalid container name allowlist')
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    token = Path(config['token_file']).read_text().strip()
    if len(token) < 32:
        raise ValueError('Agent token is too short')
    Path(config['state_path']).parent.mkdir(parents=True, exist_ok=True)
    agent = Agent(config)
    def loop():
        while True:
            agent.collect()
            time.sleep(max(5, config.get('interval', 10)))
    threading.Thread(target=loop, daemon=True).start()
    server = Server(('127.0.0.1', config.get('port', 20135)), make_handler(agent, token))
    print(json.dumps({'event': 'agent.started', 'port': server.server_port}), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
