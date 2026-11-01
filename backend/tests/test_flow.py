from fastapi.testclient import TestClient
import pytest
from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB', str(tmp_path / 'test.sqlite3'))
    monkeypatch.setenv('BLOOMATE_TEST_MODEL', '1')
    monkeypatch.setenv('MODEL_ENABLED', '0')
    with TestClient(main.app) as c:
        yield c


@pytest.mark.parametrize('kind', ['goal', 'habit'])
def test_plan_requires_confirmation_and_is_idempotent(client, kind):
    result = client.post('/api/v1/plans', json={'kind': kind, 'goal': '每天读书', 'availability': '每天二十分钟'})
    assert result.status_code == 202
    job_id = result.json()['id']
    assert client.get('/api/v1/garden').json()['plants'] == []
    assert client.get(f'/api/v1/plans/{job_id}').json()['status'] == 'done'
    first = client.post(f'/api/v1/proposals/{job_id}/confirm').json()
    second = client.post(f'/api/v1/proposals/{job_id}/confirm').json()
    assert first['id'] == second['id']
    assert len(client.get('/api/v1/garden').json()['plants']) == 1


def test_goal_actions_completion_undo_and_restart(client):
    job = client.post('/api/v1/plans', json={'kind':'goal','goal':'准备分享','availability':'每天十分钟'}).json()['id']
    plant = client.post('/api/v1/proposals/' + job + '/confirm').json()['id']
    url = f'/api/v1/plants/{plant}/actions'
    assert client.get(url).json()['actions'][0]['completed'] is False
    for expected in (True, True, False, False, True):
        response = client.put(url + '/0', json={'completed': expected})
        assert response.status_code == 200 and response.json()['completed'] is expected
    main.init_db()
    assert client.get(url).json()['actions'][0]['completed'] is True
    with main.connect() as db:
        assert db.execute('SELECT count(*) FROM goal_action_states').fetchone()[0] == 1
    assert client.put(url + '/-1',json={'completed':True}).status_code == 404
    assert client.put(url + '/0',json={'completed':'true'}).status_code == 422
    assert client.get('/api/v1/plants/unknown/actions').status_code == 404


def test_habit_and_other_owner_cannot_use_goal_actions(client, monkeypatch):
    job=client.post('/api/v1/plans',json={'kind':'habit','goal':'睡前阅读','availability':'每天十分钟'}).json()['id']
    plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id']
    url=f'/api/v1/plants/{plant}/actions'
    assert client.get(url).status_code == 409
    assert client.put(url+'/0',json={'completed':True}).status_code == 409
    monkeypatch.setattr(main,'OWNER','someone-else')
    assert client.get(url).status_code == 404
    assert client.put(url+'/0',json={'completed':True}).status_code == 404


def test_habit_scheduled_checkins_and_boundaries(client, monkeypatch):
    from datetime import date
    monkeypatch.setattr(main,'habit_today',lambda _: date(2026,11,10))
    job=client.post('/api/v1/plans',json={'kind':'habit','goal':'阅读','availability':'每天十分钟'}).json()['id']
    plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id']
    base=f'/api/v1/plants/{plant}'
    assert client.get(base+'/habit-stage').json()['stage'] is None
    config={'start_date':'2026-11-01','end_date':'2026-11-15','timezone':'Asia/Shanghai','weekdays':[0,1,2,3,4,5,6],'completion_standard':'读满十分钟'}
    assert client.post(base+'/habit-stage',json=config).status_code == 201
    assert client.post(base+'/habit-stage',json=config).status_code == 409
    stage=client.get(base+'/habit-stage').json()
    assert len(stage['occurrences']) == 4 and stage['stage']['today']=='2026-11-10'
    def put(day,completed=True,performed=True):
        return client.put(base+'/habit-occurrences/'+day,json={'completed':completed,'performed':performed})
    assert put('2026-11-10',performed=False).status_code == 422
    assert put('2026-11-11').status_code == 409
    assert put('2026-11-06').status_code == 409
    first=put('2026-11-07'); assert first.status_code == 200 and first.json()['is_backfill']
    assert put('2026-11-07').json()['recorded_at'] == first.json()['recorded_at']
    assert put('2026-11-10').json()['is_backfill'] is False
    assert put('2026-11-07',False,False).json()['completed'] is False
    main.init_db()
    assert client.get(base+'/habit-stage').json()['occurrences'][0]['completed'] is True
    with main.connect() as db:
        assert db.execute('select count(*) from habit_events').fetchone()[0] == 3
    monkeypatch.setattr(main,'OWNER','someone-else')
    assert client.get(base+'/habit-stage').status_code == 404
    assert put('2026-11-10').status_code == 404


def test_habit_non_planned_day_and_invalid_config(client, monkeypatch):
    from datetime import date
    monkeypatch.setattr(main,'habit_today',lambda _: date(2026,11,10))
    job=client.post('/api/v1/plans',json={'kind':'habit','goal':'阅读','availability':'十分钟'}).json()['id']
    plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id']; base=f'/api/v1/plants/{plant}'
    config={'start_date':'2026-11-01','end_date':'2026-11-15','timezone':'Asia/Shanghai','weekdays':[0],'completion_standard':'读十分钟'}
    assert client.post(base+'/habit-stage',json={**config,'timezone':'not/a-zone'}).status_code == 422
    assert client.post(base+'/habit-stage',json={**config,'end_date':'2026-10-31'}).status_code == 422
    assert client.post(base+'/habit-stage',json={**config,'weekdays':[0,0]}).status_code == 422
    assert client.post(base+'/habit-stage',json=config).status_code == 201
    assert client.put(base+'/habit-occurrences/2026-11-10',json={'completed':True,'performed':True}).status_code == 404
    assert client.put(base+'/habit-occurrences/2026-11-09',json={'completed':True,'performed':True}).status_code == 200


def test_habit_today_uses_stage_timezone():
    from zoneinfo import ZoneInfo
    from datetime import datetime
    assert main.habit_today('Asia/Shanghai') == datetime.now(ZoneInfo('Asia/Shanghai')).date()


