"""Disposable local fixtures only: never erase the product manager's accounts."""
import json
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import account_privacy as privacy, auth, legal, main
from test_auth import server, guest, login, plant, ORIGIN


def account(client, email='a@example.com'):
    guest(client)
    login(client, email)
    return auth.session_for(client.cookies.get(auth.ACCOUNT_COOKIE))['owner_id']


def challenge(client, monkeypatch):
    monkeypatch.setattr(auth, 'send_verification', lambda email: ('erasure-mail-fixture', 300, True))
    response = client.post('/api/v1/auth/account/deletion-challenges')
    assert response.status_code == 202, response.text
    return response.json()['challenge_id']


def delete(client, operation, code='135790', confirmation=privacy.CONFIRMATION):
    return client.post('/api/v1/auth/account/delete', json={
        'challenge_id': operation, 'code': code, 'confirmation': confirmation})


def test_export_is_complete_but_no_other_users_or_credentials(server):
    owner = account(server)
    plant(server)
    with auth.connect() as db:
        db.execute('INSERT INTO user_memories VALUES (?,?,?,?,?)', ('mine', owner, 'A_PRIVATE', 't', 't'))
    with TestClient(main.app) as other:
        other_owner = account(other, 'b@example.com')
        plant(other)
        with auth.connect() as db:
            db.execute('INSERT INTO user_memories VALUES (?,?,?,?,?)', ('theirs', other_owner, 'B_PRIVATE', 't', 't'))
    response = server.post('/api/v1/auth/account/export')
    assert response.status_code == 200
    body = response.json()
    assert body['format'] == 'bloomate-personal-data-v1'
    assert len(body['data']['plants']) == 1 and body['agreements']
    assert set(body['data']) == privacy.TABLES | {'habit_events', 'habit_occurrences', 'model_call_metrics'}
    assert 'A_PRIVATE' in response.text and 'B_PRIVATE' not in response.text
    for private in ('provider_sub', 'provider_id', 'session_digest', 'csrf_token', 'mail-fixture', server.cookies.get(auth.ACCOUNT_COOKIE)):
        assert private not in response.text
    assert 'attachment' in response.headers['content-disposition']
    assert response.headers['cache-control'] == 'no-store'


def test_export_relational_habit_rows_only_belong_to_own_stage(server):
    owner = account(server)
    _, pid = plant(server)
    with auth.connect() as db:
        db.execute('INSERT INTO habit_stages(id,owner_id,plant_id,start_date,end_date,timezone,weekdays_json,completion_standard) VALUES (?,?,?,?,?,?,?,?)',
                   ('owned-stage', owner, pid, '2026-11-05', '2026-11-05', 'Asia/Shanghai', '[3]', '一次'))
        db.execute('INSERT INTO habit_occurrences(stage_id,action_date) VALUES (?,?)', ('owned-stage', '2026-11-05'))
    result = server.post('/api/v1/auth/account/export').json()
    assert result['data']['habit_occurrences'][0]['stage_id'] == 'owned-stage'


def test_account_privacy_requires_account_and_csrf(server):
    assert server.post('/api/v1/auth/account/export', headers=ORIGIN).status_code == 401
    guest(server)
    for path in ('account',):
        assert server.get('/api/v1/auth/' + path).status_code == 401
    assert server.post('/api/v1/auth/account/export').status_code == 401
    login(server)
    assert server.post('/api/v1/auth/account/export', headers={'X-Bloomate-CSRF': 'wrong'}).status_code == 403


