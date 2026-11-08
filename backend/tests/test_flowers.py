"""Real HTTP contracts on disposable stores; model and email here are fixtures."""
import json
from datetime import date, timedelta

import httpx
from fastapi.testclient import TestClient

from app import auth, flowers, main
from test_auth import server, guest, login, plant
from test_account_privacy import challenge, delete
from test_cloud_persistence import cloud_store


def catalog(client):
    response = client.get('/api/v1/flowers')
    assert response.status_code == 200, response.text
    return response.json()


def owned(client):
    return {f['species'] for f in catalog(client)['flowers'] if f['unlocked']}


def account(client, email='flower@example.com'):
    guest(client)
    login(client, email)
    return auth.session_for(client.cookies.get(auth.ACCOUNT_COOKIE))['owner_id']


def finish_goal(client, *, finish=True):
    job, pid = plant(client)
    assert client.put(f'/api/v1/plants/{pid}/actions/0', json={'completed': True}).status_code == 200
    if finish:
        assert client.post(f'/api/v1/plants/{pid}/completion-notice', json={'choice': 'finish'}).status_code == 200
    return job, pid


def habit(client, monkeypatch, day=date(2026, 11, 8), *, complete=True):
    monkeypatch.setattr(main, 'habit_today', lambda _: day)
    schedule = {'start_date': str(day), 'end_date': str(day), 'timezone': 'Asia/Shanghai',
                'weekdays': [day.weekday()], 'completion_standard': '阅读十分钟', 'growth_threshold': 1}
    response = client.post('/api/v1/plans', json={'kind': 'habit', 'goal': '隔离阅读习惯',
        'availability': '十分钟', 'habit_schedule': schedule})
    job = response.json()['id']
    response = client.post('/api/v1/proposals/' + job + '/confirm')
    assert response.status_code == 200, response.text
    pid = response.json()['id']
    if complete:
        response = client.put(f'/api/v1/plants/{pid}/habit-occurrences/{day}', json={'completed': True, 'performed': True})
        assert response.status_code == 200, response.text
    return pid, schedule


def test_initial_catalog_locked_confirmation_and_bad_input(server):
    assert server.get('/api/v1/flowers').status_code == 401
    guest(server)
    result = catalog(server)
    assert len(result['flowers']) == 6 and result['unread_count'] == 0
    assert result['counts'] == {'goal': 0, 'habit': 0, 'total': 0}
    assert owned(server) == flowers.BASE_SPECIES
    job = server.post('/api/v1/plans', json={'kind': 'goal', 'goal': '虚构目标', 'availability': '十分钟'}).json()['id']
    plan = server.get('/api/v1/plans/' + job).json()['proposal']
    plan['flower_species'] = 'hydrangea'
    response = server.post('/api/v1/proposals/' + job + '/confirm', json=plan)
    assert response.status_code == 409 and response.json()['error']['code'] == 'FLOWER_LOCKED'
    assert server.get('/api/v1/garden').json()['plants'] == []
    plan['flower_species'] = 'made-up'
    assert server.post('/api/v1/proposals/' + job + '/confirm', json=plan).status_code == 422
    assert server.post('/api/v1/flowers/seen', json={'species': ['made-up']}).status_code == 422
    assert server.post('/api/v1/flowers/seen', json={'species': []}).status_code == 422


def test_goal_needs_final_choice_and_never_repeats_after_correction(server):
    account(server)
    _, pid = finish_goal(server, finish=False)
    assert catalog(server)['counts']['goal'] == 0
    endpoint = f'/api/v1/plants/{pid}/completion-notice'
    assert server.post(endpoint, json={'choice': 'finish'}).status_code == 200
    first = catalog(server)
    grant = next(f for f in first['flowers'] if f['species'] == 'cosmos')
    assert grant['unlocked'] and grant['is_new']
    for _ in range(2):
        server.post(endpoint, json={'choice': 'finish'})
        server.put(f'/api/v1/plants/{pid}/actions/0', json={'completed': False})
        server.put(f'/api/v1/plants/{pid}/actions/0', json={'completed': True})
    main.init_db()
    after = catalog(server)
    assert after['counts'] == {'goal': 1, 'habit': 0, 'total': 1}
    assert next(f for f in after['flowers'] if f['species'] == 'cosmos') == grant


