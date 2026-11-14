"""Typed append-only tables; retention is changed only by an explicit admin operation."""
import re

TABLES = ('service_samples', 'container_logs', 'action_events')

def years(value):
    if type(value) is not int or not 3 <= value <= 100:
        raise ValueError('Retention must be 3 to 100 calendar years')
    return value

def definitions(database, retention=None, codec='ZSTD(1)'):
    retention = retention or {}
    if codec not in ('LZ4', 'ZSTD(1)', 'ZSTD(3)'):
        raise ValueError('Unsupported codec')
    bodies = {
      'service_samples': """measured_at DateTime64(6,'UTC') CODEC(Delta,{c}), received_at DateTime64(0,'UTC') CODEC(Delta,{c}), batch_id UUID,
        module LowCardinality(String), http_ms Nullable(Float64), tcp_ms Nullable(Float64), status Nullable(UInt16), error String,
        running UInt16, total UInt16, restarts UInt64, oom_history Bool, unhealthy Bool, cpu_percent Nullable(Float64), memory_bytes Nullable(UInt64)""",
      'container_logs': """event_time DateTime64(9,'UTC') CODEC(Delta,{c}), received_at DateTime64(0,'UTC') CODEC(Delta,{c}), batch_id UUID,
        module LowCardinality(String), role LowCardinality(String), container_name LowCardinality(String), container_id FixedString(64),
        stream Enum8('stdout'=1,'stderr'=2,'tty'=3), record_id FixedString(32), occurrence UInt32, chunk UInt32,
        body String, encoding Enum8('utf8'=1,'base64'=2)""",
      'action_events': """request_id UUID, event_sequence UInt8, received_at DateTime64(0,'UTC') CODEC(Delta,{c}), started_at Float64,
        instance_id UUID, fingerprint FixedString(64), actor LowCardinality(String), target FixedString(64),
        action Enum8('start'=1,'stop'=2,'restart'=3), status Enum8('pending'=1,'succeeded'=2,'unknown'=3,'rejected'=4,'failed'=5), message String"""}
    keys = {'service_samples': ('module,measured_at,batch_id','module,measured_at'),
      'container_logs': ('module,event_time,container_id,stream,record_id,chunk','module,event_time'), 'action_events': ('request_id,event_sequence','request_id')}
    result = []
    for table, body in bodies.items():
        order, primary = keys[table]
        extra = ',fsync_after_insert=1,fsync_part_directory=1' if table == 'action_events' else ''
        result.append(f"""CREATE TABLE IF NOT EXISTS {database}.{table} ({body.format(c=codec)}) ENGINE=MergeTree
          PARTITION BY toYYYYMM(received_at) PRIMARY KEY ({primary}) ORDER BY ({order})
          TTL received_at + INTERVAL {years(retention.get(table,3))} YEAR DELETE
          SETTINGS index_granularity=8192,ttl_only_drop_parts=1,non_replicated_deduplication_window=4096,default_compression_codec='{codec}'{extra}""")
    return result

def initialize(client, retention=None):
    client.query('CREATE DATABASE IF NOT EXISTS ' + client.database)
    for sql in definitions(client.database, retention):
        client.query(sql)

def retention_status(client):
    result = {}
    for table in TABLES:
        sql = client.query('SHOW CREATE TABLE ' + client.database + '.' + table).decode()
        match = re.search(r'TTL\s+received_at\s*\+\s*toIntervalYear\((\d+)\)', sql)
        result[table] = int(match[1]) if match else None
    return result

def change_retention(client, requested, apply=False):
    current = retention_status(client)
    for table, value in requested.items():
        if table not in TABLES:
            raise ValueError('Unknown retention table')
        years(value)
    impact = {table: {'old_years':current[table], 'new_years':value,
      'eligible_rows':client.rows(f'SELECT count() AS n FROM {client.database}.{table} WHERE received_at + INTERVAL {value} YEAR < now()')[0]['n']} for table,value in requested.items()}
    if apply:
        for table,value in requested.items():
            client.query(f'ALTER TABLE {client.database}.{table} MODIFY TTL received_at + INTERVAL {value} YEAR DELETE', settings={'materialize_ttl_after_modify':0})
    return {'applied':apply,'impact':impact,'actual':retention_status(client)}
