"""TEST ONLY: isolated restart/browser evidence server with a mocked mail provider.

This file is never imported by the application. It refuses non-temporary databases.
"""
import os
from pathlib import Path
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
database = Path(os.environ.get('BLOOMATE_DB', '')).resolve()
temporary_root = Path(tempfile.gettempdir()).resolve()
if not any(base in database.parents for base in (temporary_root, Path('/private/tmp'))) or database == root / 'data' / 'bloomate.sqlite3':
    raise SystemExit('Test server requires an isolated temporary database.')
sys.path.insert(0, str(root / 'backend'))
os.environ.update({'BLOOMATE_TEST_MODEL':'1', 'MODEL_ENABLED':'0', 'CLOUDBASE_ENV_ID':'isolated-fixture',
                  'BLOOMATE_EMAIL_ENABLED':'1', 'BLOOMATE_COOKIE_SECURE':'0', 'BLOOMATE_APP_MODE':'local'})
from app import auth, main


def fake_verification(email):
    return 'restart-fixture', 300, True


def fake_identity(row, code):
    if code != '135790':
        raise auth.AuthError('CODE_INVALID', '隔离测试验证码不正确。')
    return 'isolated-' + row['email']


auth.send_verification = fake_verification
auth.verify_identity = fake_identity
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(main.app, host='127.0.0.1', port=int(sys.argv[1]))