def test_erasure_removes_account_and_bound_guest_not_other_or_legacy(server, monkeypatch):
    owner = account(server)
    plant(server)
    cookie = server.cookies.get(auth.ACCOUNT_COOKIE)
    with auth.connect() as db:
        db.execute('INSERT INTO user_memories VALUES (?,?,?,?,?)', ('mine', owner, 'private', 't', 't'))
        db.execute('INSERT INTO usage_reservations VALUES (?,?,?,?)', ('cost-mine', owner, '2026-11', 123))
        db.execute('INSERT INTO model_call_metrics VALUES (?,?,?,?,?,?,?)', ('cost-mine', 'fixture', 1, 'ok', 2, 3, 't'))
        db.execute('INSERT INTO user_memories VALUES (?,?,?,?,?)', ('legacy', 'local-user', 'legacy-keep', 't', 't'))
    with TestClient(main.app) as other:
        other_owner = account(other, 'b@example.com')
        plant(other)
        with TestClient(main.app) as bound:
            guest(bound)
            plant(bound)
            with auth.connect() as db:
                db.execute('UPDATE auth_challenges SET created_at=created_at-61')
            login(bound)
            assert server.get('/api/v1/auth/account').json()['linked_guest_count'] >= 1
        operation = challenge(server, monkeypatch)
        response = delete(server, operation)
        assert response.status_code == 200, response.text
        assert response.json()['shared_auth_identity_deleted'] is False
        assert len(other.get('/api/v1/garden').json()['plants']) == 1
        with auth.connect() as db:
            assert not db.execute('SELECT 1 FROM auth_subjects WHERE id=?', (owner,)).fetchone()
            assert db.execute('SELECT id FROM auth_subjects WHERE id=?', (other_owner,)).fetchone()
            for table in privacy.TABLES:
                assert not db.execute(f'SELECT 1 FROM "{table}" WHERE owner_id=?', (owner,)).fetchone(), table
            assert db.execute("SELECT content FROM user_memories WHERE id='legacy'").fetchone()[0] == 'legacy-keep'
            assert db.execute("SELECT SUM(amount_cents) FROM usage_reservations WHERE month='2026-11'").fetchone()[0] >= 123
            assert not db.execute("SELECT 1 FROM model_call_metrics WHERE reservation_id='cost-mine'").fetchone()
        with TestClient(main.app) as stale:
            stale.cookies.set(auth.ACCOUNT_COOKIE, cookie)
            assert stale.get('/api/v1/garden').status_code == 401
            assert stale.get('/api/v1/auth/deletion-status/' + operation).json()['status'] == 'succeeded'
    journal = privacy.journal_path().read_text()
    assert owner not in journal and 'a@example.com' not in journal and 'private' not in journal
    assert server.get('/api/v1/auth/session').json()['kind'] == 'none'
    guest(server)
    login(server)
    assert server.get('/api/v1/garden').json()['plants'] == []
    assert auth.session_for(server.cookies.get(auth.ACCOUNT_COOKIE))['owner_id'] != owner


def test_wrong_confirmation_identity_code_expiry_and_replay(server, monkeypatch):
    owner = account(server)
    operation = challenge(server, monkeypatch)
    assert delete(server, operation, confirmation='').status_code == 400
    for _ in range(5):
        assert delete(server, operation, code='000000').status_code == 400
    assert delete(server, operation).status_code == 429
    with auth.connect() as db:
        db.execute('UPDATE auth_deletion_challenges SET attempts=0,expires_at=?', (auth.now() - 1,))
    assert delete(server, operation).status_code == 410
    with auth.connect() as db:
        db.execute('UPDATE auth_deletion_challenges SET expires_at=?', (auth.now() + 100,))
    monkeypatch.setattr(auth, 'verify_identity', lambda row, code: 'wrong-sub')
    assert delete(server, operation).status_code == 403
    assert auth.owner_active(owner)


def test_delete_challenge_cannot_be_used_by_other_session(server, monkeypatch):
    account(server)
    operation = challenge(server, monkeypatch)
    with TestClient(main.app) as other:
        account(other, 'b@example.com')
        assert delete(other, operation).status_code == 409
    assert server.post('/api/v1/auth/account/deletion-challenges').status_code == 429


def test_same_account_other_device_cannot_reuse_deletion_challenge(server, monkeypatch):
    owner = account(server)
    operation = challenge(server, monkeypatch)
    with auth.connect() as db:
        db.execute('UPDATE auth_challenges SET created_at=created_at-61')
    with TestClient(main.app) as other:
        assert account(other) == owner
        assert delete(other, operation).status_code == 409
    assert auth.owner_active(owner)


