"""Action records use actual business services, disposable stores and mocked model transport."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
import json
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from app import agent,agent_records as records,auth,main
from test_auth import server,guest,login
from test_agent import query
from test_cloud_persistence import cloud_store


def goal(client,count=3):
    r=client.post('/api/v1/plans',json={'kind':'goal','goal':'测试目标','availability':'时间不确定'});r.raise_for_status()
    r=client.post('/api/v1/proposals/'+r.json()['id']+'/confirm',json={'title':'测试目标','summary':'目标计划','steps':['步骤'+str(i+1) for i in range(count)]});r.raise_for_status();return r.json()['id']


def habit(client,threshold=3,weekdays=None):
    today=main.habit_today('Asia/Shanghai');schedule={'start_date':str(today-timedelta(days=3)),'end_date':str(today+timedelta(days=3)),'timezone':'Asia/Shanghai','weekdays':weekdays or list(range(7)),'completion_standard':'阅读10分钟','growth_threshold':threshold}
    r=client.post('/api/v1/plans',json={'kind':'habit','goal':'测试阅读','availability':'每天10分钟','habit_schedule':schedule});r.raise_for_status()
    r=client.post('/api/v1/proposals/'+r.json()['id']+'/confirm');r.raise_for_status();return r.json()['id']


def route(monkeypatch,**fields):
    value=records.RecordRequest.model_validate({'operation':'complete','certainty':'reported',**fields})
    monkeypatch.setattr(agent,'route_intent',lambda *args:agent.Intent(intent='record',record=value));return value


def ask(client,pid=None,session=None,text='我已实际完成这项行动'):
    return query(client,text,session=session,context={'page':'garden' if pid else 'today','plant_id':pid})[0]


def body(turn):
    return {'version':turn['result']['data']['version'],'performed':turn['result']['data']['operation']=='complete'}


def confirm(client,turn,submitted=None):
    return client.post('/api/v1/agent/records/'+turn['id']+'/confirm',json=submitted or body(turn))


def state(client,pid,index=0):
    return client.get('/api/v1/plants/'+pid+'/actions').json()['actions'][index]['completed']


def test_explicit_goal_record_confirm_undo_last_and_repeated_reports(server,monkeypatch):
    guest(server);pid=goal(server);route(monkeypatch,step_number=1)
    first=ask(server,pid);assert first['result']['data']['state']=='ready' and not state(server,pid)
    assert confirm(server,first,{'version':body(first)['version'],'performed':False}).status_code==422
    assert confirm(server,first).status_code==200 and state(server,pid)
    again=ask(server,pid,first['session_id']);assert again['result']['data']['state']=='already_recorded'
    route(monkeypatch,operation='undo',undo_last=True)
    undo=ask(server,pid,first['session_id'],text='撤销刚才那次记录');assert undo['result']['data']['state']=='ready'
    assert confirm(server,undo).status_code==200 and not state(server,pid)
    # A lost reply retry of the original completion must not redo the later undo.
    assert confirm(server,first).status_code==200 and not state(server,pid)


def test_concurrent_record_confirmation_is_one_effect(server,monkeypatch):
    guest(server);pid=goal(server);route(monkeypatch,step_number=1);turn=ask(server,pid)
    with ThreadPoolExecutor(max_workers=4) as pool:responses=list(pool.map(lambda _:confirm(server,turn),range(4)))
    assert all(r.status_code==200 for r in responses) and state(server,pid)
    assert confirm(server,turn,{**body(turn),'performed':False}).status_code==409


def test_goal_last_step_opens_flower_once_and_undo_keeps_collection(server,monkeypatch):
    guest(server);pid=goal(server,2);session=None
    for i in [1,2]:
        route(monkeypatch,step_number=i);t=ask(server,pid,session);session=t['session_id'];result=confirm(server,t);result.raise_for_status()
    assert result.json()['stage_completed']
    assert client_collection(server,pid)
    route(monkeypatch,operation='end',kind='goal');finish=ask(server,pid,session);confirm(server,finish).raise_for_status()
    assert server.get('/api/v1/flowers').json()['counts']['goal']==1
    route(monkeypatch,operation='undo',step_number=2);u=ask(server,pid,session);confirm(server,u).raise_for_status()
    assert client_collection(server,pid) and not state(server,pid,1)
    route(monkeypatch,step_number=2);redo=ask(server,pid,session);confirm(server,redo).raise_for_status()
    assert server.get('/api/v1/flowers').json()['counts']['goal']==1


def client_collection(client,pid):
    return client.get('/api/v1/plants/'+pid+'/collection').json()['collection']


def test_ambiguous_goal_choice_is_deterministic_and_owner_scoped(server,monkeypatch):
    guest(server);pid=goal(server);route(monkeypatch);first=ask(server)
    d=first['result']['data'];assert d['state']=='select_task' and len(d['choices'])==3
    monkeypatch.setattr(agent,'route_intent',lambda *args:(_ for _ in ()).throw(AssertionError('must not classify choice')))
    chosen,_=query(server,'选第二步',session=first['session_id'],context={'page':'today','plant_id':pid,'record_choice':{'turn_id':first['id'],'version':d['version'],'target':d['choices'][1]['target']}})
    confirm(server,chosen).raise_for_status();assert not state(server,pid,0) and state(server,pid,1)
    with TestClient(main.app) as other:
        guest(other);assert confirm(other,chosen).status_code==404


@pytest.mark.parametrize('text',['我还没完成','我不确定是否完成了','我可能完成了','我准备做完'])
def test_uncertain_or_negative_report_cannot_record(server,monkeypatch,text):
    guest(server);pid=goal(server);route(monkeypatch,step_number=1)
    turn=ask(server,pid,text=text);assert turn['result']['data']['state']=='clarify_report'
    assert confirm(server,turn).status_code==409 and not state(server,pid)


@pytest.mark.parametrize('mutation',['date','text','completion','pause','cancel'])
def test_stale_record_preview_cannot_overwrite_manual_changes(server,monkeypatch,mutation):
    guest(server);pid=goal(server);route(monkeypatch,step_number=1);turn=ask(server,pid);base='/api/v1/plants/'+pid
    if mutation=='date':server.put(base+'/actions/0/date',json={'due_date':'2026-12-01'}).raise_for_status()
    if mutation=='text':server.put(base+'/actions/0/text',json={'text':'手动改过的步骤'}).raise_for_status()
    if mutation=='completion':server.put(base+'/actions/0',json={'completed':True}).raise_for_status()
    if mutation=='pause':server.put(base+'/pause',json={'paused':True}).raise_for_status()
    if mutation=='cancel':server.put(base+'/actions/0/cancel',json={'cancelled':True}).raise_for_status()
    r=confirm(server,turn);assert r.status_code==409 and r.json()['error']['code']=='PLAN_CHANGED'


def test_current_followup_records_and_undo_do_not_touch_initial_stage(server,monkeypatch):
    guest(server);pid=goal(server,1);server.put('/api/v1/plants/'+pid+'/actions/0',json={'completed':True}).raise_for_status()
    p=server.post('/api/v1/plants/'+pid+'/goal-next-proposals',json={'title':'下一阶段','steps':['后续行动']}).json()['id']
    stage=server.post('/api/v1/goal-next-proposals/'+p+'/confirm').json()['stage_id']
    route(monkeypatch,step_number=1);turn=ask(server,pid);assert turn['result']['data']['target']['stage_id']==stage
    assert confirm(server,turn).json()['stage_completed']
    route(monkeypatch,operation='undo',undo_last=True);undo=ask(server,pid,turn['session_id'],text='撤销刚才');confirm(server,undo).raise_for_status()
    assert state(server,pid)
    active=next(s for s in server.get('/api/v1/plants/'+pid+'/goal-stages').json()['stages'] if s['active']);assert active['states']==[False]


def test_habit_today_backfill_undo_and_achievement_dedup(server,monkeypatch):
    guest(server);pid=habit(server,threshold=2);route(monkeypatch,kind='habit');first=ask(server,pid);confirm(server,first).raise_for_status()
    assert ask(server,pid,first['session_id'])['result']['data']['state']=='already_recorded'
    route(monkeypatch,kind='habit',relative_day='yesterday',backfill=True);second=ask(server,pid,first['session_id']);r=confirm(server,second);r.raise_for_status();assert r.json()['stage_completed']
    assert server.get('/api/v1/flowers').json()['counts']['habit']==1
    route(monkeypatch,kind='habit',operation='undo',relative_day='yesterday');u=ask(server,pid,first['session_id']);confirm(server,u).raise_for_status()
    route(monkeypatch,kind='habit',relative_day='yesterday');redo=ask(server,pid,first['session_id']);confirm(server,redo).raise_for_status()
    assert server.get('/api/v1/flowers').json()['counts']['habit']==1
    with main.connect() as db:assert db.execute('SELECT count(*) FROM habit_collections').fetchone()[0]==1


def test_habit_missing_backfill_date_and_reply_can_continue(server,monkeypatch):
    guest(server);pid=habit(server);route(monkeypatch,kind='habit',backfill=True);first=ask(server,pid)
    assert first['result']['data']['state']=='need_date'
    route(monkeypatch,kind='habit',backfill=True,relative_day='yesterday');next_turn=ask(server,pid,first['session_id']);confirm(server,next_turn).raise_for_status()


@pytest.mark.parametrize('offset',[1,-4])
def test_habit_future_and_outside_window_are_blocked(server,monkeypatch,offset):
    guest(server);pid=habit(server);route(monkeypatch,kind='habit',action_date=main.habit_today('Asia/Shanghai')+timedelta(days=offset))
    turn=ask(server,pid);assert turn['result']['data']['state']=='blocked'
    with main.connect() as db:assert db.execute('SELECT count(*) FROM habit_events').fetchone()[0]==0


def test_non_plan_day_and_pause_resume_keep_original_rules(server,monkeypatch):
    guest(server);today=main.habit_today('Asia/Shanghai');pid=habit(server,threshold=1,weekdays=[(today.weekday()+1)%7]);route(monkeypatch,kind='habit')
    assert ask(server,pid)['result']['data']['state']=='blocked'
    route(monkeypatch,operation='pause');pause=ask(server,pid);confirm(server,pause).raise_for_status()
    route(monkeypatch,operation='resume');resume=ask(server,pid,pause['session_id']);confirm(server,resume).raise_for_status()
    assert not server.get('/api/v1/plants/'+pid+'/pause').json()['paused']


def test_habit_paused_day_cannot_record_even_after_same_day_resume(server,monkeypatch):
    guest(server);pid=habit(server);route(monkeypatch,operation='pause');t=ask(server,pid);confirm(server,t).raise_for_status()
    route(monkeypatch,operation='resume');r=ask(server,pid,t['session_id']);confirm(server,r).raise_for_status()
    route(monkeypatch,kind='habit');assert ask(server,pid)['result']['data']['state']=='blocked'


def test_end_and_next_stage_entry_preserve_business_state(server,monkeypatch):
    guest(server);pid=habit(server,threshold=1);route(monkeypatch,kind='habit');done=ask(server,pid);confirm(server,done).raise_for_status()
    route(monkeypatch,operation='end',kind='habit');end=ask(server,pid,done['session_id']);assert confirm(server,end).json()['ended']
    route(monkeypatch,operation='manage',kind='habit');entry=ask(server,pid,done['session_id']);assert entry['result']['data']['state']=='open_original'
    assert len(server.get('/api/v1/plants/'+pid+'/habit-stages').json()['collections'])==1


def test_discard_delete_expiry_guest_save_and_export(server,monkeypatch):
    guest(server);pid=goal(server);route(monkeypatch,step_number=1);turn=ask(server,pid)
    path='/api/v1/agent/records/'+turn['id']+'/discard'
    for _ in range(2):assert server.post(path,json={'version':body(turn)['version']}).json()['result']['data']['state']=='discarded'
    assert confirm(server,turn).status_code==409 and not state(server,pid)
    new=ask(server,pid);login(server);p=server.post('/api/v1/auth/guest-save/preview').json();server.post('/api/v1/auth/guest-save/'+p['operation_id']+'/confirm',json={'version':p['version']}).raise_for_status();confirm(server,new).raise_for_status()
    assert len(server.post('/api/v1/auth/account/export').json()['data']['agent_turns'])==2
    server.delete('/api/v1/agent/sessions/'+new['session_id']).raise_for_status();assert confirm(server,new).status_code==404 and state(server,pid)
    other=ask(server,pid)
    with main.connect() as db:db.execute('UPDATE agent_turns SET expires_at=? WHERE id=?',(auth.now()-1,other['id']))
    assert confirm(server,other).status_code==404


def test_archived_correction_after_next_stage_keeps_collection(server,monkeypatch):
    guest(server);pid=habit(server,threshold=1);today=main.habit_today('Asia/Shanghai')
    route(monkeypatch,kind='habit',relative_day='yesterday');first=ask(server,pid);confirm(server,first).raise_for_status()
    old=first['result']['data']['target']['stage_id']
    proposal=server.post('/api/v1/plants/'+pid+'/next-stage-proposals',json={'start_date':str(today+timedelta(days=1)),'end_date':str(today+timedelta(days=3)),'timezone':'Asia/Shanghai','weekdays':list(range(7)),'completion_standard':'阅读10分钟','growth_threshold':2});proposal.raise_for_status()
    server.post('/api/v1/habit-next-proposals/'+proposal.json()['id']+'/confirm').raise_for_status()
    route(monkeypatch,operation='undo',kind='habit',relative_day='yesterday');undo=ask(server,pid,first['session_id'])
    if undo['result']['data']['state']=='select_task':
        d=undo['result']['data'];assert len(d['choices'])==1 and d['choices'][0]['target']['stage_id']==old
        undo,_=query(server,'选择上个阶段',session=first['session_id'],context={'page':'garden','plant_id':pid,'record_choice':{'turn_id':undo['id'],'version':d['version'],'target':d['choices'][0]['target']}})
    assert undo['result']['data']['target']['stage_id']==old and undo['result']['data']['before']['archived']
    confirm(server,undo).raise_for_status()
    history=server.get('/api/v1/plants/'+pid+'/habit-stages').json()['collections'];assert len(history)==1 and history[0]['changed']
    assert server.get('/api/v1/flowers').json()['counts']['habit']==1


def test_choice_uses_habit_timezone_and_freezes_date_at_original_request(server,monkeypatch):
    guest(server);pid=habit(server)
    with main.connect() as db:db.execute("UPDATE habit_stages SET timezone='Pacific/Honolulu' WHERE plant_id=?",(pid,))
    anchor=auth.now();expected=datetime.fromtimestamp(anchor,ZoneInfo('Pacific/Honolulu')).date()
    route(monkeypatch,kind='habit',plant_title='名称不精确所以需选择')
    first=ask(server,pid);d=first['result']['data'];assert d['state']=='select_task'
    assert d['choices'][0]['target']['action_date']==str(expected)
    monkeypatch.setattr(auth,'now',lambda:anchor+86400)
    chosen,_=query(server,'选择这项',session=first['session_id'],context={'page':'garden','plant_id':pid,'record_choice':{'turn_id':first['id'],'version':d['version'],'target':d['choices'][0]['target']}})
    assert chosen['result']['data']['state']=='ready' and chosen['result']['data']['target']['action_date']==str(expected)


def test_new_pending_card_and_manual_change_prevent_undo_last_overwrite(server,monkeypatch):
    guest(server);pid=goal(server);route(monkeypatch,step_number=1);first=ask(server,pid)
    newer=ask(server,pid,first['session_id']);assert confirm(server,first).status_code==409
    confirm(server,newer).raise_for_status()
    server.put('/api/v1/plants/'+pid+'/actions/1/text',json={'text':'手动修改后的步骤'}).raise_for_status()
    route(monkeypatch,operation='undo',undo_last=True);undo=ask(server,pid,first['session_id'],text='撤销刚才')
    assert undo['result']['data']['state']=='select_task' and confirm(server,undo).status_code==409 and state(server,pid)


def test_record_cloud_contract_restores_pending_confirmed_and_isolates_owner(cloud_store,monkeypatch):
    with TestClient(main.app) as client:
        guest(client);pid=goal(client);route(monkeypatch,step_number=1);turn=ask(client,pid)
        cookies=dict(client.cookies);headers=dict(client.headers)
    with TestClient(main.app) as restored:
        restored.cookies.update(cookies);restored.headers.update(headers)
        assert restored.get('/api/v1/agent/turns/'+turn['id']).json()['result']['data']['state']=='ready'
        confirm(restored,turn).raise_for_status();assert state(restored,pid)
    with TestClient(main.app) as restored:
        restored.cookies.update(cookies);restored.headers.update(headers)
        assert restored.get('/api/v1/agent/turns/'+turn['id']).json()['result']['data']['state']=='confirmed'
        confirm(restored,turn).raise_for_status();assert state(restored,pid)
        with TestClient(main.app) as other:
            guest(other);assert confirm(other,turn).status_code==404


def test_record_routing_context_excludes_internal_ids_and_hashes(server,monkeypatch):
    import httpx
    from test_agent import configure_model
    from app.identity import acting_as
    actual_router=agent.route_intent
    guest(server);pid=goal(server);route(monkeypatch,step_number=1);turn=ask(server,pid)
    d=turn['result']['data'];configure_model(monkeypatch);captured=[]
    def response(*args,**kwargs):
        captured.append(json.loads(kwargs['json']['messages'][1]['content']))
        return httpx.Response(200,request=httpx.Request('POST',args[0]),json={'choices':[{'message':{'content':'{"intent":"today"}'}}]})
    monkeypatch.setattr(httpx,'post',response)
    with main.connect() as db:owner=db.execute('SELECT id FROM auth_subjects').fetchone()[0]
    with acting_as(owner):actual_router('今天做什么',agent.Context(),None,[],record=records.active_record(turn['session_id']))
    sent=json.dumps(captured[0]['active_record'],ensure_ascii=False)
    assert pid not in sent and d['target']['base_hash'] not in sent and '步骤1' in sent
