"""Local authenticated operations. No raw-log endpoint is exposed by the web app."""
import argparse
import datetime
import json
from pathlib import Path

from .clickhouse import Client
from .schema import TABLES, change_retention, initialize, retention_status

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--credentials',required=True,type=Path)
    sub=parser.add_subparsers(dest='operation',required=True)
    sub.add_parser('init');sub.add_parser('status')
    retain=sub.add_parser('retention');retain.add_argument('--table',choices=TABLES,required=True)
    retain.add_argument('--years',type=int,required=True);retain.add_argument('--apply',action='store_true')
    logs=sub.add_parser('logs');logs.add_argument('--module',required=True)
    logs.add_argument('--start');logs.add_argument('--end');logs.add_argument('--limit',type=int,default=100)
    args=parser.parse_args();client=Client(json.loads(args.credentials.read_text()))
    if args.operation=='init':initialize(client);result=retention_status(client)
    elif args.operation=='status':result=retention_status(client)
    elif args.operation=='retention':result=change_retention(client,{args.table:args.years},args.apply)
    else:
        if not 1<=args.limit<=1000:parser.error('limit must be 1..1000')
        now=datetime.datetime.now(datetime.timezone.utc)
        def date(value,default):
            result=datetime.datetime.fromisoformat(value.replace('Z','+00:00')) if value else default
            if result.tzinfo is None:parser.error('Timezone required')
            return result.astimezone(datetime.timezone.utc)
        end=date(args.end,now);start=date(args.start,end-datetime.timedelta(minutes=15))
        if not start<end or end-start>datetime.timedelta(days=31):parser.error('Range must be positive and at most 31 days')
        result=client.rows(f'''SELECT event_time,module,container_name,stream,record_id,chunk,body,encoding
            FROM {client.database}.container_logs WHERE module={{module:String}}
            AND event_time>={{start:DateTime64(9)}} AND event_time<{{end:DateTime64(9)}}
            ORDER BY event_time DESC,record_id,chunk LIMIT 1 BY record_id,chunk LIMIT {{limit:UInt16}}''',
            {'module':args.module,'start':start.strftime('%Y-%m-%d %H:%M:%S.%f'),'end':end.strftime('%Y-%m-%d %H:%M:%S.%f'),'limit':args.limit},
            {'max_execution_time':5,'max_result_bytes':2*1024*1024,'result_overflow_mode':'throw'})
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
