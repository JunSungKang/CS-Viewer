"""Read only inventoried Docker streams. Persist offsets after durable enqueue."""
import base64
import calendar
import hashlib
import http.client
import json
from pathlib import Path
import re
import struct
import threading
import time
import uuid
from urllib.parse import urlencode

from .clickhouse import timestamp
from .engine import UnixConnection, EngineError
from .spool import atomic_json

STAMP = re.compile(rb'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\.(\d{1,9})Z ')

def decode_record(payload):
    match = STAMP.match(payload)
    if not match: raise ValueError('Docker timestamp missing')
    ns = calendar.timegm(time.strptime(match[1].decode(), '%Y-%m-%dT%H:%M:%S')) * 1000000000 + int(match[2].ljust(9,b'0'))
    return ns, payload[match.end():]

def read_exact(response, size):
    result=bytearray()
    while len(result)<size:
        piece=response.read(size-len(result))
        if not piece: raise ValueError('Incomplete Docker log frame')
        result.extend(piece)
    return bytes(result)

def frames(response, tty=False):
    if tty:
        # A TTY has no multiplex header. Docker prefixes even partial long messages.
        marker=re.compile(rb'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{9}Z ')
        buffer=b''
        while True:
            piece=response.read1(65536)
            buffer+=piece
            matches=list(marker.finditer(buffer))
            if matches and matches[0].start()!=0:raise ValueError('TTY timestamp prefix missing')
            if len(matches)>1:
                for a,b in zip(matches,matches[1:]):yield 'tty',buffer[a.start():b.start()]
                buffer=buffer[matches[-1].start():]
            if len(buffer)>16*1024*1024:raise ValueError('TTY frame limit exceeded')
            if not piece:
                if buffer:yield 'tty',buffer
                return
    while True:
        first=response.read(1)
        if not first:return
        header=first+read_exact(response,7)
        channel=header[0];size=struct.unpack('>I',header[4:])[0]
        if channel not in (1,2) or header[1:4]!=b'\0\0\0' or size>16*1024*1024:
            raise ValueError('Invalid Docker log frame')
        yield ('stdout' if channel==1 else 'stderr'),read_exact(response,size)

class Collector:
    def __init__(self, engine, store, aliases, path, daemon_id=None):
        self.engine=engine;self.store=store;self.aliases=aliases;self.path=Path(path)
        self.path.mkdir(parents=True,exist_ok=True);self.lock=threading.Lock();self.error=None;self.daemon_id=daemon_id
        origin=self.path/'origin.json'
        if not origin.exists():atomic_json(origin,{'cutover_ns':time.time_ns()})
        self.cutover=json.loads(origin.read_text())['cutover_ns']

    def poll(self):
        if not self.lock.acquire(blocking=False):return
        try:
            if self.daemon_id and self.engine.call('GET','/info')['ID']!=self.daemon_id:raise EngineError('Docker daemon changed')
            listed=self.engine.call('GET','/containers/json?all=true')
            failures=[]
            for item in listed:
                name=next((n.lstrip('/') for n in item.get('Names',[]) if n.lstrip('/') in self.aliases),None)
                if name:
                    try:self.collect(item['Id'],name,self.aliases[name])
                    except (OSError,ValueError,EngineError,http.client.HTTPException):failures.append(name)
            self.error=('일부 컨테이너 로그 수집 지연: '+', '.join(failures)) if failures else None
        except (OSError,ValueError,EngineError,http.client.HTTPException):
            self.error='Docker 로그 수집 지연: 드라이버·대기열·회전 여부를 확인하세요.'
        finally:self.lock.release()

    def collect(self, cid, name, service):
        if not re.fullmatch('[0-9a-f]{64}',cid):raise ValueError('Invalid container identity')
        path=self.path/(cid+'.json')
        cursor=json.loads(path.read_text()) if path.exists() else {}
        seen={};last={k:dict(v) for k,v in cursor.items()};pending=[]
        def persist():
            if pending:
                self.store.enqueue('container_logs',pending)
                atomic_json(path,last)
                pending.clear()
        start=min((v['ns'] for v in cursor.values()),default=self.cutover)
        params=urlencode({'stdout':1,'stderr':1,'timestamps':1,'since':start//1000000000,'until':int(time.time())-1})
        raw=self.engine.inspect(cid)
        if raw['Id']!=cid or raw['Name'].lstrip('/')!=name:raise EngineError('Container changed')
        conn=UnixConnection(self.engine.path,5)
        try:
            conn.request('GET',self.engine.prefix+'/containers/'+cid+'/logs?'+params)
            response=conn.getresponse()
            if response.status!=200:raise EngineError('Docker logging driver cannot be read')
            for stream,payload in frames(response,raw.get('Config',{}).get('Tty',False)):
                ns,body=decode_record(payload)
                key=(stream,ns);seen[key]=seen.get(key,0)+1;occurrence=seen[key]
                old=cursor.get(stream,{'ns':self.cutover,'count':0})
                if ns<old['ns'] or ns==old['ns'] and occurrence<=old['count']:continue
                rid=hashlib.sha256(cid.encode()+stream.encode()+str(ns).encode()+str(occurrence).encode()+body).hexdigest()[:32]
                rows=[];batch=str(uuid.uuid4())
                for chunk,offset in enumerate(range(0,max(1,len(body)),256*1024)):
                    piece=body[offset:offset+256*1024]
                    try:text=piece.decode('utf-8');encoding='utf8'
                    except UnicodeError:text=base64.b64encode(piece).decode();encoding='base64'
                    rows.append({'event_time':timestamp(ns),'received_at':int(time.time()),'batch_id':batch,
                        'module':service['module'],'role':service['role'],'container_name':name,'container_id':cid,
                        'stream':stream,'record_id':rid,'occurrence':occurrence,'chunk':chunk,'body':text,'encoding':encoding})
                pending.extend(rows)
                last[stream]={'ns':ns,'count':occurrence}
                if len(pending)>=200:persist()
            persist()
        finally:conn.close()