def test_adjustment_requires_confirmation_and_preserves_other_steps(client):
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'准备分享','availability':'十分钟'}).json()['id']
    proposal=client.get('/api/v1/plans/'+job).json()['proposal']; proposal['steps']=['第一步','第二步']
    plant=client.post('/api/v1/proposals/'+job+'/confirm',json=proposal).json()['id']
    response=client.post(f'/api/v1/plants/{plant}/adjustments',json={'step_index':0,'difficulty':'太难了'});assert response.status_code==202
    aid=response.json()['id']; draft=client.get('/api/v1/adjustments/'+aid).json()
    assert draft['status']=='done' and draft['original_text']=='第一步'
    assert client.get('/api/v1/garden').json()['plants'][0]['plan']['steps']==['第一步','第二步']
    assert client.post('/api/v1/adjustments/'+aid+'/confirm',json={'text':'改小第一步'}).json()['status']=='confirmed'
    assert client.post('/api/v1/adjustments/'+aid+'/confirm',json={'text':'改小第一步'}).status_code==200
    assert client.post('/api/v1/adjustments/'+aid+'/confirm',json={'text':'不同内容'}).status_code==409
    assert client.get('/api/v1/garden').json()['plants'][0]['plan']['steps']==['改小第一步','第二步']
    main.init_db(); assert client.get('/api/v1/adjustments/'+aid).json()['applied_text']=='改小第一步'


def test_adjustment_conflicts_discard_and_completed_step(client, monkeypatch):
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'准备分享','availability':'十分钟'}).json()['id']
    plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id'];base=f'/api/v1/plants/{plant}'
    def draft(): return client.post(base+'/adjustments',json={'step_index':0,'difficulty':'时间太少'}).json()['id']
    rejected=draft(); assert client.post('/api/v1/adjustments/'+rejected+'/discard').status_code==200
    assert client.post('/api/v1/adjustments/'+rejected+'/discard').status_code==200
    assert client.post('/api/v1/adjustments/'+rejected+'/confirm',json={'text':'修改'}).status_code==409
    old=draft();client.put(base+'/actions/0',json={'completed':True})
    assert client.post('/api/v1/adjustments/'+old+'/confirm',json={'text':'修改'}).json()['error']['code']=='PLAN_CHANGED'
    assert client.post(base+'/adjustments',json={'step_index':0,'difficulty':'调整'}).status_code==409
    monkeypatch.setattr(main,'OWNER','someone-else')
    assert client.get('/api/v1/adjustments/'+old).status_code==404


def test_adjustment_retries_multiple_model_steps(client, monkeypatch):
    import json
    monkeypatch.delenv('BLOOMATE_TEST_MODEL'); monkeypatch.setenv('MODEL_ENABLED','1')
    monkeypatch.setenv('MODEL_API_KEY','fake-for-test'); monkeypatch.setenv('MODEL_BASE_URL','https://example.invalid')
    monkeypatch.setenv('MODEL_NAME','deepseek-flash'); monkeypatch.setenv('MODEL_CALL_RESERVE_CENTS','10')
    calls=[]
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'choices':[{'message':{'content':json.dumps({'question':None,'plan':{'title':'调整','summary':'调整一个步骤','steps':['小步']*(6 if len(calls)==1 else 1)}},ensure_ascii=False)}}]}
    def fake_post(*args,**kwargs): calls.append(kwargs['json']['messages'][0]['content']); return Response()
    monkeypatch.setattr(main.httpx,'post',fake_post)
    result=main.generate({'kind':'adjustment','goal':'调整步骤','availability':'不确定','_adjustment_force_plan':True})
    assert result.plan.steps==['小步'] and len(calls)==2 and '最多五条' in calls[1]


def test_split_preserves_finished_steps_and_dates(client):
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'准备分享','availability':'十分钟'}).json()['id'];plan=client.get('/api/v1/plans/'+job).json()['proposal'];plan['steps']=['第一步','第二步','第三步']
    plant=client.post('/api/v1/proposals/'+job+'/confirm',json=plan).json()['id'];base=f'/api/v1/plants/{plant}'
    client.put(base+'/actions/0/date',json={'due_date':'2026-11-05'});client.put(base+'/actions/2/date',json={'due_date':'2026-11-07'});client.put(base+'/actions/2',json={'completed':True})
    aid=client.post(base+'/adjustments',json={'step_index':0,'difficulty':'拆成更小的两步'}).json()['id']
    assert client.post('/api/v1/adjustments/'+aid+'/confirm',json={'steps':['第一小步','第二小步']}).status_code==200
    assert client.post('/api/v1/adjustments/'+aid+'/confirm',json={'steps':['第一小步','第二小步']}).status_code==200
    actions=client.get(base+'/actions').json()['actions']
    assert [a['text'] for a in actions]==['第一小步','第二小步','第二步','第三步']
    assert actions[0]['due_date']=='2026-11-05' and actions[1]['due_date'] is None
    assert actions[3]['completed'] and actions[3]['due_date']=='2026-11-07'
    main.init_db();assert client.get(base+'/actions').json()['actions']==actions


def test_split_rejects_too_many_and_preserves_plan(client):
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'准备分享','availability':'十分钟'}).json()['id'];plan=client.get('/api/v1/plans/'+job).json()['proposal'];plan['steps']=[f'步骤{i}' for i in range(10)]
    plant=client.post('/api/v1/proposals/'+job+'/confirm',json=plan).json()['id'];base=f'/api/v1/plants/{plant}'
    aid=client.post(base+'/adjustments',json={'step_index':0,'difficulty':'拆分'}).json()['id']
    assert client.post('/api/v1/adjustments/'+aid+'/confirm',json={'steps':['小步1','小步2']}).status_code==409
    assert [a['text'] for a in client.get(base+'/actions').json()['actions']]==plan['steps']
    assert client.post('/api/v1/adjustments/'+aid+'/confirm',json={'steps':['  ']}).status_code==422


def test_plan_does_not_invent_previously_confirmed_facts(client):
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'准备分享','availability':'十分钟'}).json()['id']
    plan=client.get('/api/v1/plans/'+job).json()['proposal'];plan['steps']=['从你已确定的分享方向开始']
    assert client.post('/api/v1/proposals/'+job+'/confirm',json=plan).json()['error']['code']=='PLAN_CONSTRAINT'


