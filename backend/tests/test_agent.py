"""Read-only agent contracts, disposable databases and mocked model transport only."""
import json
import sqlite3
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app import agent, auth, main
from test_auth import server, guest, login, plant
from test_account_privacy import challenge, delete
from test_cloud_persistence import cloud_store


def query(client, text='今天要做什么', session=None, context=None, request_id=None):
    body = {'text': text, 'request_id': request_id or str(uuid.uuid4()),
            'context': context or {'page': 'today'}, 'session_id': session}
    response = client.post('/api/v1/agent/turns', json=body)
    assert response.status_code == 202, response.text
    turn = client.get('/api/v1/agent/turns/' + response.json()['id'])
    assert turn.status_code == 200, turn.text
    return turn.json(), body


def test_reads_same_today_data_after_manual_completion(server):
    guest(server)
    _, pid = plant(server)
    day = main.habit_today('Asia/Shanghai').isoformat()
    server.put(f'/api/v1/plants/{pid}/actions/0/date', json={'due_date': day}).raise_for_status()
    before, _ = query(server)
    assert not before['result']['data']['goals_today'][0]['completed']
    server.put(f'/api/v1/plants/{pid}/actions/0', json={'completed': True}).raise_for_status()
    after, _ = query(server, session=before['session_id'])
    assert after['result']['data']['goals_today'][0]['completed']
    assert len(server.get('/api/v1/agent/sessions/' + after['session_id']).json()['turns']) == 2


def test_idempotency_and_changed_retry(server):
    guest(server)
    first, body = query(server)
    again = server.post('/api/v1/agent/turns', json=body)
    assert again.status_code == 202 and again.json()['id'] == first['id']
    assert server.post('/api/v1/agent/turns', json={**body, 'text': '别的内容'}).status_code == 409
    with main.connect() as db:
        assert db.execute('SELECT count(*) FROM agent_turns').fetchone()[0] == 1


def test_no_writes_and_explicit_scope(server):
    guest(server)
    _, pid = plant(server)
    result, _ = query(server, '把第一步改到明天', context={'page': 'garden', 'plant_id': pid})
    assert result['result']['kind'] == 'unsupported'
    assert server.get(f'/api/v1/plants/{pid}/actions').json()['actions'][0]['due_date'] is None
    ambiguous, _ = query(server, '这株植物进展怎样')
    assert ambiguous['result']['kind'] == 'clarify'
    selected, _ = query(server, '这株植物进展怎样', context={'page': 'garden', 'plant_id': pid})
    assert selected['result']['data']['steps'][0]['completed'] is False


def test_cross_owner_history_context_and_delete_are_rejected(server):
    guest(server)
    _, pid = plant(server)
    first, _ = query(server)
    with TestClient(main.app) as other:
        guest(other)
        for path in ('/sessions/' + first['session_id'], '/turns/' + first['id']):
            assert other.get('/api/v1/agent' + path).status_code == 404
        assert other.delete('/api/v1/agent/sessions/' + first['session_id']).status_code == 404
        body = {'text': '今天做什么', 'request_id': str(uuid.uuid4()), 'context': {'page': 'garden', 'plant_id': pid}}
        assert other.post('/api/v1/agent/turns', json=body).status_code == 404
        assert other.get('/api/v1/agent/sessions').json() == {'sessions': []}


def test_thirty_day_expiry_does_not_slide_on_new_messages(server, monkeypatch):
    guest(server)
    first, _ = query(server)
    with main.connect() as db:
        old_created = db.execute('SELECT created_at FROM agent_turns WHERE id=?', (first['id'],)).fetchone()[0]
    # Keep authenticated identity valid independently from the original seven-day guest fixture.
    with main.connect() as db:
        db.execute('UPDATE auth_subjects SET expires_at=?', (old_created + 100 * auth.DAY,))
        db.execute('UPDATE auth_sessions SET expires_at=?', (old_created + 100 * auth.DAY,))
    monkeypatch.setattr(auth, 'now', lambda: old_created + agent.RETENTION - 1)
    second, _ = query(server, session=first['session_id'])
    monkeypatch.setattr(auth, 'now', lambda: old_created + agent.RETENTION)
    assert server.get('/api/v1/agent/turns/' + first['id']).status_code == 404
    history = server.get('/api/v1/agent/sessions/' + first['session_id']).json()['turns']
    assert [t['id'] for t in history] == [second['id']]


def test_deleted_conversation_stays_deleted_after_old_backup(server, tmp_path):
    guest(server)
    first, _ = query(server)
    backup = str(tmp_path / 'before.sqlite3')
    with sqlite3.connect(main.DB) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    assert server.delete('/api/v1/agent/sessions/' + first['session_id']).json() == {'deleted': True}
    with sqlite3.connect(backup) as source, sqlite3.connect(main.DB) as destination:
        source.backup(destination)
    assert server.get('/api/v1/agent/turns/' + first['id']).status_code == 404
    with main.connect() as db:
        assert db.execute('SELECT count(*) FROM agent_turns').fetchone()[0] == 0


def test_restart_finishes_interrupted_turn_without_reissuing_model(server, monkeypatch):
    guest(server)
    monkeypatch.setattr(agent, 'process_turn', lambda _: None)
    turn, _ = query(server)
    assert turn['status'] == 'pending'
    main.init_db()
    restored = server.get('/api/v1/agent/turns/' + turn['id']).json()
    assert restored['status'] == 'error' and restored['error']['code'] == 'INTERRUPTED'