def test_running_work_blocks_export_and_erasure(server):
    owner = account(server)
    with auth.connect() as db:
        db.execute('INSERT INTO jobs(id,owner_id,status,input_json) VALUES (?,?,?,?)', ('running', owner, 'running', '{}'))
    assert server.post('/api/v1/auth/account/export').status_code == 409
    assert server.post('/api/v1/auth/account/deletion-challenges').status_code == 409


def test_late_writer_cannot_resurrect_deleted_content(server, monkeypatch):
    owner = account(server)
    with main.acting_as(owner):
        stale = main.connect()
    operation = challenge(server, monkeypatch)
    assert delete(server, operation).status_code == 200
    with pytest.raises(auth.AuthError):
        with stale:
            stale.execute('INSERT INTO jobs(id,owner_id,status,input_json) VALUES (?,?,?,?)', ('late', owner, 'pending', '{}'))
    stale.close()
    with auth.connect() as db:
        assert not db.execute("SELECT 1 FROM jobs WHERE id='late'").fetchone()


def test_failed_purge_is_blocked_and_recoverable_after_restart(server, monkeypatch):
    owner = account(server)
    plant(server)
    operation = challenge(server, monkeypatch)
    original = privacy.purge_owner
    monkeypatch.setattr(privacy, 'purge_owner', lambda db, owner: (_ for _ in ()).throw(sqlite3.OperationalError('fixture')))
    assert delete(server, operation).status_code == 503
    assert server.get('/api/v1/garden').status_code == 401
    assert server.get('/api/v1/auth/deletion-status/' + operation).json()['status'] == 'pending'
    monkeypatch.setattr(privacy, 'purge_owner', original)
    privacy.replay_erasures()
    assert server.get('/api/v1/auth/deletion-status/' + operation).json()['status'] == 'succeeded'
    assert not auth.owner_active(owner)


def test_old_backup_recovery_replays_independent_erasure_journal(server, monkeypatch, tmp_path):
    owner = account(server)
    plant(server)
    backup = tmp_path / 'old-snapshot.sqlite3'
    with sqlite3.connect(main.DB) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    operation = challenge(server, monkeypatch)
    assert delete(server, operation).status_code == 200
    shutil.copyfile(backup, main.DB)
    assert server.get('/api/v1/garden').status_code == 401
    privacy.replay_erasures()
    with auth.connect() as db:
        assert not db.execute('SELECT 1 FROM auth_subjects WHERE id=?', (owner,)).fetchone()
        assert not db.execute('SELECT 1 FROM plants WHERE owner_id=?', (owner,)).fetchone()


def test_unknown_business_table_fails_closed_and_legal_is_public(server):
    result = server.get('/api/v1/legal')
    assert result.status_code == 200 and result.json()['version'] == legal.VERSION
    account(server)
    with auth.connect() as db:
        db.execute('CREATE TABLE unexpected_private_data(owner_id TEXT,content TEXT)')
    assert server.post('/api/v1/auth/account/export').status_code == 503
    assert server.post('/api/v1/auth/account/deletion-challenges').status_code == 503


def test_unknown_child_table_cannot_be_silently_omitted(server):
    account(server)
    with auth.connect() as db:
        db.execute('CREATE TABLE unexpected_child_data(stage_id TEXT,content TEXT)')
    assert server.post('/api/v1/auth/account/export').status_code == 503
    assert server.post('/api/v1/auth/account/deletion-challenges').status_code == 503


def test_oversized_export_is_rejected_without_truncating(server):
    owner = account(server)
    with auth.connect() as db:
        db.execute('INSERT INTO user_memories VALUES (?,?,?,?,?)', ('large', owner, 'x' * (25 * 1024 * 1024), 't', 't'))
    response = server.post('/api/v1/auth/account/export')
    assert response.status_code == 413
    assert response.json()['error']['code'] == 'EXPORT_TOO_LARGE'
    assert 'content-disposition' not in response.headers


