"""Isolated tests mock the mail provider, never a production authentication route."""
import concurrent.futures
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import auth, main


ORIGIN = {'Origin': 'http://testserver'}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB', str(tmp_path / 'auth.sqlite3'))
    monkeypatch.setenv('BLOOMATE_TEST_MODEL', '1')
    monkeypatch.setenv('MODEL_ENABLED', '0')
    monkeypatch.setenv('BLOOMATE_APP_MODE', 'local')
    monkeypatch.setenv('BLOOMATE_COOKIE_SECURE', '0')
    monkeypatch.setenv('BLOOMATE_ALLOWED_ORIGINS', 'http://testserver')
    monkeypatch.setenv('CLOUDBASE_ENV_ID', 'test-env')
    monkeypatch.setenv('BLOOMATE_EMAIL_ENABLED', '1')
    monkeypatch.setattr(auth, 'send_verification', lambda email: ('mail-fixture', 300, False))

    def verify(row, code):
        if code != '135790':
            raise auth.AuthError('CODE_INVALID', '验证码不正确。')
        return 'subject-' + row['email']
    monkeypatch.setattr(auth, 'verify_identity', verify)
    with TestClient(main.app) as client:
        yield client


def guest(client):
    response = client.post('/api/v1/auth/guest', headers=ORIGIN)
    assert response.status_code in (200, 201), response.text
    client.headers.update({**ORIGIN, 'X-Bloomate-CSRF': response.json()['csrf_token']})
    return response.json()


def login(client, email='a@example.com'):
    result = client.post('/api/v1/auth/email/challenges', json={
        'email': email, 'terms_version': 'local-preview-v1', 'privacy_version': 'local-preview-v1'})
    assert result.status_code == 202, result.text
    challenge = result.json()['challenge_id']
    result = client.post('/api/v1/auth/email/verify', json={'challenge_id': challenge, 'code': '135790'})
    assert result.status_code == 200, result.text
    session = client.get('/api/v1/auth/session').json()
    client.headers['X-Bloomate-CSRF'] = session['csrf_token']
    return session, challenge


def plant(client, kind='goal'):
    response = client.post('/api/v1/plans', json={'kind': kind, 'goal': '隔离测试', 'availability': '十分钟'})
    assert response.status_code == 202, response.text
    job = response.json()['id']
    response = client.post('/api/v1/proposals/' + job + '/confirm')
    assert response.status_code == 200, response.text
    return job, response.json()['id']


def test_fail_closed_missing_identity_forged_owner_and_csrf(server):
    assert server.get('/api/v1/garden').status_code == 401
    assert server.get('/api/v1/garden', headers={'X-Owner-Id': 'local-user'}).status_code == 401
    assert server.post('/api/v1/auth/guest').status_code == 403
    info = guest(server)
    assert info['kind'] == 'guest'
    assert server.post('/api/v1/plans', headers={'X-Bloomate-CSRF': 'wrong'}, json={'kind': 'goal', 'goal': 'x'}).status_code == 403
    assert server.post('/api/v1/auth/guest', headers={'Origin': 'https://evil.invalid'}).status_code == 403
    assert server.get('/api/v1/garden').headers['cache-control'] == 'no-store'


def test_guest_content_and_memory_are_private_and_account_only(server):
    guest(server)
    job, pid = plant(server)
    assert server.get('/api/v1/memory').json() == {'enabled': False, 'items': []}
    assert server.put('/api/v1/memory/preference', json={'enabled': True}).status_code == 403
    with TestClient(main.app) as other:
        guest(other)
        assert other.get('/api/v1/garden').json()['plants'] == []
        for path in (f'/plants/{pid}/actions', f'/plants/{pid}/growth', f'/plans/{job}', f'/plants/{pid}/collection', f'/plants/{pid}/completion-notice'):
            assert other.get('/api/v1' + path).status_code == 404
        assert other.put(f'/api/v1/plants/{pid}/actions/0', json={'completed': True}).status_code == 404
    assert len(server.get('/api/v1/garden').json()['plants']) == 1