def test_guest_expiring_during_model_call_cannot_block_cleanup(server, monkeypatch):
    guest(server)
    def expire(*args):
        with main.connect() as db:
            db.execute("UPDATE auth_subjects SET expires_at=? WHERE kind='guest'", (auth.now()-1,))
        return agent.Intent(intent='today')
    monkeypatch.setattr(agent, 'route_intent', expire)
    response=server.post('/api/v1/agent/turns',json={'request_id':str(uuid.uuid4()),'text':'今天做什么','context':{'page':'today'}})
    assert response.status_code == 202
    with main.connect() as db:
        row=db.execute('SELECT status,error_code FROM agent_turns').fetchone()
        assert tuple(row)==('error','EXPIRED')
    auth.cleanup_expired_guests()
    with main.connect() as db:
        assert db.execute('SELECT count(*) FROM agent_turns').fetchone()[0] == 0


def test_guest_chat_only_can_be_saved_and_exported(server):
    guest(server)
    turn, _ = query(server)
    login(server)
    assert server.get('/api/v1/auth/session').json()['pending_save']
    preview = server.post('/api/v1/auth/guest-save/preview').json()
    path = '/api/v1/auth/guest-save/' + preview['operation_id'] + '/confirm'
    for _ in range(2):
        assert server.post(path, json={'version': preview['version']}).status_code == 200
    assert server.get('/api/v1/agent/turns/' + turn['id']).status_code == 200
    exported = server.post('/api/v1/auth/account/export').json()['data']
    assert len(exported['agent_turns']) == 1 and len(exported['agent_sessions']) == 1


def test_account_erasure_covers_agent_and_expired_guests(server, monkeypatch):
    guest(server)
    login(server)
    query(server)
    operation = challenge(server, monkeypatch)
    assert delete(server, operation).status_code == 200
    with main.connect() as db:
        assert db.execute('SELECT count(*) FROM agent_turns').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM agent_sessions').fetchone()[0] == 0
    guest(server)
    query(server)
    with main.connect() as db:
        db.execute("UPDATE auth_subjects SET expires_at=0 WHERE kind='guest'")
    auth.cleanup_expired_guests()
    with main.connect() as db:
        assert db.execute('SELECT count(*) FROM agent_turns').fetchone()[0] == 0


def test_cloud_conversation_recovery_and_independent_deletion(cloud_store):
    with TestClient(main.app) as client:
        guest(client)
        first, _ = query(client)
        old_checkpoint = cloud_store.load()
        cookie = client.cookies.get(auth.GUEST_COOKIE)
        csrf = client.headers['X-Bloomate-CSRF']
    with TestClient(main.app) as client:
        client.cookies.set(auth.GUEST_COOKIE, cookie)
        client.headers.update({'Origin': 'http://testserver', 'X-Bloomate-CSRF': csrf})
        assert client.get('/api/v1/agent/turns/' + first['id']).json()['status'] == 'done'
        assert client.delete('/api/v1/agent/sessions/' + first['session_id']).status_code == 200
        cloud_store.revision, cloud_store.checkpoint = old_checkpoint
        assert client.get('/api/v1/agent/turns/' + first['id']).status_code == 404


def configure_model(monkeypatch):
    monkeypatch.setenv('BLOOMATE_TEST_MODEL', '0')
    monkeypatch.setenv('MODEL_ENABLED', '1')
    monkeypatch.setenv('MODEL_CALL_RESERVE_CENTS', '10')
    monkeypatch.setattr(main, 'model_config', lambda: ('fixture-credential', 'https://model.invalid', 'fixture'))


def test_model_schema_retries_are_bounded_and_share_cost_ledger(server, monkeypatch):
    guest(server)
    configure_model(monkeypatch)
    calls = []
    def response(*args, **kwargs):
        calls.append(kwargs['json'])
        return httpx.Response(200, request=httpx.Request('POST', args[0]), json={'choices': [{'message': {'content': json.dumps({'intent': 'today'} if len(calls)==2 else {'intent': 'write_sql'})}}]})
    monkeypatch.setattr(httpx, 'post', response)
    turn, _ = query(server)
    assert turn['status'] == 'done' and len(calls) == 2
    with main.connect() as db:
        assert db.execute('SELECT sum(amount_cents) FROM usage_reservations').fetchone()[0] == 20
        assert db.execute('SELECT count(*) FROM model_call_metrics').fetchone()[0] == 2


@pytest.mark.parametrize('failure', ['timeout', 'invalid', 'budget'])
def test_model_failures_never_claim_success(server, monkeypatch, failure):
    guest(server)
    configure_model(monkeypatch)
    def fail(*args, **kwargs):
        if failure == 'timeout':
            raise httpx.ReadTimeout('fixture')
        return httpx.Response(200, request=httpx.Request('POST', args[0]), json={'choices': [{'message': {'content': '{}'}}]})
    monkeypatch.setattr(httpx, 'post', fail)
    if failure == 'budget':
        with main.connect() as db:
            db.execute('INSERT INTO usage_reservations VALUES (?,?,?,?)', ('fixture', 'aggregate', __import__('datetime').datetime.now(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).strftime('%Y-%m'), 5000))
    turn, _ = query(server)
    assert turn['status'] == 'error' and turn['result'] is None
    assert turn['error']['code'] == ('BUDGET_EXHAUSTED' if failure == 'budget' else 'MODEL_FAILED')
