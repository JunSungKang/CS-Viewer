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
        # Docker TTY output has no frame boundaries. Preserve its timestamped bytes.
        while True:
            piece=response.read(256*1024)
            if not piece:return
            yield 'tty',piece
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
        previous=cursor.get('window',{}).get('until')
        end=int(time.time())-1
        if previous is not None and end<=previous:return
        partials={k:dict(v) for k,v in cursor.get('partials',{}).items()}
        tty_open=cursor.get('tty_open')
        since_ns=previous*1000000000+1 if previous is not None else self.cutover
        for partial in partials.values():since_ns=min(since_ns,partial['ns'])
        if tty_open:since_ns=min(since_ns,tty_open['ns'])
        since='%d.%09d'%divmod(since_ns,1000000000)
        params=urlencode({'stdout':1,'stderr':1,'timestamps':1,'since':since,'until':end})
        raw=self.engine.inspect(cid)
        if raw['Id']!=cid or raw['Name'].lstrip('/')!=name:raise EngineError('Container changed')
        tty=raw.get('Config',{}).get('Tty',False)
        conn=UnixConnection(self.engine.path,5)
        pending=[];counts={};first_ns=None;position=0;prefix_hash=hashlib.sha256();full_hash=hashlib.sha256();last_byte=b''
        old_bytes=tty_open['bytes'] if tty_open else 0
        next_partials={k:dict(v) for k,v in partials.items()}
        def persist():
            if pending:
                self.store.enqueue('container_logs',pending)
                pending.clear()
        try:
            conn.request('GET',self.engine.prefix+'/containers/'+cid+'/logs?'+params)
            response=conn.getresponse()
            if response.status!=200:raise EngineError('Docker logging driver cannot be read')
            for stream,payload in frames(response,tty):
                if tty:
                    if first_ns is None:first_ns=decode_record(payload)[0]
                    full_hash.update(payload);last_byte=payload[-1:]
                    skipped=min(len(payload),max(0,old_bytes-position))
                    prefix_hash.update(payload[:skipped])
                    old_position=position;position+=len(payload)
                    if position>=old_bytes and tty_open and prefix_hash.hexdigest()!=tty_open['sha256']:
                        raise EngineError('TTY source prefix changed; rotation or loss requires inspection')
                    body=payload[skipped:]
                    if not body:continue
                    ns=first_ns;occurrence=1
                    offset_id=old_position+skipped
                    rid=hashlib.sha256((cid+':'+str(first_ns)).encode()).hexdigest()[:32]
                else:
                    ns,body=decode_record(payload)
                    last_count=counts.get(stream,{'ns':None,'count':0})
                    occurrence=last_count['count']+1 if last_count['ns']==ns else 1
                    counts[stream]={'ns':ns,'count':occurrence}
                    old_partial=partials.get(stream)
                    replay_partial=old_partial is not None and ns==old_partial['ns']
                    if previous is not None and ns<=previous*1000000000 and not replay_partial:continue
                    if replay_partial and occurrence<=old_partial['count']:continue
                    if body.endswith(b'\n'):next_partials.pop(stream,None)
                    else:next_partials[stream]={'ns':ns,'count':occurrence}
                    rid=hashlib.sha256(cid.encode()+stream.encode()+str(ns).encode()+str(occurrence).encode()+body).hexdigest()[:32]
                batch=str(uuid.uuid4())
                for chunk,offset in enumerate(range(0,max(1,len(body)),256*1024)):
                    piece=body[offset:offset+256*1024]
                    try:text=piece.decode('utf-8');encoding='utf8'
                    except UnicodeError:text=base64.b64encode(piece).decode();encoding='base64'
                    pending.append({'event_time':timestamp(ns),'received_at':int(time.time()),'batch_id':batch,
                        'module':service['module'],'role':service['role'],'container_name':name,'container_id':cid,
                        'stream':stream,'record_id':rid,'occurrence':occurrence,'chunk':offset_id+offset if tty else chunk,'body_bytes':len(piece),'body':text,'encoding':encoding})
                if len(pending)>=200:persist()
            if tty and position<old_bytes:raise EngineError('TTY prefix rotated before archive could resume')
            persist()
            next_cursor={'window':{'until':end},'partials':next_partials}
            if tty and position and last_byte!=b'\n':
                next_cursor['tty_open']={'ns':first_ns,'bytes':position,'sha256':full_hash.hexdigest()}
            # Preserve unfinished partial timestamps: Docker can append later chunks with the same old timestamp.
            atomic_json(path,next_cursor)
        finally:conn.close()