def test_manual_goal_actions_cancel_restore_and_history(client, monkeypatch):
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'准备分享','availability':'十分钟'}).json()['id'];plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id'];base=f'/api/v1/plants/{plant}'
    draft=client.post(base+'/adjustments',json={'step_index':0,'difficulty':'调整'}).json()['id']
    r=client.post(base+'/actions',json={'text':'手动新增'});assert r.status_code==201 and r.json()['index']==1
    assert client.post('/api/v1/adjustments/'+draft+'/confirm',json={'text':'旧建议'}).status_code==409
    assert client.put(base+'/actions/1/date',json={'due_date':'2026-11-01'}).status_code==200
    assert client.put(base+'/actions/1/text',json={'text':'修改后的步骤'}).json()['text']=='修改后的步骤'
    for _ in range(2):assert client.put(base+'/actions/1/cancel',json={'cancelled':True}).status_code==200
    assert client.put(base+'/actions/1',json={'completed':True}).json()['error']['code']=='CANCELLED'
    assert not any(a['text']=='修改后的步骤' for group in ('goals_today','goals_overdue','goals_unscheduled') for a in client.get('/api/v1/today').json()[group])
    assert client.put(base+'/actions/1/cancel',json={'cancelled':False}).json()['due_date']=='2026-11-01'
    assert client.put(base+'/actions/1',json={'completed':True}).status_code==200
    assert client.put(base+'/actions/1/cancel',json={'cancelled':True}).status_code==409
    assert client.put(base+'/actions/1/text',json={'text':'不可改'}).status_code==409
    with main.connect() as db:assert [r['event'] for r in db.execute('select event from goal_action_events order by rowid')]==['added','edited_before','edited_after','cancelled','restored']
    main.init_db();assert client.get(base+'/actions').json()['actions'][1]['completed']
    monkeypatch.setattr(main,'OWNER','other');assert client.post(base+'/actions',json={'text':'不能访问'}).status_code==404


def test_manual_goal_actions_maximum_and_invalid(client):
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'分享','availability':'十分钟'}).json()['id'];plan=client.get('/api/v1/plans/'+job).json()['proposal'];plan['steps']=[f'步骤{i}' for i in range(10)]
    plant=client.post('/api/v1/proposals/'+job+'/confirm',json=plan).json()['id'];base=f'/api/v1/plants/{plant}'
    assert client.post(base+'/actions',json={'text':'第十一条'}).status_code==409
    assert client.post(base+'/actions',json={'text':'  '}).status_code==422


def test_goal_shares_preserve_earned_progress_on_split_and_expand(client):
    from fractions import Fraction
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'准备分享','availability':'十分钟'}).json()['id'];plan=client.get('/api/v1/plans/'+job).json()['proposal'];plan['steps']=['第一步','第二步','第三步']
    plant=client.post('/api/v1/proposals/'+job+'/confirm',json=plan).json()['id'];base=f'/api/v1/plants/{plant}'
    assert client.get(base+'/growth').json()['progress_percent']==0
    client.put(base+'/actions/2',json={'completed':True});earned=client.get(base+'/growth').json()['progress_percent'];assert earned==33.3
    aid=client.post(base+'/adjustments',json={'step_index':0,'difficulty':'拆小'}).json()['id']
    assert client.post('/api/v1/adjustments/'+aid+'/confirm',json={'steps':['小步一','小步二']}).status_code==200
    assert client.get(base+'/growth').json()['progress_percent']==earned
    assert client.post(base+'/actions',json={'text':'新增步骤'}).status_code==201
    assert client.get(base+'/growth').json()['progress_percent']==earned
    client.put(base+'/actions/0/cancel',json={'cancelled':True});assert client.get(base+'/growth').json()['progress_percent']==earned
    client.put(base+'/actions/0/cancel',json={'cancelled':False});assert client.get(base+'/growth').json()['progress_percent']==earned
    with main.connect() as db:
        weights=[Fraction(int(r['numerator']),int(r['denominator'])) for r in db.execute('select * from goal_action_weights where plant_id=?',(plant,))]
        assert sum(weights,Fraction())==1
    client.put(base+'/actions/0',json={'completed':True});assert client.get(base+'/growth').json()['progress_percent']>earned
    client.put(base+'/actions/0',json={'completed':False});assert client.get(base+'/growth').json()['progress_percent']==earned
    main.init_db();assert client.get(base+'/growth').json()['progress_percent']==earned


def test_goal_shares_all_cancelled_then_restored(client):
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'分享','availability':'十分钟'}).json()['id'];plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id'];base=f'/api/v1/plants/{plant}'
    client.put(base+'/actions/0/cancel',json={'cancelled':True});assert client.get(base+'/growth').json()['progress_percent']==0
    client.put(base+'/actions/0/cancel',json={'cancelled':False});client.put(base+'/actions/0',json={'completed':True});assert client.get(base+'/growth').json()['progress_percent']==100


def test_expired_habit_awaits_review_without_auto_achievement(client, monkeypatch):
    from datetime import date
    clock=[date(2026,11,1)];monkeypatch.setattr(main,'habit_today',lambda _:clock[0])
    job=client.post('/api/v1/plans',json={'kind':'habit','goal':'阅读','availability':'十分钟'}).json()['id'];plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id'];base=f'/api/v1/plants/{plant}'
    config={'start_date':'2026-11-02','end_date':'2026-11-05','timezone':'Asia/Shanghai','weekdays':list(range(7)),'completion_standard':'读满十分钟'}
    assert client.post(base+'/habit-stage',json=config).status_code==201
    assert client.get(base+'/habit-stage').json()['stage']['status']=='upcoming'
    clock[0]=date(2026,11,3);assert client.get(base+'/habit-stage').json()['stage']['status']=='active'
    client.put(base+'/growth/standard',json={'threshold':1})
    client.put(base+'/habit-occurrences/2026-11-03',json={'completed':True,'performed':True})
    client.put(base+'/pause',json={'paused':True});pause=client.get(base+'/pause').json()
    assert pause['threshold']==1 and pause['completed']==1 and pause['future_dates']==['2026-11-04','2026-11-05']
    clock[0]=date(2026,11,6);assert client.get(base+'/habit-stage').json()['stage']['status']=='review_due'
    assert client.get(base+'/growth').json()['achieved'] is False


def test_schedule_preview_includes_standard_without_changing_it(client, monkeypatch):
    from datetime import date
    monkeypatch.setattr(main,'habit_today',lambda _:date(2026,11,10))
    job=client.post('/api/v1/plans',json={'kind':'habit','goal':'阅读','availability':'十分钟'}).json()['id'];plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id'];base=f'/api/v1/plants/{plant}'
    client.post(base+'/habit-stage',json={'start_date':'2026-11-01','end_date':'2026-11-20','timezone':'Asia/Shanghai','weekdays':list(range(7)),'completion_standard':'读十分钟'})
    client.put(base+'/growth/standard',json={'threshold':5});client.put(base+'/habit-occurrences/2026-11-10',json={'completed':True,'performed':True})
    proposal=client.post(base+'/habit-schedule-proposals',json={'end_date':'2026-11-18','weekdays':[0,2,4]}).json()
    assert proposal['after']['threshold']==5 and proposal['after']['completed']==1
    assert client.get(base+'/growth').json()['threshold']==5


