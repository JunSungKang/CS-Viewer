import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler):
        import threading
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'CS-Viewer'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(45)

    def log_message(self, *_args):
        pass  # Keep request headers and bodies out of operational logs.

    def send(self, status, value, headers=None, content_type='application/json; charset=utf-8'):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        for key, val in {'Content-Type': content_type, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store',
                         'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                         'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
                         **(headers or {})}.items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)

    def json_body(self):
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            raise APIError(400, '요청 길이를 확인하세요.')
        try:
            length = int(self.headers['Content-Length'])
        except ValueError as exc:
            raise APIError(400, '요청 길이를 확인하세요.') from exc
        if length < 2 or length > 8192:
            raise APIError(413, '요청 크기가 허용 범위를 벗어났습니다.')
        if self.headers.get_content_type() != 'application/json':
            raise APIError(415, 'JSON 요청이 필요합니다.')
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            result = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise APIError(400, '잘못된 JSON 요청입니다.') from exc
        if not isinstance(result, dict):
            raise APIError(400, 'JSON 객체가 필요합니다.')
        return result

    def run_route(self, callback):
        try:
            callback()
        except APIError as exc:
            self.send(exc.status, {'error': exc.message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self.send(503, {'error': '요청을 완료하지 못했습니다. 수집기와 운영 로그를 확인하세요.'})
