"""Email authentication, revocable sessions and ownership transfer.

No development login endpoint, default OTP or plaintext credential logging.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.identity import acting_as

ACCOUNT_COOKIE = 'bloomate_session'
GUEST_COOKIE = 'bloomate_guest'
DAY = 86400
SCHEMA = '''
CREATE TABLE IF NOT EXISTS auth_subjects (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('guest','account')),
 provider_sub TEXT UNIQUE, email TEXT, created_at INTEGER NOT NULL,
 expires_at INTEGER, migrated_to TEXT, bound_account TEXT);
CREATE TABLE IF NOT EXISTS auth_sessions (
 digest TEXT PRIMARY KEY, owner_id TEXT NOT NULL, created_at INTEGER NOT NULL,
 expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS auth_challenges (
 id TEXT PRIMARY KEY, session_digest TEXT NOT NULL, email TEXT NOT NULL,
 provider_id TEXT, is_user INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, terms_version TEXT NOT NULL, privacy_version TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS auth_challenge_email_time ON auth_challenges(email,created_at);
CREATE TABLE IF NOT EXISTS auth_saves (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, guest_id TEXT NOT NULL,
 version TEXT NOT NULL, status TEXT NOT NULL, plant_ids TEXT NOT NULL,
 created_at INTEGER NOT NULL, UNIQUE(account_id,guest_id));
CREATE TABLE IF NOT EXISTS auth_request_limits (
 source_hash TEXT NOT NULL, kind TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS auth_request_source_time ON auth_request_limits(source_hash,kind,created_at);
'''


def connect():
    from app.main import connect as business_connect
    return business_connect()


def now():
    return int(time.time())


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def mask_email(email):
    local, _, domain = (email or '').partition('@')
    return f'{local[:2]}***@{domain}' if domain else None


class AuthError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def reject(code, message, status=400):
    return JSONResponse({'error': {'code': code, 'message': message}}, status_code=status,
                        headers={'Cache-Control': 'no-store'})


def init_schema():
    with connect() as db:
        db.executescript(SCHEMA)


def configured():
    return os.environ.get('BLOOMATE_EMAIL_ENABLED') == '1' and bool(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]{2,99}', os.environ.get('CLOUDBASE_ENV_ID', '')))


def provider_request(path, payload):
    if not configured():
        raise AuthError('AUTH_NOT_CONFIGURED', '邮箱登录服务尚未配置，体验内容仍会保留。', 503)
    env_id = os.environ['CLOUDBASE_ENV_ID']
    try:
        response = httpx.post(f'https://{env_id}.api.tcloudbasegateway.com/auth/v1/{path}',
                              json=payload, timeout=15, follow_redirects=False)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError('invalid auth response')
        if response.status_code >= 400:
            code = str(result.get('error', ''))
            if response.status_code == 429 or code == 'resource_exhausted':
                raise AuthError('RATE_LIMITED', '操作较频繁，请稍后再试。', 429)
            if code in ('invalid_verification_code', 'invalid_code', 'invalid_argument') and path == 'verification/verify':
                raise AuthError('CODE_INVALID', '验证码不正确或已过期，请核对或重新获取。')
            if code in ('code_expired', 'verification_expired'):
                raise AuthError('CODE_EXPIRED', '验证码已过期，请重新获取。', 410)
            if code in ('captcha_required', 'captcha_invalid'):
                raise AuthError('AUTH_CAPTCHA_REQUIRED', '邮件服务要求额外验证，请稍后重试；当前内容仍保留。', 503)
            raise AuthError('AUTH_UNAVAILABLE', '邮箱验证暂不可用，请稍后重试。', 503)
        return result
    except (httpx.HTTPError, ValueError, TypeError):
        raise AuthError('AUTH_UNAVAILABLE', '邮箱验证暂时无法连接，请稍后重试。', 503) from None


def send_verification(email):
    result = provider_request('verification', {'email': email, 'target': 'ANY'})
    provider_id, ttl = result.get('verification_id'), result.get('expires_in')
    if not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 4096 or type(ttl) is not int or ttl <= 0:
        raise AuthError('AUTH_UNAVAILABLE', '邮件服务返回异常，请稍后重试。', 503)
    return provider_id, min(ttl, 300), result.get('is_user') is True


def verify_identity(challenge, code):
    result = provider_request('verification/verify', {
        'verification_id': challenge['provider_id'], 'verification_code': code})
    verification_token = result.get('verification_token')
    if not isinstance(verification_token, str) or not 1 <= len(verification_token) <= 8192:
        raise AuthError('AUTH_UNAVAILABLE', '邮件验证返回异常，请重新获取验证码。', 503)
    payload = {'verification_token': verification_token}
    if challenge['is_user']:
        result = provider_request('signin', payload)
    else:
        result = provider_request('signup', {**payload, 'email': challenge['email']})
    sub = result.get('sub')
    if not isinstance(sub, str) or not 1 <= len(sub) <= 200:
        raise AuthError('AUTH_UNAVAILABLE', '无法确认账号身份，请重新获取验证码。', 503)
    return sub


def create_session(db, owner, expires_at):
    raw = secrets.token_urlsafe(32)
    db.execute('INSERT INTO auth_sessions VALUES (?,?,?,?,0)', (digest(raw), owner, now(), expires_at))
    return raw


def csrf(raw):
    return digest('csrf:' + raw)


def cookie_value(request, name):
    return request.cookies.get(name, '')


def session_for(raw, allow_bound=False):
    if not raw or len(raw) > 200:
        return None
    with connect() as db:
        row = db.execute('''SELECT s.digest,s.owner_id,s.expires_at AS session_expires,
            p.kind,p.email,p.expires_at,p.migrated_to,p.bound_account
            FROM auth_sessions s JOIN auth_subjects p ON p.id=s.owner_id
            WHERE s.digest=? AND s.revoked=0 AND s.expires_at>?''', (digest(raw), now())).fetchone()
    from app.account_privacy import is_erased
    if not row or row['migrated_to'] or (row['expires_at'] and row['expires_at'] <= now()) or is_erased(row['owner_id']):
        return None
    if row['kind'] == 'guest' and row['bound_account'] and not allow_bound:
        return None
    return {**dict(row), 'raw': raw}


def resolve_session(request):
    account = cookie_value(request, ACCOUNT_COOKIE)
    # An expired account must never silently fall back to a different identity.
    return session_for(account) if account else session_for(cookie_value(request, GUEST_COOKIE))


def owner_active(owner):
    with connect() as db:
        row = db.execute('SELECT kind,expires_at,migrated_to FROM auth_subjects WHERE id=?', (owner,)).fetchone()
    from app.account_privacy import is_erased
    return bool(row and not row['migrated_to'] and (not row['expires_at'] or row['expires_at'] > now()) and not is_erased(owner))


def owner_is_guest(owner):
    with connect() as db:
        row = db.execute('SELECT kind FROM auth_subjects WHERE id=?', (owner,)).fetchone()
    return bool(row and row['kind'] == 'guest')


def allowed_origins():
    return set(filter(None, (item.strip() for item in os.environ.get(
        'BLOOMATE_ALLOWED_ORIGINS', 'http://127.0.0.1:3001,http://localhost:3001').split(','))))


def check_origin(request):
    if request.headers.get('origin') not in allowed_origins() or request.headers.get('sec-fetch-site') == 'cross-site':
        raise AuthError('ORIGIN_DENIED', '请求来源无效，请从应用页面重新操作。', 403)


def reserve_source_request(db, request, kind, limit):
    source = request.client.host if request.client else 'unknown'
    shared = os.environ.get('BLOOMATE_PROXY_SHARED_SECRET', '')
    if shared and hmac.compare_digest(request.headers.get('x-proxy-secret', ''), shared):
        try:
            source = str(ipaddress.ip_address(request.headers.get('x-client-ip', '')))
        except ValueError:
            raise AuthError('ORIGIN_DENIED', '请求来源无效。', 403) from None
    source_hash = digest('source:' + source)
    db.execute('DELETE FROM auth_request_limits WHERE created_at<?', (now() - DAY,))
    count = db.execute('SELECT count(*) FROM auth_request_limits WHERE source_hash=? AND kind=? AND created_at>?',
                       (source_hash, kind, now() - 3600)).fetchone()[0]
    if count >= limit:
        raise AuthError('RATE_LIMITED', '操作较频繁，请稍后再试。', 429)
    db.execute('INSERT INTO auth_request_limits VALUES (?,?,?)', (source_hash, kind, now()))


def set_cookie(response, name, value, lifetime):
    secure = os.environ.get('BLOOMATE_COOKIE_SECURE', '1') == '1'
    if not secure and os.environ.get('BLOOMATE_APP_MODE') != 'local':
        raise AuthError('AUTH_CONFIG_INVALID', '登录服务安全配置不完整。', 503)
    response.set_cookie(name, value, max_age=lifetime, path='/', secure=secure,
                        httponly=True, samesite='lax')


def pending_guest(request, account):
    guest = session_for(cookie_value(request, GUEST_COOKIE), allow_bound=True)
    if not guest or guest['kind'] != 'guest' or guest['bound_account'] != account:
        return None
    return guest


def session_info(request):
    from app.legal import agreement_required
    session = resolve_session(request)
    if not session:
        return {'kind': 'none', 'email_masked': None, 'guest_expires_at': None,
                'csrf_token': None, 'cache_scope': None, 'pending_save': False, 'email_enabled': configured()}
    guest = pending_guest(request, session['owner_id']) if session['kind'] == 'account' else None
    with connect() as db:
        pending = bool(guest and db.execute('SELECT 1 FROM jobs WHERE owner_id=? LIMIT 1', (guest['owner_id'],)).fetchone())
    return {'kind': session['kind'], 'email_masked': mask_email(session['email']),
            'guest_expires_at': iso(guest['expires_at'] if guest else session['expires_at']),
            'csrf_token': csrf(session['raw']), 'cache_scope': digest(session['owner_id'])[:24],
            'pending_save': pending, 'email_enabled': configured(),
            'agreements_required': session['kind'] == 'account' and agreement_required(session['owner_id'])}


class EmailInput(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    terms_version: str = Field(min_length=1, max_length=40)
    privacy_version: str = Field(min_length=1, max_length=40)

    @model_validator(mode='after')
    def validate_email(self):
        self.email = self.email.strip().lower()
        if not re.fullmatch(r'[a-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}', self.email):
            raise ValueError('invalid email')
        from app.legal import agreement_versions_valid
        if not agreement_versions_valid(self.terms_version, self.privacy_version):
            raise ValueError('unsupported agreement version')
        return self


class VerifyInput(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=80)
    code: str = Field(pattern=r'^\d{6}$')


class ConfirmInput(BaseModel):
    version: str = Field(min_length=1, max_length=80)


def require_account(request):
    session = request.state.auth
    if not session or session['kind'] != 'account':
        raise AuthError('AUTH_REQUIRED', '请先登录后继续。', 401)
    return session['owner_id']


BUSINESS_WITHOUT_OWNER = {'habit_occurrences', 'habit_events', 'model_call_metrics'}


def owned_tables(db):
    tables = [r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return [name for name in tables if not name.startswith(('auth_', 'sqlite_'))
            and 'owner_id' in {r['name'] for r in db.execute(f'PRAGMA table_info("{name}")')}]


def guest_version(db, owner):
    content = []
    for name in sorted(owned_tables(db)):
        rows = db.execute(f'SELECT * FROM "{name}" WHERE owner_id=? ORDER BY rowid', (owner,)).fetchall()
        content.append((name, [dict(row) for row in rows]))
    for name in ('habit_occurrences', 'habit_events'):
        content.append((name, [dict(row) for row in db.execute(f'''SELECT * FROM {name}
            WHERE stage_id IN (SELECT id FROM habit_stages WHERE owner_id=?) ORDER BY rowid''', (owner,))]))
    return digest(json.dumps(content, sort_keys=True, ensure_ascii=False))


def has_running_work(db, owner):
    return any(db.execute(f'SELECT 1 FROM {table} WHERE owner_id=? AND status IN (\'pending\',\'running\')', (owner,)).fetchone()
               for table in ('jobs', 'plan_adjustments', 'growth_suggestions', 'habit_advice', 'habit_plan_revisions'))


def cleanup_expired_guests():
    """Idempotent physical cleanup; access expires independently of this task."""
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM auth_challenges WHERE expires_at<?', (now() - DAY,))
        db.execute('DELETE FROM auth_sessions WHERE expires_at<?', (now() - DAY,))
        db.execute('DELETE FROM auth_request_limits WHERE created_at<?', (now() - DAY,))
        db.execute('DELETE FROM auth_deletion_challenges WHERE expires_at<?', (now() - DAY,))
        expired = db.execute("SELECT id FROM auth_subjects WHERE kind='guest' AND expires_at<=? AND migrated_to IS NULL", (now(),)).fetchall()
        for subject in expired:
            owner = subject['id']
            if has_running_work(db, owner):
                continue
            db.execute('PRAGMA defer_foreign_keys=ON')
            for table in ('habit_events', 'habit_occurrences'):
                db.execute(f'DELETE FROM {table} WHERE stage_id IN (SELECT id FROM habit_stages WHERE owner_id=?)', (owner,))
            db.execute('DELETE FROM model_call_metrics WHERE reservation_id IN (SELECT id FROM usage_reservations WHERE owner_id=?)', (owner,))
            for table in owned_tables(db):
                # Preserve minimal cost reservations so expiry cannot reset the global monthly budget.
                if table != 'usage_reservations':
                    db.execute(f'DELETE FROM "{table}" WHERE owner_id=?', (owner,))
            db.execute('DELETE FROM auth_challenges WHERE session_digest IN (SELECT digest FROM auth_sessions WHERE owner_id=?)', (owner,))
            db.execute('DELETE FROM auth_sessions WHERE owner_id=?', (owner,))
            db.execute("UPDATE auth_saves SET status='expired',plant_ids='[]' WHERE guest_id=? AND status!='succeeded'", (owner,))
            db.execute('DELETE FROM auth_subjects WHERE id=?', (owner,))


def install(app):
    @app.exception_handler(AuthError)
    async def auth_error(_request, exc):
        return reject(exc.code, exc.message, exc.status)

    @app.middleware('http')
    async def identity_middleware(request, call_next):
        if not request.url.path.startswith('/api/'):
            return await call_next(request)
        try:
            session = resolve_session(request)
            request.state.auth = session
            public = (request.method, request.url.path) in {
                ('GET', '/api/v1/auth/session'), ('POST', '/api/v1/auth/guest'), ('POST', '/api/v1/auth/logout'),
                ('GET', '/api/v1/legal')}
            public = public or (request.method == 'GET' and request.url.path.startswith('/api/v1/auth/deletion-status/'))
            if not public and not session:
                raise AuthError('AUTH_REQUIRED', '登录或体验已到期，请重新进入。', 401)
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                check_origin(request)
                if session and request.url.path != '/api/v1/auth/guest' and not hmac.compare_digest(
                        request.headers.get('x-bloomate-csrf', ''), csrf(session['raw'])):
                    raise AuthError('CSRF_INVALID', '页面状态已变化，请刷新后再操作。', 403)
            if session and not public and not request.url.path.startswith('/api/v1/auth/') and os.environ.get('BLOOMATE_APP_MODE') != 'local':
                from app.legal import public_ready, agreement_required
                if not public_ready():
                    raise AuthError('LEGAL_NOT_CONFIGURED', '服务公示信息尚未完成；你仍可导出或注销账号。', 503)
                if session['kind'] == 'account' and agreement_required(session['owner_id']):
                    raise AuthError('AGREEMENT_REQUIRED', '请先在账号与隐私中阅读并同意当前条款；你仍可导出或注销。', 403)
            # Guest privacy: no account-level long-term memory or proactive-care settings.
            if session and session['kind'] == 'guest':
                if request.url.path == '/api/v1/memory' and request.method == 'GET':
                    return JSONResponse({'enabled': False, 'items': []}, headers={'Cache-Control': 'no-store'})
                if request.url.path == '/api/v1/care' and request.method == 'GET':
                    return JSONResponse({'enabled': False, 'messages': []}, headers={'Cache-Control': 'no-store'})
                if request.url.path == '/api/v1/care/refresh':
                    return JSONResponse({'created': 0}, headers={'Cache-Control': 'no-store'})
                if request.url.path.startswith(('/api/v1/memory', '/api/v1/care')):
                    raise AuthError('ACCOUNT_REQUIRED', '保存到账号后，才能管理园丁记忆和关心。', 403)
            if session:
                with acting_as(session['owner_id']):
                    response = await call_next(request)
            else:
                response = await call_next(request)
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Vary'] = 'Cookie'
            return response
        except AuthError as exc:
            return reject(exc.code, exc.message, exc.status)

    @app.get('/api/v1/auth/session')
    def get_session(request: Request):
        return session_info(request)

    @app.post('/api/v1/auth/guest', status_code=201)
    def start_guest(request: Request):
        from app.legal import public_ready
        if os.environ.get('BLOOMATE_APP_MODE') != 'local' and not public_ready():
            raise AuthError('LEGAL_NOT_CONFIGURED', '服务公示信息尚未完成，请稍后再试。', 503)
        if request.state.auth:
            return session_info(request)
        # Invalid/expired credentials are never reattached to their old business data.
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            reserve_source_request(db, request, 'guest', 60)
            owner = 'guest_' + uuid.uuid4().hex
            expiry = now() + 7 * DAY
            db.execute('INSERT INTO auth_subjects(id,kind,created_at,expires_at) VALUES (?,\'guest\',?,?)', (owner, now(), expiry))
            raw = create_session(db, owner, expiry)
        response = JSONResponse({'kind': 'guest', 'email_masked': None, 'guest_expires_at': iso(expiry),
                                 'csrf_token': csrf(raw), 'cache_scope': digest(owner)[:24],
                                 'pending_save': False, 'email_enabled': configured()}, status_code=201)
        set_cookie(response, GUEST_COOKIE, raw, 7 * DAY)
        response.delete_cookie(ACCOUNT_COOKIE, path='/')
        return response

    @app.post('/api/v1/auth/email/challenges', status_code=202)
    def email_challenge(data: EmailInput, request: Request):
        from app.legal import public_ready
        if os.environ.get('BLOOMATE_APP_MODE') != 'local' and not public_ready():
            raise AuthError('LEGAL_NOT_CONFIGURED', '服务公示信息尚未完成，请稍后再试。', 503)
        if not configured():
            raise AuthError('AUTH_NOT_CONFIGURED', '邮箱登录服务尚未配置，体验内容仍会保留。', 503)
        session = request.state.auth
        challenge_id, created = uuid.uuid4().hex, now()
        # Reserve BEFORE sending; failed/unknown sends also consume cooldown and rate quota.
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            reserve_source_request(db, request, 'email', 30)
            recent = db.execute('SELECT 1 FROM auth_challenges WHERE (email=? OR session_digest=?) AND created_at>?', (data.email, session['digest'], created - 60)).fetchone()
            count = db.execute('SELECT count(*) FROM auth_challenges WHERE (email=? OR session_digest=?) AND created_at>?', (data.email, session['digest'], created - DAY)).fetchone()[0]
            global_count = db.execute('''SELECT (SELECT count(*) FROM auth_challenges WHERE created_at>?) +
                (SELECT count(*) FROM auth_deletion_challenges WHERE created_at>?)''', (created - DAY, created - DAY)).fetchone()[0]
            limit = max(0, min(int(os.environ.get('BLOOMATE_EMAIL_DAILY_LIMIT', '100')), 10000))
            if recent or count >= 10 or global_count >= limit:
                raise AuthError('RATE_LIMITED', '获取验证码较频繁，请稍后再试。', 429)
            db.execute("UPDATE auth_challenges SET status='superseded' WHERE session_digest=? AND status IN ('ready','sending')", (session['digest'],))
            db.execute('INSERT INTO auth_challenges VALUES (?,?,?,?,?,\'sending\',?,?,0,?,?)',
                       (challenge_id, session['digest'], data.email, None, 0, created, created + 300, data.terms_version, data.privacy_version))
        try:
            provider_id, ttl, existing = send_verification(data.email)
            with connect() as db:
                updated = db.execute("UPDATE auth_challenges SET provider_id=?,is_user=?,status='ready',expires_at=? WHERE id=? AND status='sending'", (provider_id, int(existing), created + ttl, challenge_id))
                if not updated.rowcount:
                    raise AuthError('CHALLENGE_CHANGED', '邮箱输入已变化，请重新获取验证码。', 409)
            return {'challenge_id': challenge_id, 'expires_at': iso(created + ttl), 'resend_after_seconds': 60}
        except AuthError:
            with connect() as db:
                db.execute("UPDATE auth_challenges SET status='send_failed' WHERE id=? AND status='sending'", (challenge_id,))
            raise

    @app.post('/api/v1/auth/email/verify')
    def email_verify(data: VerifyInput, request: Request):
        session = request.state.auth
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM auth_challenges WHERE id=? AND session_digest=?', (data.challenge_id, session['digest'])).fetchone()
            if not row:
                raise AuthError('NOT_FOUND', '没有找到有效验证码，请重新获取。', 404)
            if row['expires_at'] <= now():
                raise AuthError('CODE_EXPIRED', '验证码已过期，请重新获取。', 410)
            if row['attempts'] >= 5:
                raise AuthError('TOO_MANY_ATTEMPTS', '尝试次数已用完，请重新获取验证码。', 429)
            if row['status'] != 'ready':
                raise AuthError('CHALLENGE_USED', '验证码已使用或状态已变化，请重新获取。', 409)
            db.execute("UPDATE auth_challenges SET status='verifying',attempts=attempts+1 WHERE id=?", (row['id'],))
        try:
            sub = verify_identity(row, data.code)
        except AuthError:
            with connect() as db:
                db.execute("UPDATE auth_challenges SET status='ready' WHERE id=? AND status='verifying'", (row['id'],))
            raise
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            account = db.execute("SELECT id FROM auth_subjects WHERE provider_sub=? AND kind='account'", (sub,)).fetchone()
            owner = account['id'] if account else 'user_' + uuid.uuid4().hex
            if not account:
                db.execute('INSERT INTO auth_subjects(id,kind,provider_sub,email,created_at) VALUES (?,\'account\',?,?,?)', (owner, sub, row['email'], now()))
            db.execute('INSERT OR IGNORE INTO auth_policy_acceptances VALUES (?,?,?,?)',
                       (owner, row['terms_version'], row['privacy_version'], now()))
            raw = create_session(db, owner, now() + 30 * DAY)
            if session['kind'] == 'guest':
                db.execute('UPDATE auth_subjects SET bound_account=? WHERE id=? AND kind=\'guest\' AND migrated_to IS NULL', (owner, session['owner_id']))
            else:
                db.execute('UPDATE auth_sessions SET revoked=1 WHERE digest=?', (session['digest'],))
            db.execute("UPDATE auth_challenges SET status='consumed' WHERE id=?", (row['id'],))
        response = JSONResponse({'kind': 'account', 'save_required': session['kind'] == 'guest'})
        set_cookie(response, ACCOUNT_COOKIE, raw, 30 * DAY)
        return response

    @app.post('/api/v1/auth/guest-save/preview', status_code=201)
    def preview_save(request: Request):
        owner = require_account(request)
        guest = pending_guest(request, owner)
        if not guest:
            return {'status': 'empty', 'plant_count': 0}
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM auth_saves WHERE account_id=? AND guest_id=?', (owner, guest['owner_id'])).fetchone()
            if not db.execute('SELECT 1 FROM jobs WHERE owner_id=? LIMIT 1', (guest['owner_id'],)).fetchone():
                return {'status': 'empty', 'plant_count': 0}
            if has_running_work(db, guest['owner_id']):
                raise AuthError('WORK_RUNNING', '园丁仍在处理这次体验，请稍后再保存。', 409)
            ids = [row['id'] for row in db.execute('SELECT id FROM plants WHERE owner_id=? ORDER BY id', (guest['owner_id'],))]
            version = guest_version(db, guest['owner_id'])
            operation_id = previous['id'] if previous else uuid.uuid4().hex
            if previous:
                db.execute("UPDATE auth_saves SET version=?,plant_ids=?,status='pending' WHERE id=?", (version, json.dumps(ids), operation_id))
            else:
                db.execute('INSERT INTO auth_saves VALUES (?,?,?,?,\'pending\',?,?)', (operation_id, owner, guest['owner_id'], version, json.dumps(ids), now()))
        return {'status': 'pending', 'operation_id': operation_id, 'account_masked': mask_email(request.state.auth['email']),
                'plant_count': len(ids), 'version': version, 'expires_at': iso(guest['expires_at'])}

    @app.get('/api/v1/auth/guest-save/{operation_id}')
    def get_save(operation_id: str, request: Request):
        owner = require_account(request)
        with connect() as db:
            row = db.execute('SELECT * FROM auth_saves WHERE id=? AND account_id=?', (operation_id, owner)).fetchone()
        if not row:
            raise AuthError('NOT_FOUND', '没有找到这次保存记录。', 404)
        return {'status': row['status'], 'plant_count': len(json.loads(row['plant_ids'])), 'retryable': row['status'] == 'pending'}

    @app.post('/api/v1/auth/guest-save/{operation_id}/confirm')
    def confirm_save(operation_id: str, data: ConfirmInput, request: Request):
        owner = require_account(request)
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM auth_saves WHERE id=? AND account_id=?', (operation_id, owner)).fetchone()
            if not row:
                raise AuthError('NOT_FOUND', '没有找到这次保存记录。', 404)
            if row['version'] != data.version:
                raise AuthError('SAVE_CHANGED', '体验内容有变化，请重新查看保存范围。', 409)
            if row['status'] == 'succeeded':
                return {'status': 'succeeded', 'plant_ids': json.loads(row['plant_ids']), 'already_applied': True}
            guest = pending_guest(request, owner)
            if not guest or guest['owner_id'] != row['guest_id']:
                raise AuthError('GUEST_UNAVAILABLE', '体验已到期或无法验证，请重新查看。', 410)
            if has_running_work(db, guest['owner_id']) or guest_version(db, guest['owner_id']) != row['version']:
                raise AuthError('SAVE_CHANGED', '体验内容有变化，请重新查看保存范围。', 409)
            for table in owned_tables(db):
                if table in ('memory_preferences', 'care_preferences'):
                    db.execute(f'DELETE FROM {table} WHERE owner_id=?', (guest['owner_id'],))
                else:
                    db.execute(f'UPDATE "{table}" SET owner_id=? WHERE owner_id=?', (owner, guest['owner_id']))
            db.execute('UPDATE auth_subjects SET migrated_to=? WHERE id=?', (owner, guest['owner_id']))
            db.execute('UPDATE auth_sessions SET revoked=1 WHERE owner_id=?', (guest['owner_id'],))
            db.execute("UPDATE auth_saves SET status='succeeded' WHERE id=?", (row['id'],))
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise AuthError('SAVE_FAILED', '保存未完成，原体验仍保留，请重试。', 503)
        response = JSONResponse({'status': 'succeeded', 'plant_ids': json.loads(row['plant_ids']), 'already_applied': False})
        response.delete_cookie(GUEST_COOKIE, path='/')
        return response

    @app.post('/api/v1/auth/logout')
    def logout(request: Request):
        with connect() as db:
            for name in (ACCOUNT_COOKIE, GUEST_COOKIE):
                raw = cookie_value(request, name)
                if raw:
                    db.execute('UPDATE auth_sessions SET revoked=1 WHERE digest=?', (digest(raw),))
        response = JSONResponse({'signed_out': True})
        response.delete_cookie(ACCOUNT_COOKIE, path='/')
        response.delete_cookie(GUEST_COOKIE, path='/')
        return response
