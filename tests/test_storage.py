import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import threading
from backend import admin
from backend.engine import EngineError

from backend.clickhouse import StorageError, timestamp
from backend.logs import Collector, frames, decode_record
from backend.schema import years, definitions, retention_status
from backend.spool import Spool
from backend.store import Store
from test_core import MemoryClient
import test_core as fixtures

class StorageTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'.runtime/tests'
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root);self.path=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_stable_batch_survives_restart_and_acknowledgement(self):
        s=Spool(self.path);s.enqueue('container_logs',[{'value':1}]);first=s.prepare()
        self.assertEqual(Spool(self.path).prepare(),first)
        s.acknowledge();self.assertIsNone(s.prepare())

    def test_capacity_keeps_unacknowledged_data(self):
        s=Spool(self.path,limit=5000);s.enqueue('container_logs',[{'value':'x'*300}])
        first=s.prepare()
        with self.assertRaises(OSError):s.enqueue('container_logs',[{'value':'y'*2000}])
        self.assertEqual(s.prepare(),first)

    def test_ack_crash_does_not_requeue_deleted_inputs(self):
        s=Spool(self.path);s.enqueue('container_logs',[{}]);s.prepare()
        (self.path/'inflight.json').rename(self.path/'acked.json')
        self.assertIsNone(Spool(self.path).prepare())

    def test_db_outage_retains_queue_and_live_history(self):
        c=MemoryClient();s=Store({},self.path,client=c)
        s.sample([{'module':'test','error':None}])
        with patch.object(c,'query',side_effect=StorageError('down')):s.flush()
        self.assertEqual(len(s.history('test')),1);self.assertIsNotNone(s.status()['error'])
        self.assertGreater(s.status()['backlog_bytes'],0)
        s.flush();self.assertIsNone(s.spool.prepare());self.assertIsNone(s.status()['error'])

    def test_durable_intent_failure_prevents_engine_action(self):
        fixture=fixtures.AgentTests();fixture.setUp()
        try:
            with patch.object(fixture.client,'insert',side_effect=StorageError('down')):
                fixture.rejected(fixture.request(),503)
        finally:fixture.tearDown()

    def test_lost_intent_ack_never_replays_action(self):
        fixture=fixtures.AgentTests();fixture.setUp()
        try:
            request=fixture.request();original=fixture.client.insert
            def committed_then_disconnected(*args):
                original(*args)
                raise StorageError('Acknowledgement lost')
            with patch.object(fixture.client,'insert',side_effect=committed_then_disconnected):
                fixture.rejected(request,503)
            self.assertEqual(fixture.agent.action(request)['status'],'pending')
            self.assertFalse(fixture.engine.actions)
        finally:fixture.tearDown()

    def test_segment_and_batch_size_remain_bounded(self):
        s=Spool(self.path)
        s.enqueue('container_logs',[{'body':'x'*700000} for _ in range(3)])
        self.assertEqual(len(list(self.path.glob('*.pending'))),3)
        _,rows=s.prepare()
        self.assertEqual(len(rows),1)

    def test_tty_partial_messages_and_multiline_bytes(self):
        first=b'2026-11-14T12:13:14.123456789Z partial'
        second=b'2026-11-14T12:13:14.123456790Z continuation\r\n'
        decoded=[payload for _,payload in frames(io.BytesIO(first+second),tty=True)]
        self.assertEqual(b''.join(decoded),first+second)

    def test_cli_converts_timezone_to_utc(self):
        credentials=self.path/'client.json'
        credentials.write_text('{}')
        client=unittest.mock.Mock(database='test')
        client.rows.return_value=[]
        argv=['admin','--credentials',str(credentials),'logs','--module','test',
              '--start','2026-11-15T09:00:00+09:00','--end','2026-11-15T09:15:00+09:00']
        with patch('sys.argv',argv),patch('backend.admin.Client',return_value=client),contextlib.redirect_stdout(io.StringIO()):
            admin.main()
        params=client.rows.call_args[0][1]
        self.assertEqual(params['start'],'2026-11-15 00:00:00.000000')
        self.assertEqual(params['end'],'2026-11-15 00:15:00.000000')

    def test_retention_validation_and_default_ttl_partition(self):
        for v in (0,1,2,True,3.0,101):
            with self.assertRaises(ValueError):years(v)
        self.assertEqual(years(3),3);self.assertEqual(years(7),7)
        for ddl in definitions('example'):
            self.assertIn('INTERVAL 3 YEAR',ddl);self.assertIn('toYYYYMM(received_at)',ddl)

    def test_nanoseconds_and_frames_preserve_bytes(self):
        raw=b'2026-11-14T12:13:14.123456789Z hello\xff\nsecond line\n'
        framed=b'\x02\0\0\0'+struct.pack('>I',len(raw))+raw
        stream,payload=list(frames(io.BytesIO(framed)))[0]
        ns,body=decode_record(payload)
        self.assertEqual(stream,'stderr');self.assertEqual(body,b'hello\xff\nsecond line\n')
        self.assertTrue(timestamp(ns).endswith('.123456789'))
        with self.assertRaises(ValueError):list(frames(io.BytesIO(framed[:-1])))

    def test_repeated_identical_lines_keep_occurrence_and_resume(self):
        cid='a'*64;raw=b'2026-11-14T12:13:14.123456789Z identical\n'
        wire=(b'\x01\0\0\0'+struct.pack('>I',len(raw))+raw)*2
        class Response(io.BytesIO): status=200
        class Connection:
            def __init__(self,*a):pass
            def request(self,*a):pass
            def getresponse(self):return Response(wire)
            def close(self):pass
        class Engine:
            path='test';prefix='/v1.51'
            def inspect(self,identity):return {'Id':cid,'Name':'/owned','Config':{'Tty':False}}
        class Sink:
            def __init__(self):self.rows=[]
            def enqueue(self,kind,rows):self.rows.extend(rows)
        sink=Sink();collector=Collector(Engine(),sink,{},self.path);collector.cutover=0
        with patch('backend.logs.UnixConnection',Connection), patch('backend.logs.time.time',return_value=1795000000):
            collector.collect(cid,'owned',{'module':'test','role':'web'})
            self.assertEqual(len(sink.rows),2)
            self.assertNotEqual(sink.rows[0]['record_id'],sink.rows[1]['record_id'])
            collector.collect(cid,'owned',{'module':'test','role':'web'})
            self.assertEqual(len(sink.rows),2)

    def test_empty_windows_advance_without_rescanning_quiet_streams(self):
        cid='a'*64;requests=[]
        class Response(io.BytesIO):status=200
        class Connection:
            def __init__(self,*a):pass
            def request(self,method,path):requests.append(path)
            def getresponse(self):return Response(b'')
            def close(self):pass
        engine=unittest.mock.Mock(path='test',prefix='/v1.51')
        engine.inspect.return_value={'Id':cid,'Name':'/owned','Config':{'Tty':False}}
        collector=Collector(engine,unittest.mock.Mock(),{},self.path);collector.cutover=0
        with patch('backend.logs.UnixConnection',Connection):
            with patch('backend.logs.time.time',return_value=100):collector.collect(cid,'owned',{'module':'test','role':'web'})
            with patch('backend.logs.time.time',return_value=110):collector.collect(cid,'owned',{'module':'test','role':'web'})
        self.assertIn('since=99.000000001',requests[1])

    def test_delayed_partials_with_old_timestamps_are_not_lost(self):
        for tty in (False,True):
            folder=self.path/('tty' if tty else 'plain')
            cid='a'*64;prefix=b'1970-01-01T00:00:02.123456789Z '
            first=prefix+b'first';tail=prefix+b'last\n';requests=[]
            def frame(body):return b'\x01\0\0\0'+struct.pack('>I',len(body))+body
            responses=[first,first+tail] if tty else [frame(first),frame(first)+frame(tail)]
            class Response(io.BytesIO):status=200
            class Connection:
                def __init__(self,*a):pass
                def request(self,method,path):requests.append(path)
                def getresponse(self):return Response(responses.pop(0))
                def close(self):pass
            engine=unittest.mock.Mock(path='test',prefix='/v1.51')
            engine.inspect.return_value={'Id':cid,'Name':'/owned','Config':{'Tty':tty}}
            captured=[]
            sink=unittest.mock.Mock();sink.enqueue.side_effect=lambda kind,rows:captured.extend(rows)
            collector=Collector(engine,sink,{},folder);collector.cutover=0
            with patch('backend.logs.UnixConnection',Connection):
                with patch('backend.logs.time.time',return_value=100):collector.collect(cid,'owned',{'module':'test','role':'web'})
                with patch('backend.logs.time.time',return_value=110):collector.collect(cid,'owned',{'module':'test','role':'web'})
            self.assertIn('since=2.123456789',requests[1])
            actual=b''.join(row['body'].encode() for row in captured)
            self.assertEqual(actual,first+tail if tty else b'firstlast\n')
            state=json.loads((folder/(cid+'.json')).read_text())
            self.assertFalse(state.get('partials'));self.assertNotIn('tty_open',state)

    def test_conditional_short_ttl_is_not_accepted(self):
        client=unittest.mock.Mock(database='test')
        client.query.return_value=b"CREATE TABLE x (received_at DateTime64(0)) ENGINE=MergeTree ORDER BY tuple() TTL received_at + toIntervalYear(3) DELETE WHERE module != 'short', received_at + toIntervalDay(1) DELETE WHERE module='short' SETTINGS index_granularity=8192"
        self.assertEqual(set(retention_status(client).values()),{None})

    def test_rotated_non_tty_partial_prefix_is_reported_without_advancing(self):
        for rotated in (b'B',b'BC'):
            folder=self.path/rotated.decode();cid='a'*64;prefix=b'1970-01-01T00:00:02.123456789Z '
            def frame(body):
                raw=prefix+body
                return b'\x01\0\0\0'+struct.pack('>I',len(raw))+raw
            responses=[frame(b'A')+frame(b'B'),b''.join(frame(bytes([c])) for c in rotated)]
            class Response(io.BytesIO):status=200
            class Connection:
                def __init__(self,*a):pass
                def request(self,*a):pass
                def getresponse(self):return Response(responses.pop(0))
                def close(self):pass
            engine=unittest.mock.Mock(path='test',prefix='/v1.51')
            engine.inspect.return_value={'Id':cid,'Name':'/owned','Config':{'Tty':False}}
            collector=Collector(engine,unittest.mock.Mock(),{},folder);collector.cutover=0
            with patch('backend.logs.UnixConnection',Connection):
                with patch('backend.logs.time.time',return_value=100):collector.collect(cid,'owned',{'module':'test','role':'web'})
                before=(folder/(cid+'.json')).read_bytes()
                with patch('backend.logs.time.time',return_value=110),self.assertRaisesRegex(EngineError,'Partial source prefix'):
                    collector.collect(cid,'owned',{'module':'test','role':'web'})
            self.assertEqual((folder/(cid+'.json')).read_bytes(),before)

    def test_size_failure_does_not_escape_archive_loop(self):
        store=Store({},self.path,client=MemoryClient())
        with patch.object(store.spool,'size',side_effect=OSError('filesystem')):
            store.flush()
        self.assertIsNotNone(store.status()['error'])

    def test_size_enqueue_and_acknowledgement_can_run_concurrently(self):
        spool=Spool(self.path);done=threading.Event();errors=[];written=[]
        def producer():
            try:
                for i in range(100):spool.enqueue('container_logs',[{'n':i}])
            except Exception as exc:errors.append(exc)
            finally:done.set()
        def consumer():
            try:
                while not done.is_set() or list(self.path.glob('*.pending')):
                    batch=spool.prepare()
                    if batch:written.extend(row['n'] for row in batch[1]);spool.acknowledge()
            except Exception as exc:errors.append(exc)
        producer_thread=threading.Thread(target=producer);consumer_thread=threading.Thread(target=consumer)
        producer_thread.start();consumer_thread.start()
        while producer_thread.is_alive() or consumer_thread.is_alive():
            try:spool.size()
            except Exception as exc:errors.append(exc)
        producer_thread.join();consumer_thread.join()
        self.assertFalse(errors);self.assertEqual(sorted(written),list(range(100)))

    def test_failed_container_does_not_stop_other_log_sources(self):
        engine=unittest.mock.Mock()
        engine.call.return_value=[{'Id':'a'*64,'Names':['/bad']},{'Id':'b'*64,'Names':['/good']}]
        collector=Collector(engine,unittest.mock.Mock(),{'bad':{},'good':{}},self.path)
        with patch.object(collector,'collect',side_effect=[ValueError('bad frame'),None]) as collect:
            collector.poll()
        self.assertEqual(collect.call_count,2)
        self.assertIn('bad',collector.error)
