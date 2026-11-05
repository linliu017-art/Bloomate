"""Real model regression through authenticated business APIs in an isolated DB.

Identity is a test fixture; this does NOT validate real mailbox delivery.
Only cost reservations/metrics are copied to the local budget ledger.
"""
import json
import argparse
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from fastapi.testclient import TestClient
from app import main,auth

parser=argparse.ArgumentParser()
parser.add_argument('--complete-goal',action='store_true')
parser.add_argument('--output')
args=parser.parse_args()
complete_goal=args.complete_goal
result_path=(ROOT/args.output).resolve() if args.output else ROOT/('docs/评测/阶段3-2目标全链路真实验证.json' if complete_goal else 'docs/评测/阶段3-2真实模型验证.json')
if (ROOT/'docs/评测').resolve() not in result_path.parents or result_path.suffix!='.json':
    raise SystemExit('Smoke output must be a JSON report under docs/评测.')
if result_path.exists():
    raise SystemExit('Existing smoke result retained; explicit rerun requires a new filename.')
original_db=main.DB
os.environ.update({'BLOOMATE_TEST_MODEL':'0','BLOOMATE_ALLOWED_ORIGINS':'http://testserver',
                   'BLOOMATE_APP_MODE':'local','BLOOMATE_COOKIE_SECURE':'0'})
records=[]
previous=set()
with tempfile.TemporaryDirectory(prefix='bloomate-auth-real-') as temporary:
    main.DB=str(Path(temporary)/'smoke.sqlite3')
    try:
        with TestClient(main.app) as client:
            with sqlite3.connect(original_db) as source, main.connect() as target:
                previous={r[0] for r in source.execute('SELECT id FROM usage_reservations')}
                target.executemany('INSERT INTO usage_reservations VALUES (?,?,?,?)',source.execute('SELECT * FROM usage_reservations').fetchall())
                owner='user_'+os.urandom(16).hex()
                target.execute("INSERT INTO auth_subjects(id,kind,created_at) VALUES (?,'account',?)",(owner,auth.now()))
                raw=auth.create_session(target,owner,auth.now()+3600)
            client.cookies.set(auth.ACCOUNT_COOKIE,raw)
            client.headers.update({'Origin':'http://testserver','X-Bloomate-CSRF':auth.csrf(raw)})
            cases=[('goal','虚构验收：3天内准备一份5分钟园艺分享')]
            if not complete_goal:
                cases.append(('habit','虚构验收：每天阅读十分钟'))
            for kind,goal in cases:
                response=client.post('/api/v1/plans',json={'kind':kind,'goal':goal,'background':'仅用虚构信息，其他条件暂不确定','availability':'每次十分钟','session_budget_minutes':10,'habit_completion_minutes':10 if kind=='habit' else None})
                response.raise_for_status()
                job_id=response.json()['id']
                job=client.get('/api/v1/plans/'+job_id).json()
                turns=1
                if complete_goal:
                    for _ in range(2):
                        if job['status']!='done' or job.get('proposal') or not job.get('question'):
                            break
                        response=client.post('/api/v1/plans/'+job_id+'/reply',json={'answer':'面向零基础的虚构听众，其他条件不确定，请按已有三天、每次十分钟的约束给出可编辑计划。'})
                        response.raise_for_status();job_id=response.json()['id']
                        job=client.get('/api/v1/plans/'+job_id).json();turns+=1
                valid=job['status']=='done' and bool(job.get('proposal') or job.get('question'))
                if complete_goal:
                    valid=valid and bool(job.get('proposal'))
                record={'kind':kind,'valid_model_turn':valid,'status':job['status'],'turns':turns,'error_code':(job.get('error') or {}).get('code')}
                if valid and job.get('proposal'):
                    planted=client.post('/api/v1/proposals/'+job_id+'/confirm')
                    record['confirmed']=planted.status_code==200
                    if record['confirmed']:
                        record['garden_restored']=any(p['id']==planted.json()['id'] for p in client.get('/api/v1/garden').json()['plants'])
                records.append(record)
                print(kind,'passed' if valid else 'failed',flush=True)
    finally:
        with main.connect() as source,sqlite3.connect(original_db) as target:
            new_usage=[tuple(row) for row in source.execute('SELECT * FROM usage_reservations') if row['id'] not in previous]
            target.executemany('INSERT OR IGNORE INTO usage_reservations VALUES (?,?,?,?)',new_usage)
            calls=[dict(row) for row in source.execute('SELECT * FROM model_call_metrics')]
            for row in calls:
                target.execute('INSERT OR IGNORE INTO model_call_metrics VALUES (?,?,?,?,?,?,?)',tuple(row.values()))
        main.DB=original_db
result_path.write_text(json.dumps({'identity':'isolated session fixture; real email delivery NOT tested','model':'real configured provider','results':records,'calls':calls},ensure_ascii=False,indent=2))
if not all(r['valid_model_turn'] and r.get('confirmed', True) and r.get('garden_restored',True) for r in records):
    raise SystemExit(1)