def test_pause_and_resume_preserves_history_and_suppresses_pause_days(client, monkeypatch):
    from datetime import date
    clock=[date(2026,11,10)];monkeypatch.setattr(main,'habit_today',lambda _:clock[0])
    job=client.post('/api/v1/plans',json={'kind':'habit','goal':'每天阅读','availability':'10分钟'}).json()['id'];plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id'];base=f'/api/v1/plants/{plant}'
    config={'start_date':'2026-11-01','end_date':'2026-11-30','timezone':'Asia/Shanghai','weekdays':list(range(7)),'completion_standard':'读满十分钟'}
    assert client.post(base+'/habit-stage',json=config).status_code==201
    assert client.put(base+'/habit-occurrences/2026-11-09',json={'completed':True,'performed':True}).status_code==200
    assert client.put(base+'/pause',json={'paused':True}).json()['paused'] is True
    assert client.put(base+'/pause',json={'paused':True}).status_code==200
    assert client.put(base+'/habit-occurrences/2026-11-10',json={'completed':True,'performed':True}).json()['error']['code']=='PAUSED'
    clock[0]=date(2026,11,11)
    assert client.put(base+'/pause',json={'paused':False}).json()['end_date']=='2026-11-30'
    assert client.put(base+'/pause',json={'paused':False}).json()['paused'] is False
    assert client.put(base+'/habit-occurrences/2026-11-11',json={'completed':True,'performed':True}).status_code==409
    stage=client.get(base+'/habit-stage').json();assert any(x['date']=='2026-11-09' and x['completed'] for x in stage['occurrences'])
    clock[0]=date(2026,11,12);assert client.put(base+'/habit-occurrences/2026-11-12',json={'completed':True,'performed':True}).status_code==200
    main.init_db();assert client.get(base+'/pause').json()['paused'] is False
    with main.connect() as db:assert db.execute('select count(*) from plant_pauses').fetchone()[0]==1


def test_habit_schedule_preview_confirm_future_only_and_conflict(client, monkeypatch):
    from datetime import date
    monkeypatch.setattr(main,'habit_today',lambda _:date(2026,11,10))
    job=client.post('/api/v1/plans',json={'kind':'habit','goal':'阅读','availability':'10分钟'}).json()['id'];plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id'];base=f'/api/v1/plants/{plant}'
    config={'start_date':'2026-11-01','end_date':'2026-11-20','timezone':'Asia/Shanghai','weekdays':list(range(7)),'completion_standard':'读满十分钟'}
    client.post(base+'/habit-stage',json=config)
    client.put(base+'/habit-occurrences/2026-11-09',json={'completed':True,'performed':True})
    old=client.get(base+'/habit-stage').json()
    response=client.post(base+'/habit-schedule-proposals',json={'end_date':'2026-11-18','weekdays':[0,2,4]});assert response.status_code==201
    pid=response.json()['id'];assert response.json()['after']['effective_date']=='2026-11-11';assert client.get(base+'/habit-stage').json()==old
    assert client.post('/api/v1/habit-schedule-proposals/'+pid+'/confirm').status_code==200
    assert client.post('/api/v1/habit-schedule-proposals/'+pid+'/confirm').status_code==200
    stage=client.get(base+'/habit-stage').json();assert stage['stage']['end_date']=='2026-11-18' and any(x['date']=='2026-11-09' and x['completed'] for x in stage['occurrences'])
    with main.connect() as db:
        rows={r['action_date']:r['enabled'] for r in db.execute('select action_date,enabled from habit_occurrences')}
        assert rows['2026-11-10']==1 and rows['2026-11-11']==1 and rows['2026-11-12']==0 and rows['2026-11-19']==0
    stale=client.post(base+'/habit-schedule-proposals',json={'end_date':'2026-11-19','weekdays':[0]}).json()['id']
    client.put(base+'/habit-occurrences/2026-11-10',json={'completed':True,'performed':True})
    assert client.post('/api/v1/habit-schedule-proposals/'+stale+'/confirm').json()['error']['code']=='PLAN_CHANGED'
    assert client.post('/api/v1/habit-schedule-proposals/'+stale+'/discard').status_code==200
    main.init_db();assert client.get(base+'/habit-stage').json()['stage']['end_date']=='2026-11-18'


def test_goal_growth_requires_standard_and_confirmation_and_reverses(client, monkeypatch):
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'准备分享','availability':'十分钟'}).json()['id']
    plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id'];base=f'/api/v1/plants/{plant}'
    assert client.get(base+'/growth').json()['threshold'] is None
    assert client.post(base+'/growth/confirm').status_code==409
    suggestion=client.post(base+'/growth/suggestions').json()['id'];assert client.get('/api/v1/growth/suggestions/'+suggestion).json()['status']=='done'
    assert client.get(base+'/growth').json()['threshold'] is None
    assert client.put(base+'/growth/standard',json={'threshold':2}).status_code==422
    assert client.put(base+'/growth/standard',json={'threshold':1}).status_code==200
    assert client.post(base+'/growth/confirm').status_code==409
    client.put(base+'/actions/0',json={'completed':True})
    assert client.get(base+'/growth').json()['eligible'] and not client.get(base+'/growth').json()['achieved']
    assert client.post(base+'/growth/confirm').json()['achieved'] is True
    assert client.post(base+'/growth/confirm').status_code==200
    client.put(base+'/pause',json={'paused':True});assert client.get(base+'/growth').json()['achieved'] is True
    client.put(base+'/actions/0',json={'completed':False});assert client.get(base+'/growth').json()['achieved'] is False
    with main.connect() as db: assert [r['event'] for r in db.execute('select event from growth_events order by rowid')]==['standard_confirmed','achieved','revoked']
    main.init_db();assert not client.get(base+'/growth').json()['achieved']
    monkeypatch.setattr(main,'OWNER','another');assert client.get(base+'/growth').status_code==404


def test_habit_growth_counts_checkins_and_undo(client, monkeypatch):
    from datetime import date
    monkeypatch.setattr(main,'habit_today',lambda _:date(2026,11,10))
    job=client.post('/api/v1/plans',json={'kind':'habit','goal':'阅读','availability':'十分钟'}).json()['id'];plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id'];base=f'/api/v1/plants/{plant}'
    assert client.put(base+'/growth/standard',json={'threshold':1}).status_code==409
    config={'start_date':'2026-11-01','end_date':'2026-11-20','timezone':'Asia/Shanghai','weekdays':list(range(7)),'completion_standard':'阅读十分钟'}
    assert client.post(base+'/habit-stage',json=config).status_code==201
    assert client.put(base+'/growth/standard',json={'threshold':1}).status_code==200
    client.put(base+'/habit-occurrences/2026-11-10',json={'completed':True,'performed':True})
    assert client.post(base+'/growth/confirm').status_code==200
    client.put(base+'/habit-occurrences/2026-11-10',json={'completed':False})
    assert client.get(base+'/growth').json()['achieved'] is False