def test_verify_failure_attempts_are_persistent_and_replay_rejected(server):
    guest(server)
    response = server.post('/api/v1/auth/email/challenges', json={'email': 'a@example.com', 'terms_version': 'local-preview-v1', 'privacy_version': 'local-preview-v1'})
    challenge = response.json()['challenge_id']
    for _ in range(5):
        assert server.post('/api/v1/auth/email/verify', json={'challenge_id': challenge, 'code': '000000'}).status_code == 400
    main.init_db(); auth.init_schema()
    assert server.post('/api/v1/auth/email/verify', json={'challenge_id': challenge, 'code': '135790'}).status_code == 429
    assert server.post('/api/v1/auth/email/challenges', json={'email': 'a@example.com', 'terms_version': 'local-preview-v1', 'privacy_version': 'local-preview-v1'}).status_code == 429


def test_login_and_save_keep_ids_records_then_revoke_source(server):
    guest(server)
    original_guest = server.cookies.get(auth.GUEST_COOKIE)
    job, pid = plant(server)
    server.put(f'/api/v1/plants/{pid}/actions/0', json={'completed': True})
    info, challenge = login(server)
    assert info['kind'] == 'account' and info['pending_save']
    assert server.get('/api/v1/garden').json()['plants'] == []
    preview = server.post('/api/v1/auth/guest-save/preview').json()
    assert preview['plant_count'] == 1
    main.init_db(); auth.init_schema()
    result = server.post('/api/v1/auth/guest-save/' + preview['operation_id'] + '/confirm', json={'version': preview['version']})
    assert result.status_code == 200, result.text
    assert result.json()['plant_ids'] == [pid]
    assert server.get('/api/v1/garden').json()['plants'][0]['id'] == pid
    assert server.get(f'/api/v1/plants/{pid}/actions').json()['actions'][0]['completed']
    assert server.get('/api/v1/plans/' + job).status_code == 200
    assert server.post('/api/v1/auth/guest-save/' + preview['operation_id'] + '/confirm', json={'version': preview['version']}).json()['already_applied']
    with TestClient(main.app) as stale:
        stale.cookies.set(auth.GUEST_COOKIE, original_guest)
        assert stale.get('/api/v1/garden').status_code == 401
    assert server.post('/api/v1/auth/email/verify', json={'challenge_id': challenge, 'code': '135790'}).status_code in (404, 409)


def test_existing_account_append_and_logout_revocation(server):
    guest(server)
    login(server)
    _, original = plant(server)
    raw_account = server.cookies.get(auth.ACCOUNT_COOKIE)
    assert server.post('/api/v1/auth/logout').status_code == 200
    assert server.get('/api/v1/garden').status_code == 401
    with TestClient(main.app) as stolen:
        stolen.cookies.set(auth.ACCOUNT_COOKIE, raw_account)
        assert stolen.get('/api/v1/garden').status_code == 401
    # Advance rate-limit clock without resetting or bypassing its persisted counters.
    with auth.connect() as db:
        db.execute('UPDATE auth_challenges SET created_at=created_at-61')
    guest(server)
    _, added = plant(server)
    login(server)
    preview = server.post('/api/v1/auth/guest-save/preview').json()
    server.post('/api/v1/auth/guest-save/' + preview['operation_id'] + '/confirm', json={'version': preview['version']})
    assert {p['id'] for p in server.get('/api/v1/garden').json()['plants']} == {original, added}


def test_account_b_cannot_claim_guest_or_poll_a_save(server):
    guest(server); _, pid = plant(server); login(server)
    preview = server.post('/api/v1/auth/guest-save/preview').json()
    with TestClient(main.app) as other:
        guest(other); login(other, 'b@example.com')
        assert other.get('/api/v1/auth/guest-save/' + preview['operation_id']).status_code == 404
        assert other.post('/api/v1/auth/guest-save/' + preview['operation_id'] + '/confirm', json={'version': preview['version']}).status_code == 404
        assert other.get(f'/api/v1/plants/{pid}/actions').status_code == 404


