"""Real change routing and split generation in a disposable fictional account."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))
from fastapi.testclient import TestClient
from app import main, auth


def run(output):
    destination = (ROOT/output).resolve()
    if ROOT/'docs'/'评测' not in destination.parents or destination.suffix != '.json' or destination.exists():
        raise SystemExit('Use a new JSON report under docs/评测.')
    if os.environ.get('DATA_BACKEND') == 'cloudbase_rest':
        raise SystemExit('Local disposable validation only.')
    original = main.DB
    report = {'identity': 'fictional account fixture; real email NOT tested',
              'seed': 'mock plan seed, real configured routing and split generation', 'passed': False, 'checks': []}
    before = set(); metrics = []; usage = []
    os.environ.update({'BLOOMATE_APP_MODE': 'local', 'BLOOMATE_COOKIE_SECURE': '0', 'BLOOMATE_ALLOWED_ORIGINS': 'http://testserver'})
    with tempfile.TemporaryDirectory(prefix='bloomate-agent-change-real-') as temporary:
        main.DB = str(Path(temporary)/'changes.sqlite3')
        try:
            with TestClient(main.app) as client:
                with sqlite3.connect(original) as source, main.connect() as db:
                    before = {r[0] for r in source.execute('SELECT id FROM usage_reservations')}
                    db.executemany('INSERT INTO usage_reservations VALUES (?,?,?,?)', source.execute('SELECT * FROM usage_reservations').fetchall())
                    owner = 'user_'+secrets.token_hex(16)
                    db.execute("INSERT INTO auth_subjects(id,kind,created_at) VALUES (?,'account',?)", (owner,auth.now()))
                    raw = auth.create_session(db, owner, auth.now()+3600)
                client.cookies.set(auth.ACCOUNT_COOKIE,raw)
                client.headers.update({'Origin':'http://testserver','X-Bloomate-CSRF':auth.csrf(raw)})
                os.environ['BLOOMATE_TEST_MODEL'] = '1'
                job = client.post('/api/v1/plans', json={'kind':'goal','goal':'虚构园艺分享','availability':'每次最多20分钟','session_budget_minutes':20});job.raise_for_status()
                plan = {'title':'虚构园艺分享','summary':'虚构验收目标','steps':['整理分享素材','写一份分享提纲','检查已有素材'], 'session_actions':[{'text':'整理素材','kind':'core','minutes':20}]}
                planted = client.post('/api/v1/proposals/'+job.json()['id']+'/confirm', json=plan);planted.raise_for_status();pid=planted.json()['id']
                base = '/api/v1/plants/'+pid
                today = main.habit_today('Asia/Shanghai')
                for i in range(3):
                    client.put(base+f'/actions/{i}/date',json={'due_date':today.isoformat()}).raise_for_status()
                client.put(base+'/actions/2',json={'completed':True}).raise_for_status()
                progress = client.get(base+'/growth').json()['progress_percent']
                os.environ['BLOOMATE_TEST_MODEL'] = '0'
                def ask(text, session=None, selected=True, choice=None):
                    context = {'page':'garden' if selected else 'today', 'plant_id':pid if selected else None}
                    if choice:context['change_choice']=choice;context['plant_id']=choice['target']['plant_id']
                    r = client.post('/api/v1/agent/turns',json={'request_id':str(uuid.uuid4()),'text':text,'session_id':session,'context':context});r.raise_for_status()
                    turn = client.get('/api/v1/agent/turns/'+r.json()['id']).json()
                    if turn['status'] != 'done':raise RuntimeError((turn.get('error') or {}).get('code','NOT_DONE'))
                    if turn['result']['kind'] != 'change':raise RuntimeError('WRONG_INTENT')
                    return turn
                def confirm(turn, steps=None):
                    d = turn['result']['data']
                    body = {'version':d['version'], **({'steps':steps or d['after']['steps']} if d['operation']=='split' else {'due_date':d['after']['due_date']})}
                    return client.post('/api/v1/agent/changes/'+turn['id']+'/confirm', json=body), body
                first = ask('把这个已经种植的目标第1步改到明天，只修改日期。')
                assert first['result']['data']['state']=='ready' and first['result']['data']['target']['index']==0
                assert first['result']['data']['after']['due_date']==(today+timedelta(days=1)).isoformat()
                assert client.get(base+'/actions').json()['actions'][0]['due_date']==today.isoformat()
                response, submitted = confirm(first);response.raise_for_status()
                client.post('/api/v1/agent/changes/'+first['id']+'/confirm',json=submitted).raise_for_status()
                assert client.get(base+'/actions').json()['actions'][0]['due_date']==(today+timedelta(days=1)).isoformat()
                report['checks'].append('real date intent and step resolution; no pre-confirm mutation; tomorrow in Shanghai; idempotent actual date update')
                split = ask('这个已种植目标的第2步太难了，帮我拆成两条更小的动作，每次最多20分钟，只调整这一步。',first['session_id'])
                assert split['result']['data']['state']=='ready' and split['result']['data']['target']['index']==1
                proposed = split['result']['data']['after']['steps']
                assert 1 <= len(proposed) <= 5
                response, submitted = confirm(split);response.raise_for_status()
                client.post('/api/v1/agent/changes/'+split['id']+'/confirm',json=submitted).raise_for_status()
                actions = client.get(base+'/actions').json()['actions']
                assert len(actions)==2+len(proposed) and actions[-1]['completed'] and actions[-1]['due_date']==today.isoformat()
                assert client.get(base+'/growth').json()['progress_percent']==progress
                report['checks'].append('real split generator, explicit original adjustment transaction, existing completion/date/growth fraction preserved')
                stale = ask('把这个目标第1步改到后天。',first['session_id'])
                assert stale['result']['data']['state']=='ready'
                client.put(base+'/actions/0/date',json={'due_date':today.isoformat()}).raise_for_status()
                response,_=confirm(stale);assert response.status_code==409 and response.json()['error']['code']=='PLAN_CHANGED'
                report['checks'].append('manual page date changes invalidate old Agent preview')
                ambiguous = ask('把这一步改到明天。', selected=False)
                d=ambiguous['result']['data'];assert d['state']=='select_task'
                selected = ask('选择这一步',ambiguous['session_id'],selected=False,choice={'turn_id':ambiguous['id'],'version':d['version'],'target':d['choices'][0]['target']})
                assert selected['result']['data']['state']=='ready'
                client.post('/api/v1/agent/changes/'+selected['id']+'/discard',json={'version':selected['result']['data']['version']}).raise_for_status()
                report['checks'].append('ambiguous object requires explicit server-owned choice; deterministic continuation; discard does not apply')
                client.delete('/api/v1/agent/sessions/'+first['session_id']).raise_for_status()
                assert len(client.get(base+'/actions').json()['actions'])==len(actions)
                report['checks'].append('confirmed changes survive conversation deletion');report['passed']=True
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
            main.DB=original;report['calls']=metrics;report['reserved_cents']=sum(r[3] for r in usage)
            destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'checks':report['checks'],'calls':len(metrics),'reserved_cents':report['reserved_cents']},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);run(parser.parse_args().output)
