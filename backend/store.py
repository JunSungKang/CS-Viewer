import json
import sqlite3
import threading
import time


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS samples(ts REAL, module TEXT, data TEXT);
          CREATE INDEX IF NOT EXISTS samples_module_ts ON samples(module,ts);
          CREATE TABLE IF NOT EXISTS audit(request_id TEXT PRIMARY KEY, fingerprint TEXT, ts REAL, actor TEXT,
            target TEXT, action TEXT, status TEXT, message TEXT);
        ''')
        with self.lock, self.db:
            self.db.execute("UPDATE audit SET status='unknown',message='프로세스 재시작: 실제 상태를 다시 확인하세요.' WHERE status='pending'")

    def sample(self, services):
        now = time.time()
        with self.lock, self.db:
            self.db.executemany('INSERT INTO samples VALUES(?,?,?)', [(now, s['module'], json.dumps(s)) for s in services])
            self.db.execute('DELETE FROM samples WHERE ts < ?', (now - 86400,))
            self.db.execute('DELETE FROM audit WHERE ts < ? AND status != ?', (now - 90 * 86400, 'pending'))

    def history(self, module):
        with self.lock:
            return [dict(ts=row['ts'], **json.loads(row['data'])) for row in reversed(self.db.execute(
                'SELECT ts,data FROM samples WHERE module=? ORDER BY ts DESC LIMIT 60', (module,)).fetchall())]

    def begin(self, request_id, fingerprint, actor, target, action):
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM audit WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise ValueError('다른 요청에서 사용한 작업 ID입니다.')
                return dict(row)
            self.db.execute('INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)',
                            (request_id, fingerprint, time.time(), actor, target, action, 'pending', '상태 확인 중'))
            return None

    def finish(self, request_id, status, message):
        with self.lock, self.db:
            self.db.execute('UPDATE audit SET status=?,message=? WHERE request_id=?', (status, message, request_id))
            return dict(self.db.execute('SELECT * FROM audit WHERE request_id=?', (request_id,)).fetchone())

    def audit(self):
        with self.lock:
            return [{k: row[k] for k in ('request_id', 'ts', 'actor', 'target', 'action', 'status', 'message')}
                    for row in self.db.execute('SELECT * FROM audit ORDER BY ts DESC LIMIT 50')]
