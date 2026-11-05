"""Application-scoped export and erasure; never delete a shared cloud identity."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app import auth
from app.identity import owner_context

TABLES = frozenset('''care_messages care_preferences completion_notices goal_action_events
goal_action_states goal_action_weights goal_collections goal_followup_dates goal_followups
goal_next_proposals growth_events growth_standards growth_suggestions habit_advice
habit_collections habit_lifecycle habit_next_proposals habit_plan_revisions
habit_schedule_proposals habit_stages jobs memory_preferences plan_adjustments
plant_pauses plants usage_reservations user_memories'''.split())
CONFIRMATION = '注销Bloomate账号'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS auth_deletion_challenges (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, session_digest TEXT NOT NULL,
 provider_id TEXT, is_user INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL,
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS auth_policy_acceptances (
 owner_id TEXT NOT NULL, terms_version TEXT NOT NULL, privacy_version TEXT NOT NULL,
 accepted_at INTEGER NOT NULL, PRIMARY KEY(owner_id,terms_version,privacy_version));
'''


def owner_hash(owner):
    return hashlib.sha256(('bloomate-erasure:' + owner).encode()).hexdigest()


def journal_path(database=None):
    if database is None:
        from app.main import DB
        database = DB
    return Path(str(database) + '.erasures.jsonl')


def journal_records(database=None):
    path = journal_path(database)
    if not path.exists():
        return []
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if any(not isinstance(r, dict) or not re.fullmatch(r'[a-f0-9]{64}', r.get('owner_hash', ''))
               or not re.fullmatch(r'[a-f0-9]{48}', r.get('operation_id', ''))
               or type(r.get('created_at')) is not int for r in rows):
            raise ValueError('invalid erasure record')
        return rows
    except (ValueError, OSError, TypeError):
        raise auth.AuthError('ERASURE_RECOVERY_REQUIRED', '账号清理记录暂时无法读取，请联系运营者。', 503) from None


def is_erased(owner, database=None):
    return any(row['owner_hash'] == owner_hash(owner) for row in journal_records(database))


class OwnedConnection(sqlite3.Connection):
    """Validate an admitted request again under its actual SQLite write lock."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_owner = owner_context.get()

    def validate_writer(self):
        owner = self.request_owner
        if not owner or not owner.startswith(('user_', 'guest_')):
            return
        row = super().execute('SELECT expires_at,migrated_to FROM auth_subjects WHERE id=?', (owner,)).fetchone()
        database = super().execute('PRAGMA database_list').fetchone()[2]
        if not row or row[1] or (row[0] and row[0] <= auth.now()) or is_erased(owner, database):
            raise auth.AuthError('AUTH_REQUIRED', '账号或体验已失效，请重新进入。', 401)

    def execute(self, sql, parameters=()):
        command = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ''
        if command == 'BEGIN':
            result = super().execute(sql, parameters)
            self.validate_writer()
            return result
        if command in ('INSERT', 'UPDATE', 'DELETE', 'REPLACE') and not self.in_transaction and self.request_owner:
            super().execute('BEGIN IMMEDIATE')
            self.validate_writer()
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        if not self.in_transaction and self.request_owner:
            super().execute('BEGIN IMMEDIATE')
            self.validate_writer()
        return super().executemany(sql, parameters)


def init_schema():
    with auth.connect() as db:
        db.executescript(SCHEMA)
        db.execute('''INSERT OR IGNORE INTO auth_policy_acceptances
            SELECT s.id,c.terms_version,c.privacy_version,MIN(c.created_at)
            FROM auth_subjects s JOIN auth_challenges c ON c.email=s.email
            WHERE s.kind='account' AND c.status='consumed'
            GROUP BY s.id,c.terms_version,c.privacy_version''')


def check_tables(db):
    actual = set(auth.owned_tables(db))
    business = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not r[0].startswith(('auth_', 'sqlite_'))}
    if actual != TABLES or business != TABLES | auth.BUSINESS_WITHOUT_OWNER:
        raise auth.AuthError('ACCOUNT_SCHEMA_CHANGED', '数据结构已变化，请联系运营者完成导出与注销适配。', 503)


