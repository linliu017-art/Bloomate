"""Synthetic selected-model API smoke; removes only records created by this script."""
import json
import sys
from pathlib import Path
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app import main

output = ROOT / 'docs' / '评测' / '第一轮约束校验真实冒烟.json'
if output.exists():
    raise SystemExit('Existing result retained.')
main.init_db()
with main.connect() as db:
    before = {row['reservation_id'] for row in db.execute('SELECT reservation_id FROM model_call_metrics')}
results = []
for kind, goal, background, availability in [
    ('goal', '虚构终验G1101：一个月完成英语演讲', '主题是介绍一本书，演讲3分钟，英语基础一般', '每天15分钟'),
    ('habit', '虚构终验H1101：睡前阅读30天', '每次读满10分钟算完成', '每天10分钟'),
]:
    jobs = []
    record = {'kind': kind, 'status': 'started'}
    try:
        with TestClient(main.app) as client:
            r = client.post('/api/v1/plans', json={'kind':kind,'goal':goal,'background':background,'availability':availability,'session_budget_minutes':15 if kind == 'goal' else 10,'habit_completion_minutes':10 if kind == 'habit' else None})
            r.raise_for_status(); job_id = r.json()['id']; jobs.append(job_id)
            for step in range(4):
                state = client.get('/api/v1/plans/' + job_id).json()
                if state['status'] == 'error':
                    record.update(status='error', error_code=state['error']['code']); break
                if state.get('proposal'):
                    response = client.post('/api/v1/proposals/' + job_id + '/confirm')
                    response.raise_for_status(); plant = response.json()
                    garden = client.get('/api/v1/garden').json()
                    record.update(status='passed' if any(p['id'] == plant['id'] for p in garden['plants']) else 'missing_plant', turns=step+1, proposal=state['proposal'])
                    break
                if not state.get('question'):
                    record.update(status='unexpected_state'); break
                answer = '其他条件暂不确定，请基于已提供的信息提出可修改的建议。'
                response = client.post('/api/v1/plans/' + job_id + '/reply', json={'answer':answer})
                response.raise_for_status(); job_id = response.json()['id']; jobs.append(job_id)
            else:
                record.update(status='no_plan_after_four_turns')
    except Exception as exc:
        record.update(status='error', exception_type=type(exc).__name__)
    finally:
        with main.connect() as db:
            for job_id in jobs:
                db.execute('DELETE FROM plants WHERE job_id=? AND owner_id=?', (job_id,main.OWNER))
                db.execute('DELETE FROM jobs WHERE id=? AND owner_id=?', (job_id,main.OWNER))
        results.append(record)
        print(kind, record['status'], flush=True)
with main.connect() as db:
    calls = [dict(row) for row in db.execute('SELECT * FROM model_call_metrics') if row['reservation_id'] not in before]
output.write_text(json.dumps({'results':results,'call_metrics':calls}, ensure_ascii=False, indent=2), encoding='utf-8')
