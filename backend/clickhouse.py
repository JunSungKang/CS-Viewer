"""HTTP client with sanitized errors and explicit credentials."""
import base64
import datetime
import http.client
import json
import re
from urllib.parse import urlencode, urlsplit

class StorageError(Exception):
    pass

def timestamp(ns):
    sec, fraction = divmod(ns, 1000000000)
    return datetime.datetime.fromtimestamp(sec, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S') + '.%09d' % fraction

class Client:
    def __init__(self, settings):
        self.settings = dict(settings)
        self.url = urlsplit(settings['url'])
        if self.url.scheme not in ('http', 'https') or self.url.username or self.url.query or self.url.fragment or self.url.path not in ('', '/'):
            raise ValueError('Invalid ClickHouse endpoint')
        self.database = settings.get('database', 'cs_viewer')
        if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', self.database):
            raise ValueError('Invalid database')

    def query(self, sql, parameters=None, settings=None, data=None):
        options = {'query': sql, **(settings or {})}
        options.update({'param_' + k: str(v) for k, v in (parameters or {}).items()})
        kind = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
        conn = kind(self.url.hostname, self.url.port, timeout=self.settings.get('timeout', 8))
        auth = base64.b64encode((self.settings['user'] + ':' + self.settings['password']).encode()).decode()
        try:
            conn.request('POST', '/?' + urlencode(options), body=data or b'', headers={'Authorization': 'Basic ' + auth})
            response = conn.getresponse(); body = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200 or len(body) > 4 * 1024 * 1024 or re.search(rb'(?:^|\n)Code: [0-9]+\.',body):
                raise StorageError('ClickHouse request failed (HTTP %s)' % response.status)
            return body
        except (OSError, http.client.HTTPException) as exc:
            raise StorageError('ClickHouse connection failed') from exc
        finally:
            conn.close()

    def rows(self, sql, parameters=None, settings=None):
        try:
            return [json.loads(line) for line in self.query(sql + ' FORMAT JSONEachRow', parameters, {'output_format_json_quote_64bit_integers':0, **(settings or {})}).splitlines() if line]
        except (ValueError, UnicodeError) as exc:
            raise StorageError('Invalid ClickHouse response') from exc

    def insert(self, table, rows, token):
        if table not in ('service_samples', 'container_logs', 'action_events'):
            raise ValueError('Invalid destination')
        data = b'\n'.join(json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode() for row in rows) + b'\n'
        self.query('INSERT INTO ' + self.database + '.' + table + ' FORMAT JSONEachRow', settings={'async_insert': 0,
            'insert_deduplicate': 1, 'insert_deduplication_token': token, 'date_time_input_format': 'best_effort'}, data=data)
