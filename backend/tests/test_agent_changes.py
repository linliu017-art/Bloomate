"""Owned, expiring change previews and real existing business transaction regressions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import sqlite3
import uuid

from fastapi.testclient import TestClient
import pytest

from app import agent, agent_changes as changes, auth, main
from test_auth import server,guest,login
from test_agent import query
from test_cloud_persistence import cloud_store


def seed(client,title='目标A'):
    r=client.post('/api/v1/plans',json={'kind':'goal','goal':title,'availability':'时间不确定'});r.raise_for_status()
    r=client.post('/api/v1/proposals/'+r.json()['id']+'/confirm',json={'title':title,'summary':'阶段计划','steps':['整理资料','写提纲','复核成果']});r.raise_for_status()
    return r.json()['id']


def route(monkeypatch,**values):
    request=changes.ChangeRequest.model_validate({'operation':'date','relative_day':'tomorrow',**values})
    monkeypatch.setattr(agent,'route_intent',lambda *args:agent.Intent(intent='change',change=request))
    return request


def ask(client,pid=None,session=None):
    return query(client,'调整这个已种植的目标',session=session,context={'page':'garden' if pid else 'today','plant_id':pid})[0]


def body(turn):
    d=turn['result']['data'];return {'version':d['version'],**({'due_date':d['after']['due_date']} if d['operation']=='date' else {'steps':d['after']['steps']})}


def confirm(client,turn,data=None):
    return client.post('/api/v1/agent/changes/'+turn['id']+'/confirm',json=data or body(turn))


def action(client,pid,index=0):
    return client.get('/api/v1/plants/'+pid+'/actions').json()['actions'][index]


def test_date_preview_requires_confirmation_and_retries_are_idempotent(server,monkeypatch):
    guest(server);pid=seed(server);route(monkeypatch,step_number=1)
    turn=ask(server,pid);assert turn['result']['data']['state']=='ready'
    assert action(server,pid)['due_date'] is None
    with ThreadPoolExecutor(max_workers=4) as pool:responses=list(pool.map(lambda _:confirm(server,turn),range(4)))
    assert all(r.status_code==200 for r in responses)
    tomorrow=(main.habit_today('Asia/Shanghai')+timedelta(days=1)).isoformat()
    assert action(server,pid)['due_date']==tomorrow
    changed=body(turn);changed['due_date']=None;assert confirm(server,turn,changed).status_code==409
    assert len(server.get('/api/v1/garden').json()['plants'])==1


def test_ambiguous_tasks_require_explicit_owned_choice_and_no_second_router_call(server,monkeypatch):
    guest(server);login(server);a=seed(server);b=seed(server,'目标B');route(monkeypatch)
    first=ask(server);d=first['result']['data'];assert d['state']=='select_task' and len(d['choices'])==6
    picked=next(r for r in d['choices'] if r['target']['plant_id']==b and r['target']['index']==1)
    monkeypatch.setattr(agent,'route_intent',lambda *args:(_ for _ in ()).throw(AssertionError('choice must be deterministic')))
    turn,_=query(server,'选这一步',session=first['session_id'],context={'page':'today','plant_id':b,'change_choice':{'turn_id':first['id'],'version':d['version'],'target':picked['target']}})
    assert turn['result']['data']['state']=='ready'
    assert confirm(server,turn).status_code==200
    assert action(server,a,1)['due_date'] is None and action(server,b,1)['due_date']


def test_need_date_reply_and_clear_date(server,monkeypatch):
    guest(server);pid=seed(server);route(monkeypatch,step_number=1,relative_day=None)
    first=ask(server,pid);assert first['result']['data']['state']=='need_date'
    route(monkeypatch,clear_date=True,relative_day=None)
    second=ask(server,pid,first['session_id']);assert second['result']['data']['state']=='ready'
    assert confirm(server,first,{'version':first['result']['data']['version'],'due_date':None}).status_code==409
    assert confirm(server,second).status_code==200


@pytest.mark.parametrize('mutation',['date','text','completion','pause','cancel'])
def test_manual_changes_make_old_preview_stale(server,monkeypatch,mutation):
    guest(server);pid=seed(server);route(monkeypatch,step_number=1);turn=ask(server,pid)
    base='/api/v1/plants/'+pid
    if mutation=='date':server.put(base+'/actions/1/date',json={'due_date':'2026-12-01'}).raise_for_status()
    if mutation=='text':server.put(base+'/actions/1/text',json={'text':'已手动修改的步骤'}).raise_for_status()
    if mutation=='completion':server.put(base+'/actions/1',json={'completed':True}).raise_for_status()
    if mutation=='pause':server.put(base+'/pause',json={'paused':True}).raise_for_status()
    if mutation=='cancel':server.put(base+'/actions/1/cancel',json={'cancelled':True}).raise_for_status()
    r=confirm(server,turn);assert r.status_code==409 and r.json()['error']['code']=='PLAN_CHANGED'
    assert action(server,pid)['due_date'] is None


def test_selection_after_index_change_requires_fresh_choice(server,monkeypatch):
    guest(server);pid=seed(server);route(monkeypatch)
    first=ask(server,pid);d=first['result']['data'];picked=d['choices'][0]['target']
    server.put('/api/v1/plants/'+pid+'/actions/0/text',json={'text':'变化后的步骤'}).raise_for_status()
    turn,_=query(server,'选择',session=first['session_id'],context={'page':'garden','plant_id':pid,'change_choice':{'turn_id':first['id'],'version':d['version'],'target':picked}})
    assert turn['result']['data']['state']=='select_task'
    assert '变化' in turn['result']['text']


@pytest.mark.parametrize('mutation',['completion','cancel','pause'])
def test_ineligible_steps_are_never_modified(server,monkeypatch,mutation):
    guest(server);pid=seed(server);base='/api/v1/plants/'+pid
    if mutation=='completion':server.put(base+'/actions/0',json={'completed':True}).raise_for_status()
    if mutation=='cancel':server.put(base+'/actions/0/cancel',json={'cancelled':True}).raise_for_status()
    if mutation=='pause':server.put(base+'/pause',json={'paused':True}).raise_for_status()
    route(monkeypatch,step_number=1);turn=ask(server,pid)
    assert turn['result']['data']['state']=='blocked'
    assert confirm(server,turn,{'version':turn['result']['data']['version'],'due_date':None}).status_code==409


def test_discard_new_version_delete_expiry_and_owner_boundaries(server,monkeypatch):
    guest(server);pid=seed(server);route(monkeypatch,step_number=1);first=ask(server,pid);second=ask(server,pid,first['session_id'])
    assert confirm(server,first).status_code==409
    with TestClient(main.app) as other:
        guest(other);assert confirm(other,second).status_code==404
        assert other.post('/api/v1/agent/changes/'+second['id']+'/discard',json={'version':body(second)['version']}).status_code==404
    path='/api/v1/agent/changes/'+second['id']+'/discard'
    for _ in range(2):assert server.post(path,json={'version':body(second)['version']}).json()['result']['data']['state']=='discarded'
    assert confirm(server,second).status_code==409
    third=ask(server,pid);server.delete('/api/v1/agent/sessions/'+third['session_id']).raise_for_status();assert confirm(server,third).status_code==404
    fourth=ask(server,pid)
    with main.connect() as db:db.execute('UPDATE agent_turns SET expires_at=? WHERE id=?',(auth.now()-1,fourth['id']))
    assert confirm(server,fourth).status_code==404 and action(server,pid)['due_date'] is None


def test_split_reuses_original_weights_completion_and_date_rules(server,monkeypatch):
    guest(server);pid=seed(server);base='/api/v1/plants/'+pid
    server.put(base+'/actions/0/date',json={'due_date':'2026-11-14'}).raise_for_status()
    server.put(base+'/actions/2/date',json={'due_date':'2026-11-16'}).raise_for_status()
    server.put(base+'/actions/2',json={'completed':True}).raise_for_status()
    growth=server.get(base+'/growth').json()['progress_percent']
    route(monkeypatch,operation='split',relative_day=None,step_number=1,difficulty='太难了')
    turn=ask(server,pid);assert turn['result']['data']['state']=='ready'
    with main.connect() as db:assert db.execute('SELECT count(*) FROM plan_adjustments').fetchone()[0]==0
    submitted=body(turn);submitted['steps']=['先列一个关键点','再补一条说明']
    response=confirm(server,turn,submitted);assert response.status_code==200,response.text
    assert confirm(server,turn,submitted).status_code==200
    rows=server.get(base+'/actions').json()['actions'];assert len(rows)==4
    assert rows[0]['due_date']=='2026-11-14' and rows[1]['due_date'] is None
    assert rows[3]['completed'] and rows[3]['due_date']=='2026-11-16'
    assert server.get(base+'/growth').json()['progress_percent']==growth
    with main.connect() as db:assert db.execute('SELECT count(*) FROM plan_adjustments').fetchone()[0]==1
    server.delete('/api/v1/agent/sessions/'+turn['session_id']).raise_for_status()
    assert len(server.get(base+'/actions').json()['actions'])==4


def test_invalid_split_rolls_back_and_shifted_index_preview_is_rejected(server,monkeypatch):
    guest(server);pid=seed(server);route(monkeypatch,operation='split',relative_day=None,step_number=1)
    turn=ask(server,pid)
    assert confirm(server,turn,{'version':body(turn)['version'],'steps':[' ']}).status_code==422
    assert confirm(server,turn,{'version':body(turn)['version'],'steps':['一','二'],'due_date':None}).status_code==422
    # A separate manual split changes indices before confirmation.
    a=server.post('/api/v1/plants/'+pid+'/adjustments',json={'step_index':0,'difficulty':'拆小'}).json()['id']
    server.post('/api/v1/adjustments/'+a+'/confirm',json={'steps':['手动一','手动二']}).raise_for_status()
    assert confirm(server,turn).status_code==409
    assert len(server.get('/api/v1/plants/'+pid+'/actions').json()['actions'])==4


def test_current_followup_dates_and_archived_initial_are_distinct(server,monkeypatch):
    guest(server);pid=seed(server);base='/api/v1/plants/'+pid
    for i in range(3):server.put(base+'/actions/'+str(i),json={'completed':True}).raise_for_status()
    proposal=server.post(base+'/goal-next-proposals',json={'title':'下一阶段','steps':['下一阶段第一步','下一阶段第二步']}).json()['id']
    stage=server.post('/api/v1/goal-next-proposals/'+proposal+'/confirm').json()['stage_id']
    route(monkeypatch,step_number=1);turn=ask(server,pid)
    assert turn['result']['data']['target']['stage_id']==stage
    assert confirm(server,turn).status_code==200
    rows=server.get(base+'/goal-stages').json()['stages'];active=next(s for s in rows if s['active']);assert active['dates'][0]
    assert action(server,pid)['due_date'] is None
    route(monkeypatch,operation='split',relative_day=None,step_number=1);blocked=ask(server,pid)
    assert blocked['result']['data']['state']=='blocked'


def test_legacy_context_retry_remains_compatible(server,monkeypatch):
    guest(server);turn,submitted=query(server)
    # Previous release serialized exactly these two fields.
    with main.connect() as db:assert db.execute('SELECT context_json FROM agent_turns WHERE id=?',(turn['id'],)).fetchone()[0]=='{"page":"today","plant_id":null}'
    assert server.post('/api/v1/agent/turns',json=submitted).json()['id']==turn['id']


def test_cloud_change_preview_and_confirmation_survive_restarts(cloud_store,monkeypatch):
    route(monkeypatch,step_number=1)
    with TestClient(main.app) as client:
        guest(client);pid=seed(client);turn=ask(client,pid)
        cookie=client.cookies.get(auth.GUEST_COOKIE);csrf=client.headers['X-Bloomate-CSRF']
    with TestClient(main.app) as client:
        client.cookies.set(auth.GUEST_COOKIE,cookie);client.headers.update({'Origin':'http://testserver','X-Bloomate-CSRF':csrf})
        assert client.get('/api/v1/agent/turns/'+turn['id']).json()['result']['data']['state']=='ready'
        confirm(client,turn).raise_for_status()
    with TestClient(main.app) as client:
        client.cookies.set(auth.GUEST_COOKIE,cookie);client.headers.update({'Origin':'http://testserver','X-Bloomate-CSRF':csrf})
        assert client.get('/api/v1/agent/turns/'+turn['id']).json()['result']['data']['state']=='confirmed'
        confirm(client,turn).raise_for_status();assert action(client,pid)['due_date']


def test_guest_change_preview_moves_to_account_and_can_export(server,monkeypatch):
    guest(server);pid=seed(server);route(monkeypatch,step_number=1);turn=ask(server,pid);login(server)
    p=server.post('/api/v1/auth/guest-save/preview').json()
    server.post('/api/v1/auth/guest-save/'+p['operation_id']+'/confirm',json={'version':p['version']}).raise_for_status()
    confirm(server,turn).raise_for_status()
    assert len(server.post('/api/v1/auth/account/export').json()['data']['agent_turns'])==1


def test_forged_and_cross_session_choices_do_not_select_a_different_step(server,monkeypatch):
    guest(server);pid=seed(server);route(monkeypatch);turn=ask(server,pid);d=turn['result']['data']
    forged={**d['choices'][0]['target'],'base_hash':'0'*64}
    result,_=query(server,'选择',session=turn['session_id'],context={'page':'garden','plant_id':pid,'change_choice':{'turn_id':turn['id'],'version':d['version'],'target':forged}})
    assert result['status']=='error' and result['error']['code']=='CHANGE_STALE'
    result,_=query(server,'另一个会话选择',context={'page':'garden','plant_id':pid,'change_choice':{'turn_id':turn['id'],'version':d['version'],'target':d['choices'][0]['target']}})
    assert result['status']=='error' and result['error']['code']=='NOT_FOUND'
    assert action(server,pid)['due_date'] is None


def test_split_step_limit_failure_rolls_back_business_adjustment(server,monkeypatch):
    guest(server)
    r=server.post('/api/v1/plans',json={'kind':'goal','goal':'十步目标','availability':'不确定'}).json()['id']
    pid=server.post('/api/v1/proposals/'+r+'/confirm',json={'title':'十步目标','summary':'测试上限','steps':['步骤'+str(i) for i in range(10)]}).json()['id']
    route(monkeypatch,operation='split',relative_day=None,step_number=1)
    turn=ask(server,pid)
    assert turn['result']['data']['state']=='ready'
    response=confirm(server,turn,{'version':body(turn)['version'],'steps':['小步一','小步二']})
    assert response.status_code==409
    with main.connect() as db:assert db.execute('SELECT count(*) FROM plan_adjustments').fetchone()[0]==0
    assert len(server.get('/api/v1/plants/'+pid+'/actions').json()['actions'])==10


def test_relative_date_is_frozen_before_choice_and_not_shifted_after_restart(server,monkeypatch):
    guest(server);pid=seed(server);route(monkeypatch)
    first=ask(server,pid);d=first['result']['data'];expected=(main.habit_today('Asia/Shanghai')+timedelta(days=1)).isoformat()
    assert d['request']['due_date']==expected and d['request']['relative_day'] is None
    old_day=main.habit_today('Asia/Shanghai');monkeypatch.setattr(main,'habit_today',lambda _:old_day+timedelta(days=1))
    main.init_db()
    chosen,_=query(server,'选择',session=first['session_id'],context={'page':'garden','plant_id':pid,'change_choice':{'turn_id':first['id'],'version':d['version'],'target':d['choices'][0]['target']}})
    assert chosen['result']['data']['after']['due_date']==expected


def test_routing_context_omits_internal_action_ids_and_baseline_hash(server,monkeypatch):
    import httpx
    from test_agent import configure_model
    from app.identity import acting_as
    actual_router=agent.route_intent
    guest(server);pid=seed(server);route(monkeypatch,step_number=1);turn=ask(server,pid)
    d=turn['result']['data'];configure_model(monkeypatch);captured=[]
    def response(*args,**kwargs):
        captured.append(json.loads(kwargs['json']['messages'][1]['content']))
        return httpx.Response(200,request=httpx.Request('POST',args[0]),json={'choices':[{'message':{'content':'{"intent":"today"}'}}]})
    monkeypatch.setattr(httpx,'post',response)
    with main.connect() as db:owner=db.execute('SELECT id FROM auth_subjects').fetchone()[0]
    with acting_as(owner):actual_router('今天做什么',agent.Context(),None,[],change=changes.active_change(turn['session_id']))
    sent=json.dumps(captured[0]['active_change'],ensure_ascii=False)
    assert pid not in sent and d['target']['base_hash'] not in sent and '整理资料' in sent
