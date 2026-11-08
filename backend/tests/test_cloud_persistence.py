"""Simulated gateway contract tests; live PostgreSQL checks are recorded separately."""
import copy
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app import account_privacy, auth, main
from app.identity import acting_as
from app.repository import check_config
from app.repository import cloudbase
from test_auth import guest, login, plant


class MemoryGateway:
    def __init__(self):
        self.revision, self.checkpoint = 0, None
        self.operations, self.erasures = {}, []

    def load(self):
        return self.revision, copy.deepcopy(self.checkpoint)

    def commit(self, revision, checkpoint, operation):
        if operation in self.operations:
            return self.operations[operation]
        if revision != self.revision:
            raise auth.AuthError('DB_CONFLICT', 'conflict', 409)
        self.revision += 1
        self.checkpoint = copy.deepcopy(checkpoint)
        self.operations[operation] = self.revision
        return self.revision

    def journal(self):
        return copy.deepcopy(self.erasures)

    def append(self, owners, operation):
        for owner in owners:
            record = {'owner_hash': account_privacy.owner_hash(owner), 'operation_id': operation, 'created_at': auth.now()}
            if record['owner_hash'] not in {r['owner_hash'] for r in self.erasures}:
                self.erasures.append(record)


@pytest.fixture
def cloud_store(monkeypatch, tmp_path):
    store = MemoryGateway()
    monkeypatch.setenv('DATA_BACKEND', 'cloudbase_rest')
    monkeypatch.setenv('BLOOMATE_APP_MODE', 'local')
    monkeypatch.setenv('BLOOMATE_TEST_MODEL', '1')
    monkeypatch.setenv('MODEL_ENABLED', '0')
    monkeypatch.setenv('BLOOMATE_COOKIE_SECURE', '0')
    monkeypatch.setenv('BLOOMATE_ALLOWED_ORIGINS', 'http://testserver')
    monkeypatch.setenv('CLOUDBASE_ENV_ID', 'test-env')
    monkeypatch.setenv('BLOOMATE_EMAIL_ENABLED', '1')
    monkeypatch.setattr(cloudbase, '_gateway', store)
    monkeypatch.setattr(main, 'DB', str(tmp_path / 'must-not-be-created.sqlite3'))
    monkeypatch.setattr(auth, 'send_verification', lambda email: ('mail-fixture', 300, False))
    monkeypatch.setattr(auth, 'verify_identity', lambda row, code: 'subject-' + row['email'])
    return store


def test_cloud_goal_save_restart_and_cross_owner_contract(cloud_store):
    with TestClient(main.app) as a:
        guest(a)
        job, pid = plant(a)
        login(a)
        preview = a.post('/api/v1/auth/guest-save/preview').json()
        endpoint = '/api/v1/auth/guest-save/' + preview['operation_id'] + '/confirm'
        for _ in range(2):
            assert a.post(endpoint, json={'version': preview['version']}).status_code == 200
        assert a.put(f'/api/v1/plants/{pid}/actions/0', json={'completed': True}).status_code == 200
        cookie = a.cookies.get(auth.ACCOUNT_COOKIE)
    with TestClient(main.app) as a:
        a.cookies.set(auth.ACCOUNT_COOKIE, cookie)
        assert a.get('/api/v1/garden').json()['plants'][0]['id'] == pid
        assert a.get(f'/api/v1/plants/{pid}/actions').json()['actions'][0]['completed']
        with TestClient(main.app) as b:
            guest(b)
            assert b.get('/api/v1/garden').json()['plants'] == []
            assert b.get(f'/api/v1/plants/{pid}/actions').status_code == 404
    from pathlib import Path
    assert not Path(main.DB).exists()


