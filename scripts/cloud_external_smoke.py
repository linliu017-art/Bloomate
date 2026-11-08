"""Real HTTP flow and redeployment recovery; private cookies stay in an ignored file."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / 'backend/.env.external-smoke'
BASE = 'https://bloomate-web-333684-11-1514848819.sh.run.tcloudbase.com'


def save(state):
    PRIVATE.write_text(json.dumps(state))
    PRIVATE.chmod(0o600)


def run(action):
    if action == 'create':
        assert not PRIVATE.exists(), 'Resume existing smoke before creating another'
        with httpx.Client(base_url=BASE, headers={'Origin': BASE}, timeout=40) as client:
            response = client.post('/api/v1/auth/guest')
            assert response.status_code == 201, response.status_code
            state = {'cookies': dict(client.cookies), 'csrf': response.json()['csrf_token']}
            save(state)
            client.headers['X-Bloomate-CSRF'] = state['csrf']
            assert client.post('/api/v1/plans', headers={'Origin': 'https://invalid.example'}, json={}).status_code == 403
            assert client.post('/api/v1/plans', headers={'X-Bloomate-CSRF': 'invalid'}, json={}).status_code == 403
            response = client.post('/api/v1/plans', json={'kind': 'goal', 'goal': '准备一段虚构读书会开场分享',
                'background': '云端外网恢复验证虚构案例，不含个人资料；只需要两条简单行动。', 'availability': '时间不确定'})
            assert response.status_code == 202, response.status_code
            job = response.json()['id']
            for turn in range(3):
                for _ in range(60):
                    task = client.get('/api/v1/plans/' + job).json()
                    if task['status'] not in ('pending', 'running'):
                        break
                    time.sleep(2)
                assert task['status'] == 'done', task.get('error', task['status'])
                if task.get('proposal'):
                    break
                response = client.post('/api/v1/plans/' + job + '/reply', json={'answer': '不确定，请直接给一份简单可编辑建议'})
                assert response.status_code == 202
                job = response.json()['id']
            result = client.post('/api/v1/proposals/' + job + '/confirm')
            assert result.status_code == 200, result.json().get('error')
            state['plant'] = result.json()['id']
            state['job'] = job
            result = client.put('/api/v1/plants/' + state['plant'] + '/actions/0', json={'completed': True})
            assert result.status_code == 200
            save(state)
            print(json.dumps({'external_real_model': True, 'confirmed': True, 'action_recorded': True,
                              'origin_csrf_denied': True, 'private_recovery_state_saved': True}), flush=True)
    elif action == 'verify':
        state = json.loads(PRIVATE.read_text())
        with httpx.Client(base_url=BASE, cookies=state['cookies'], timeout=40) as client:
            session = client.get('/api/v1/auth/session').json()
            assert session['kind'] == 'guest'
            garden = client.get('/api/v1/garden').json()
            assert any(p['id'] == state['plant'] for p in garden['plants'])
            actions = client.get('/api/v1/plants/' + state['plant'] + '/actions').json()
            assert actions['actions'][0]['completed'] is True
            assert client.get('/api/health').json()['storage'] == 'cloudbase_rest'
        print(json.dumps({'external_redeployment_session_recovered': True, 'plant_recovered': True,
                          'action_recovered': True, 'cloud_health_ok': True}))
    elif action == 'cleanup':
        state = json.loads(PRIVATE.read_text())
        for line in (ROOT / 'backend/.env.deploy').read_text().splitlines():
            if '=' in line:
                k, v = line.split('=', 1); os.environ[k] = v
        sys.path.insert(0, str(ROOT / 'backend'))
        from app import auth, main, account_privacy
        session = auth.session_for(state['cookies'].get(auth.GUEST_COOKIE))
        removed = False
        if session:
            owner = session['owner_id']
            assert owner.startswith('guest_')
            account_privacy.append_erasure([owner], secrets.token_hex(24))
            with main.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                account_privacy.purge_owner(db, owner)
            with httpx.Client(base_url=BASE, cookies=state['cookies'], timeout=40) as client:
                assert client.get('/api/v1/garden').status_code == 401
            removed = True
        with httpx.Client(base_url=BASE, cookies=state['cookies'], timeout=40) as client:
            rejected = client.get('/api/v1/garden').status_code == 401
        assert rejected
        PRIVATE.unlink()
        print(json.dumps({'test_subject_removed_now': removed, 'old_test_session_rejected': rejected,
                          'actual_user_accounts_untouched': True, 'anonymous_cost_preserved': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['create', 'verify', 'cleanup'])
    run(parser.parse_args().action)