def test_expiry_and_cleanup_do_not_touch_accounts_or_legacy_data(server):
    guest(server); _, pid = plant(server)
    owner = auth.session_for(server.cookies.get(auth.GUEST_COOKIE))['owner_id']
    with auth.connect() as db:
        db.execute('INSERT INTO auth_subjects(id,kind,created_at) VALUES (\'keep\',\'account\',?)', (auth.now(),))
        db.execute('UPDATE auth_subjects SET expires_at=? WHERE id=?', (auth.now() - 1, owner))
    assert server.get('/api/v1/garden').status_code == 401
    auth.cleanup_expired_guests()
    auth.cleanup_expired_guests()
    with auth.connect() as db:
        assert not db.execute('SELECT 1 FROM plants WHERE id=?', (pid,)).fetchone()
        assert db.execute("SELECT 1 FROM auth_subjects WHERE id='keep'").fetchone()
        assert not db.execute('PRAGMA foreign_key_check').fetchone()


def test_no_config_never_reports_email_sent(server, monkeypatch):
    guest(server)
    monkeypatch.delenv('CLOUDBASE_ENV_ID')
    response = server.post('/api/v1/auth/email/challenges', json={'email': 'a@example.com', 'terms_version': 'local-preview-v1', 'privacy_version': 'local-preview-v1'})
    assert response.status_code == 503 and response.json()['error']['code'] == 'AUTH_NOT_CONFIGURED'
    assert 'challenge_id' not in response.json()


def test_concurrent_users_have_distinct_background_job_owners(server):
    def work(email):
        with TestClient(main.app) as client:
            guest(client); login(client, email)
            job, pid = plant(client)
            return job, pid, auth.resolve_session(type('R', (), {'cookies': client.cookies})())['owner_id']
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, ['a@example.com', 'b@example.com']))
    assert results[0][2] != results[1][2]
    with auth.connect() as db:
        for job, pid, owner in results:
            assert db.execute('SELECT owner_id FROM jobs WHERE id=?', (job,)).fetchone()[0] == owner
            assert db.execute('SELECT owner_id FROM plants WHERE id=?', (pid,)).fetchone()[0] == owner


def test_provider_transport_checks_signup_and_masks_failure(server, monkeypatch):
    sent = []
    class Response:
        status_code = 200
        def __init__(self, data): self.data = data
        def json(self): return self.data
    def post(url, **kwargs):
        sent.append((url, kwargs['json']))
        return Response({'verification_token': 'test-proof'} if url.endswith('/verify') else {'sub': 'verified-sub'})
    monkeypatch.setattr(auth.httpx, 'post', post)
    # Use original provider method rather than this fixture's authentication stub.
    from importlib import util
    from pathlib import Path
    spec = util.spec_from_file_location('provider_contract', Path(auth.__file__))
    provider = util.module_from_spec(spec); spec.loader.exec_module(provider)
    assert provider.verify_identity({'provider_id': 'challenge', 'is_user': False, 'email': 'a@example.com'}, '135790') == 'verified-sub'
    assert sent[-1][0].endswith('/signup') and 'password' not in sent[-1][1]
    assert sent[-1][1]['email'] == 'a@example.com'


def test_every_business_route_requires_identity_before_validation(server):
    methods_checked = 0
    for route in main.app.routes:
        path = getattr(route, 'path', '')
        if not path.startswith('/api/v1/') or path.startswith('/api/v1/auth/'):
            continue
        if path == '/api/v1/legal':
            # This new public document route must remain read-only and credential-free.
            assert route.methods == {'GET'}
            public = server.get(path)
            assert public.status_code == 200 and 'terms' in public.json()
            assert 'csrf_token' not in public.json()
            continue
        import re
        url = re.sub(r'\{[^}]+\}', 'unknown', path)
        for method in route.methods:
            if method in ('HEAD', 'OPTIONS'):
                continue
            response = server.request(method, url, json={})
            assert response.status_code == 401, (method, path, response.text)
            methods_checked += 1
    assert methods_checked == 79  # Existing 68, five queries and two actions each for drafts/changes/records.