def test_goal_followup_is_not_a_second_complete_goal(server):
    account(server)
    _, pid = finish_goal(server, finish=False)
    server.post(f'/api/v1/plants/{pid}/completion-notice', json={'choice': 'continue'})
    assert catalog(server)['counts']['goal'] == 0
    response = server.post(f'/api/v1/plants/{pid}/goal-next-proposals', json={'title': '继续改善', 'steps': ['再整理一次']})
    proposal = response.json()['id']
    response = server.post(f'/api/v1/goal-next-proposals/{proposal}/confirm')
    stage = response.json()['stage_id']
    assert server.put(f'/api/v1/goal-stages/{stage}/actions/0', json={'completed': True}).status_code == 200
    assert catalog(server)['counts']['goal'] == 0
    server.post(f'/api/v1/plants/{pid}/completion-notice', json={'choice': 'finish'})
    assert catalog(server)['counts']['goal'] == 1


def test_habit_five_accomplishments_unlock_and_corrections_keep_rewards(server, monkeypatch):
    account(server)
    finish_goal(server)
    pid, schedule = habit(server, monkeypatch, complete=False)
    assert catalog(server)['counts']['habit'] == 0
    for completed in (True, True, False, True):
        assert server.put(f'/api/v1/plants/{pid}/habit-occurrences/{schedule["start_date"]}',
                          json={'completed': completed, 'performed': completed}).status_code == 200
    assert catalog(server)['counts']['habit'] == 1 and 'lavender' in owned(server)
    for _ in range(3):
        habit(server, monkeypatch)
    result = catalog(server)
    assert result['counts'] == {'goal': 1, 'habit': 4, 'total': 5}
    assert len(owned(server)) == 6 and result['unread_count'] == 3
    assert next(f for f in result['flowers'] if f['species'] == 'hydrangea')['progress'] == 5


def test_same_habit_new_stage_counts_once_and_keeps_species(server, monkeypatch):
    account(server)
    pid, schedule = habit(server, monkeypatch)
    next_day = date(2026, 11, 9)
    draft = {**schedule, 'start_date': str(next_day), 'end_date': str(next_day), 'weekdays': [next_day.weekday()]}
    proposal = server.post(f'/api/v1/plants/{pid}/next-stage-proposals', json=draft).json()['id']
    assert server.post('/api/v1/habit-next-proposals/' + proposal + '/confirm').status_code == 200
    monkeypatch.setattr(main, 'habit_today', lambda _: next_day)
    endpoint = f'/api/v1/plants/{pid}/habit-occurrences/{next_day}'
    for _ in range(2):
        assert server.put(endpoint, json={'completed': True, 'performed': True}).status_code == 200
    assert catalog(server)['counts']['habit'] == 2
    assert server.get('/api/v1/garden').json()['plants'][0]['id'] == pid


def test_history_backfill_requires_real_snapshots_and_does_not_infer_plants(server, monkeypatch):
    owner = account(server)
    finish_goal(server)
    habit(server, monkeypatch)
    _, unfinished = plant(server)
    with auth.connect() as db:
        db.execute('DELETE FROM flower_achievements WHERE owner_id=?', (owner,))
        db.execute('DELETE FROM flower_unlocks WHERE owner_id=?', (owner,))
    result = catalog(server)
    assert result['counts'] == {'goal': 1, 'habit': 1, 'total': 2}
    assert result['unread_count'] == 2
    with auth.connect() as db:
        assert db.execute('SELECT count(*) FROM flower_achievements WHERE id=?', ('goal:' + unfinished,)).fetchone()[0] == 0
        db.execute('DELETE FROM flower_achievements WHERE owner_id=?', (owner,))
        db.execute('DELETE FROM flower_unlocks WHERE owner_id=?', (owner,))
        db.execute("UPDATE habit_collections SET snapshot_json='{}' WHERE owner_id=?", (owner,))
    result = catalog(server)
    assert result['counts']['habit'] == 0 and result['history_pending'] == 1


