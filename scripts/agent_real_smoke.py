"""Real gardener classification + actual business reads in a disposable local store.

Fictional account and initial plan fixtures; no real email or user garden writes.
Only newly incurred model reservations/metrics return to the existing budget ledger.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from fastapi.testclient import TestClient
from app import main, auth


def run(output):
    path = (ROOT / output).resolve()
    if ROOT/'docs'/'评测' not in path.parents or path.suffix != '.json' or path.exists():
        raise SystemExit('Use a new JSON filename under docs/评测.')
    if os.environ.get('DATA_BACKEND') == 'cloudbase_rest':
        raise SystemExit('Local disposable validation only.')
    original_db = main.DB
    report = {'identity': 'isolated account fixture; real email NOT tested',
              'seed': 'fictional goal/habit via mocked plan generation; gardener queries use real configured model',
              'checks': [], 'passed': False}
    previous = set()
    original_mode = os.environ.get('BLOOMATE_TEST_MODEL')
    os.environ.update({'BLOOMATE_APP_MODE':'local','BLOOMATE_COOKIE_SECURE':'0',
                       'BLOOMATE_ALLOWED_ORIGINS':'http://testserver'})
    with tempfile.TemporaryDirectory(prefix='bloomate-agent-real-') as temporary:
        main.DB = str(Path(temporary)/'agent.sqlite3')
        try:
            with TestClient(main.app) as client:
                with sqlite3.connect(original_db) as source, main.connect() as target:
                    previous = {r[0] for r in source.execute('SELECT id FROM usage_reservations')}
                    target.executemany('INSERT INTO usage_reservations VALUES (?,?,?,?)', source.execute('SELECT * FROM usage_reservations').fetchall())
                    owner = 'user_' + secrets.token_hex(16)
                    target.execute("INSERT INTO auth_subjects(id,kind,created_at) VALUES (?,'account',?)", (owner,auth.now()))
                    raw=auth.create_session(target,owner,auth.now()+3600)
                client.cookies.set(auth.ACCOUNT_COOKIE,raw)
                client.headers.update({'Origin':'http://testserver','X-Bloomate-CSRF':auth.csrf(raw)})
                os.environ['BLOOMATE_TEST_MODEL']='1'
                proposal=client.post('/api/v1/plans',json={'kind':'goal','goal':'虚构验收：整理一份园艺分享提纲','availability':'每天十分钟'});proposal.raise_for_status()
                plant=client.post('/api/v1/proposals/'+proposal.json()['id']+'/confirm');plant.raise_for_status()
                pid=plant.json()['id'];day=main.habit_today('Asia/Shanghai').isoformat()
                client.put(f'/api/v1/plants/{pid}/actions/0/date',json={'due_date':day}).raise_for_status()
                proposal=client.post('/api/v1/plans',json={'kind':'habit','goal':'虚构验收：阅读十分钟','availability':'每天十分钟'});proposal.raise_for_status()
                habit=client.post('/api/v1/proposals/'+proposal.json()['id']+'/confirm');habit.raise_for_status()
                hid=habit.json()['id']
                client.post(f'/api/v1/plants/{hid}/habit-stage',json={'start_date':day,'end_date':day,'timezone':'Asia/Shanghai',
                    'weekdays':[main.habit_today('Asia/Shanghai').weekday()],'completion_standard':'阅读十分钟','growth_threshold':1}).raise_for_status()
                os.environ['BLOOMATE_TEST_MODEL']='0'

                def ask(text,context=None,session=None):
                    response=client.post('/api/v1/agent/turns',json={'request_id':str(uuid.uuid4()),'text':text,
                        'session_id':session,'context':context or {'page':'today'}});response.raise_for_status()
                    result=client.get('/api/v1/agent/turns/'+response.json()['id']);result.raise_for_status()
                    data=result.json()
                    if data['status']!='done':
                        raise RuntimeError((data.get('error') or {}).get('code','QUERY_NOT_DONE'))
                    return data

                first=ask('今天有哪些目标任务和习惯需要做？')
                assert first['result']['kind']=='today'
                assert first['result']['data']['goals_today'][0]['completed'] is False
                assert first['result']['data']['habits_today'][0]['completed'] is False
                report['checks'].append('real model routes today query to actual fictional goal and habit records')
                client.put(f'/api/v1/plants/{pid}/actions/0',json={'completed':True}).raise_for_status()
                client.put(f'/api/v1/plants/{hid}/habit-occurrences/{day}',json={'completed':True,'performed':True}).raise_for_status()
                after=ask('再查询一下，今天的任务和习惯完成了哪些？',session=first['session_id'])
                assert after['result']['kind']=='today'
                assert after['result']['data']['goals_today'][0]['completed'] is True
                assert after['result']['data']['habits_today'][0]['completed'] is True
                report['checks'].append('manual completion/check-in is visible to next real query in same conversation')
                selected=ask('这株植物的当前进展怎样？',context={'page':'garden','plant_id':pid},session=first['session_id'])
                assert selected['result']['kind']=='plant' and selected['result']['data']['plant_id']==pid
                assert selected['result']['data']['steps'][0]['completed'] is True
                report['checks'].append('selected plant context resolves to same owner and fresh actual steps')
                assert len(client.get('/api/v1/agent/sessions/'+first['session_id']).json()['turns'])==3
                report['checks'].append('three real turns restore from server conversation history')
                report['passed']=True
        except Exception as exc:
            report['error_type']=type(exc).__name__
            if isinstance(exc,RuntimeError):report['error_code']=str(exc)
            raise
        finally:
            with main.connect() as source, sqlite3.connect(original_db) as target:
                new_usage=[tuple(r) for r in source.execute('SELECT * FROM usage_reservations') if r['id'] not in previous]
                target.executemany('INSERT OR IGNORE INTO usage_reservations VALUES (?,?,?,?)',new_usage)
                metrics=[dict(r) for r in source.execute('SELECT * FROM model_call_metrics')]
                for row in metrics:target.execute('INSERT OR IGNORE INTO model_call_metrics VALUES (?,?,?,?,?,?,?)',tuple(row.values()))
            report['calls']=metrics;report['reserved_cents']=sum(r[3] for r in new_usage)
            main.DB=original_db
            if original_mode is None:os.environ.pop('BLOOMATE_TEST_MODEL',None)
            else:os.environ['BLOOMATE_TEST_MODEL']=original_mode
            path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'passed':report['passed'],'checks':report['checks'],'calls':len(metrics),'reserved_cents':report['reserved_cents']},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    run(parser.parse_args().output)