def test_failed_transfer_rolls_back_and_retry_uses_original_operation(server):
    guest(server); _, pid = plant(server); login(server)
    preview = server.post('/api/v1/auth/guest-save/preview').json()
    with auth.connect() as db:
        original_owner = db.execute('SELECT owner_id FROM plants WHERE id=?', (pid,)).fetchone()[0]
        db.execute("CREATE TRIGGER reject_transfer BEFORE UPDATE OF owner_id ON plants BEGIN SELECT RAISE(ABORT,'test storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        server.post('/api/v1/auth/guest-save/' + preview['operation_id'] + '/confirm', json={'version': preview['version']})
    with auth.connect() as db:
        assert db.execute('SELECT owner_id FROM plants WHERE id=?', (pid,)).fetchone()[0] == original_owner
        assert db.execute('SELECT status FROM auth_saves WHERE id=?', (preview['operation_id'],)).fetchone()[0] == 'pending'
        assert not db.execute('PRAGMA foreign_key_check').fetchone()
        db.execute('DROP TRIGGER reject_transfer')
    assert server.post('/api/v1/auth/guest-save/' + preview['operation_id'] + '/confirm', json={'version': preview['version']}).status_code == 200


def test_session_cookie_flags_and_no_token_in_response(server, monkeypatch):
    monkeypatch.setenv('BLOOMATE_COOKIE_SECURE', '1')
    response = server.post('/api/v1/auth/guest', headers=ORIGIN)
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie
    body = response.json()
    assert all(key not in body for key in ('access_token','refresh_token','session_token','raw'))
    raw = response.cookies.get(auth.GUEST_COOKIE)
    with auth.connect() as db:
        assert db.execute('SELECT digest FROM auth_sessions').fetchone()[0] != raw


def test_expired_account_never_falls_back_to_valid_guest(server):
    guest(server); login(server)
    with auth.connect() as db:
        db.execute('UPDATE auth_sessions SET expires_at=? WHERE digest=?', (auth.now()-1, auth.digest(server.cookies.get(auth.ACCOUNT_COOKIE))))
    assert server.get('/api/v1/garden').status_code == 401
    assert server.get('/api/v1/auth/session').json()['kind'] == 'none'


def test_empty_login_does_not_force_empty_transfer(server):
    guest(server); session, _ = login(server)
    assert session['pending_save'] is False
    assert server.post('/api/v1/auth/guest-save/preview').json()['status'] == 'empty'


def test_guest_limit_and_global_model_budget_cannot_reset_per_account(server, monkeypatch):
    monkeypatch.setenv('MODEL_CALL_RESERVE_CENTS','2500')
    guest(server); login(server)
    with main.acting_as(auth.session_for(server.cookies.get(auth.ACCOUNT_COOKIE))['owner_id']):
        main.reserve_budget()
    with TestClient(main.app) as other:
        guest(other); login(other,'b@example.com')
        with main.acting_as(auth.session_for(other.cookies.get(auth.ACCOUNT_COOKIE))['owner_id']):
            main.reserve_budget()
            with pytest.raises(RuntimeError,match='BUDGET_EXHAUSTED'):
                main.reserve_budget()


def test_source_limit_is_persisted_not_bypassed_with_new_cookie(server):
    with auth.connect() as db:
        for _ in range(60):
            db.execute('INSERT INTO auth_request_limits VALUES (?,?,?)', (auth.digest('source:testclient'),'guest',auth.now()))
    assert server.post('/api/v1/auth/guest',headers=ORIGIN).status_code == 429


def test_expired_queued_work_finishes_without_model_and_can_be_cleaned(server, monkeypatch):
    guest(server); job, pid = plant(server)
    owner = auth.session_for(server.cookies.get(auth.GUEST_COOKIE))['owner_id']
    with auth.connect() as db:
        db.execute("UPDATE jobs SET status='pending' WHERE id=?", (job,))
        db.execute('UPDATE auth_subjects SET expires_at=? WHERE id=?', (auth.now()-1,owner))
    def should_not_generate(_data):
        pytest.fail('expired guest invoked model')
    monkeypatch.setattr(main,'generate',should_not_generate)
    main.run_owned(owner, main.process, job, {})
    with auth.connect() as db:
        assert db.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()[0] == 'error'
    auth.cleanup_expired_guests()
    with auth.connect() as db:
        assert not db.execute('SELECT 1 FROM plants WHERE id=?',(pid,)).fetchone()