def test_seen_is_explicit_csrf_protected_and_other_owner_cannot_clear_it(server):
    account(server)
    finish_goal(server)
    assert catalog(server)['unread_count'] == 1
    assert server.post('/api/v1/flowers/seen', json={'species': ['cosmos']}, headers={'X-Bloomate-CSRF': 'bad'}).status_code == 403
    with TestClient(main.app) as other:
        account(other, 'other-flower@example.com')
        assert catalog(other)['counts']['total'] == 0
        assert other.post('/api/v1/flowers/seen', json={'species': ['cosmos']}).json()['updated'] == 0
    assert catalog(server)['unread_count'] == 1
    assert server.post('/api/v1/flowers/seen', json={'species': ['cosmos', 'cosmos']}).json()['updated'] == 1
    assert catalog(server)['unread_count'] == 0 and 'cosmos' in owned(server)


def test_guest_save_merges_existing_rewards_without_conflicts_or_reannouncing(server, monkeypatch):
    owner = account(server)
    finish_goal(server)
    habit(server, monkeypatch)
    assert server.post('/api/v1/flowers/seen', json={'species': ['cosmos', 'lavender']}).status_code == 200
    server.post('/api/v1/auth/logout')
    guest(server)
    finish_goal(server)
    assert catalog(server)['unread_count'] == 1
    previous_now = auth.now()
    monkeypatch.setattr(auth, 'now', lambda: previous_now + 61)
    login(server, 'flower@example.com')
    preview = server.post('/api/v1/auth/guest-save/preview').json()
    endpoint = '/api/v1/auth/guest-save/' + preview['operation_id'] + '/confirm'
    for _ in range(2):
        response = server.post(endpoint, json={'version': preview['version']})
        assert response.status_code == 200, response.text
    result = catalog(server)
    assert result['counts'] == {'goal': 2, 'habit': 1, 'total': 3} and result['unread_count'] == 0
    with auth.connect() as db:
        assert db.execute('SELECT count(*) FROM flower_unlocks WHERE owner_id=?', (owner,)).fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM flower_unlocks WHERE owner_id LIKE 'guest_%'").fetchone()[0] == 0


def test_expired_guest_rewards_are_cleaned_and_old_session_rejected(server):
    info = guest(server)
    finish_goal(server)
    assert catalog(server)['counts']['total'] == 1
    with auth.connect() as db:
        owner = db.execute("SELECT id FROM auth_subjects WHERE kind='guest'").fetchone()[0]
        db.execute('UPDATE auth_subjects SET expires_at=? WHERE id=?', (auth.now() - 1, owner))
    assert server.get('/api/v1/flowers').status_code == 401
    auth.cleanup_expired_guests()
    with auth.connect() as db:
        for table in ('flower_achievements', 'flower_unlocks'):
            assert db.execute(f'SELECT count(*) FROM {table} WHERE owner_id=?', (owner,)).fetchone()[0] == 0


def test_unlocked_flower_can_be_planted_and_locked_model_result_is_rejected(server, monkeypatch):
    account(server)
    finish_goal(server)
    job = server.post('/api/v1/plans', json={'kind': 'goal', 'goal': '新的虚构目标', 'availability': '十分钟'}).json()['id']
    plan = server.get('/api/v1/plans/' + job).json()['proposal']
    plan['flower_species'] = 'cosmos'
    response = server.post('/api/v1/proposals/' + job + '/confirm', json=plan)
    assert response.status_code == 200 and response.json()['plan']['flower_species'] == 'cosmos'
    before = response.json()
    assert server.put(f'/api/v1/plants/{before["id"]}/flower', json={'species': 'hydrangea'}).status_code == 409
    monkeypatch.setattr(main, 'generate', lambda _: main.Turn(plan=main.Plan(title='模型越权', summary='不应成功', steps=['整理'], flower_species='hydrangea')))
    job = server.post('/api/v1/plans', json={'kind': 'goal', 'goal': '模型越权检查', 'availability': '十分钟'}).json()['id']
    task = server.get('/api/v1/plans/' + job).json()
    assert task['status'] == 'error' and task['error']['code'] == 'FLOWER_LOCKED'
    assert server.post('/api/v1/proposals/' + job + '/confirm').status_code == 409


