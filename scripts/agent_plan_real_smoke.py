"""Real router/generator, disposable account; only new costs return to the real ledger."""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import uuid
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from fastapi.testclient import TestClient
from app import main,auth


def run(output):
    destination=(ROOT/output).resolve()
    if ROOT/'docs'/'评测' not in destination.parents or destination.suffix!='.json' or destination.exists():raise SystemExit('Use a new JSON report under docs/评测.')
    if os.environ.get('DATA_BACKEND')=='cloudbase_rest':raise SystemExit('Local disposable validation only.')
    original=main.DB
    report={'identity':'fictional account fixture; real email NOT tested','model':'real configured router and plan generator','passed':False,'checks':[]}
    before=set();usage=[];metrics=[]
    os.environ.update({'BLOOMATE_TEST_MODEL':'0','BLOOMATE_APP_MODE':'local','BLOOMATE_COOKIE_SECURE':'0','BLOOMATE_ALLOWED_ORIGINS':'http://testserver'})
    with tempfile.TemporaryDirectory(prefix='bloomate-agent-plan-real-') as temporary:
        main.DB=str(Path(temporary)/'plans.sqlite3')
        try:
            with TestClient(main.app) as client:
                with sqlite3.connect(original) as source,main.connect() as db:
                    before={r[0] for r in source.execute('SELECT id FROM usage_reservations')}
                    db.executemany('INSERT INTO usage_reservations VALUES (?,?,?,?)',source.execute('SELECT * FROM usage_reservations').fetchall())
                    owner='user_'+secrets.token_hex(16);db.execute("INSERT INTO auth_subjects(id,kind,created_at) VALUES (?,'account',?)",(owner,auth.now()))
                    raw=auth.create_session(db,owner,auth.now()+3600)
                client.cookies.set(auth.ACCOUNT_COOKIE,raw);client.headers.update({'Origin':'http://testserver','X-Bloomate-CSRF':auth.csrf(raw)})
                def ask(text,session=None):
                    r=client.post('/api/v1/agent/turns',json={'request_id':str(uuid.uuid4()),'text':text,'session_id':session,'context':{'page':'today'}});r.raise_for_status()
                    t=client.get('/api/v1/agent/turns/'+r.json()['id']).json()
                    if t['status']!='done':raise RuntimeError((t.get('error') or {}).get('code','NOT_DONE'))
                    if t['result']['kind']!='plan':raise RuntimeError('WRONG_INTENT')
                    return t
                def ready(t,answer):
                    for _ in range(2):
                        if t['result']['data']['state']=='ready':return t
                        t=ask(answer,t['session_id'])
                    if t['result']['data']['state']!='ready':raise RuntimeError('FOLLOWUP_NOT_READY')
                    return t
                def body(t):
                    d=t['result']['data'];return {'version':d['version'],'plan':d['proposal'],'goal_dates':d['goal_dates']}
                first=ready(ask('帮我创建一个新目标：一周内整理一页园艺分享提纲，每天最多10分钟。只是虚构演示，已有主题，不需查外部材料。'),'7天完成，每天10分钟，其他条件不确定，请给出最小可编辑草案。')
                assert not client.get('/api/v1/garden').json()['plants']
                revised=ready(ask('这份尚未种植的草案，每天可用时间改为8分钟，仍是7天，其他条件保留。',first['session_id']),'仍是这份草案，7天，每天8分钟，其他信息不确定，先给最小方案。')
                assert revised['result']['data']['fields']['daily_minutes']==8
                assert revised['result']['data']['fields']['daily'] is True
                assert revised['result']['data']['fields']['duration_days']==7
                assert client.post('/api/v1/agent/turns/'+first['id']+'/confirm',json=body(first)).status_code==409
                r=client.post('/api/v1/agent/turns/'+revised['id']+'/confirm',json=body(revised));r.raise_for_status();pid=r.json()['id']
                assert client.post('/api/v1/agent/turns/'+revised['id']+'/confirm',json=body(revised)).json()['id']==pid
                assert any(a['plant_id']==pid for key,rows in client.get('/api/v1/today').json().items() if key.startswith('goals_') for a in rows)
                report['checks'].append('real goal intake/revision; old card rejected; explicit idempotent planting; original today actions')
                start=main.habit_today('Asia/Shanghai');end=start+timedelta(days=6)
                habit=ready(ask(f'创建一个新习惯：阅读。周期从{start}到{end}，每周一三五做，每次最多10分钟，单次完成标准是阅读满10分钟，本阶段累计完成3次达标。无需其他准备或记录。'),'起止日期、每周一三五、每次阅读满10分钟、阶段完成3次不变，其他不确定，请按这些条件给最小草案。')
                schedule=habit['result']['data']['source']['habit_schedule'];assert schedule['weekdays']==[0,2,4] and schedule['growth_threshold']==3
                r=client.post('/api/v1/agent/turns/'+habit['id']+'/confirm',json=body(habit));r.raise_for_status();hid=r.json()['id']
                assert client.get('/api/v1/plants/'+hid+'/habit-stage').json()['stage']['weekdays']==[0,2,4]
                with main.connect() as db:assert db.execute('SELECT count(*) FROM habit_occurrences').fetchone()[0]==3
                report['checks'].append('real non-daily habit generation; original stage, three dates and threshold')
                client.delete('/api/v1/agent/sessions/'+revised['session_id']).raise_for_status();assert len(client.get('/api/v1/garden').json()['plants'])==2
                report['checks'].append('confirmed plants survive chat deletion');report['passed']=True
        except Exception as exc:
            report['error_type']=type(exc).__name__
            if isinstance(exc,RuntimeError):report['error_code']=str(exc)
            raise
        finally:
            with main.connect() as db,sqlite3.connect(original) as target:
                usage=[tuple(r) for r in db.execute('SELECT * FROM usage_reservations') if r['id'] not in before]
                target.executemany('INSERT OR IGNORE INTO usage_reservations VALUES (?,?,?,?)',usage)
                metrics=[dict(r) for r in db.execute('SELECT * FROM model_call_metrics')]
                for row in metrics:target.execute('INSERT OR IGNORE INTO model_call_metrics VALUES (?,?,?,?,?,?,?)',tuple(row.values()))
            report['calls']=metrics;report['reserved_cents']=sum(r[3] for r in usage);main.DB=original
            destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'checks':report['checks'],'calls':len(metrics),'reserved_cents':report['reserved_cents']},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);run(parser.parse_args().output)