def test_cloud_habit_and_rollback_contract(cloud_store):
    with TestClient(main.app) as a:
        guest(a)
        _, pid = plant(a, 'habit')
        assert a.get(f'/api/v1/plants/{pid}/actions').status_code == 409
        with pytest.raises(ValueError):
            with main.connect() as db:
                db.execute("UPDATE plants SET title='must roll back' WHERE id=?", (pid,))
                raise ValueError()
        with main.connect() as db:
            assert db.execute('SELECT title FROM plants WHERE id=?', (pid,)).fetchone()[0] != 'must roll back'


def test_conflicting_checkpoint_never_overwrites_newer_state(cloud_store):
    with TestClient(main.app):
        before = cloud_store.checkpoint
        with pytest.raises(auth.AuthError) as exc:
            with main.connect() as db:
                db.execute("INSERT INTO usage_reservations VALUES ('test','aggregate','2026-11',10)")
                cloud_store.revision += 1
        assert exc.value.code == 'DB_CONFLICT'
        assert cloud_store.checkpoint == before


def test_erasures_survive_restoring_old_business_checkpoint(cloud_store):
    with TestClient(main.app) as a:
        guest(a); _, pid = plant(a); login(a)
        owner = next(r['id'] for r in _subjects() if r['kind'] == 'account')
        old = copy.deepcopy(cloud_store.checkpoint)
        cloud_store.append([owner], 'a' * 48)
        account_privacy.replay_erasures()
        cloud_store.checkpoint = old
        cloud_store.revision += 1
        account_privacy.replay_erasures()
        assert owner not in {r['id'] for r in _subjects()}
        assert account_privacy.is_erased(owner)
        assert a.get('/api/v1/garden').status_code == 401


def _subjects():
    with main.connect() as db:
        return db.execute('SELECT * FROM auth_subjects').fetchall()


def test_concurrent_erasure_blocks_an_admitted_writer(cloud_store):
    with TestClient(main.app) as a:
        guest(a); login(a)
        owner = next(r['id'] for r in _subjects() if r['kind'] == 'account')
        with acting_as(owner):
            with pytest.raises(auth.AuthError) as exc:
                with main.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("INSERT INTO user_memories VALUES ('id',?,'private','today','today')", (owner,))
                    cloud_store.append([owner], 'b' * 48)
            assert exc.value.code == 'AUTH_REQUIRED'


def test_lost_commit_response_is_retried_with_same_operation():
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            raise httpx.ReadTimeout('lost acknowledgement')
        return httpx.Response(200, json=7)
    gateway = cloudbase.Gateway('test-env', 'fixture', httpx.MockTransport(handle))
    assert gateway.commit(6, {'sha256': 'fixture'}, 'operation') == 7
    assert requests[0] == requests[1]


def test_gateway_denial_and_malformed_state_fail_closed():
    for response in [httpx.Response(403, json={'private': 'not exposed'}), httpx.Response(200, json=[])]:
        gateway = cloudbase.Gateway('test-env', 'fixture', httpx.MockTransport(lambda _: response))
        with pytest.raises(auth.AuthError):
            gateway.load()


def test_void_erasure_rpc_accepts_204_but_state_still_requires_a_payload():
    gateway = cloudbase.Gateway('test-env', 'fixture', httpx.MockTransport(lambda _: httpx.Response(204)))
    gateway.append(['guest-fixture'], 'c' * 48)
    with pytest.raises(auth.AuthError):
        gateway.load()
    with pytest.raises(auth.AuthError):
        gateway.commit(1, {}, 'operation')


def test_checkpoint_corruption_and_server_database_guard(monkeypatch):
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE test (value TEXT)')
        checkpoint = cloudbase.pack(db.serialize())
    assert cloudbase.unpack(checkpoint).startswith(b'SQLite format 3')
    checkpoint['sha256'] = '0' * 64
    with pytest.raises(auth.AuthError):
        cloudbase.unpack(checkpoint)
    monkeypatch.setenv('REQUIRE_SERVER_DB', '1')
    monkeypatch.delenv('DATA_BACKEND', raising=False)
    with pytest.raises(RuntimeError):
        check_config()
