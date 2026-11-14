"""Real model: conversation creation -> adjustment -> record -> correction -> retained results."""
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
    original=main.DB;before=set();usage=[];metrics=[]
    report={'identity':'fictional account fixture; real email NOT tested','model':'all plan/intents/splits use real configured model, no mocked business generation','passed':False,'checks':[]}
    os.environ.update({'BLOOMATE_TEST_MODEL':'0','BLOOMATE_APP_MODE':'local','BLOOMATE_COOKIE_SECURE':'0','BLOOMATE_ALLOWED_ORIGINS':'http://testserver'})
    with tempfile.TemporaryDirectory(prefix='bloomate-agent-loop-real-') as temporary:
        main.DB=str(Path(temporary)/'loop.sqlite3')
        try:
            with TestClient(main.app) as client:
                with sqlite3.connect(original) as source,main.connect() as db:
                    before={r[0] for r in source.execute('SELECT id FROM usage_reservations')}
                    db.executemany('INSERT INTO usage_reservations VALUES (?,?,?,?)',source.execute('SELECT * FROM usage_reservations').fetchall())
                    owner='user_'+secrets.token_hex(16);db.execute("INSERT INTO auth_subjects(id,kind,created_at) VALUES (?,'account',?)",(owner,auth.now()));raw=auth.create_session(db,owner,auth.now()+3600)
                client.cookies.set(auth.ACCOUNT_COOKIE,raw);client.headers.update({'Origin':'http://testserver','X-Bloomate-CSRF':auth.csrf(raw)})
                def ask(text,session=None,pid=None,expected=None):
                    r=client.post('/api/v1/agent/turns',json={'request_id':str(uuid.uuid4()),'text':text,'session_id':session,'context':{'page':'garden' if pid else 'today','plant_id':pid}});r.raise_for_status()
                    t=client.get('/api/v1/agent/turns/'+r.json()['id']).json()
                    if t['status']!='done':raise RuntimeError((t.get('error') or {}).get('code','NOT_DONE'))
                    report['last']={'kind':t['result']['kind'],'state':t['result']['data'].get('state'),'operation':t['result']['data'].get('operation')}
                    if expected and t['result']['kind']!=expected:raise RuntimeError('WRONG_INTENT')
                    return t
                def plan(text,answer):
                    t=ask(text,expected='plan')
                    for _ in range(2):
                        if t['result']['data']['state']=='ready':break
                        t=ask(answer,t['session_id'],expected='plan')
                    d=t['result']['data'];assert d['state']=='ready'
                    assert len(client.get('/api/v1/garden').json()['plants'])==int(d['fields']['kind']=='habit')
                    r=client.post('/api/v1/agent/turns/'+t['id']+'/confirm',json={'version':d['version'],'plan':d['proposal'],'goal_dates':d['goal_dates']});r.raise_for_status();return t,r.json()['id']
                def record(text,session,pid):
                    t=ask(text,session,pid,expected='record');d=t['result']['data']
                    if d['state']=='select_task':
                        choices=[c for c in d['choices'] if c['target']['plant_id']==pid]
                        report['selection']={'count':len(choices),'candidates':[{'date':c['target']['action_date'],'archived':c['archived']} for c in choices]}
                        assert len(choices)==1,'Expected one explicitly scoped candidate'
                        r=client.post('/api/v1/agent/turns',json={'request_id':str(uuid.uuid4()),'text':'选择这项行动','session_id':session,'context':{'page':'garden','plant_id':pid,'record_choice':{'turn_id':t['id'],'version':d['version'],'target':choices[0]['target']}}});r.raise_for_status()
                        t=client.get('/api/v1/agent/turns/'+r.json()['id']).json();d=t['result']['data']
                        report['last']={'kind':t['result']['kind'],'state':d.get('state'),'operation':d.get('operation')}
                    assert d['state']=='ready',report['last']
                    submitted={'version':d['version'],'performed':d['operation']=='complete'}
                    r=client.post('/api/v1/agent/records/'+t['id']+'/confirm',json=submitted);r.raise_for_status()
                    retry=client.post('/api/v1/agent/records/'+t['id']+'/confirm',json=submitted);retry.raise_for_status();assert retry.json()==r.json()
                    return t,r.json()
                g,pid=plan('帮我创建一个新目标：今天1天内整理书桌的一个小角落，每次最多10分钟。只清理可见物件，已有收纳盒，不需要额外采购或列清单。','1天，每次10分钟，其他不确定，给最小可编辑方案。')
                report['checks'].append('real conversation creates and explicitly confirms a new goal')
                change=ask('把这个已种植目标第1步改到明天。',g['session_id'],pid,'change');d=change['result']['data'];assert d['state']=='ready'
                client.post('/api/v1/agent/changes/'+change['id']+'/confirm',json={'version':d['version'],'due_date':d['after']['due_date']}).raise_for_status()
                split=ask('这个已种植目标的第1步太难，帮我拆成两条更小的动作，每次仍最多10分钟。',g['session_id'],pid,'change');d=split['result']['data'];assert d['state']=='ready'
                client.post('/api/v1/agent/changes/'+split['id']+'/confirm',json={'version':d['version'],'steps':d['after']['steps']}).raise_for_status()
                report['checks'].append('real goal date adjustment and split use original preview/confirmation rules')
                rows=client.get('/api/v1/plants/'+pid+'/actions').json()['actions'];assert len(rows)<=10
                first,_=record('这个已种植目标的第1步我确实做完了，请记录。',g['session_id'],pid)
                undo,_=record('撤销刚才那次完成记录。',g['session_id'],pid);assert client.get('/api/v1/plants/'+pid+'/actions').json()['actions'][0]['completed'] is False
                first,_=record('这个目标的第1步确实已完成，重新记录。',g['session_id'],pid)
                repeated=ask('这个目标第1步已经完成了，请再记录。',g['session_id'],pid,'record');assert repeated['result']['data']['state']=='already_recorded'
                for i in range(1,len(rows)):record(f'这个目标第{i+1}步确实已做完，请记录完成。',g['session_id'],pid)
                done=client.get('/api/v1/plants/'+pid+'/actions').json()['actions'];assert all(a['completed'] for a in done)
                record('当前目标所有步骤都做完了，我要确认结束整个目标。',g['session_id'],pid)
                assert client.get('/api/v1/flowers').json()['counts']['goal']==1
                report['checks'].append('goal records, undo-last, semantic repeat, all steps, stage collection and finish reward form one real loop')
                today=main.habit_today('Asia/Shanghai');start=today-timedelta(days=1);end=today+timedelta(days=2)
                h,hid=plan(f'创建一个新习惯：阅读。周期{start}至{end}，每天，每次最多5分钟，单次完成标准阅读满5分钟，本阶段累计2次达标，不安排额外准备或记录。','上述起止日期不变，每天，单次阅读5分钟，预算5分钟，阶段2次达标，其他不确定，请给最小草案。')
                record('这株习惯今天我确实阅读满5分钟了，请打卡。',h['session_id'],hid)
                _,result=record('补签昨天这株阅读习惯，昨天我也确实读满5分钟。',h['session_id'],hid);assert result['stage_completed']
                record('撤销昨天这株习惯的打卡记录。',h['session_id'],hid)
                record('昨天这株习惯确实读满5分钟，重新补签。',h['session_id'],hid)
                assert client.get('/api/v1/flowers').json()['counts']['habit']==1
                record('暂停这株习惯，先不要继续安排。',h['session_id'],hid)
                record('恢复这株习惯，继续按原周期。',h['session_id'],hid)
                record('这株习惯已达标，本阶段不继续了，确认结束。',h['session_id'],hid)
                entry=ask('我想为这株习惯继续下一阶段，打开对应管理入口。',h['session_id'],hid,'record');assert entry['result']['data']['state']=='open_original'
                report['checks'].append('real habit creation, today/backfill, correction/redo, deduplicated collection, pause/resume/end and management entry')
                schedule={'start_date':str(today+timedelta(days=1)),'end_date':str(today+timedelta(days=3)),'timezone':'Asia/Shanghai','weekdays':list(range(7)),'completion_standard':'阅读5分钟','growth_threshold':2}
                p=client.post('/api/v1/plants/'+hid+'/next-stage-proposals',json=schedule);p.raise_for_status()
                client.post('/api/v1/habit-next-proposals/'+p.json()['id']+'/confirm').raise_for_status()
                assert len(client.get('/api/v1/plants/'+hid+'/habit-stages').json()['collections'])==1
                record('更正昨天的记录：撤销这株习惯上一已收藏阶段昨天的打卡。',h['session_id'],hid)
                history=client.get('/api/v1/plants/'+hid+'/habit-stages').json()['collections'];assert len(history)==1 and history[0]['changed']
                report['checks'].append('existing next-stage preview/confirmation works; archived correction preserves collection with changed marker')
                client.delete('/api/v1/agent/sessions/'+g['session_id']).raise_for_status();client.delete('/api/v1/agent/sessions/'+h['session_id']).raise_for_status()
                assert len(client.get('/api/v1/garden').json()['plants'])==2 and client.get('/api/v1/flowers').json()['counts']['goal']==1
                report['checks'].append('chat deletion preserves confirmed plants, action records and accomplishments');report['passed']=True
        except Exception as exc:
            report['error_type']=type(exc).__name__
            if isinstance(exc,RuntimeError):report['error_code']=str(exc)
            raise
        finally:
            with main.connect() as db,sqlite3.connect(original) as target:
                usage=[tuple(r) for r in db.execute('SELECT * FROM usage_reservations') if r['id'] not in before];target.executemany('INSERT OR IGNORE INTO usage_reservations VALUES (?,?,?,?)',usage)
                metrics=[dict(r) for r in db.execute('SELECT * FROM model_call_metrics')]
                for row in metrics:target.execute('INSERT OR IGNORE INTO model_call_metrics VALUES (?,?,?,?,?,?,?)',tuple(row.values()))
            main.DB=original;report['calls']=metrics;report['reserved_cents']=sum(r[3] for r in usage);destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'checks':report['checks'],'calls':len(metrics),'reserved_cents':report['reserved_cents']},ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);run(p.parse_args().output)