def test_missing_operator_also_blocks_production_guest_work(server, monkeypatch):
    guest(server)
    monkeypatch.delenv('BLOOMATE_OPERATOR_NAME', raising=False)
    monkeypatch.delenv('BLOOMATE_CONTACT_EMAIL', raising=False)
    monkeypatch.setenv('BLOOMATE_APP_MODE', 'production')
    assert server.get('/api/v1/garden').status_code == 503
    assert server.post('/api/v1/auth/guest').status_code == 503
    assert server.get('/api/v1/legal').status_code == 200


def test_deletion_send_counts_against_shared_mail_budget(server, monkeypatch):
    account(server)
    monkeypatch.setenv('BLOOMATE_EMAIL_DAILY_LIMIT', '1')
    sent = []
    monkeypatch.setattr(auth, 'send_verification', lambda email: sent.append(email))
    assert server.post('/api/v1/auth/account/deletion-challenges').status_code == 429
    assert sent == []


def test_new_background_work_after_reauthentication_consumes_challenge(server, monkeypatch):
    owner = account(server)
    operation = challenge(server, monkeypatch)
    def verify(row, code):
        with auth.connect() as db:
            db.execute('INSERT INTO jobs(id,owner_id,status,input_json) VALUES (?,?,?,?)', ('race-work', owner, 'pending', '{}'))
        return 'subject-' + row['email']
    monkeypatch.setattr(auth, 'verify_identity', verify)
    assert delete(server, operation).status_code == 409
    assert auth.owner_active(owner)
    with auth.connect() as db:
        assert db.execute('SELECT status FROM auth_deletion_challenges WHERE id=?', (operation,)).fetchone()[0] == 'superseded'
    assert delete(server, operation).status_code == 409


def test_corrupt_erasure_journal_never_reopens_account(server):
    owner = account(server)
    privacy.journal_path().write_text('{broken-record')
    response = server.get('/api/v1/garden')
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'ERASURE_RECOVERY_REQUIRED'
    with auth.connect() as db:
        assert db.execute('SELECT 1 FROM auth_subjects WHERE id=?', (owner,)).fetchone()


def test_legal_versions_acceptance_and_missing_public_operator(server, monkeypatch):
    monkeypatch.delenv('BLOOMATE_OPERATOR_NAME', raising=False)
    monkeypatch.delenv('BLOOMATE_CONTACT_EMAIL', raising=False)
    assert server.get('/api/v1/legal').json()['ready'] is False
    account(server)
    response = server.post('/api/v1/auth/account/agreements', json={'terms_version': legal.VERSION, 'privacy_version': legal.VERSION})
    assert response.status_code == 200
    exported = server.post('/api/v1/auth/account/export').json()
    assert any(r['terms_version'] == legal.VERSION for r in exported['agreements'])
    assert server.post('/api/v1/auth/account/agreements', json={'terms_version': 'old', 'privacy_version': 'old'}).status_code == 409
    monkeypatch.setenv('BLOOMATE_APP_MODE', 'production')
    assert server.post('/api/v1/auth/email/challenges', json={
        'email': 'a@example.com', 'terms_version': legal.VERSION, 'privacy_version': legal.VERSION}).status_code == 503
    assert server.post('/api/v1/auth/email/challenges', json={
        'email': 'a@example.com', 'terms_version': 'local-preview-v1', 'privacy_version': 'local-preview-v1'}).status_code == 422


def test_production_old_agreement_blocks_business_but_preserves_privacy_rights(server, monkeypatch):
    account(server)
    monkeypatch.setenv('BLOOMATE_OPERATOR_NAME', '隔离测试运营者')
    monkeypatch.setenv('BLOOMATE_CONTACT_EMAIL', 'fixture@example.com')
    monkeypatch.setenv('BLOOMATE_APP_MODE', 'production')
    assert server.get('/api/v1/auth/session').json()['agreements_required'] is True
    assert server.get('/api/v1/garden').status_code == 403
    assert server.post('/api/v1/auth/account/export').status_code == 200
    assert server.post('/api/v1/auth/account/agreements', json={'terms_version': legal.VERSION, 'privacy_version': legal.VERSION}).status_code == 200
    assert server.get('/api/v1/garden').status_code == 200
