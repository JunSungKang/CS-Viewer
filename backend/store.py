"""ClickHouse persistence with a bounded outbox and in-memory latest telemetry."""
from collections import defaultdict, deque
import threading
import time
import uuid
from .clickhouse import Client, StorageError, timestamp
from .schema import retention_status
from .spool import Spool

class Store:
    def __init__(self, settings, spool_path, client=None, spool_limit=1024**3):
        self.client=client or Client(settings);self.spool=Spool(spool_path,spool_limit)
        self.instance=str(uuid.uuid4());self.lock=threading.RLock();self.flush_lock=threading.Lock()
        self.cache=defaultdict(lambda:deque(maxlen=60));self.audit_cache=[];self.hydrated=False
        self.storage={'error':None,'last_write':None,'backlog_bytes':self.spool.size(),'retention_years':{}}

    def sample(self, services):
        now=time.time_ns();batch=str(uuid.uuid4());rows=[]
        with self.lock:
            for s in services:
                self.cache[s['module']].append({'ts':now/1e9,**s})
                rows.append({**s,'error':s.get('error') or '', 'measured_at':timestamp(now),'received_at':now//1000000000,'batch_id':batch})
        self.enqueue('service_samples',rows)

    def enqueue(self, kind, rows):
        try:self.spool.enqueue(kind,rows)
        except (OSError,ValueError):
            with self.lock:self.storage['error']='저장 대기열 한도 또는 파일 오류'
            raise

    def flush(self):
        with self.flush_lock:
            try:
                actual=retention_status(self.client)
                if any(v is None or v<3 for v in actual.values()):raise StorageError('Invalid retention policy')
                for _ in range(8):
                    batch=self.spool.prepare()
                    if batch is None:break
                    manifest,rows=batch
                    self.client.insert(manifest['kind'],rows,manifest['token'])
                    self.spool.acknowledge()
                    with self.lock:self.storage['last_write']=time.time()
                with self.lock:self.storage.update(error=None,retention_years=actual)
                self.refresh_audit()
                self.hydrate()
            except (StorageError,OSError,ValueError):
                with self.lock:self.storage['error']='ClickHouse 저장 지연 또는 보존 정책 오류'
            finally:
                with self.lock:self.storage['backlog_bytes']=self.spool.size()

    def hydrate(self):
        if self.hydrated:return
        fields='http_ms,tcp_ms,status,error,running,total,restarts,oom_history,unhealthy,cpu_percent,memory_bytes'
        rows=self.client.rows(f'SELECT module,toFloat64(toUnixTimestamp64Micro(measured_at))/1000000 AS ts,{fields} FROM {self.client.database}.service_samples WHERE measured_at>now()-INTERVAL 1 DAY ORDER BY module,measured_at DESC LIMIT 60 BY module')
        with self.lock:
            combined=defaultdict(dict)
            for row in rows:
                row['error']=row['error'] or None
                combined[row['module']][row['ts']]=row
            for module,items in self.cache.items():
                for row in items:combined[module][row['ts']]=row
            for module,items in combined.items():self.cache[module]=deque(sorted(items.values(),key=lambda r:r['ts'])[-60:],maxlen=60)
            self.hydrated=True

    def status(self):
        with self.lock:return dict(self.storage)

    def history(self, module):
        with self.lock:return list(self.cache[module])

    def _last(self, request_id):
        rows=self.client.rows(f'SELECT * FROM {self.client.database}.action_events WHERE request_id={{id:UUID}} ORDER BY event_sequence DESC LIMIT 1',{'id':request_id})
        if not rows:return None
        row=rows[0];row['ts']=row.pop('started_at')
        if row['status']=='pending' and row['instance_id']!=self.instance:
            row.update(status='unknown',message='프로세스 재시작: 실제 상태를 다시 확인하세요.')
        return row

    def begin(self, request_id, fingerprint, actor, target, action):
        previous=self._last(request_id)
        if previous:
            if previous['fingerprint']!=fingerprint:raise ValueError('다른 요청에서 사용한 작업 ID입니다.')
            return previous
        now=time.time()
        row={'request_id':request_id,'fingerprint':fingerprint,'started_at':now,'received_at':int(now),
            'instance_id':self.instance,'actor':actor,'target':target,'action':action,'status':'pending',
            'message':'상태 확인 중','event_sequence':0}
        self.client.insert('action_events',[row],request_id+':pending')
        return None

    def finish(self, request_id, status, message):
        row=self._last(request_id)
        if not row:raise StorageError('Action intent missing')
        row['started_at']=row.pop('ts')
        row.update(status=status,message=message,event_sequence=1,received_at=int(time.time()))
        self.client.insert('action_events',[row],request_id+':result')
        self.refresh_audit()
        return {'ts':row['started_at'],**row}

    def refresh_audit(self):
        sql=(f'SELECT request_id,argMax(started_at,event_sequence) AS ts,argMax(actor,event_sequence) AS actor,'
            'argMax(target,event_sequence) AS target,argMax(action,event_sequence) AS action,'
            'argMax(status,event_sequence) AS status,argMax(message,event_sequence) AS message,'
            f'argMax(instance_id,event_sequence) AS instance_id FROM (SELECT * FROM {self.client.database}.action_events '
            'ORDER BY started_at DESC,event_sequence DESC LIMIT 100) GROUP BY request_id ORDER BY ts DESC LIMIT 50')
        rows=self.client.rows(sql)
        for r in rows:
            previous_instance=r.pop('instance_id')
            if r['status']=='pending' and previous_instance!=self.instance:r.update(status='unknown',message='프로세스 재시작: 실제 상태를 다시 확인하세요.')
        with self.lock:self.audit_cache=rows

    def audit(self):
        with self.lock:return list(self.audit_cache)
