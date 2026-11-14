"""Immutable bounded outbox with crash-safe acknowledgement."""
import hashlib
import json
import os
import re
from pathlib import Path
import threading
import time
import uuid

def atomic_json(path, value):
    path = Path(path); temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as f:
            os.chmod(temporary, 0o600)
            json.dump(value, f, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)

class Spool:
    def __init__(self, path, limit=1024**3):
        self.path = Path(path); self.path.mkdir(parents=True, exist_ok=True)
        self.limit = limit; self.lock = threading.RLock(); self._recover()

    def _names(self, manifest):
        names = manifest['files']
        if not isinstance(names,list) or any(not isinstance(n,str) or Path(n).name != n or not re.fullmatch(r'[0-9]{20}-[0-9a-f]{32}\.pending',n) for n in names):
            raise ValueError('Invalid outbox manifest')
        return names

    def _recover(self):
        ack = self.path / 'acked.json'
        if ack.exists():
            for name in self._names(json.loads(ack.read_text())): (self.path/name).unlink(missing_ok=True)
            ack.unlink()
        for p in self.path.glob('*.tmp'):
            if re.fullmatch(r'(?:[0-9]{20}-[0-9a-f]{32}\.pending|inflight\.json|acked\.json)\.[0-9a-f]{32}\.tmp',p.name):p.unlink()

    def size(self):
        with self.lock:
            return sum(p.stat().st_size for p in self.path.iterdir() if p.is_file())

    def enqueue(self, kind, rows):
        if not rows: return
        value = {'kind':kind,'rows':rows}; needed = len(json.dumps(value,ensure_ascii=True).encode())
        with self.lock:
            if self.size()+needed+4096 > self.limit: raise OSError('Outbox capacity reached')
            group=[];size=0
            for row in rows:
                row_size=len(json.dumps(row,ensure_ascii=True).encode())
                if group and (size+row_size>1024**2 or len(group)>=1000):
                    atomic_json(self.path/('%020d-'%time.time_ns()+uuid.uuid4().hex+'.pending'),{'kind':kind,'rows':group})
                    group=[];size=0
                group.append(row);size+=row_size
            if group:atomic_json(self.path/('%020d-'%time.time_ns()+uuid.uuid4().hex+'.pending'),{'kind':kind,'rows':group})

    def prepare(self):
        with self.lock:
            self._recover(); p = self.path/'inflight.json'
            if p.exists(): manifest = json.loads(p.read_text())
            else:
                names=[]; kind=None; count=0; size=0
                for f in sorted(self.path.glob('*.pending')):
                    item=json.loads(f.read_text())
                    if kind is not None and item['kind'] != kind: continue
                    if names and (count+len(item['rows'])>1000 or size+f.stat().st_size>1024**2): break
                    kind=item['kind'];names.append(f.name);count+=len(item['rows']);size+=f.stat().st_size
                if not names: return None
                manifest={'files':names,'kind':kind,'token':hashlib.sha256('\n'.join(names).encode()).hexdigest()}
                atomic_json(p,manifest)
            rows=[]
            for name in self._names(manifest):
                item=json.loads((self.path/name).read_text())
                if item['kind']!=manifest['kind']: raise ValueError('Outbox destination mismatch')
                rows.extend(item['rows'])
            return manifest,rows

    def acknowledge(self):
        with self.lock:
            os.replace(self.path/'inflight.json',self.path/'acked.json')
            fd=os.open(self.path,os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
            self._recover()