def related_owners(db, owner):
    return [owner] + [r['id'] for r in db.execute(
        "SELECT id FROM auth_subjects WHERE kind='guest' AND (bound_account=? OR migrated_to=?)", (owner, owner))]


def summary(db, owner):
    subjects = related_owners(db, owner)
    return {'plant_count': sum(db.execute('SELECT count(*) FROM plants WHERE owner_id=?', (o,)).fetchone()[0] for o in subjects),
            'memory_count': db.execute('SELECT count(*) FROM user_memories WHERE owner_id=?', (owner,)).fetchone()[0],
            'linked_guest_count': len(subjects) - 1,
            'running_work': any(auth.has_running_work(db, o) for o in subjects),
            'shared_auth_identity_deleted': False,
            'identity_notice': '注销仅删除Bloomate账号和数据，不删除其他产品共用的CloudBase认证身份。共享认证记录的删除请联系运营者单独处理。'}


def export_data(db, owner):
    check_tables(db)
    account = db.execute('SELECT email,created_at FROM auth_subjects WHERE id=?', (owner,)).fetchone()
    data = {}
    for table in sorted(TABLES):
        rows = db.execute(f'SELECT * FROM "{table}" WHERE owner_id=? ORDER BY rowid', (owner,)).fetchall()
        data[table] = [{k: v for k, v in dict(r).items() if k != 'owner_id'} for r in rows]
    for table in ('habit_events', 'habit_occurrences'):
        data[table] = [dict(r) for r in db.execute(f'''SELECT * FROM {table} WHERE stage_id IN
            (SELECT id FROM habit_stages WHERE owner_id=?) ORDER BY rowid''', (owner,))]
    data['model_call_metrics'] = [dict(r) for r in db.execute('''SELECT * FROM model_call_metrics WHERE reservation_id IN
        (SELECT id FROM usage_reservations WHERE owner_id=?) ORDER BY rowid''', (owner,))]
    agreements = [dict(r) for r in db.execute('''SELECT terms_version,privacy_version,accepted_at
        FROM auth_policy_acceptances WHERE owner_id=? ORDER BY accepted_at''', (owner,))]
    return {'format': 'bloomate-personal-data-v1', 'exported_at': auth.iso(auth.now()),
            'account': dict(account), 'agreements': agreements, 'data': data,
            'notes': ['仅包含此Bloomate账号的数据；未保存的游客体验不在账户导出内。',
                      '不包含Cookie、验证码、认证令牌或其他账号的数据。']}


def purge_owner(db, owner):
    """Caller holds the write lock. The separate journal is already durable."""
    db.execute('PRAGMA defer_foreign_keys=ON')
    for table in ('habit_events', 'habit_occurrences'):
        db.execute(f'DELETE FROM {table} WHERE stage_id IN (SELECT id FROM habit_stages WHERE owner_id=?)', (owner,))
    totals = db.execute('SELECT month,SUM(amount_cents) FROM usage_reservations WHERE owner_id=? GROUP BY month', (owner,)).fetchall()
    db.execute('DELETE FROM model_call_metrics WHERE reservation_id IN (SELECT id FROM usage_reservations WHERE owner_id=?)', (owner,))
    for table in TABLES:
        db.execute(f'DELETE FROM "{table}" WHERE owner_id=?', (owner,))
    for month, amount in totals:
        db.execute('INSERT INTO usage_reservations VALUES (?,?,?,?)', (secrets.token_hex(16), 'accounting-aggregate', month, amount))
    db.execute('DELETE FROM auth_challenges WHERE session_digest IN (SELECT digest FROM auth_sessions WHERE owner_id=?)', (owner,))
    db.execute('DELETE FROM auth_challenges WHERE email IN (SELECT email FROM auth_subjects WHERE id=?)', (owner,))
    db.execute('DELETE FROM auth_sessions WHERE owner_id=?', (owner,))
    db.execute('DELETE FROM auth_saves WHERE account_id=? OR guest_id=?', (owner, owner))
    db.execute('DELETE FROM auth_deletion_challenges WHERE owner_id=?', (owner,))
    db.execute('DELETE FROM auth_policy_acceptances WHERE owner_id=?', (owner,))
    db.execute('DELETE FROM auth_subjects WHERE id=?', (owner,))


