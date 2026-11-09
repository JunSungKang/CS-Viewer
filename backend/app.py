import argparse
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from urllib.parse import urlsplit

from .http_support import APIError, Handler, Server

WEB = Path(__file__).resolve().parents[1] / 'web'


class Application:
    def __init__(self, password, agent_token, agent_url, origins):
        if len(password) < 16 or len(agent_token) < 32:
            raise ValueError('Use generated secrets with at least 16/32 characters')
        self.salt = secrets.token_bytes(32)
        self.password_hash = self.hash_password(password)
        self.agent_token, self.agent_url = agent_token, urlsplit(agent_url)
        if self.agent_url.scheme != 'http' or self.agent_url.hostname not in ('host.docker.internal', '127.0.0.1', 'localhost'):
            raise ValueError('Agent must be a local HTTP adapter')
        self.origins = {origin.strip().rstrip('/') for origin in origins}
        for origin in self.origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError('Use canonical HTTP(S) origins without paths')
            if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
                raise ValueError('Non-loopback browser access requires HTTPS')
        self.hosts = {urlsplit(origin).netloc for origin in self.origins}
        self.secure_cookie = all(urlsplit(origin).scheme == 'https' for origin in self.origins)
        self.lock = threading.Lock()
        self.sessions, self.failures = {}, {}

    def hash_password(self, password):
        return hashlib.scrypt(password.encode(), salt=self.salt, n=16384, r=8, p=1)

    def login(self, ip, body):
        password = body.get('password')
        if body.get('username') != 'operator' or not isinstance(password, str) or len(password) > 1024:
            raise APIError(401, '계정 또는 비밀번호가 일치하지 않습니다.')
        with self.lock:
            now = time.monotonic()
            self.failures = {key: values for key, values in self.failures.items() if values and values[-1] > now - 60}
            attempts = [t for t in self.failures.get(ip, []) if t > now - 60]
            if len(attempts) >= 5 or sum(map(len, self.failures.values())) >= 100:
                raise APIError(429, '로그인 시도가 많습니다. 잠시 후 다시 시도하세요.')
            attempts.append(now)
            self.failures[ip] = attempts
        valid = hmac.compare_digest(self.hash_password(password), self.password_hash)
        if not valid:
            raise APIError(401, '계정 또는 비밀번호가 일치하지 않습니다.')
        with self.lock:
            self.failures.pop(ip, None)
            self.sessions = {key: val for key, val in self.sessions.items() if val['expires'] > now and val['last'] > now - 1800}
            if len(self.sessions) >= 100:
                raise APIError(429, '활성 세션이 너무 많습니다.')
            token = secrets.token_urlsafe(32)
            session = {'csrf': secrets.token_urlsafe(32), 'expires': now + 28800, 'last': now}
            self.sessions[token] = session
            return token, session['csrf']

    def session(self, cookie):
        try:
            parsed = SimpleCookie(cookie or '')
            token = parsed['CS_SESSION'].value
        except (CookieError, KeyError):
            raise APIError(401, '로그인이 필요합니다.')
        with self.lock:
            session = self.sessions.get(token)
            now = time.monotonic()
            if not session or session['expires'] < now or session['last'] < now - 1800:
                self.sessions.pop(token, None)
                raise APIError(401, '세션이 만료되었습니다.')
            session['last'] = now
            return token, dict(session)

    def proxy(self, method, path, body=None):
        if (method, path) not in (('GET', '/state'), ('GET', '/healthz'), ('POST', '/action')):
            raise APIError(404, '허용되지 않은 요청')
        connection = http.client.HTTPConnection(self.agent_url.hostname, self.agent_url.port or 20135, timeout=65)
        try:
            connection.request(method, path, json.dumps(body).encode() if body is not None else None,
                               {'Authorization': 'Bearer ' + self.agent_token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError()
            value = json.loads(raw)
            if response.status != 200:
                raise APIError(response.status if response.status in (400, 403, 409, 503) else 503,
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
            if self.command == 'GET' and url.path == '/healthz' and not url.query:
                self.send(200, app.proxy('GET', '/healthz'))
                return
            if self.headers.get('Host') not in app.hosts:
                raise APIError(403, '허용되지 않은 호스트입니다.')
            if url.query:
                raise APIError(404, '지원하지 않는 경로입니다.')
            if self.command == 'POST':
                if self.headers.get('Origin') not in app.origins:
                    raise APIError(403, '요청 출처가 일치하지 않습니다.')
                if url.path == '/api/login':
                    token, csrf = app.login(self.client_address[0], self.json_body())
                    cookie = 'CS_SESSION=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'
                    if app.secure_cookie or urlsplit(self.headers['Origin']).scheme == 'https':
                        cookie += '; Secure'
                    self.send(200, {'csrf': csrf, 'username': 'operator'}, {'Set-Cookie': cookie})
                    return
                token, session = app.session(self.headers.get('Cookie'))
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
                    raise APIError(403, '요청 검증에 실패했습니다. 다시 로그인하세요.')
                if url.path == '/api/logout':
                    self.json_body()
                    with app.lock:
                        app.sessions.pop(token, None)
                    self.send(200, {}, {'Set-Cookie': 'CS_SESSION=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
                elif url.path == '/api/action':
                    self.send(200, app.proxy('POST', '/action', self.json_body()))
                else:
                    raise APIError(404, '지원하지 않는 작업입니다.')
            elif self.command == 'GET':
                if url.path in ('/', '/app.js', '/style.css'):
                    filename, content_type = {'/': ('index.html', 'text/html; charset=utf-8'),
                        '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}[url.path]
                    self.send(200, (WEB / filename).read_bytes(), content_type=content_type)
                elif url.path == '/api/session':
                    _, session = app.session(self.headers.get('Cookie'))
                    self.send(200, {'csrf': session['csrf'], 'username': 'operator'})
                elif url.path == '/api/state':
                    app.session(self.headers.get('Cookie'))
                    self.send(200, app.proxy('GET', '/state'))
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
    app = Application(Path(os.environ['CS_PASSWORD_FILE']).read_text().strip(),
                      Path(os.environ['CS_AGENT_TOKEN_FILE']).read_text().strip(),
                      os.environ.get('CS_AGENT_URL', 'http://host.docker.internal:20135'),
                      os.environ.get('CS_ALLOWED_ORIGINS', 'http://127.0.0.1:20035,http://localhost:20035').split(','))
    server = Server((args.bind, args.port), make_handler(app))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
