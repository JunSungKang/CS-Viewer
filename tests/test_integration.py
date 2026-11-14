"""Real server tests, enabled locally or in CI with an isolated database."""
import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
import time
import io
import contextlib
from unittest.mock import patch
from backend import admin

from backend.clickhouse import Client
from backend.schema import initialize, retention_status, change_retention
from backend.store import Store

@unittest.skipUnless(os.environ.get('CS_TEST_CLICKHOUSE_CONFIG') or os.environ.get('CS_TEST_CLICKHOUSE_URL'), 'ClickHouse test server not configured')
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get('CS_TEST_CLICKHOUSE_CONFIG'):
            self.settings=json.loads(Path(os.environ['CS_TEST_CLICKHOUSE_CONFIG']).read_text())
        else:self.settings={'url':os.environ['CS_TEST_CLICKHOUSE_URL'],'user':'default','password':'integration-only'}
        self.settings['database']='cs_test_'+uuid.uuid4().hex
        self.client=Client(self.settings);initialize(self.client)
        root=Path(__file__).resolve().parents[1]/'.runtime/tests';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.store=Store(self.settings,self.temp.name)
    def tearDown(self):
        self.client.query('DROP DATABASE '+self.client.database)
        self.temp.cleanup()

    def test_real_metrics_flush_and_retry_token(self):
        s={'module':'test','http_ms':1.2,'tcp_ms':None,'status':200,'error':None,'running':1,'total':1,
           'restarts':0,'oom_history':False,'unhealthy':False,'cpu_percent':0.1,'memory_bytes':100}
        self.store.sample([s]);manifest,rows=self.store.spool.prepare()
        self.client.insert('service_samples',rows,manifest['token'])
        # Lost acknowledgement: retry the same immutable batch.
        self.store.flush()
        self.assertIsNone(self.store.status()['error'])
        row=self.client.rows('SELECT count() AS n,any(http_ms) AS ms,any(tcp_ms) AS tcp FROM '+self.client.database+'.service_samples')[0]
        self.assertEqual(row['n'],1);self.assertEqual(row['ms'],1.2);self.assertIsNone(row['tcp'])

    def test_real_action_pending_restart_result_and_fingerprint(self):
        rid=str(uuid.uuid4());fp='b'*64
        self.assertIsNone(self.store.begin(rid,fp,'dashboard','a'*64,'restart'))
        other=Store(self.settings,Path(self.temp.name)/'other')
        self.assertEqual(other.begin(rid,fp,'dashboard','a'*64,'restart')['status'],'unknown')
        with self.assertRaises(ValueError):other.begin(rid,'c'*64,'dashboard','a'*64,'restart')
        self.assertEqual(self.store.finish(rid,'succeeded','verified')['status'],'succeeded')
        self.assertEqual(other.begin(rid,fp,'dashboard','a'*64,'restart')['status'],'succeeded')

    def test_real_retention_preview_expansion_and_leap_boundary(self):
        self.assertEqual(set(retention_status(self.client).values()),{3})
        result=change_retention(self.client,{'container_logs':5})
        self.assertFalse(result['applied']);self.assertEqual(retention_status(self.client)['container_logs'],3)
        change_retention(self.client,{'container_logs':5},True)
        self.assertEqual(retention_status(self.client)['container_logs'],5)
        change_retention(self.client,{'container_logs':3},True)
        with self.assertRaises(ValueError):change_retention(self.client,{'container_logs':2},True)
        row=self.client.rows("SELECT toString(toDateTime('2024-02-29 12:00:00','UTC') + INTERVAL 3 YEAR) AS expiry")[0]
        self.assertEqual(row['expiry'],'2027-02-28 12:00:00')
        row=self.client.rows("SELECT toString(toDateTime64('2026-11-15 12:00:00',0,'UTC') + INTERVAL 100 YEAR) AS expiry")[0]
        self.assertEqual(row['expiry'],'2126-11-15 12:00:00')

    def test_log_cursor_reads_all_rows_at_identical_timestamp(self):
        rows=[{'event_time':'2026-11-15 01:00:00.123456789','received_at':int(time.time()),
               'batch_id':str(uuid.uuid4()),'module':'test','role':'web','container_name':'test',
               'container_id':'a'*64,'stream':'stdout','record_id':format(i,'032x'),
               'occurrence':i+1,'chunk':0,'body_bytes':3,'body':'log','encoding':'utf8'} for i in range(1500)]
        self.client.insert('container_logs',rows,'page-test')
        credentials=Path(self.temp.name)/'credentials.json';credentials.write_text(json.dumps(self.settings))
        args=['admin','--credentials',str(credentials),'logs','--module','test','--limit','1000',
              '--start','2026-11-15T01:00:00+00:00','--end','2026-11-15T01:01:00+00:00']
        def page(cursor=None):
            output=io.StringIO()
            with patch('sys.argv',args+(['--cursor',cursor] if cursor else [])),contextlib.redirect_stdout(output):admin.main()
            return json.loads(output.getvalue())
        first=page();second=page(first['next_cursor'])
        self.assertEqual(len(first['rows']),1000);self.assertEqual(len(second['rows']),500)
        self.assertEqual(len({r['record_id'] for r in first['rows']+second['rows']}),1500)
        self.assertIsNone(second['next_cursor'])

    def test_server_conditional_short_retention_is_detected(self):
        self.client.query(f"ALTER TABLE {self.client.database}.container_logs MODIFY TTL received_at + INTERVAL 3 YEAR DELETE WHERE module!='short', received_at + INTERVAL 1 DAY DELETE WHERE module='short'",settings={'materialize_ttl_after_modify':0})
        self.assertIsNone(retention_status(self.client)['container_logs'])
        self.store.flush();self.assertIsNotNone(self.store.status()['error'])
        change_retention(self.client,{'container_logs':3},True)
        self.store.flush();self.assertIsNone(self.store.status()['error'])

    def test_large_escaped_log_chunks_can_be_paged_under_response_limit(self):
        body='\x01'*(256*1024)
        rows=[{'event_time':'2026-11-15 01:00:00.123456789','received_at':int(time.time()),
               'batch_id':str(uuid.uuid4()),'module':'test','role':'web','container_name':'test',
               'container_id':'a'*64,'stream':'tty','record_id':'a'*32,'occurrence':1,
               'chunk':i*len(body),'body_bytes':len(body),'body':body,'encoding':'utf8'} for i in range(8)]
        self.client.insert('container_logs',rows,'large-page')
        credentials=Path(self.temp.name)/'credentials.json';credentials.write_text(json.dumps(self.settings))
        args=['admin','--credentials',str(credentials),'logs','--module','test','--limit','1000',
              '--start','2026-11-15T01:00:00+00:00','--end','2026-11-15T01:01:00+00:00']
        cursor=None;seen=[]
        for _ in range(10):
            output=io.StringIO()
            with patch('sys.argv',args+(['--cursor',cursor] if cursor else [])),contextlib.redirect_stdout(output):admin.main()
            page=json.loads(output.getvalue());seen.extend(page['rows']);cursor=page['next_cursor']
            if cursor is None:break
        self.assertIsNone(cursor);self.assertEqual([row['chunk'] for row in seen],[i*len(body) for i in range(8)])
        self.assertTrue(all(row['body']==body for row in seen))
        output=io.StringIO()
        limited=[*args];limited[limited.index('1000')]='2'
        with patch('sys.argv',limited),contextlib.redirect_stdout(output):admin.main()
        self.assertLessEqual(len(output.getvalue().encode()),2*1024*1024)
        self.assertEqual(len(json.loads(output.getvalue())['rows']),1)
