"""Real server tests, enabled locally or in CI with an isolated database."""
import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

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
