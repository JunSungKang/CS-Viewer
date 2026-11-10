import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from backend.agent import Agent, deploy_lock, load_config, make_handler as agent_handler
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
                       'services': [{'name': 'mailbox-web', 'module': 'mailbox', 'role': 'web', 'names': ['mailbox-web'], 'control': True}], 'bases': {}, 'probes': {}}
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
        self.rejected(self.request(), 403)

    def test_unknown_container_is_rejected(self):
        self.engine.raw['Name'] = '/crm'
        self.rejected(self.request(), 403)

    def test_read_only_service_is_rejected(self):
        self.config['services'][0]['control'] = False
        self.rejected(self.request(), 403)

    def test_string_permission_is_not_truthy_and_config_cannot_escape_workspace(self):
        self.config['services'][0]['control'] = 'false'
        self.rejected(self.request(), 403)
        config = {**self.config, 'workspace_root': str(self.path), 'state_path': str(self.path / 'state.db'), 'token_file': str(self.path / 'token')}
        file = self.path / 'config.json';file.write_text(json.dumps(config))
        with self.assertRaises(ValueError):
            load_config(file)
        config['services'][0]['control'] = False
        config['lock_path'] = '/outside-authorized-workspace/deploy.lock'
        file.write_text(json.dumps(config))
        with self.assertRaises(ValueError):
            load_config(file)

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
        self.store.begin('request', 'fingerprint', 'operator', CONTAINER_ID, 'restart')
        another = Store(self.path / 'state.db')
        self.assertEqual(another.audit()[0]['status'], 'unknown')
        another.db.close()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.app = Application('test-password-for-local-unit-tests', 'a' * 64, 'http://127.0.0.1:20135', ['http://127.0.0.1:20035'])
        self.server = Server(('127.0.0.1', 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, **headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        defaults = {'Host': '127.0.0.1:20035', 'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1:20035', **headers}
        connection.request(method, path, json.dumps(body) if body is not None else None, defaults)
        response = connection.getresponse()
        data, response_headers = response.read(), dict(response.getheaders())
        status = response.status
        connection.close()
        return status, json.loads(data) if 'application/json' in response_headers.get('Content-Type', '') else data, response_headers

    def login(self):
        status, data, headers = self.request('POST', '/api/login', {'username': 'operator', 'password': 'test-password-for-local-unit-tests'})
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        return data['csrf'], headers['Set-Cookie'].split(';')[0]

    def test_authentication_csrf_and_origin(self):
        self.assertEqual(self.request('GET', '/api/state')[0], 401)
        csrf, cookie = self.login()
        self.assertEqual(self.request('POST', '/api/action', {}, Cookie=cookie)[0], 403)
        self.assertEqual(self.request('POST', '/api/action', {}, Cookie=cookie, **{'X-CSRF-Token': csrf, 'Origin': 'https://other.invalid'})[0], 403)
        self.assertEqual(self.request('POST', '/api/logout', {}, Cookie=cookie, **{'X-CSRF-Token': csrf})[0], 200)
        self.assertEqual(self.request('GET', '/api/session', Cookie=cookie)[0], 401)

    def test_no_arbitrary_proxy_or_static_traversal(self):
        _, cookie = self.login()
        self.assertEqual(self.request('GET', '/api/containers/create', Cookie=cookie)[0], 404)
        self.assertEqual(self.request('GET', '/../../.git/config')[0], 404)
        self.assertEqual(self.request('GET', '/api/state', Host='evil.invalid')[0], 403)

    def test_login_rate_limit_and_invalid_json_shape(self):
        for _ in range(5):
            self.assertEqual(self.request('POST', '/api/login', {'username': 'operator', 'password': 'wrong'})[0], 401)
        self.assertEqual(self.request('POST', '/api/login', {'username': 'operator', 'password': 'wrong'})[0], 429)
        self.assertEqual(self.request('POST', '/api/login', [1, 2])[0], 400)

    def test_valid_session_and_static_csp(self):
        csrf, cookie = self.login()
        self.assertEqual(self.request('GET', '/api/session', Cookie=cookie)[1]['csrf'], csrf)
        status, body, headers = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'CS-Viewer', body)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_expired_session_body_limit_and_media_type(self):
        _, cookie = self.login()
        self.app.sessions[cookie.split('=', 1)[1]]['expires'] = 0
        self.assertEqual(self.request('GET', '/api/session', Cookie=cookie)[0], 401)
        self.assertEqual(self.request('POST', '/api/login', {'password': 'x' * 9000})[0], 413)
        self.assertEqual(self.request('POST', '/api/login', {}, **{'Content-Type': 'text/plain'})[0], 415)

    def test_public_origin_requires_tls_and_mixed_origins_keep_secure_cookie(self):
        with self.assertRaises(ValueError):
            Application('test-password-for-local-unit-tests', 'a' * 64, 'http://127.0.0.1:20135', ['http://public.invalid'])
        self.app.origins.add('https://control.example.invalid')
        self.app.hosts.add('control.example.invalid')
        status, _, headers = self.request('POST', '/api/login', {'username': 'operator', 'password': 'test-password-for-local-unit-tests'}, Host='control.example.invalid', Origin='https://control.example.invalid')
        self.assertEqual(status, 200)
        self.assertIn('; Secure', headers['Set-Cookie'])

    def test_remote_http_opt_in_preserves_auth_host_origin_and_csrf(self):
        origin = 'http://control.example.invalid:20035'
        for disabled in (False, '1'):
            with self.assertRaises(ValueError):
                Application('test-password-for-local-unit-tests', 'a' * 64, 'http://127.0.0.1:20135', [origin], allow_remote_http=disabled)
        self.app = Application('test-password-for-local-unit-tests', 'a' * 64, 'http://127.0.0.1:20135', [origin], allow_remote_http=True)
        self.server.RequestHandlerClass = make_handler(self.app)
        headers = {'Host': 'control.example.invalid:20035', 'Origin': origin}
        self.assertEqual(self.request('GET', '/api/state', **headers)[0], 401)
        status, data, response = self.request('POST', '/api/login', {'username': 'operator', 'password': 'test-password-for-local-unit-tests'}, **headers)
        self.assertEqual(status, 200)
        cookie = response['Set-Cookie'].split(';')[0]
        self.assertNotIn('; Secure', response['Set-Cookie'])
        self.assertIn('; HttpOnly; SameSite=Strict;', response['Set-Cookie'])
        self.assertEqual(self.request('GET', '/api/session', Cookie=cookie, **headers)[0], 200)
        self.assertEqual(self.request('POST', '/api/action', {}, Cookie=cookie, **headers)[0], 403)
        self.assertEqual(self.request('POST', '/api/action', {}, Cookie=cookie, **{**headers, 'Origin': 'http://other.invalid:20035', 'X-CSRF-Token': data['csrf']})[0], 403)
        self.assertEqual(self.request('GET', '/', **{**headers, 'Host': 'other.invalid:20035'})[0], 403)
        self.assertEqual(self.request('POST', '/api/logout', {}, Cookie=cookie, **{**headers, 'X-CSRF-Token': data['csrf']})[0], 200)


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