def test_export_and_erasure_include_reward_data(server, monkeypatch):
    owner = account(server)
    finish_goal(server)
    habit(server, monkeypatch)
    response = server.post('/api/v1/auth/account/export')
    assert response.status_code == 200
    assert len(response.json()['data']['flower_achievements']) == 2
    assert len(response.json()['data']['flower_unlocks']) == 2
    operation = challenge(server, monkeypatch)
    assert delete(server, operation).json()['status'] == 'succeeded'
    with auth.connect() as db:
        for table in ('flower_achievements', 'flower_unlocks'):
            assert db.execute(f'SELECT count(*) FROM {table} WHERE owner_id=?', (owner,)).fetchone()[0] == 0
    assert server.get('/api/v1/flowers').status_code == 401


def test_cloud_checkpoint_keeps_unlocks_and_seen_state_across_restart(cloud_store):
    with TestClient(main.app) as client:
        guest(client)
        _, pid = finish_goal(client)
        assert 'cosmos' in owned(client)
        login(client)
        preview = client.post('/api/v1/auth/guest-save/preview').json()
        response = client.post('/api/v1/auth/guest-save/' + preview['operation_id'] + '/confirm', json={'version': preview['version']})
        assert response.status_code == 200, response.text
        assert client.post('/api/v1/flowers/seen', json={'species': ['cosmos']}).status_code == 200
        cookie = client.cookies.get(auth.ACCOUNT_COOKIE)
    with TestClient(main.app) as client:
        client.cookies.set(auth.ACCOUNT_COOKIE, cookie)
        result = catalog(client)
        assert result['counts']['goal'] == 1 and result['unread_count'] == 0
        assert 'cosmos' in owned(client)
        assert client.get('/api/v1/garden').json()['plants'][0]['id'] == pid
    from pathlib import Path
    assert not Path(main.DB).exists()


def test_real_model_adapter_retries_wrong_growth_node_count_without_lowering_contract(server, monkeypatch):
    account(server)
    monkeypatch.setenv('BLOOMATE_TEST_MODEL', '0')
    monkeypatch.setenv('MODEL_ENABLED', '1')
    monkeypatch.setenv('MODEL_API_KEY', 'fixture-only')
    monkeypatch.setenv('MODEL_BASE_URL', 'https://model.invalid/v1')
    monkeypatch.setenv('MODEL_NAME', 'deepseek-flash')
    monkeypatch.setenv('MODEL_CALL_RESERVE_CENTS', '10')
    prompts = []
    def provider(url, **kwargs):
        prompts.append(kwargs['json']['messages'][0]['content'])
        nodes = [25, 50, 75] if len(prompts) == 1 else [14, 42, 71, 85]
        content = {'question': None, 'plan': {'title': '虚构计划', 'summary': '仅验收', 'steps': ['整理', '练习', '检查'], 'growth_nodes': nodes, 'flower_species': 'sunflower'}}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(content)}}]}, request=httpx.Request('POST', url))
    monkeypatch.setattr(main.httpx, 'post', provider)
    response = server.post('/api/v1/plans', json={'kind': 'goal', 'goal': '三步虚构计划', 'availability': '十分钟'})
    task = server.get('/api/v1/plans/' + response.json()['id']).json()
    assert task['status'] == 'done' and task['proposal']['growth_nodes'] == [14, 42, 71, 85]
    assert len(prompts) == 2 and '必须恰好4个' in prompts[1]
    assert '本次本人可用花种' in prompts[0] and '"cosmos"' not in prompts[0].split('本次本人可用花种：', 1)[1].split('。', 1)[0]
