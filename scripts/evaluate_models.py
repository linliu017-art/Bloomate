"""Run synthetic model comparison. Credentials are read locally and never printed."""
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app import main

CASES = [
 ('E01', 'goal', '一个月准备产品经理面试', '已有一年相关工作经验', '每天20分钟', '两周后有一次模拟面试'),
 ('E02', 'goal', '准备英语演讲', '', '每天15分钟', '一个月后演讲，基础一般'),
 ('E03', 'goal', '两天内完成大型求职作品集', '', '每天10分钟', '可以先完成一页简版作品介绍'),
 ('E04', 'goal', '学习基础绘画', '零基础；原先说每周3小时，现更正为每周1小时', '每周1小时', '以更正后的每周1小时为准'),
 ('E05', 'habit', '每天睡前读书', '', '每天10分钟', '先尝试30天，每次读10分钟就算完成'),
 ('E06', 'habit', '每周运动三次', '', '每次20分钟', '计划持续4周，先从轻度活动开始'),
 ('E07', 'habit', '每晚复盘', '', '周一、三、五各10分钟', '持续两周，复盘记录一件做得好的事即可'),
 ('E08', 'goal', '改善生活状态', '不愿提供健康细节', '每天15分钟', '想从散步和规律作息开始，不愿透露健康细节'),
]
MODELS = {
 'qwen': ('qwen3.7-flash-2026-07-15', 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'),
 'deepseek': ('deepseek-flash', 'https://api.deepseek.com/chat/completions'),
}
OUT = ROOT / 'docs' / '评测' / '第一轮真实模型原始结果.json'
PROMPT = (ROOT / 'backend' / 'app' / 'prompt.txt').read_text(encoding='utf-8')


def key_for(name):
    path = ROOT / 'backend' / ('.env.' + name)
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.startswith('MODEL_API_KEY='):
            return line.partition('=')[2].strip()
    raise ValueError('missing credential')


def save(records):
    temporary = OUT.with_suffix('.tmp')
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(OUT)


def call(name, data):
    model, endpoint = MODELS[name]
    body = {'model': model, 'messages': [{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}], 'response_format': {'type': 'json_object'}, 'max_tokens': 1024}
    if name == 'qwen': body['enable_thinking'] = False
    else: body['thinking'] = {'type': 'disabled'}
    main.reserve_budget()
    started = time.monotonic()
    try:
        response = httpx.post(endpoint, headers={'Authorization': 'Bearer ' + key_for(name)}, json=body, timeout=45)
        elapsed = round(time.monotonic() - started, 2)
        if response.status_code != 200:
            return {'error': 'HTTP_' + str(response.status_code), 'seconds': elapsed}
        payload = response.json()
        text = payload['choices'][0]['message']['content']
        turn = main.Turn.model_validate_json(text)
        if bool(turn.question) == bool(turn.plan):
            return {'error': 'INVALID_STRUCTURE', 'seconds': elapsed}
        return {'result': turn.model_dump(), 'usage': payload.get('usage', {}), 'seconds': elapsed, 'returned_model': payload.get('model')}
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        return {'error': 'NETWORK_OR_INVALID_RESPONSE', 'exception_type': type(exc).__name__, 'seconds': round(time.monotonic() - started, 2)}


def run():
    if OUT.exists():
        raise SystemExit('Existing results retained; do not overwrite.')
    os.environ['MODEL_CALL_RESERVE_CENTS'] = '10'
    main.init_db()
    records = {'started_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), 'scope': '8 synthetic cases, non-thinking, up to three calls per case; no automatic retry', 'models': {}, 'cases': []}
    save(records)
    for name in MODELS:
        records['models'][name] = {'model': MODELS[name][0], 'mode': 'non-thinking', 'status': 'running'}
        for case_id, kind, goal, background, availability, addition in CASES:
            data = {'kind': kind, 'goal': goal, 'background': background, 'availability': availability, 'history': []}
            entry = {'model': name, 'case': case_id, 'initial': data.copy(), 'turns': [], 'status': 'running'}
            records['cases'].append(entry)
            # Include pre-declared supplemental details identically, after an initial question.
            for i in range(3):
                try:
                    result = call(name, data)
                except RuntimeError as exc:
                    result = {'error': str(exc)}
                entry['turns'].append(result)
                save(records)
                if 'error' in result:
                    entry['status'] = 'error'
                    break
                turn = result['result']
                if turn['plan']:
                    entry['status'] = 'plan'
                    break
                answer = addition if i == 0 else '其他信息暂不确定，请基于已知信息给出可调整的建议。'
                data['history'].append({'question': turn['question'], 'answer': answer})
            else:
                entry['status'] = 'no_plan_after_three_calls'
            save(records)
            print(name, case_id, entry['status'], flush=True)
            if entry['turns'][-1].get('error') in ('HTTP_401', 'HTTP_403', 'BUDGET_EXHAUSTED', 'BUDGET_NOT_CONFIGURED'):
                records['models'][name]['status'] = 'stopped'
                break
        else:
            records['models'][name]['status'] = 'done'
        save(records)
    records['finished_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    save(records)


if __name__ == '__main__':
    run()