def append_erasure(owners, operation_id):
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = ''.join(json.dumps({'owner_hash': owner_hash(o), 'operation_id': operation_id,
                                 'created_at': auth.now()}) + '\n' for o in owners).encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if not written:
                raise OSError('erasure journal write failed')
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def replay_erasures():
    records = journal_records()
    if not records:
        return
    hashes = {r['owner_hash'] for r in records}
    with auth.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        check_tables(db)
        for row in db.execute('SELECT id FROM auth_subjects').fetchall():
            if owner_hash(row['id']) in hashes:
                purge_owner(db, row['id'])


class DeleteInput(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=80)
    code: str = Field(pattern=r'^\d{6}$')
    confirmation: str = Field(max_length=40)


def install(app):
    @app.get('/api/v1/auth/account')
    def account_summary(request: Request):
        owner = auth.require_account(request)
        with auth.connect() as db:
            return summary(db, owner)

    @app.post('/api/v1/auth/account/export')
    def download_export(request: Request):
        owner = auth.require_account(request)
        with auth.connect() as db:
            db.execute('BEGIN')
            if summary(db, owner)['running_work']:
                raise auth.AuthError('WORK_IN_PROGRESS', '还有任务处理中，请结束后再导出。', 409)
            body = json.dumps(export_data(db, owner), ensure_ascii=False, indent=2).encode()
        if len(body) > 25 * 1024 * 1024:
            raise auth.AuthError('EXPORT_TOO_LARGE', '数据超过单次导出上限，请联系运营者处理。', 413)
        return Response(body, media_type='application/json', headers={
            'Content-Disposition': 'attachment; filename="bloomate-personal-data.json"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.post('/api/v1/auth/account/deletion-challenges', status_code=202)
    def deletion_challenge(request: Request):
        owner = auth.require_account(request)
        created, operation_id = auth.now(), secrets.token_hex(24)
        with auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            check_tables(db)
            if summary(db, owner)['running_work']:
                raise auth.AuthError('WORK_IN_PROGRESS', '还有任务处理中，完成后再注销。', 409)
            auth.reserve_source_request(db, request, 'account-erasure', 10)
            count = db.execute('SELECT count(*) FROM auth_deletion_challenges WHERE owner_id=? AND created_at>?', (owner, created - auth.DAY)).fetchone()[0]
            recent = db.execute('SELECT 1 FROM auth_deletion_challenges WHERE owner_id=? AND created_at>?', (owner, created - 60)).fetchone()
            global_count = db.execute('''SELECT (SELECT count(*) FROM auth_challenges WHERE created_at>?) +
                (SELECT count(*) FROM auth_deletion_challenges WHERE created_at>?)''', (created - auth.DAY, created - auth.DAY)).fetchone()[0]
            limit = max(0, min(int(os.environ.get('BLOOMATE_EMAIL_DAILY_LIMIT', '100')), 10000))
            if count >= 5 or recent or global_count >= limit:
                raise auth.AuthError('RATE_LIMITED', '获取验证码较频繁，请稍后再试。', 429)
            email = db.execute('SELECT email FROM auth_subjects WHERE id=?', (owner,)).fetchone()[0]
            db.execute("UPDATE auth_deletion_challenges SET status='superseded' WHERE owner_id=? AND status='ready'", (owner,))
            db.execute('INSERT INTO auth_deletion_challenges VALUES (?,?,?,?,1,\'sending\',?,?,0)',
                       (operation_id, owner, request.state.auth['digest'], None, created, created + 300))
        try:
            provider_id, ttl, is_user = auth.send_verification(email)
            if not is_user:
                raise auth.AuthError('IDENTITY_CHANGED', '认证身份状态已变化，请重新登录后再操作。', 409)
            with auth.connect() as db:
                db.execute("UPDATE auth_deletion_challenges SET provider_id=?,status='ready',expires_at=? WHERE id=? AND status='sending'",
                           (provider_id, created + min(ttl, 300), operation_id))
        except auth.AuthError:
            with auth.connect() as db:
                db.execute("UPDATE auth_deletion_challenges SET status='send_failed' WHERE id=?", (operation_id,))
            raise
        return {'challenge_id': operation_id, 'expires_at': auth.iso(created + min(ttl, 300)), 'resend_after_seconds': 60}

    @app.post('/api/v1/auth/account/delete')
    def delete_account(data: DeleteInput, request: Request):
        owner = auth.require_account(request)
        if data.confirmation != CONFIRMATION:
            raise auth.AuthError('CONFIRMATION_REQUIRED', '请输入“注销Bloomate账号”以确认。')
        with auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM auth_deletion_challenges WHERE id=? AND owner_id=? AND session_digest=?',
                             (data.challenge_id, owner, request.state.auth['digest'])).fetchone()
            if not row or row['status'] != 'ready':
                raise auth.AuthError('CHALLENGE_USED', '注销验证码不存在或已使用，请重新获取。', 409)
            if row['expires_at'] <= auth.now():
                raise auth.AuthError('CODE_EXPIRED', '验证码已过期，请重新获取。', 410)
            if row['attempts'] >= 5:
                raise auth.AuthError('TOO_MANY_ATTEMPTS', '尝试次数已用完，请重新获取验证码。', 429)
            account = db.execute('SELECT provider_sub,email FROM auth_subjects WHERE id=?', (owner,)).fetchone()
            db.execute("UPDATE auth_deletion_challenges SET status='verifying',attempts=attempts+1 WHERE id=?", (row['id'],))
        try:
            sub = auth.verify_identity({**dict(row), 'email': account['email']}, data.code)
            if sub != account['provider_sub']:
                raise auth.AuthError('IDENTITY_MISMATCH', '验证码对应的身份与当前账号不一致。', 403)
        except auth.AuthError:
            with auth.connect() as db:
                db.execute("UPDATE auth_deletion_challenges SET status='ready' WHERE id=? AND status='verifying'", (row['id'],))
            raise
        try:
            with auth.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT status FROM auth_deletion_challenges WHERE id=?', (row['id'],)).fetchone()
                if not current or current['status'] != 'verifying':
                    raise auth.AuthError('CHALLENGE_CHANGED', '注销状态已变化，请重新核对。', 409)
                if summary(db, owner)['running_work']:
                    db.execute("UPDATE auth_deletion_challenges SET status='superseded' WHERE id=?", (row['id'],))
                    # Commit below; never reuse a provider verification consumed during the race.
                    blocked = True
                else:
                    blocked = False
                    check_tables(db)
                    owners = related_owners(db, owner)
                    append_erasure(owners, row['id'])
                    for subject in owners:
                        purge_owner(db, subject)
            if blocked:
                raise auth.AuthError('WORK_IN_PROGRESS', '有新的任务正在处理，完成后请重新获取注销验证码。', 409)
        except (OSError, sqlite3.Error):
            raise auth.AuthError('ERASURE_PENDING', '注销处理未完成，请保留回执并稍后查询。', 503) from None
        response = JSONResponse({'status': 'succeeded', 'operation_id': row['id'], 'shared_auth_identity_deleted': False})
        response.delete_cookie(auth.ACCOUNT_COOKIE, path='/')
        response.delete_cookie(auth.GUEST_COOKIE, path='/')
        return response

    @app.get('/api/v1/auth/deletion-status/{operation_id}')
    def deletion_status(operation_id: str):
        if not re.fullmatch(r'[a-f0-9]{48}', operation_id):
            raise auth.AuthError('NOT_FOUND', '没有找到注销回执。', 404)
        rows = [r for r in journal_records() if r['operation_id'] == operation_id]
        if not rows:
            raise auth.AuthError('NOT_FOUND', '没有找到注销回执。', 404)
        hashes = {r['owner_hash'] for r in rows}
        with auth.connect() as db:
            pending = any(owner_hash(r[0]) in hashes for r in db.execute('SELECT id FROM auth_subjects'))
        return {'status': 'pending' if pending else 'succeeded', 'operation_id': operation_id}
