import argparse
import http.client
import json
import os
from pathlib import Path
import signal
import threading
from urllib.parse import urlsplit

from .http_support import APIError, Handler, Server

WEB = Path(__file__).resolve().parents[1] / 'web'


class Application:
    def __init__(self, agent_url):
        self.agent_url = urlsplit(agent_url)
        if (self.agent_url.scheme != 'http'
                or self.agent_url.hostname not in ('host.docker.internal', '127.0.0.1', 'localhost')
                or self.agent_url.path not in ('', '/')
                or self.agent_url.query or self.agent_url.fragment or '@' in self.agent_url.netloc):
            raise ValueError('Agent must be a local HTTP adapter')

    def proxy(self, method, path, body=None):
        if (method, path) not in (('GET', '/state'), ('GET', '/healthz'), ('POST', '/action')):
            raise APIError(404, '지원하지 않는 요청')
        connection = http.client.HTTPConnection(self.agent_url.hostname, self.agent_url.port or 20135, timeout=65)
        try:
            connection.request(method, path, json.dumps(body).encode() if body is not None else None,
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError()
            value = json.loads(raw)
            if response.status != 200:
                raise APIError(response.status if response.status in (400, 404, 409, 503) else 503,
                               value.get('error', '에이전트 요청 실패'))
            return value
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise APIError(503, '호스트 수집기에 연결할 수 없습니다.') from exc
        finally:
            connection.close()


def make_handler(app):
    class AppHandler(Handler):
        def route(self):
            url = urlsplit(self.path)
            if url.query:
                raise APIError(404, '지원하지 않는 경로입니다.')
            if self.command == 'POST':
                if url.path != '/api/action':
                    raise APIError(404, '지원하지 않는 작업입니다.')
                self.send(200, app.proxy('POST', '/action', self.json_body()))
            elif self.command == 'GET':
                if url.path in ('/', '/app.js', '/style.css'):
                    filename, content_type = {'/': ('index.html', 'text/html; charset=utf-8'),
                        '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}[url.path]
                    self.send(200, (WEB / filename).read_bytes(), content_type=content_type)
                elif url.path in ('/api/state', '/healthz'):
                    self.send(200, app.proxy('GET', '/state' if url.path == '/api/state' else '/healthz'))
                else:
                    raise APIError(404, '지원하지 않는 경로입니다.')

        def do_GET(self):
            self.run_route(self.route)

        def do_POST(self):
            self.run_route(self.route)
    return AppHandler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--bind', default='0.0.0.0')
    args = parser.parse_args()
    app = Application(os.environ.get('CS_AGENT_URL', 'http://host.docker.internal:20135'))
    server = Server((args.bind, args.port), make_handler(app))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