def test_today_explicit_dates_and_adjustment_conflict(client, monkeypatch):
    from datetime import date
    monkeypatch.setattr(main,'habit_today',lambda _:date(2026,11,10))
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'准备分享','availability':'十分钟'}).json()['id'];plan=client.get('/api/v1/plans/'+job).json()['proposal'];plan['steps']=['今天步骤','逾期步骤','未安排步骤']
    plant=client.post('/api/v1/proposals/'+job+'/confirm',json=plan).json()['id'];base=f'/api/v1/plants/{plant}'
    data=client.get('/api/v1/today').json();assert len(data['goals_unscheduled'])==3 and not data['goals_today']
    draft=client.post(base+'/adjustments',json={'step_index':0,'difficulty':'时间不够'}).json()['id']
    assert client.put(base+'/actions/0/date',json={'due_date':'2026-11-10'}).status_code==200
    assert client.post('/api/v1/adjustments/'+draft+'/confirm',json={'text':'修改'}).json()['error']['code']=='PLAN_CHANGED'
    assert client.put(base+'/actions/1/date',json={'due_date':'2026-11-09'}).status_code==200
    today=client.get('/api/v1/today').json();assert len(today['goals_today'])==1 and len(today['goals_overdue'])==1 and len(today['goals_unscheduled'])==1
    client.put(base+'/actions/1',json={'completed':True});assert not client.get('/api/v1/today').json()['goals_overdue']
    assert client.get(base+'/actions').json()['actions'][1]['due_date']=='2026-11-09'
    client.put(base+'/actions/1/date',json={'due_date':None});assert client.get(base+'/actions').json()['actions'][1]['completed']
    client.put(base+'/pause',json={'paused':True});today=client.get('/api/v1/today').json();assert not today['goals_today'] and not today['goals_unscheduled']
    main.init_db();assert client.get(base+'/actions').json()['actions'][0]['due_date']=='2026-11-10'


def test_today_habit_uses_its_timezone_and_pause(client, monkeypatch):
    from datetime import date
    monkeypatch.setattr(main,'habit_today',lambda tz:date(2026,11,9) if tz=='America/New_York' else date(2026,11,10))
    job=client.post('/api/v1/plans',json={'kind':'habit','goal':'阅读','availability':'十分钟'}).json()['id'];plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id'];base=f'/api/v1/plants/{plant}'
    client.post(base+'/habit-stage',json={'start_date':'2026-11-01','end_date':'2026-11-30','timezone':'America/New_York','weekdays':list(range(7)),'completion_standard':'读10分钟'})
    assert client.get('/api/v1/today').json()['habits_today'][0]['date']=='2026-11-09'
    client.put(base+'/pause',json={'paused':True});assert not client.get('/api/v1/today').json()['habits_today']


def test_habit_advice_is_persistent_and_never_changes_plan(client, monkeypatch):
    job=client.post('/api/v1/plans',json={'kind':'habit','goal':'阅读','availability':'十分钟'}).json()['id'];plant=client.post('/api/v1/proposals/'+job+'/confirm').json()['id'];base=f'/api/v1/plants/{plant}'
    assert client.post(base+'/habit-advice',json={'difficulty':'时间不够'}).json()['error']['code']=='NO_STAGE'
    config={'start_date':'2026-11-01','end_date':'2026-11-30','timezone':'Asia/Shanghai','weekdays':list(range(7)),'completion_standard':'读满十分钟'}
    client.post(base+'/habit-stage',json=config);before=client.get(base+'/habit-stage').json()
    response=client.post(base+'/habit-advice',json={'difficulty':'时间不够'});assert response.status_code==202
    aid=response.json()['id'];assert client.get('/api/v1/habit-advice/'+aid).json()['status']=='done'
    assert client.get(base+'/habit-stage').json()==before
    main.init_db();assert client.get('/api/v1/habit-advice/'+aid).json()['status']=='done'
    monkeypatch.setattr(main,'OWNER','another');assert client.get('/api/v1/habit-advice/'+aid).status_code==404


def test_validation_and_missing(client):
    assert client.post('/api/v1/plans', json={'kind': 'other', 'goal': 'x', 'availability': ''}).json()['error']['code'] == 'INVALID_INPUT'
    assert client.post('/api/v1/proposals/missing/confirm').json()['error']['code'] == 'NOT_FOUND'


def test_no_model_returns_explicit_error(client, monkeypatch):
    monkeypatch.delenv('BLOOMATE_TEST_MODEL')
    result = client.post('/api/v1/plans', json={'kind': 'goal', 'goal': '完成面试', 'availability': '每天半小时'})
    job = client.get('/api/v1/plans/' + result.json()['id']).json()
    assert job['status'] == 'error' and job['error']['code'] == 'MODEL_NOT_CONFIGURED'
    assert client.get('/api/v1/garden').json()['plants'] == []


def test_interrupted_job_recovered_as_error(client):
    with main.connect() as db:
        db.execute("INSERT INTO jobs (id,owner_id,status,input_json) VALUES ('old',?,'running','{}')", (main.OWNER,))
    main.init_db()
    assert client.get('/api/v1/plans/old').json()['error']['code'] == 'INTERRUPTED'


def test_confirmation_saves_user_edits(client):
    job = client.post('/api/v1/plans', json={'kind': 'goal', 'goal': '准备面试', 'availability': '每天半小时'}).json()['id']
    edited = {'title': '我的面试计划', 'summary': '先准备基础', 'steps': ['梳理经历']}
    response = client.post(f'/api/v1/proposals/{job}/confirm', json=edited)
    assert response.status_code == 200
    assert client.get('/api/v1/garden').json()['plants'][0]['plan'] == edited


