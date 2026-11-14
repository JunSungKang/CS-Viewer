"""Local authenticated operations. No raw-log endpoint is exposed by the web app."""
import argparse
import base64
import datetime
import json
from pathlib import Path

from .clickhouse import Client, StorageError
from .schema import TABLES, change_retention, initialize, retention_status

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--credentials',required=True,type=Path)
    sub=parser.add_subparsers(dest='operation',required=True)
    sub.add_parser('init');sub.add_parser('status')
    retain=sub.add_parser('retention');retain.add_argument('--table',choices=TABLES,required=True)
    retain.add_argument('--years',type=int,required=True);retain.add_argument('--apply',action='store_true')
    logs=sub.add_parser('logs');logs.add_argument('--module',required=True)
    logs.add_argument('--start');logs.add_argument('--end');logs.add_argument('--limit',type=int,default=100);logs.add_argument('--cursor')
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
        params={'module':args.module,'start':start.strftime('%Y-%m-%d %H:%M:%S.%f'),'end':end.strftime('%Y-%m-%d %H:%M:%S.%f'),'limit':args.limit}
        extra='';page_hint=args.limit
        if args.cursor:
            try:
                cursor=json.loads(base64.urlsafe_b64decode(args.cursor.encode()))
                if cursor['module']!=args.module or cursor['start']!=params['start'] or cursor['end']!=params['end']:raise ValueError('Cursor range differs')
                page_hint=min(args.limit,int(cursor.get('page_limit',args.limit)))
                if page_hint<1:raise ValueError('Invalid page limit')
                params.update(last_time=cursor['time'],last_id=cursor['id'],last_chunk=int(cursor['chunk']))
            except (ValueError,KeyError,TypeError):parser.error('Invalid cursor; keep the same module and explicit time range')
            extra=" AND (event_time < {last_time:DateTime64(9,'UTC')} OR (event_time={last_time:DateTime64(9,'UTC')} AND (record_id,chunk)>({last_id:FixedString(32)},{last_chunk:UInt64})))"
        effective=page_hint
        while True:
            params['limit']=effective
            try:
                rows=client.rows(f"""SELECT event_time,module,container_name,stream,record_id,chunk,body_bytes,body,encoding
                    FROM {client.database}.container_logs WHERE module={{module:String}}
                    AND event_time>={{start:DateTime64(9,'UTC')}} AND event_time<{{end:DateTime64(9,'UTC')}} {extra}
                    ORDER BY event_time DESC,record_id,chunk,body_bytes DESC LIMIT 1 BY record_id,chunk LIMIT {{limit:UInt16}}""",params,
                    {'max_execution_time':5,'max_threads':1,'max_block_size':min(effective,128),'preferred_block_size_bytes':1048576,'max_result_bytes':2*1024*1024,'result_overflow_mode':'throw'})
                break
            except StorageError as exc:
                if exc.code not in (396,241,-1) or effective==1:raise
                effective=max(1,effective//2)
        next_cursor=None
        if len(rows)==effective:
            last=rows[-1]
            next_cursor=base64.urlsafe_b64encode(json.dumps({'module':args.module,'start':params['start'],'end':params['end'],'time':last['event_time'],'id':last['record_id'],'chunk':last['chunk'],'page_limit':effective}).encode()).decode()
        result={'start':params['start']+'+00:00','end':params['end']+'+00:00','page_limit':effective,'rows':rows,'next_cursor':next_cursor}
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
