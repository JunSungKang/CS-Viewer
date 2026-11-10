import http.client
import json
import socket
from urllib.parse import quote


class EngineError(Exception):
    pass


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=5):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Engine:
    def __init__(self, path):
        self.path = path
        version = self.call('GET', '/version')
        api = min(tuple(map(int, version['ApiVersion'].split('.'))), (1, 56))
        if api < (1, 44):
            raise EngineError('Docker API 1.44 이상이 필요합니다.')
        self.prefix = '/v' + '.'.join(map(str, api))

    def call(self, method, path, timeout=5):
        connection = UnixConnection(self.path, timeout)
        try:
            connection.request(method, getattr(self, 'prefix', '') + path)
            response = connection.getresponse()
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise EngineError('Docker 응답 한도를 초과했습니다.')
            if response.status not in (200, 204, 304):
                raise EngineError('Docker 요청 실패 (HTTP %s)' % response.status)
            return json.loads(body) if body else {}
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise EngineError('Docker 연결 또는 응답을 확인할 수 없습니다.') from exc
        finally:
            connection.close()

    def inspect(self, container_id):
        return self.call('GET', '/containers/' + quote(container_id, safe='') + '/json')

    def action(self, container_id, action):
        if action not in ('start', 'stop', 'restart'):
            raise EngineError('허용되지 않은 작업입니다.')
        return self.call('POST', '/containers/' + quote(container_id, safe='') + '/' + action + '?t=20', 35)


def resource_usage(stats):
    cpu, previous = stats.get('cpu_stats', {}), stats.get('precpu_stats', {})
    delta = cpu.get('cpu_usage', {}).get('total_usage', 0) - previous.get('cpu_usage', {}).get('total_usage', 0)
    system = cpu.get('system_cpu_usage', 0) - previous.get('system_cpu_usage', 0)
    cores = cpu.get('online_cpus', 0) or len(cpu.get('cpu_usage', {}).get('percpu_usage', []))
    memory = stats.get('memory_stats', {})
    cache = memory.get('stats', {}).get('inactive_file', memory.get('stats', {}).get('total_inactive_file', 0))
    return {'cpu_percent': round(delta / system * cores * 100, 1) if system > 0 and delta >= 0 else None,
            'memory_bytes': max(0, memory['usage'] - cache) if 'usage' in memory else None,
            'memory_limit': memory.get('limit')}