def test_followup_preserves_context_and_prevents_old_confirmation(client):
    first = client.post('/api/v1/plans', json={'kind': 'goal', 'goal': '需要澄清的目标', 'availability': '每天二十分钟'}).json()['id']
    assert client.get(f'/api/v1/plans/{first}').json()['question']
    assert client.post(f'/api/v1/proposals/{first}/confirm').status_code == 409
    second = client.post(f'/api/v1/plans/{first}/reply', json={'answer': '一个月内'}).json()['id']
    assert client.post(f'/api/v1/plans/{first}/reply', json={'answer': '重复'}).status_code == 409
    assert client.post(f'/api/v1/plans/{first}/reply', json={'answer': '一个月内'}).json()['id'] == second
    assert client.get(f'/api/v1/plans/{first}').json()['next_job_id'] == second
    assert client.get(f'/api/v1/plans/{second}').json()['proposal']['title'] == '需要澄清的目标'
    with main.connect() as db:
        row = db.execute('SELECT input_json FROM jobs WHERE id=?', (second,)).fetchone()
    assert '一个月内' in row['input_json']


def test_budget_reservation_is_persistent_and_enforced(client, monkeypatch):
    monkeypatch.setenv('MODEL_CALL_RESERVE_CENTS', '3000')
    main.reserve_budget()
    with pytest.raises(RuntimeError, match='BUDGET_EXHAUSTED'):
        main.reserve_budget()
    with main.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM usage_reservations').fetchone()[0] == 1


def test_model_call_records_usage_without_user_content(client, monkeypatch):
    monkeypatch.delenv('BLOOMATE_TEST_MODEL')
    monkeypatch.setenv('MODEL_ENABLED', '1')
    monkeypatch.setenv('MODEL_API_KEY', 'fake-for-test')
    monkeypatch.setenv('MODEL_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('MODEL_NAME', 'deepseek-flash')
    monkeypatch.setenv('MODEL_CALL_RESERVE_CENTS', '10')
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'usage': {'prompt_tokens': 123, 'completion_tokens': 45}, 'choices': [{'message': {'content': '{"question":null,"plan":{"title":"计划","summary":"说明","steps":["一步"]}}'}}]}
    monkeypatch.setattr(main.httpx, 'post', lambda *args, **kwargs: Response())
    result = main.generate({'kind':'goal', 'goal':'不应记入调用日志', 'availability':'十分钟'})
    assert result.plan.title == '计划'
    with main.connect() as db:
        row = db.execute('SELECT * FROM model_call_metrics').fetchone()
    assert row['prompt_tokens'] == 123 and row['completion_tokens'] == 45
    assert '不应记入调用日志' not in str(dict(row))


def test_overlong_model_plan_retry_explains_required_limit(client, monkeypatch):
    import json
    monkeypatch.delenv('BLOOMATE_TEST_MODEL')
    monkeypatch.setenv('MODEL_ENABLED', '1')
    monkeypatch.setenv('MODEL_API_KEY', 'fake-for-test')
    monkeypatch.setenv('MODEL_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('MODEL_NAME', 'deepseek-flash')
    monkeypatch.setenv('MODEL_CALL_RESERVE_CENTS', '10')
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self):
            steps = ['阅读'] * (11 if len(calls) == 1 else 4)
            return {'choices':[{'message':{'content':json.dumps({'question':None,'plan':{'title':'阅读','summary':'按周安排','steps':steps}},ensure_ascii=False)}}]}
    def fake_post(*args, **kwargs):
        calls.append(kwargs['json']['messages'][0]['content'])
        return Response()
    monkeypatch.setattr(main.httpx, 'post', fake_post)
    assert len(main.generate({'kind':'habit','goal':'阅读30天','availability':'每天20分钟'}).plan.steps) == 4
    assert len(calls) == 2 and 'steps 必须为1至10条' in calls[1]


def test_reads_selected_local_credential_file(client, monkeypatch, tmp_path):
    monkeypatch.delenv('BLOOMATE_TEST_MODEL')
    monkeypatch.delenv('MODEL_API_KEY', raising=False)
    monkeypatch.setenv('MODEL_ENABLED', '1')
    monkeypatch.setenv('MODEL_CREDENTIAL_FILE', '.env.deepseek')
    monkeypatch.setenv('MODEL_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('MODEL_NAME', 'deepseek-flash')
    monkeypatch.setenv('MODEL_CALL_RESERVE_CENTS', '10')
    fake_app = tmp_path / 'backend' / 'app' / 'main.py'
    fake_app.parent.mkdir(parents=True)
    (tmp_path / 'backend' / '.env.deepseek').write_text('MODEL_API_KEY=fake-for-test\n')
    (fake_app.parent / 'prompt.txt').write_text('JSON')
    monkeypatch.setattr(main, '__file__', str(fake_app))
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'usage': {}, 'choices': [{'message': {'content': '{"question":"还有什么目标？","plan":null}'}}]}
    def fake_post(*args, **kwargs):
        assert kwargs['headers']['Authorization'] == 'Bearer fake-for-test'
        return Response()
    monkeypatch.setattr(main.httpx, 'post', fake_post)
    assert main.generate({'goal':'示例'}).question == '还有什么目标？'


def test_numeric_constraints_are_preserved_and_checked_on_confirm(client):
    data = {'kind':'habit', 'goal':'睡前阅读', 'availability':'每天十分钟', 'session_budget_minutes':10, 'habit_completion_minutes':10}
    job = client.post('/api/v1/plans', json=data).json()['id']
    result = client.get('/api/v1/plans/' + job).json()
    assert result['constraint_check'] == {'budget_checked':True, 'completion_checked':True, 'session_total_minutes':10}
    proposal = result['proposal']
    proposal['session_actions'][0]['minutes'] = 7
    result = client.post('/api/v1/proposals/' + job + '/confirm', json=proposal)
    assert result.status_code == 409 and result.json()['error']['code'] == 'PLAN_CONSTRAINT'
    assert client.get('/api/v1/garden').json()['plants'] == []


def test_rejects_conflicting_completion_claim_and_overtime(client):
    data = {'kind':'habit','goal':'阅读习惯','availability':'每天十分钟','session_budget_minutes':10,'habit_completion_minutes':10}
    job = client.post('/api/v1/plans', json=data).json()['id']
    proposal = client.get('/api/v1/plans/' + job).json()['proposal']
    proposal['steps'] = ['读7分钟即算本次达标']
    assert client.post('/api/v1/proposals/' + job + '/confirm', json=proposal).status_code == 409
    proposal['steps'] = ['阅读10分钟']
    proposal['session_actions'].append({'text':'准备','kind':'other','minutes':2})
    assert client.post('/api/v1/proposals/' + job + '/confirm', json=proposal).status_code == 409
    assert client.get('/api/v1/garden').json()['plants'] == []


