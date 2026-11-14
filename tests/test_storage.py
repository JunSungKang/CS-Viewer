import base64
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import uuid
import contextlib
from backend import admin

from backend.clickhouse import StorageError, timestamp
from backend.logs import Collector, frames, decode_record
from backend.schema import years, definitions
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
        manifest,rows=s.prepare()
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

    def test_retention_and_no_unique_key_assumption(self):
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

    def test_failed_container_does_not_stop_other_log_sources(self):
        engine=unittest.mock.Mock()
        engine.call.return_value=[{'Id':'a'*64,'Names':['/bad']},{'Id':'b'*64,'Names':['/good']}]
        collector=Collector(engine,unittest.mock.Mock(),{'bad':{},'good':{}},self.path)
        with patch.object(collector,'collect',side_effect=[ValueError('bad frame'),None]) as collect:
            collector.poll()
        self.assertEqual(collect.call_count,2)
        self.assertIn('bad',collector.error)
