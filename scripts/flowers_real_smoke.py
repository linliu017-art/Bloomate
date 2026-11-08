"""Real model + reward HTTP flow, fictional data in a disposable local DB.

Only newly incurred cost reservations/metrics are copied to the existing ledger.
No real emails, cloud mutations, or writes to the existing user's garden.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from fastapi.testclient import TestClient
from app import auth, main


def run(output):
    path = (ROOT / output).resolve()
    if ROOT / 'docs' / '评测' not in path.parents or path.suffix != '.json' or path.exists():
        raise SystemExit('Use a new JSON filename under docs/评测; old evidence is never overwritten.')
    original_db = main.DB
    if os.environ.get('DATA_BACKEND') == 'cloudbase_rest':
        raise SystemExit('This smoke accepts only the local budget ledger, never the cloud store.')
    os.environ.update({'BLOOMATE_TEST_MODEL': '0', 'BLOOMATE_APP_MODE': 'local',
                       'BLOOMATE_COOKIE_SECURE': '0', 'BLOOMATE_ALLOWED_ORIGINS': 'http://testserver'})
    report = {'identity': 'isolated session fixture, real email NOT tested', 'provider': 'real configured model', 'checks': []}
    original_post = main.httpx.post
    def inspect_model_response(url, *args, **kwargs):
        response = original_post(url, *args, **kwargs)
        if url.endswith('/chat/completions') and response.status_code < 400:
            try:
                content = response.json()['choices'][0]['message']['content']
                main.Turn.model_validate_json(content)
            except main.ValidationError as exc:
                report.setdefault('model_contract_errors', []).append([
                    {'location': list(e['loc']), 'type': e['type'], 'message': e['msg']}
                    for e in exc.errors(include_input=False, include_context=False)])
            except (ValueError, KeyError, IndexError, TypeError):
                report.setdefault('model_contract_errors', []).append('invalid provider response structure')
        return response
    main.httpx.post = inspect_model_response

    previous = set()
    calls = []
    with tempfile.TemporaryDirectory(prefix='bloomate-flowers-real-') as temporary:
        main.DB = str(Path(temporary) / 'smoke.sqlite3')
        try:
            with TestClient(main.app) as client:
                with sqlite3.connect(original_db) as source, main.connect() as target:
                    previous = {r[0] for r in source.execute('SELECT id FROM usage_reservations')}
                    target.executemany('INSERT INTO usage_reservations VALUES (?,?,?,?)', source.execute('SELECT * FROM usage_reservations').fetchall())
                    owner = 'user_' + secrets.token_hex(16)
                    target.execute("INSERT INTO auth_subjects(id,kind,created_at) VALUES (?,'account',?)", (owner, auth.now()))
                    raw = auth.create_session(target, owner, auth.now() + 3600)
                client.cookies.set(auth.ACCOUNT_COOKIE, raw)
                client.headers.update({'Origin': 'http://testserver', 'X-Bloomate-CSRF': auth.csrf(raw)})

                def generate(kind, goal, schedule=None):
                    response = client.post('/api/v1/plans', json={'kind': kind, 'goal': goal,
                        'background': '纯虚构验收，已有基础不确定。听众是零基础成年人。请按已提供条件给出可编辑的小计划。',
                        'availability': '每天最多十分钟，三天内完成' if kind == 'goal' else '今天阅读十分钟',
                        'session_budget_minutes': 10, 'habit_completion_minutes': 10 if kind == 'habit' else None,
                        'habit_schedule': schedule})
                    response.raise_for_status()
                    job = response.json()['id']
                    for _ in range(3):
                        task = client.get('/api/v1/plans/' + job).json()
                        if task['status'] == 'done' and task.get('proposal'):
                            return job, task['proposal']
                        if task['status'] != 'done' or not task.get('question'):
                            raise RuntimeError('Real model failed: ' + str((task.get('error') or {}).get('code')))
                        response = client.post('/api/v1/plans/' + job + '/reply', json={'answer': '其他信息不确定，请按已有条件给出可编辑计划；这是虚构验收，不要假设个人情况。'})
                        response.raise_for_status()
                        job = response.json()['id']
                    raise RuntimeError('No proposal after bounded real clarification turns')

                job, proposal = generate('goal', '虚构验收：三天内准备一份三分钟园艺分享')
                response = client.post('/api/v1/proposals/' + job + '/confirm', json=proposal)
                response.raise_for_status()
                pid = response.json()['id']
                for index in range(len(proposal['steps'])):
                    client.put(f'/api/v1/plants/{pid}/actions/{index}', json={'completed': True}).raise_for_status()
                client.post(f'/api/v1/plants/{pid}/completion-notice', json={'choice': 'finish'}).raise_for_status()
                data = client.get('/api/v1/flowers').json()
                assert next(f for f in data['flowers'] if f['species'] == 'cosmos')['unlocked']
                report['checks'].append('real goal generation/confirmation/final completion unlocks cosmos')

                job, proposal = generate('goal', '虚构验收：三天内整理一份小花园养护清单，希望种已解锁的波斯菊')
                recommended = proposal.get('flower_species')
                proposal['flower_species'] = 'cosmos'
                response = client.post('/api/v1/proposals/' + job + '/confirm', json=proposal)
                response.raise_for_status()
                planted = response.json()['id']
                assert any(p['id'] == planted and p['plan']['flower_species'] == 'cosmos' for p in client.get('/api/v1/garden').json()['plants'])
                report['model_recommended_species_after_unlock'] = recommended
                report['checks'].append('unlocked cosmos selected for real new plan and restored from garden')

                today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
                schedule = {'start_date': str(today), 'end_date': str(today), 'timezone': 'Asia/Shanghai',
                            'weekdays': [today.weekday()], 'completion_standard': '连续阅读十分钟', 'growth_threshold': 1}
                job, proposal = generate('habit', '虚构验收：今天连续阅读十分钟', schedule)
                response = client.post('/api/v1/proposals/' + job + '/confirm', json=proposal)
                response.raise_for_status()
                pid = response.json()['id']
                client.put(f'/api/v1/plants/{pid}/habit-occurrences/{today}', json={'completed': True, 'performed': True}).raise_for_status()
                data = client.get('/api/v1/flowers').json()
                assert next(f for f in data['flowers'] if f['species'] == 'lavender')['unlocked']
                assert data['counts'] == {'goal': 1, 'habit': 1, 'total': 2}
                report['checks'].append('real one-day habit plan/check-in unlocks lavender; counts stay exact')
                report['passed'] = True
        except Exception as exc:
            report['passed'] = False
            report['error_type'] = type(exc).__name__
            raise
        finally:
            with main.connect() as source, sqlite3.connect(original_db) as target:
                new_usage = [tuple(r) for r in source.execute('SELECT * FROM usage_reservations') if r['id'] not in previous]
                target.executemany('INSERT OR IGNORE INTO usage_reservations VALUES (?,?,?,?)', new_usage)
                calls = [dict(r) for r in source.execute('SELECT * FROM model_call_metrics')]
                for row in calls:
                    target.execute('INSERT OR IGNORE INTO model_call_metrics VALUES (?,?,?,?,?,?,?)', tuple(row.values()))
            main.DB = original_db
            main.httpx.post = original_post
            report['calls'] = calls
            report['reserved_cents'] = sum(r[3] for r in new_usage)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'passed': report['passed'], 'checks': report['checks'], 'calls': len(calls), 'reserved_cents': report['reserved_cents']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    run(parser.parse_args().output)