def test_unknown_constraints_do_not_claim_automatic_validation(client):
    job = client.post('/api/v1/plans', json={'kind':'goal','goal':'准备面试','availability':'不确定'}).json()['id']
    result = client.get('/api/v1/plans/' + job).json()
    assert result['constraint_check']['budget_checked'] is False
    assert result['constraint_check']['completion_checked'] is False


def test_incompatible_input_rejected_without_model_call(client):
    data = {'kind':'habit','goal':'阅读习惯','availability':'每天10分钟','session_budget_minutes':10,'habit_completion_minutes':15}
    assert client.post('/api/v1/plans', json=data).status_code == 422


def test_action_text_conflicting_with_completion_is_rejected(client):
    data = {'kind':'habit','goal':'阅读习惯','availability':'每天十分钟','session_budget_minutes':10,'habit_completion_minutes':10}
    job = client.post('/api/v1/plans', json=data).json()['id']
    proposal = client.get('/api/v1/plans/' + job).json()['proposal']
    proposal['session_actions'][0]['text'] = '读七分钟即可完成'
    assert client.post('/api/v1/proposals/' + job + '/confirm', json=proposal).status_code == 409
    assert client.get('/api/v1/garden').json()['plants'] == []


def test_recording_contradiction_is_rejected_on_confirmation(client):
    job = client.post('/api/v1/plans', json={'kind':'goal','goal':'工作分享','availability':'每天五分钟','session_budget_minutes':5}).json()['id']
    proposal = client.get('/api/v1/plans/' + job).json()['proposal']
    proposal['summary'] = '不另行安排记录、复盘或额外整理。'
    proposal['steps'] = ['完整过一遍，并记下需调整的地方。']
    assert client.post('/api/v1/proposals/' + job + '/confirm', json=proposal).status_code == 409
    proposal['steps'] = ['完整过一遍，只记下需要改的一处。']
    assert client.post('/api/v1/proposals/' + job + '/confirm', json=proposal).status_code == 409
    assert client.get('/api/v1/garden').json()['plants'] == []


def test_blank_edited_step_is_rejected(client):
    job=client.post('/api/v1/plans',json={'kind':'goal','goal':'准备面试','availability':'十分钟'}).json()['id']
    plan=client.get('/api/v1/plans/' + job).json()['proposal']
    plan['steps']=['   ']
    assert client.post('/api/v1/proposals/' + job + '/confirm',json=plan).status_code == 422


def test_weekly_goal_with_daily_time_needs_all_seven_days():
    import pytest
    from app.constraints import ConstraintError, validate_plan
    source = {'kind':'goal', 'goal':'一周完成简历制作', 'availability':'一天一小时', 'session_budget_minutes':60}
    plan = main.Plan(title='简历', summary='一周完成', steps=['第1天：完成初稿'], session_actions=[{'text':'写初稿','kind':'core','minutes':60}])
    with pytest.raises(ConstraintError, match='第1天到第7天'):
        validate_plan(plan, source)
    plan.steps = [f'第{day}天：推进简历' for day in range(1, 8)]
    assert validate_plan(plan, source)['session_total_minutes'] == 60


def test_followup_chain_survives_restart_and_retry(client):
    first=client.post('/api/v1/plans',json={'kind':'goal','goal':'需要澄清的目标','availability':'十分钟'}).json()['id']
    next_id=client.post('/api/v1/plans/'+first+'/reply',json={'answer':'一个月'}).json()['id']
    main.init_db()
    old=client.get('/api/v1/plans/'+first).json()
    assert old['status']=='superseded' and old['next_job_id']==next_id
    assert client.post('/api/v1/plans/'+first+'/reply',json={'answer':'一个月'}).json()['id']==next_id
    assert client.get('/api/v1/plans/'+next_id).json()['status']=='done'


def test_old_jobs_are_readable_after_additive_migration(tmp_path, monkeypatch):
    import sqlite3
    db_path=tmp_path/'old.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, status TEXT NOT NULL, input_json TEXT NOT NULL, proposal_json TEXT, error_code TEXT)')
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',('old','local-user','done','{}','{"question":"旧问题","plan":null}',None))
    monkeypatch.setattr(main,'DB',str(db_path))
    with TestClient(main.app) as client:
        result=client.get('/api/v1/plans/old')
        assert result.status_code==200 and result.json()['question']=='旧问题'
        assert result.json()['next_job_id'] is None


def test_rejects_explicit_out_of_budget_work(client):
    data = {'kind':'habit','goal':'阅读习惯','availability':'每天十分钟','session_budget_minutes':10,'habit_completion_minutes':10}
    job = client.post('/api/v1/plans', json=data).json()['id']
    proposal = client.get('/api/v1/plans/' + job).json()['proposal']
    proposal['summary'] = '准备工作请在白天做，不占用这10分钟。'
    result=client.post('/api/v1/proposals/' + job + '/confirm',json=proposal)
    assert result.status_code==409 and result.json()['error']['code']=='PLAN_CONSTRAINT'
    assert client.get('/api/v1/garden').json()['plants']==[]


def test_rejects_disguised_time_outside_budget(client):
    data={'kind':'habit','goal':'阅读习惯','availability':'每天十分钟','session_budget_minutes':10,'habit_completion_minutes':10}
    job=client.post('/api/v1/plans',json=data).json()['id']
    proposal=client.get('/api/v1/plans/'+job).json()['proposal']
    proposal['summary']='准备和记录不另占时间，可在开始前放好书、读完后记一笔。'
    assert client.post('/api/v1/proposals/'+job+'/confirm',json=proposal).status_code == 409


def test_model_cannot_ask_more_than_two_followups(client, monkeypatch):
    monkeypatch.delenv('BLOOMATE_TEST_MODEL')
    monkeypatch.setenv('MODEL_ENABLED','1')
    monkeypatch.setenv('MODEL_API_KEY','fake-for-test')
    monkeypatch.setenv('MODEL_BASE_URL','https://example.invalid')
    monkeypatch.setenv('MODEL_NAME','deepseek-flash')
    monkeypatch.setenv('MODEL_CALL_RESERVE_CENTS','10')
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'usage':{},'choices':[{'message':{'content':'{"question":"再问一次？","plan":null}'}}]}
    monkeypatch.setattr(main.httpx,'post',lambda *args,**kwargs: Response())
    history=[{'question':'问题一','answer':'未知'},{'question':'问题二','answer':'未知'}]
    with pytest.raises(RuntimeError,match='MODEL_FAILED'):
        main.generate({'kind':'goal','goal':'准备演讲','availability':'15分钟','history':history})


