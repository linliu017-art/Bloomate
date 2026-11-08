"""Live cloud DB + real model smoke. Mail identity is not simulated or claimed."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists() or not output.is_relative_to(ROOT / 'docs'):
        raise SystemExit('Choose a new report path under product docs.')
    for line in (ROOT / 'backend/.env.deploy').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            os.environ[key] = value
    os.environ['BLOOMATE_ALLOWED_ORIGINS'] = 'https://cloud-smoke.invalid'
    assert os.environ.get('BLOOMATE_TEST_MODEL') != '1'
    from fastapi.testclient import TestClient
    from app import account_privacy, auth, main as app
    from app.repository.cloudbase import gateway
    owners, results = [], []
    try:
        with TestClient(app.app, base_url='https://cloud-smoke.invalid') as client:
            client.headers.update({'Origin': 'https://cloud-smoke.invalid', 'x-envoy-external-address': '192.0.2.90'})
            response = client.post('/api/v1/auth/guest')
            assert response.status_code == 201, response.status_code
            raw = client.cookies.get(auth.GUEST_COOKIE)
            owners.append(auth.session_for(raw)['owner_id'])
            client.headers['x-bloomate-csrf'] = response.json()['csrf_token']
            for kind in ('goal', 'habit'):
                if kind == 'habit':
                    assert client.post('/api/v1/auth/logout').status_code == 200
                    info = client.post('/api/v1/auth/guest')
                    assert info.status_code == 201
                    owners.append(auth.session_for(client.cookies.get(auth.GUEST_COOKIE))['owner_id'])
                    client.headers['x-bloomate-csrf'] = info.json()['csrf_token']
                response = client.post('/api/v1/plans', json={
                    'kind': kind, 'goal': '为虚构读书会整理两条开场提纲' if kind == 'goal' else '每天阅读一页虚构练习材料',
                    'background': '上线验证虚构案例，不包含个人资料。',
                    'availability': '两天内完成，每次最多十分钟；内容不确定请直接给可编辑建议',
                    'session_budget_minutes': 10})
                assert response.status_code == 202, response.status_code
                job = response.json()['id']
                task = client.get('/api/v1/plans/' + job).json()
                for _ in range(2):
                    if task['status'] == 'done' and task.get('proposal'):
                        break
                    assert task['status'] == 'done' and task.get('question'), task.get('error', task['status'])
                    follow = client.post('/api/v1/plans/' + job + '/reply', json={'answer': '不确定，请给一份可修改的简单建议'})
                    assert follow.status_code == 202, follow.status_code
                    job = follow.json()['id']
                    task = client.get('/api/v1/plans/' + job).json()
                assert task['status'] == 'done', task.get('error_code')
                response = client.post('/api/v1/proposals/' + job + '/confirm')
                assert response.status_code == 200, response.json().get('error', {}).get('code')
                pid = response.json()['id']
                assert any(p['id'] == pid for p in client.get('/api/v1/garden').json()['plants'])
                results.append({'kind': kind, 'real_model': True, 'confirmed': True})
            with TestClient(app.app, base_url='https://cloud-smoke.invalid') as other:
                other.headers.update({'Origin': 'https://cloud-smoke.invalid', 'x-envoy-external-address': '192.0.2.91'})
                info = other.post('/api/v1/auth/guest')
                assert info.status_code == 201
                owners.append(auth.session_for(other.cookies.get(auth.GUEST_COOKIE))['owner_id'])
                assert other.get('/api/v1/garden').json()['plants'] == []
                assert other.get('/api/v1/plants/' + pid + '/actions').status_code == 404
            app.init_db()
            assert len(client.get('/api/v1/garden').json()['plants']) == 1
            rev, checkpoint = gateway().load()
            # Exercise real PostgreSQL CAS and idempotency without changing data.
            import uuid
            operation = uuid.uuid4().hex
            assert gateway().commit(rev, checkpoint, operation) == rev + 1
            assert gateway().commit(rev, checkpoint, operation) == rev + 1
            try:
                gateway().commit(rev, checkpoint, uuid.uuid4().hex)
                raise AssertionError('Stale checkpoint was accepted')
            except auth.AuthError as exc:
                assert exc.code == 'DB_CONFLICT'
    finally:
        for owner in owners:
            with app.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                account_privacy.purge_owner(db, owner)
    with app.connect() as db:
        total = db.execute('SELECT SUM(amount_cents) FROM usage_reservations').fetchone()[0]
    report = {'live_cloud_db': True, 'model': results, 'cross_guest_isolation': True,
              'restart_recovery': True, 'live_cas_conflict_and_idempotency': True,
              'smoke_subjects_removed': len(owners), 'anonymous_reserved_cents_total': total,
              'real_email_login': 'pending browser user verification'}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
