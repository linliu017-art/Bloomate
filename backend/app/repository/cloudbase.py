"""Small-trial transactional checkpoints in PostgreSQL, never container storage.

The business transaction still runs in SQLite in memory. Its committed image is
durably compared-and-swapped by PostgreSQL before the request may succeed. An
independent erasure journal survives restoring an older business checkpoint.
This intentionally favors compatibility over high write throughput; conflicts
fail closed and must never overwrite a newer revision.
"""
import base64
import hashlib
import logging
import os
import re
import sqlite3
import threading
import uuid
import zlib

import httpx

from app import account_privacy, auth

MAX_RAW_BYTES = 32 * 1024 * 1024
MAX_ENCODED_BYTES = 4 * 1024 * 1024
_lock = threading.RLock()
_gateway = None
logging.getLogger('httpx').setLevel(logging.WARNING)


def unavailable(code='DB_UNAVAILABLE'):
    return auth.AuthError(code, '云端保存暂时不可用，请稍后重试；未确认成功的操作请刷新核对。', 503)


def pack(raw):
    if len(raw) > MAX_RAW_BYTES:
        raise unavailable('DB_CAPACITY_REACHED')
    encoded = base64.b64encode(zlib.compress(raw, 6)).decode()
    if len(encoded) > MAX_ENCODED_BYTES:
        raise unavailable('DB_CAPACITY_REACHED')
    return {'format': 'sqlite-zlib-v1', 'data': encoded, 'raw_bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()}


def unpack(value):
    try:
        if value['format'] != 'sqlite-zlib-v1' or not 0 < value['raw_bytes'] <= MAX_RAW_BYTES:
            raise ValueError()
        if len(value['data']) > MAX_ENCODED_BYTES:
            raise ValueError()
        compressed = base64.b64decode(value['data'], validate=True)
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed, MAX_RAW_BYTES + 1)
        if not decompressor.eof or decompressor.unused_data or len(raw) != value['raw_bytes']:
            raise ValueError()
        if hashlib.sha256(raw).hexdigest() != value['sha256'] or not raw.startswith(b'SQLite format 3\x00'):
            raise ValueError()
        return raw
    except (KeyError, TypeError, ValueError, zlib.error):
        raise unavailable('DB_RECOVERY_REQUIRED') from None


class Gateway:
    def __init__(self, env_id, key, transport=None):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]{2,99}', env_id or '') or not key:
            raise RuntimeError('Cloud data requires CLOUDBASE_ENV_ID and CLOUDBASE_API_KEY')
        self.client = httpx.Client(
            base_url=f'https://{env_id}.api.tcloudbasegateway.com/v1/rdb/rest/',
            headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'},
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False, transport=transport)

    def request(self, path, payload=None, *, read=False):
        # All RPC writes are idempotent by operation id, including lost responses.
        for attempt in range(2):
            try:
                response = self.client.request('GET' if read else 'POST', path, json=payload)
                if response.status_code == 409:
                    raise auth.AuthError('DB_CONFLICT', '内容刚刚有更新，请刷新后重试这次操作。', 409)
                if response.status_code >= 500 and attempt == 0:
                    continue
                if response.status_code >= 400:
                    raise unavailable()
                if response.status_code == 204:
                    return None
                return response.json()
            except (httpx.HTTPError, ValueError):
                if attempt:
                    raise unavailable() from None
        raise unavailable()

    def load(self):
        result = self.request('bloomate_state?id=eq.1&select=revision,checkpoint', read=True)
        if not isinstance(result, list) or len(result) != 1 or type(result[0].get('revision')) is not int:
            raise unavailable('DB_RECOVERY_REQUIRED')
        return result[0]['revision'], result[0]['checkpoint']

    def commit(self, revision, checkpoint, operation):
        result = self.request('rpc/bloomate_commit_state', {
            'p_expected': revision, 'p_checkpoint': checkpoint, 'p_operation': operation})
        if type(result) is not int or result != revision + 1:
            raise unavailable('DB_RECOVERY_REQUIRED')
        return result

    def journal(self):
        # RPC aggregation avoids the gateway's default row pagination limit.
        rows = self.request('rpc/bloomate_read_erasures', {})
        if not isinstance(rows, list) or any(not isinstance(r, dict) or
                not re.fullmatch(r'[a-f0-9]{64}', r.get('owner_hash', '')) or
                not re.fullmatch(r'[a-f0-9]{48}', r.get('operation_id', '')) or
                type(r.get('created_at')) is not int for r in rows):
            raise unavailable('ERASURE_RECOVERY_REQUIRED')
        return rows

    def append(self, owners, operation):
        self.request('rpc/bloomate_append_erasures', {
            'p_hashes': [account_privacy.owner_hash(o) for o in owners], 'p_operation': operation})


def gateway():
    global _gateway
    if _gateway is None:
        _gateway = Gateway(os.environ.get('CLOUDBASE_ENV_ID'), os.environ.get('CLOUDBASE_API_KEY'))
    return _gateway


class CloudConnection(account_privacy.OwnedConnection):
    def __init__(self):
        _lock.acquire()
        self._held = True
        try:
            super().__init__(':memory:', timeout=10)
            self._store = gateway()
            self._revision, checkpoint = self._store.load()
            self._original = unpack(checkpoint) if checkpoint else None
            if self._original:
                self.deserialize(self._original)
            self.row_factory = sqlite3.Row
            sqlite3.Connection.execute(self, 'PRAGMA foreign_keys=ON')
        except BaseException:
            self._release()
            raise

    def _release(self):
        if getattr(self, '_held', False):
            self._held = False
            _lock.release()

    def commit(self):
        # A concurrent erasure is authoritative even while restoring old state.
        erased = {r['owner_hash'] for r in self._store.journal()}
        if erased and sqlite3.Connection.execute(self, "SELECT 1 FROM sqlite_master WHERE name='auth_subjects'").fetchone():
            owners = sqlite3.Connection.execute(self, 'SELECT id FROM auth_subjects').fetchall()
            for row in owners:
                if account_privacy.owner_hash(row[0]) in erased:
                    if self.request_owner == row[0]:
                        # The erasure transaction itself has already removed this row.
                        raise auth.AuthError('AUTH_REQUIRED', '账号已失效，请重新进入。', 401)
                    account_privacy.purge_owner(self, row[0])
        sqlite3.Connection.commit(self)
        raw = self.serialize() if sqlite3.Connection.execute(self, 'PRAGMA page_count').fetchone()[0] else None
        if raw is not None and raw != self._original:
            checkpoint = pack(raw)
            self._revision = self._store.commit(self._revision, checkpoint, uuid.uuid4().hex)
            self._original = raw

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.commit()
            else:
                sqlite3.Connection.rollback(self)
        finally:
            self.close()
        return False

    def close(self):
        try:
            sqlite3.Connection.close(self)
        finally:
            self._release()