def test_similar_repeat_question_is_detected_and_not_presented():
    from app.constraints import repeats_question
    history=[{'question':'两天内你希望作品集达到什么最低可交付标准，例如“完成一页项目提纲草稿”或“确定首页结构”？','answer':'先做一页'}]
    repeated='两天内作品集的最低可交付标准，你更愿意定为“完成一页项目提纲草稿”还是“确定首页结构”？'
    assert repeats_question(repeated,history)
    assert not repeats_question('你目前每次可以投入多少分钟？',history)


def test_repeated_question_triggers_one_retry_for_plan(client, monkeypatch):
    import json
    monkeypatch.delenv('BLOOMATE_TEST_MODEL')
    monkeypatch.setenv('MODEL_ENABLED','1')
    monkeypatch.setenv('MODEL_API_KEY','fake-for-test')
    monkeypatch.setenv('MODEL_BASE_URL','https://example.invalid')
    monkeypatch.setenv('MODEL_NAME','deepseek-flash')
    monkeypatch.setenv('MODEL_CALL_RESERVE_CENTS','10')
    outputs=[{'question':'两天内作品集的最低可交付标准，你更愿意定为“完成一页项目提纲草稿”还是“确定首页结构”？','plan':None}, {'question':None,'plan':{'title':'提纲','summary':'以已知信息制定草案','steps':['先写提纲']}}]
    class Response:
        def __init__(self, value): self.value=value
        def raise_for_status(self): pass
        def json(self): return {'usage':{},'choices':[{'message':{'content':json.dumps(self.value,ensure_ascii=False)}}]}
    calls=[]
    def fake_post(*args,**kwargs):
        calls.append(kwargs['json']['messages'][0]['content'])
        return Response(outputs.pop(0))
    monkeypatch.setattr(main.httpx,'post',fake_post)
    history=[{'question':'两天内你希望作品集达到什么最低可交付标准，例如“完成一页项目提纲草稿”或“确定首页结构”？','answer':'先做一页'}]
    turn=main.generate({'kind':'goal','goal':'作品集','availability':'每天10分钟','history':history})
    assert turn.plan.title=='提纲' and len(calls)==2
    assert '刚才的追问与已问问题过于相似' in calls[1]


def test_rejects_four_weeks_claimed_as_thirty_days(client):
    data={'kind':'habit','goal':'阅读30天','availability':'每天10分钟','session_budget_minutes':10,'habit_completion_minutes':10}
    job=client.post('/api/v1/plans',json=data).json()['id']
    proposal=client.get('/api/v1/plans/'+job).json()['proposal']
    proposal['steps']=['第4周：完成最后7天，凑满30天。']
    assert client.post('/api/v1/proposals/'+job+'/confirm',json=proposal).status_code == 409
    proposal['steps']=['第1至4周每周7天，最后2天继续阅读。']
    assert client.post('/api/v1/proposals/'+job+'/confirm',json=proposal).status_code == 200


def test_rephrased_repeat_is_detected_without_blocking_different_question():
    from app.constraints import repeats_question
    history=[{'question':'两天内你希望每天这10分钟具体完成什么：只做提纲，还是每天各完成一页？','answer':'不确定'}]
    assert repeats_question('两天内，这每天10分钟你希望优先完成哪一件事：只做一页作品集提纲，还是每天各推进一页作品集内容？',history)
    assert not repeats_question('你每天这10分钟适合早上还是晚上？',[{'question':'这10分钟你希望做几项任务？','answer':'一项'}])


def test_core_action_cannot_hide_explicit_preparation_time(client):
    data={'kind':'habit','goal':'轻度运动','availability':'每次20分钟','session_budget_minutes':20,'habit_completion_minutes':15}
    job=client.post('/api/v1/plans',json=data).json()['id']
    proposal=client.get('/api/v1/plans/'+job).json()['proposal']
    proposal['session_actions']=[{'text':'用5分钟做开始前准备和结束后整理，核心运动至少15分钟','kind':'core','minutes':15},{'text':'准备整理','kind':'other','minutes':5}]
    assert client.post('/api/v1/proposals/'+job+'/confirm',json=proposal).status_code == 409


def test_core_action_allows_explicit_no_extra_preparation(client):
    data={'kind':'habit','goal':'睡前阅读','availability':'十分钟','session_budget_minutes':10,'habit_completion_minutes':10}
    job=client.post('/api/v1/plans',json=data).json()['id']
    proposal=client.get('/api/v1/plans/'+job).json()['proposal']
    proposal['session_actions'][0]['text']='阅读10分钟，不额外安排准备。'
    assert client.post('/api/v1/proposals/'+job+'/confirm',json=proposal).status_code == 200


def test_rejects_unconfirmed_claim_about_user_intent(client):
    data={'kind':'goal','goal':'两天制作求职作品集','background':'可以先写提纲','availability':'每天十分钟','session_budget_minutes':10}
    job=client.post('/api/v1/plans',json=data).json()['id']
    proposal=client.get('/api/v1/plans/'+job).json()['proposal']
    proposal['summary']='你并非要立刻完成整份作品集。'
    assert client.post('/api/v1/proposals/'+job+'/confirm',json=proposal).status_code == 409


def test_model_cannot_repeat_after_user_says_uncertain(client, monkeypatch):
    monkeypatch.delenv('BLOOMATE_TEST_MODEL')
    monkeypatch.setenv('MODEL_ENABLED','1')
    monkeypatch.setenv('MODEL_API_KEY','fake-for-test')
    monkeypatch.setenv('MODEL_BASE_URL','https://example.invalid')
    monkeypatch.setenv('MODEL_NAME','deepseek-flash')
    monkeypatch.setenv('MODEL_CALL_RESERVE_CENTS','10')
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'usage':{},'choices':[{'message':{'content':'{"question":"再问一次？","plan":null}'}}]}
    monkeypatch.setattr(main.httpx,'post',lambda *args,**kwargs: Response())
    with pytest.raises(RuntimeError,match='MODEL_FAILED'):
        main.generate({'kind':'goal','goal':'准备演讲','availability':'15分钟','history':[{'question':'主题？','answer':'暂不确定'}]})


def test_does_not_reask_explicit_goal_duration():
    from app.constraints import asks_known_duration
    assert asks_known_duration('分享的总时长和书目是否已确定？',{'goal':'准备3分钟英文分享'})
    assert not asks_known_duration('你希望哪天完成？',{'goal':'准备3分钟英文分享'})
    assert not asks_known_duration('分享的总时长是否确定？',{'goal':'准备英文分享'})
