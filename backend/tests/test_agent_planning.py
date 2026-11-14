"""Draft creation uses real business rules, temporary databases and mocked model outputs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json

from fastapi.testclient import TestClient

from app import agent, agent_planning as planning, main, auth
from test_auth import server, guest, login, plant
from test_agent import query


def fields(**changes):
    return planning.DraftFields.model_validate({'kind':'goal','goal':'整理分享提纲', 'duration_days':3,'daily_minutes':10,**changes})


def route(monkeypatch, value):
    monkeypatch.setattr(agent, 'route_intent', lambda *args: agent.Intent(intent='plan',fields=value))


def payload(turn, **changes):
    d=turn['result']['data']
    return {'version':d['version'],'plan':d['proposal'],'goal_dates':d['goal_dates'],**changes}


def confirm(client, turn, body=None):
    return client.post('/api/v1/agent/turns/'+turn['id']+'/confirm',json=body or payload(turn))


def test_goal_draft_has_no_business_writes_and_edit_is_confirmed_atomically(server,monkeypatch):
    guest(server);route(monkeypatch,fields())
    turn,_=query(server,'帮我拆一个新目标')
    assert turn['result']['data']['state']=='ready'
    with main.connect() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM plants').fetchone()[0]==0
    body=payload(turn);body['plan']['title']='修改后的提纲';body['plan']['steps']=['修改后的第一步']
    planted=confirm(server,turn,body);assert planted.status_code==200,planted.text
    pid=planted.json()['id']
    assert server.get('/api/v1/today').json()['goals_today'][0]['plant_id']==pid
    assert server.get('/api/v1/garden').json()['plants'][0]['title']=='修改后的提纲'
    for _ in range(2):assert confirm(server,turn,body).json()['id']==pid
    body['plan']['title']='不同版本';assert confirm(server,turn,body).status_code==409
    with main.connect() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM plants').fetchone()[0]==1
        source=json.loads(db.execute('SELECT input_json FROM jobs').fetchone()[0]);assert 'history' not in source


def test_missing_conditions_and_generator_followup_can_resume(server,monkeypatch):
    guest(server);route(monkeypatch,fields(duration_days=None,daily_minutes=None))
    first,_=query(server,'想整理提纲')
    assert first['result']['data']['state']=='collecting'
    route(monkeypatch,fields(goal='需要澄清的目标'))
    second,_=query(server,'三天十分钟',session=first['session_id'])
    assert second['result']['data']['state']=='collecting'
    assert second['result']['data']['source']
    third,_=query(server,'时间不确定',session=first['session_id'])
    assert third['result']['data']['state']=='ready'
    assert confirm(server,third).status_code==200


def test_new_draft_invalidates_old_card_and_discard_never_plants(server,monkeypatch):
    guest(server);route(monkeypatch,fields())
    first,_=query(server)
    route(monkeypatch,fields(goal='新版提纲'))
    second,_=query(server,'修改草案',session=first['session_id'])
    assert confirm(server,first).status_code==409
    assert server.get('/api/v1/agent/turns/'+first['id']).json()['result']['data']['state']=='superseded'
    path='/api/v1/agent/turns/'+second['id']+'/discard'
    for _ in range(2):assert server.post(path,json={'version':payload(second)['version']}).json()['result']['data']['state']=='discarded'
    assert confirm(server,second).status_code==409
    assert server.get('/api/v1/garden').json()['plants']==[]


def test_cross_owner_confirm_discard_and_deleted_draft(server,monkeypatch):
    guest(server);route(monkeypatch,fields());turn,_=query(server)
    with TestClient(main.app) as other:
        guest(other)
        assert confirm(other,turn).status_code==404
        assert other.post('/api/v1/agent/turns/'+turn['id']+'/discard',json={'version':payload(turn)['version']}).status_code==404
    assert server.delete('/api/v1/agent/sessions/'+turn['session_id']).status_code==200
    assert confirm(server,turn).status_code==404
    assert server.get('/api/v1/garden').json()['plants']==[]


def test_concurrent_confirmations_create_one_plant(server,monkeypatch):
    guest(server);route(monkeypatch,fields());turn,_=query(server)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses=list(pool.map(lambda _:confirm(server,turn),range(4)))
    assert all(r.status_code==200 for r in responses)
    assert len({r.json()['id'] for r in responses})==1


def test_constraint_failures_rollback_and_guest_limit_is_preserved(server,monkeypatch):
    guest(server);route(monkeypatch,fields());turn,_=query(server)
    body=payload(turn);body['plan']['session_actions'][0]['minutes']=100
    assert confirm(server,turn,body).status_code==409
    assert confirm(server,turn,payload(turn,goal_dates=[])).status_code==422
    wrong=payload(turn);wrong['goal_dates']=['2099-01-01'];assert confirm(server,turn,wrong).status_code==409
    plant(server)
    assert confirm(server,turn).status_code==409
    with main.connect() as db:assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==1


def test_confirmed_plant_survives_chat_delete_and_expired_drafts_cannot_confirm(server,monkeypatch):
    guest(server);route(monkeypatch,fields());turn,_=query(server)
    pid=confirm(server,turn).json()['id']
    assert server.delete('/api/v1/agent/sessions/'+turn['session_id']).status_code==200
    assert server.get('/api/v1/garden').json()['plants'][0]['id']==pid
    draft,_=query(server)
    with main.connect() as db:db.execute('UPDATE agent_turns SET expires_at=? WHERE id=?',(auth.now()-1,draft['id']))
    assert confirm(server,draft).status_code==404
    assert server.get('/api/v1/garden').json()['plants'][0]['id']==pid


def test_unconfirmed_draft_can_save_to_account_and_export(server,monkeypatch):
    guest(server);route(monkeypatch,fields());turn,_=query(server);login(server)
    p=server.post('/api/v1/auth/guest-save/preview').json()
    server.post('/api/v1/auth/guest-save/'+p['operation_id']+'/confirm',json={'version':p['version']}).raise_for_status()
    assert confirm(server,turn).status_code==200
    assert len(server.post('/api/v1/auth/account/export').json()['data']['agent_turns'])==1


def habit_fields(**changes):
    start=main.habit_today('Asia/Shanghai')
    return fields(kind='habit',goal='阅读习惯',start_date=start,duration_days=7,weekdays=[0,2,4],completion_standard='阅读10分钟',growth_threshold=3,**changes)


def test_non_daily_habit_uses_original_occurrences_and_growth_rules(server,monkeypatch):
    guest(server);route(monkeypatch,habit_fields());turn,_=query(server)
    pid=confirm(server,turn).json()['id']
    stage=server.get('/api/v1/plants/'+pid+'/habit-stage').json()['stage']
    assert stage['weekdays']==[0,2,4] and stage['completion_standard']=='阅读10分钟'
    with main.connect() as db:
        assert db.execute('SELECT count(*) FROM habit_occurrences').fetchone()[0]==3
        assert db.execute('SELECT threshold FROM growth_standards').fetchone()[0]==3
    assert confirm(server,turn).json()['id']==pid


def test_same_day_habit_and_conflicting_threshold(server,monkeypatch):
    guest(server);today=main.habit_today('Asia/Shanghai')
    v=fields(kind='habit',goal='阅读习惯',start_date=today,duration_days=1,weekdays=[today.weekday()],completion_standard='阅读10分钟',growth_threshold=2)
    route(monkeypatch,v);first,_=query(server)
    assert first['result']['data']['state']=='collecting'
    route(monkeypatch,v.model_copy(update={'growth_threshold':1}));second,_=query(server,session=first['session_id'])
    assert second['result']['data']['state']=='ready'
    assert confirm(server,second).status_code==200
    with main.connect() as db:assert db.execute('SELECT count(*) FROM habit_occurrences').fetchone()[0]==1


def test_suggestions_require_explicit_opt_in_and_remain_a_preview(server,monkeypatch):
    guest(server);route(monkeypatch,fields(kind='habit',duration_days=None,daily_minutes=None))
    first,_=query(server);assert first['result']['data']['state']=='collecting'
    route(monkeypatch,fields(kind='habit',duration_days=None,daily_minutes=None,use_suggestions=True))
    second,_=query(server,'按推荐',session=first['session_id'])
    assert second['result']['data']['state']=='ready'
    assert 'weekdays' in second['result']['data']['suggestions']
    assert server.get('/api/v1/garden').json()['plants']==[]


def test_habit_completion_time_conflict_requires_explicit_change(server,monkeypatch):
    guest(server);v=habit_fields().model_copy(update={'daily_minutes':5});route(monkeypatch,v)
    turn,_=query(server)
    assert turn['result']['data']['state']=='collecting'
    assert '超过可用时间' in turn['result']['text']
    assert server.get('/api/v1/garden').json()['plants']==[]


def test_unknown_time_goal_preserves_unassigned_dates(server,monkeypatch):
    guest(server);route(monkeypatch,fields(duration_days=None,daily_minutes=None,flexible_time=True))
    turn,_=query(server)
    assert turn['result']['data']['goal_dates']==[None]
    pid=confirm(server,turn).json()['id']
    assert server.get('/api/v1/today').json()['goals_unscheduled'][0]['plant_id']==pid


def test_confirmation_waits_for_pending_revision(server,monkeypatch):
    guest(server);route(monkeypatch,fields());turn,_=query(server)
    monkeypatch.setattr(agent,'process_turn',lambda _:None)
    pending,_=query(server,'继续修改草案',session=turn['session_id'])
    assert pending['status']=='pending'
    assert confirm(server,turn).status_code==409
    assert server.get('/api/v1/garden').json()['plants']==[]


from test_cloud_persistence import cloud_store


def test_cloud_checkpoint_restores_draft_and_confirmed_operation(cloud_store,monkeypatch):
    route(monkeypatch,fields())
    with TestClient(main.app) as client:
        guest(client);turn,_=query(client)
        cookie=client.cookies.get(auth.GUEST_COOKIE);csrf=client.headers['X-Bloomate-CSRF']
    with TestClient(main.app) as client:
        client.cookies.set(auth.GUEST_COOKIE,cookie);client.headers.update({'Origin':'http://testserver','X-Bloomate-CSRF':csrf})
        assert client.get('/api/v1/agent/turns/'+turn['id']).json()['result']['data']['state']=='ready'
        result=confirm(client,turn);assert result.status_code==200;pid=result.json()['id']
    with TestClient(main.app) as client:
        client.cookies.set(auth.GUEST_COOKIE,cookie);client.headers.update({'Origin':'http://testserver','X-Bloomate-CSRF':csrf})
        assert confirm(client,turn).json()['id']==pid
        assert len(client.get('/api/v1/garden').json()['plants'])==1


def test_explicit_daily_time_survives_omitted_model_default(server,monkeypatch):
    import httpx
    from test_agent import configure_model
    guest(server);configure_model(monkeypatch)
    # The router output omits daily, but the user's actual sentence states it.
    def response(*args,**kwargs):
        return httpx.Response(200,request=httpx.Request('POST',args[0]),json={'choices':[{'message':{'content':json.dumps({'intent':'plan','fields':{'kind':'goal','goal':'整理提纲','duration_days':3,'daily_minutes':10}})}}]})
    monkeypatch.setattr(httpx,'post',response)
    from app.identity import acting_as
    with main.connect() as db:owner=db.execute('SELECT id FROM auth_subjects').fetchone()[0]
    with acting_as(owner):
        result=agent.route_intent('3天整理提纲，每天最多10分钟',agent.Context(),None,[])
    assert result.fields.daily is True
