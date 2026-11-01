"""Synthetic end-to-end API smoke test; never prints model credentials."""
import json
import os
import sys
from pathlib import Path
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app import main

CONFIG = {
    'qwen': ('qwen3.7-flash-2026-07-15', 'https://dashscope.aliyuncs.com/compatible-mode/v1'),
    'deepseek': ('deepseek-flash', 'https://api.deepseek.com'),
}
OUT = ROOT / 'docs' / '评测' / '第一轮产品接口真实冒烟.json'
if OUT.exists():
    raise SystemExit('Existing smoke results retained.')
results = []
for provider, (model, url) in CONFIG.items():
    lines = (ROOT / 'backend' / ('.env.' + provider)).read_text().splitlines()
    key = next(line.partition('=')[2].strip() for line in lines if line.startswith('MODEL_API_KEY='))
    os.environ.update(MODEL_ENABLED='1', MODEL_NAME=model, MODEL_BASE_URL=url, MODEL_API_KEY=key, MODEL_CALL_RESERVE_CENTS='10')
    job_ids = []
    result = {'provider': provider, 'model': model, 'status': 'started'}
    try:
        with TestClient(main.app) as client:
            response = client.post('/api/v1/plans', json={'kind':'goal','goal':'一个月准备英语演讲','background':'基础一般','availability':'每天15分钟'})
            response.raise_for_status()
            job_id = response.json()['id']; job_ids.append(job_id)
            for step in range(4):
                state = client.get('/api/v1/plans/' + job_id).json()
                if state['status'] == 'error':
                    result.update(status='error', error_code=state['error']['code']); break
                if state.get('proposal'):
                    confirm = client.post('/api/v1/proposals/' + job_id + '/confirm')
                    confirm.raise_for_status()
                    plant = confirm.json()
                    garden = client.get('/api/v1/garden').json()
                    result.update(status='passed' if any(p['id'] == plant['id'] for p in garden['plants']) else 'missing_plant', proposal=state['proposal'], turns=step+1)
                    break
                if not state.get('question'):
                    result.update(status='unexpected_state'); break
                reply = client.post('/api/v1/plans/' + job_id + '/reply', json={'answer':'演讲时长和主题尚未确定，请根据已知条件先提出可调整的建议。'})
                reply.raise_for_status(); job_id = reply.json()['id']; job_ids.append(job_id)
            else:
                result.update(status='no_plan_after_four_turns')
    except Exception as exc:
        result.update(status='error', exception_type=type(exc).__name__)
    finally:
        with main.connect() as db:
            for job_id in job_ids:
                db.execute('DELETE FROM plants WHERE job_id=? AND owner_id=?', (job_id, main.OWNER))
                db.execute('DELETE FROM jobs WHERE id=? AND owner_id=?', (job_id, main.OWNER))
        os.environ.pop('MODEL_API_KEY', None)
        results.append(result)
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(provider, result['status'], flush=True)
