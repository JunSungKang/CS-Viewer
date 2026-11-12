import copy
import http.client
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from backend.agent import Agent, deploy_lock, load_config, make_handler as agent_handler, probe
from backend.app import Application, make_handler
from backend.engine import resource_usage, EngineError
from backend.http_support import APIError, Server
from backend.store import Store

CONTAINER_ID = 'a' * 64


class FakeEngine:
    def __init__(self):
        self.actions = []
        self.daemon = 'expected-daemon'
        self.raw = {'Id': CONTAINER_ID, 'Name': '/mailbox-web', 'Image': 'sha256:' + 'b' * 64,
                    'State': {'Status': 'running', 'StartedAt': 'before', 'Health': {'Status': 'healthy', 'Log': ['PRIVATE']}},
                    'NetworkSettings': {'Ports': {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '20105'}]}},
                    'Mounts': [{'Type': 'volume', 'Name': 'mail-data', 'Source': '/vm/data', 'Destination': '/data', 'RW': True}],
                    'Config': {'Env': ['PASSWORD=PRIVATE']}}

    def call(self, method, path, timeout=5):
        if path == '/info':
            return {'ID': self.daemon, 'MemTotal': 8000000000}
        if path == '/containers/json?all=true':
            return [{'Id': CONTAINER_ID, 'Names': ['/mailbox-web']}]
        if path.endswith('/stats?stream=false&one-shot=true'):
            return {'memory_stats': {'usage': 100, 'stats': {'inactive_file': 20}}}
        if path.startswith('/images/'):
            return {'Id': 'sha256:' + 'b' * 64, 'RootFS': {'Layers': ['sha256:base', 'sha256:app']}, 'Size': 100}
        raise AssertionError((method, path))

    def inspect(self, target):
        return copy.deepcopy(self.raw)

    def action(self, target, action):
        self.actions.append((target, action))
        self.raw['State']['Status'] = 'exited' if action == 'stop' else 'running'
        self.raw['State']['StartedAt'] = str(len(self.actions))


class AgentTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.runtime/tests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.path = Path(self.temp.name)
        self.engine = FakeEngine()
        self.store = Store(self.path / 'state.db')
        self.config = {'daemon_id': 'expected-daemon', 'lock_path': str(self.path / 'deploy.lock'),
                       'services': [{'name': 'mailbox-web', 'module': 'mailbox', 'role': 'web', 'names': ['mailbox-web']}], 'bases': {}, 'probes': {}}
        self.agent = Agent(self.config, self.engine, self.store)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def request(self, action='restart', **changes):
        return {'action': action, 'id': CONTAINER_ID, 'request_id': str(uuid.uuid4()), 'expected_state': 'running', 'confirmation': 'mailbox-web', **changes}

    def rejected(self, body, status):
        with self.assertRaises(APIError) as context:
            self.agent.action(body)
        self.assertEqual(context.exception.status, status)
        self.assertFalse(self.engine.actions)

    def test_restart_verifies_state_and_idempotency(self):
        body = self.request()
        self.assertEqual(self.agent.action(body)['status'], 'succeeded')
        self.assertEqual(self.agent.action(body)['status'], 'succeeded')
        self.assertEqual(len(self.engine.actions), 1)
        with self.assertRaises(APIError) as context:
            self.agent.action({**body, 'action': 'stop'})
        self.assertEqual(context.exception.status, 409)

    def test_stop_and_start(self):
        self.assertEqual(self.agent.action(self.request('stop'))['status'], 'succeeded')
        self.assertEqual(self.agent.action(self.request('start', expected_state='exited'))['status'], 'succeeded')

    def test_uncertain_engine_result_is_recorded_and_never_replayed(self):
        body = self.request()
        with patch.object(self.engine, 'action', side_effect=EngineError('Disconnected')) as action:
            with self.assertRaises(APIError):
                self.agent.action(body)
            self.assertEqual(self.store.audit()[0]['status'], 'unknown')
            self.assertEqual(self.agent.action(body)['status'], 'unknown')
            self.assertEqual(action.call_count, 1)

    def test_changed_daemon_is_rejected(self):
        self.engine.daemon = 'another-daemon'
        self.rejected(self.request(), 503)
        self.assertEqual(self.store.audit()[0]['status'], 'failed')

    def test_identity_change_is_rejected(self):
        self.engine.raw['Id'] = 'c' * 64
        self.rejected(self.request(), 409)

    def test_unknown_container_is_rejected(self):
        self.engine.raw['Name'] = '/crm'
        self.rejected(self.request(), 404)

    def test_config_without_credentials_and_workspace_boundary(self):
        config = {**self.config, 'workspace_root': str(self.path), 'state_path': str(self.path / 'state.db')}
        file = self.path / 'config.json'
        file.write_text(json.dumps(config))
        self.assertEqual(load_config(file)['services'][0]['name'], 'mailbox-web')
        config['services'][0]['critical'] = 'false'
        file.write_text(json.dumps(config))
        with self.assertRaises(ValueError):
            load_config(file)
        config['services'][0]['critical'] = False
        config['lock_path'] = '/outside-workspace/deploy.lock'
        file.write_text(json.dumps(config))
        with self.assertRaises(ValueError):
            load_config(file)

    def test_all_inventory_services_can_be_controlled(self):
        self.assertNotIn('control', self.config['services'][0])
        self.assertEqual(self.agent.action(self.request())['status'], 'succeeded')

    def test_stale_state_and_wrong_confirmation(self):
        self.rejected(self.request(expected_state='exited'), 409)
        self.rejected(self.request(confirmation='another-service'), 400)

    def test_arbitrary_operation_and_non_id_are_rejected(self):
        self.rejected(self.request(action='exec'), 400)
        self.rejected(self.request(id='mailbox-web'), 400)

    def test_actual_flock_blocks_second_operation(self):
        with deploy_lock(self.config['lock_path']):
            with self.assertRaises(APIError):
                with deploy_lock(self.config['lock_path'], timeout=.01):
                    self.fail('Competing lock was obtained')

    def test_sanitized_snapshot_excludes_environment_and_health_logs(self):
        with patch('backend.agent.host_memory', return_value={'total': 24}):
            self.agent.collect()
        data = self.agent.state()
        self.assertNotIn('PRIVATE', json.dumps(data))
        self.assertEqual(data['containers'][0]['mounts'][0]['Destination'], '/data')
        self.assertEqual(data['containers'][0]['memory_bytes'], 80)

    def test_failure_preserves_timestamp_and_marks_old_data(self):
        with patch('backend.agent.host_memory', return_value={}):
            self.agent.collect()
        first = self.agent.state()['collected_at']
        self.engine.daemon = 'wrong'
        self.agent.collect()
        self.assertEqual(self.agent.state()['collected_at'], first)
        self.assertIsNotNone(self.agent.state()['error'])

    def test_pending_audit_survives_restart_as_unknown(self):
        self.store.begin('request', 'fingerprint', 'dashboard', CONTAINER_ID, 'restart')
        another = Store(self.path / 'state.db')
        self.assertEqual(another.audit()[0]['status'], 'unknown')
        another.db.close()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.app = Application('http://127.0.0.1:20135')
        self.server = Server(('127.0.0.1', 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, **headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, path, json.dumps(body) if body is not None else None,
                           {'Content-Type': 'application/json', **headers})
        response = connection.getresponse()
        data, response_headers = response.read(), dict(response.getheaders())
        status = response.status
        connection.close()
        return status, json.loads(data) if 'application/json' in response_headers.get('Content-Type', '') else data, response_headers

    def test_state_and_action_work_without_browser_credentials(self):
        with patch.object(self.app, 'proxy', return_value={'containers': []}) as proxy:
            self.assertEqual(self.request('GET', '/api/state')[1], {'containers': []})
            proxy.assert_called_once_with('GET', '/state')
        body = {'action': 'restart', 'id': CONTAINER_ID}
        with patch.object(self.app, 'proxy', return_value={'status': 'succeeded'}) as proxy:
            self.assertEqual(self.request('POST', '/api/action', body)[0], 200)
            proxy.assert_called_once_with('POST', '/action', body)

    def test_dashboard_is_visible_and_keeps_static_csp(self):
        status, body, headers = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'<div id="console">', body)
        self.assertNotIn(b'type="password"', body)
        self.assertNotIn('Set-Cookie', headers)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_no_arbitrary_proxy_or_static_traversal(self):
        for path in ('/api/containers/create', '/../../.git/config', '/api/state?extra=1'):
            self.assertEqual(self.request('GET', path)[0], 404)
        with self.assertRaises(APIError):
            self.app.proxy('POST', '/containers/create', {})
        with self.assertRaises(ValueError):
            Application('http://external.invalid:20135')

    def test_invalid_json_body_limit_and_media_type(self):
        self.assertEqual(self.request('POST', '/api/action', [1, 2])[0], 400)
        self.assertEqual(self.request('POST', '/api/action', {'data': 'x' * 9000})[0], 413)
        self.assertEqual(self.request('POST', '/api/action', {}, **{'Content-Type': 'text/plain'})[0], 415)

    def test_agent_readiness_and_actions_are_reachable_directly(self):
        agent = unittest.mock.Mock()
        agent.state.return_value = {'collected_at': time.time(), 'error': None, 'containers': []}
        agent.action.return_value = {'status': 'succeeded'}
        server = Server(('127.0.0.1', 0), agent_handler(agent))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.app.agent_url = Application('http://127.0.0.1:' + str(server.server_port)).agent_url
        try:
            self.assertEqual(self.request('GET', '/healthz')[1], {'ready': True})
            self.assertEqual(self.request('GET', '/api/state')[1]['containers'], [])
            body = {'action': 'stop', 'id': CONTAINER_ID}
            self.assertEqual(self.request('POST', '/api/action', body)[1]['status'], 'succeeded')
            agent.action.assert_called_once_with(body)
            agent.state.return_value['collected_at'] = time.time() - 120
            self.assertEqual(self.request('GET', '/healthz')[0], 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class ProbeTests(unittest.TestCase):
    def test_trickling_headers_and_body_stop_at_total_deadline(self):
        class SlowResponse(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                initial = b'HTTP/1.1 200 OK\r\nX-Slow: ' if self.path == '/headers' else b'HTTP/1.1 200 OK\r\nContent-Length: 32\r\n\r\n'
                try:
                    self.wfile.write(initial)
                    for _ in range(32):
                        self.wfile.write(b'x')
                        time.sleep(.04)
                except OSError:
                    pass

        server = Server(('127.0.0.1', 0), SlowResponse)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ('/headers', '/body'):
                with self.subTest(path=path), patch('backend.agent.PROBE_TIMEOUT', .15):
                    start = time.monotonic()
                    result = probe('slow', {'host': '127.0.0.1', 'port': server.server_port, 'path': path})
                    self.assertLess(time.monotonic() - start, .8)
                    self.assertIsNotNone(result['error'])
                    self.assertIsNone(result['http_ms'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class ResourceTests(unittest.TestCase):
    def test_engine_cpu_delta_and_cgroup_cache_subtraction(self):
        result = resource_usage({'cpu_stats': {'cpu_usage': {'total_usage': 300}, 'system_cpu_usage': 1000, 'online_cpus': 4},
                                 'precpu_stats': {'cpu_usage': {'total_usage': 100}, 'system_cpu_usage': 500},
                                 'memory_stats': {'usage': 100, 'stats': {'inactive_file': 30}}})
        self.assertEqual(result['cpu_percent'], 160)
        self.assertEqual(result['memory_bytes'], 70)
        self.assertIsNone(resource_usage({})['cpu_percent'])


if __name__ == '__main__':
    unittest.main()
