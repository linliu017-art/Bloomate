"""Versioned change proposals; only deterministic confirmation mutates a business record."""
import hashlib
import json
import uuid
from datetime import date, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from app import auth, account_privacy
from app.identity import owner_id


class ActionRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plant_id: str = Field(min_length=1, max_length=80)
    stage_id: str | None = Field(default=None, max_length=80)
    index: int = Field(strict=True, ge=0, le=99)
    base_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class ChangeChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    turn_id: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=80)
    target: ActionRef


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['date', 'split']
    due_date: date | None = None
    relative_day: Literal['today', 'tomorrow', 'day_after_tomorrow'] | None = None
    clear_date: bool = Field(default=False, strict=True)
    plant_title: str | None = Field(default=None, min_length=1, max_length=100)
    step_number: int | None = Field(default=None, strict=True, ge=1, le=100)
    step_text: str | None = Field(default=None, min_length=1, max_length=500)
    difficulty: str | None = Field(default=None, min_length=1, max_length=500)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def actions_for(db, plant_id):
    from app import main
    plant = db.execute("SELECT * FROM plants WHERE id=? AND owner_id=? AND kind='goal'", (plant_id, owner_id())).fetchone()
    if not plant:
        return []
    paused = bool(db.execute('SELECT 1 FROM plant_pauses WHERE owner_id=? AND plant_id=? AND end_date IS NULL', (owner_id(), plant_id)).fetchone())
    stage = db.execute('SELECT * FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1', (owner_id(), plant_id)).fetchone()
    if stage:
        steps, states = json.loads(stage['steps_json']), json.loads(stage['states_json'])
        dates = {r['step_index']: r['due_date'] for r in db.execute('SELECT step_index,due_date FROM goal_followup_dates WHERE owner_id=? AND stage_id=? ORDER BY step_index', (owner_id(), stage['id']))}
        base = digest({'stage': dict(stage), 'dates': dates, 'paused': paused, 'plant_id': plant_id})
        records = [{'completed': bool(s), 'cancelled': False, 'due_date': dates.get(i)} for i, s in enumerate(states)]
        archived = bool(stage['completed_at'])
    else:
        steps = json.loads(plant['plan_json'])['steps']
        states = {r['step_index']: dict(r) for r in db.execute('SELECT step_index,completed,cancelled,due_date FROM goal_action_states WHERE owner_id=? AND plant_id=?', (owner_id(), plant_id))}
        archived = bool(db.execute('SELECT 1 FROM goal_collections WHERE owner_id=? AND plant_id=?', (owner_id(), plant_id)).fetchone())
        base = digest({'base': main.adjustment_base(db, plant), 'paused': paused, 'archived': archived})
        records = [states.get(i, {'completed': False, 'cancelled': False, 'due_date': None}) for i in range(len(steps))]
    result = []
    for i, text in enumerate(steps):
        row = records[i]
        blocked = '目标已暂停，请先在原页面恢复。' if paused else '这个阶段已收藏，请在当前可行动阶段调整。' if archived else '已完成步骤不能通过园丁直接调整。' if row['completed'] else '这一步已取消，请先在原页面恢复。' if row['cancelled'] else None
        result.append({'target': {'plant_id': plant_id, 'stage_id': stage['id'] if stage else None, 'index': i, 'base_hash': base},
                       'plant_title': plant['title'], 'stage_title': stage['title'] if stage else '初始阶段',
                       'step_number': i+1, 'text': text, 'due_date': row['due_date'], 'blocked_reason': blocked})
    return result


def choices(db, plant_id=None, title=None):
    if plant_id:
        rows = actions_for(db, plant_id)
    else:
        plants = db.execute("SELECT id FROM plants WHERE owner_id=? AND kind='goal'" + (' AND title=?' if title else '') + ' ORDER BY created_at DESC,id', (owner_id(), title) if title else (owner_id(),)).fetchall()
        rows = []
        for plant in plants:
            rows.extend(actions_for(db, plant['id']))
            if sum(not row['blocked_reason'] for row in rows) >= 41:
                break
    return rows


def latest_change(db, session_id):
    for row in db.execute("SELECT * FROM agent_turns WHERE owner_id=? AND session_id=? AND status='done' AND expires_at>? ORDER BY rowid DESC", (owner_id(), session_id, auth.now())):
        result = json.loads(row['response_json'])
        if result['kind'] == 'change':
            return row, result
    return None, None


def active_change(session_id):
    from app import main
    with main.connect() as db:
        _, result = latest_change(db, session_id)
    if not result or result['data']['state'] not in ('select_task', 'need_date', 'ready'):
        return None
    data = result['data']
    return {'request': data['request'], 'target': data.get('target'), 'before': data.get('before'), 'state': data['state']}


def result(request, state, message, **values):
    return {'kind': 'change', 'queried_at': auth.now(), 'text': message,
            'data': {'state': state, 'version': str(uuid.uuid4()), 'operation': request.operation,
                     'request': request.model_dump(mode='json'), 'choices': [], **values}}


def selection(request, rows, message='请选要调整的具体步骤，我不会替你猜。'):
    eligible = [r for r in rows if not r['blocked_reason']]
    if not eligible:
        return result(request, 'blocked', '没有可调整的未完成目标步骤。已完成、暂停、取消或已收藏阶段请先在原页面处理；习惯日期仍由周期管理。')
    return result(request, 'select_task', message, choices=eligible[:40], omitted=max(0, len(eligible)-40))


def prepare(request, context, session_id, user_text, target=None):
    from app import main
    request = request.model_copy(deep=True)
    if request.relative_day:
        request.due_date = main.habit_today('Asia/Shanghai') + timedelta(days={'today': 0, 'tomorrow': 1, 'day_after_tomorrow': 2}[request.relative_day])
        request.relative_day = None
    with main.connect() as db:
        scope = context.plant_id
        if request.plant_title:
            scope = None
        rows = choices(db, scope, request.plant_title)
        if target:
            matching = [r for r in actions_for(db, target.plant_id) if r['target']['index'] == target.index and r['target']['stage_id'] == target.stage_id]
            chosen = matching[0] if matching else None
            if not chosen or chosen['target']['base_hash'] != target.base_hash:
                return selection(request, rows, '步骤已变化，请重新选择并预览。')
        else:
            matching = rows
            if request.step_number is not None:
                matching = [r for r in matching if r['step_number'] == request.step_number]
            if request.step_text:
                matching = [r for r in matching if r['text'].strip() == request.step_text.strip()]
            if len(matching) == 1 and (scope or request.plant_title or request.step_text):
                chosen = matching[0]
            else:
                chosen = None
                previous = active_change(session_id)
                if request.step_number is None and request.step_text is None and request.plant_title is None and previous and previous.get('target') and previous['request']['operation'] == request.operation:
                    old = previous['target']
                    if scope is None or scope == old['plant_id']:
                        chosen = next((r for r in rows if r['target'] == old), None)
                if not chosen:
                    return selection(request, rows)
        if chosen['blocked_reason']:
            return result(request, 'blocked', chosen['blocked_reason'], target=chosen['target'], before=chosen)
        target = ActionRef.model_validate(chosen['target'])
        plant = db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?', (target.plant_id, owner_id())).fetchone()
        source = json.loads(db.execute('SELECT input_json FROM jobs WHERE id=? AND owner_id=?', (plant['job_id'], owner_id())).fetchone()['input_json'])
        plan = json.loads(plant['plan_json'])
    if request.operation == 'date':
        due = request.due_date
        if request.relative_day:
            due = main.habit_today('Asia/Shanghai') + timedelta(days={'today': 0, 'tomorrow': 1, 'day_after_tomorrow': 2}[request.relative_day])
        if request.clear_date:
            due = None
        elif due is None:
            return result(request, 'need_date', '要改到哪一天？可以说“明天”、具体日期，或“清空日期”。', target=target.model_dump(), before=chosen)
        return result(request, 'ready', '这是日期修改预览，点击确认修改后才会生效。', target=target.model_dump(), before=chosen, after={'due_date': due.isoformat() if due else None})
    if target.stage_id:
        return result(request, 'blocked', '当前续阶段可以改日期；拆分工具目前支持初始阶段，续阶段拆分仍在后续范围。', target=target.model_dump(), before=chosen)
    model_data = {'kind': 'adjustment', 'goal': '调整计划中的一个步骤',
                  'background': json.dumps({'目标': plant['title'], '完整步骤': plan['steps'], '选中步骤': chosen['text'], '用户遇到的困难': request.difficulty or user_text}, ensure_ascii=False),
                  'availability': source.get('availability', '不确定'), 'session_budget_minutes': source.get('session_budget_minutes'),
                  'habit_completion_minutes': None, '_adjustment_force_plan': True}
    turn = main.generate(model_data)
    if not turn.plan or not 1 <= len(turn.plan.steps) <= 5:
        raise RuntimeError('MODEL_FAILED')
    proposed = main.normalize_adjustment_steps(chosen['text'], turn.plan.steps)
    preview = {**plan, 'steps': plan['steps'][:target.index] + proposed + plan['steps'][target.index+1:]}
    try:
        main.validate_plan(main.Plan.model_validate(preview), source)
    except (ValueError, main.ConstraintError):
        return result(request, 'blocked', '拆分后超过原计划的步骤或时间约束，请缩小调整范围后重试。', target=target.model_dump(), before=chosen)
    with main.connect() as db:
        current = actions_for(db, target.plant_id)
        if not any(r['target'] == target.model_dump() for r in current):
            return selection(request, current, '生成期间原计划已变化，请重新选择。')
    return result(request, 'ready', '这是拆小后的建议，可编辑后确认。其他步骤与已有完成记录保持，第一条沿用原日期，新小步骤待安排。',
                  target=target.model_dump(), before=chosen, after={'steps': proposed})


def from_choice(choice, context, session_id):
    from app import main
    with main.connect() as db:
        row = db.execute("SELECT * FROM agent_turns WHERE id=? AND owner_id=? AND session_id=? AND status='done' AND expires_at>?", (choice.turn_id, owner_id(), session_id, auth.now())).fetchone()
        if not row:
            raise auth.AuthError('NOT_FOUND', '选择卡已删除、过期或不可用。', 404)
        original = json.loads(row['response_json'])
        data = original['data']
        if original['kind'] != 'change' or data.get('version') != choice.version or data.get('state') != 'select_task' or not any(r['target'] == choice.target.model_dump() for r in data['choices']):
            raise auth.AuthError('CHANGE_STALE', '请选择最新卡片中的步骤。', 409)
        request = ChangeRequest.model_validate(data['request'])
    return prepare(request, context, session_id, row['user_text'], choice.target)


def supersede_previous(db, session_id):
    row, value = latest_change(db, session_id)
    if row and value['data']['state'] in ('select_task', 'need_date', 'ready'):
        value['data']['state'] = 'superseded'
        db.execute('UPDATE agent_turns SET response_json=? WHERE id=? AND owner_id=?', (json.dumps(value, ensure_ascii=False), row['id'], owner_id()))


class Confirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str = Field(min_length=1, max_length=80)
    due_date: date | None = None
    steps: list[Annotated[str, Field(min_length=1, max_length=500, pattern=r'\S')]] | None = Field(default=None, min_length=1, max_length=5)


def read_change(db, turn_id, version):
    from app import agent
    row = db.execute("SELECT * FROM agent_turns WHERE id=? AND owner_id=? AND status='done' AND expires_at>?", (turn_id, owner_id(), auth.now())).fetchone()
    if not row or account_privacy.is_erased(agent.erased_key(row['session_id'])):
        raise auth.AuthError('NOT_FOUND', '这份修改已删除、过期或不可用。', 404)
    value = json.loads(row['response_json'])
    if value['kind'] != 'change' or value['data']['version'] != version:
        raise auth.AuthError('CHANGE_STALE', '请重新读取这份修改。', 409)
    return row, value


def require_latest(db, row):
    latest, _ = latest_change(db, row['session_id'])
    if not latest or latest['id'] != row['id'] or db.execute("SELECT 1 FROM agent_turns WHERE owner_id=? AND session_id=? AND status IN ('pending','running')", (owner_id(), row['session_id'])).fetchone():
        raise auth.AuthError('CHANGE_STALE', '已有新版或园丁正在处理新的要求，请重新预览。', 409)


def install(app):
    from app import agent, main, agent_planning

    @app.post('/api/v1/agent/changes/{turn_id}/confirm')
    def confirm(turn_id: str, data: Confirmation):
        agent.cleanup()
        fingerprint = digest(data.model_dump(mode='json'))
        with main.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row, value = read_change(db, turn_id, data.version)
            change = value['data']
            if change['state'] == 'confirmed':
                if change['confirmation_hash'] != fingerprint:
                    raise auth.AuthError('REQUEST_CHANGED', '这份修改已按另一内容确认，请查看原植物。', 409)
                return change['applied']
            require_latest(db, row)
            if change['state'] != 'ready':
                raise auth.AuthError('NOT_READY', '请使用已准备好的最新修改预览。', 409)
            target = ActionRef.model_validate(change['target'])
            current = next((r for r in actions_for(db, target.plant_id) if r['target']['index'] == target.index and r['target']['stage_id'] == target.stage_id), None)
            if not current or current['target']['base_hash'] != target.base_hash or current['blocked_reason']:
                raise auth.AuthError('PLAN_CHANGED', '原计划、阶段或完成状态已经改变，请重新预览。', 409)
            if change['operation'] == 'date':
                if data.steps is not None or 'due_date' not in data.model_fields_set:
                    raise auth.AuthError('INVALID_INPUT', '请明确修改后的日期或清空日期。', 422)
                update = main.ActionDateInput(due_date=data.due_date)
                applied = main.set_followup_date_in_transaction(db, target.stage_id, target.index, update) if target.stage_id else main.set_goal_date_in_transaction(db, target.plant_id, target.index, update)
            else:
                if data.steps is None or 'due_date' in data.model_fields_set:
                    raise auth.AuthError('INVALID_INPUT', '请核对替代步骤。', 422)
                adjustment_id = str(uuid.uuid4())
                plant = db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?', (target.plant_id, owner_id())).fetchone()
                db.execute('INSERT INTO plan_adjustments (id,owner_id,plant_id,step_index,base_hash,original_text,request_text,status,suggestion,suggestion_steps_json) VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (adjustment_id, owner_id(), target.plant_id, target.index, main.adjustment_base(db, plant), current['text'], '园丁对话确认的步骤调整', 'done', change['after']['steps'][0], json.dumps(change['after']['steps'], ensure_ascii=False)))
                applied = main.confirm_adjustment_in_transaction(db, adjustment_id, main.AdjustmentConfirmation(steps=data.steps))
            if isinstance(applied, Response):
                error = json.loads(applied.body)['error']
                raise auth.AuthError(error['code'], error['message'], applied.status_code)
            change['after'] = {'due_date': applied['due_date']} if change['operation'] == 'date' else {'steps': applied['steps']}
            response = {'status': 'confirmed', 'plant_id': target.plant_id, 'operation': change['operation'], 'result': applied}
            change.update(state='confirmed', confirmation_hash=fingerprint, applied=response)
            value['text'] = '修改已生效，原今日行动与植物记录已同步。'
            db.execute('UPDATE agent_turns SET response_json=? WHERE id=? AND owner_id=?', (json.dumps(value, ensure_ascii=False), turn_id, owner_id()))
            return response

    @app.post('/api/v1/agent/changes/{turn_id}/discard')
    def discard(turn_id: str, data: agent_planning.VersionInput):
        agent.cleanup()
        with main.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row, value = read_change(db, turn_id, data.version)
            if value['data']['state'] != 'discarded':
                require_latest(db, row)
                if value['data']['state'] not in ('select_task', 'need_date', 'ready', 'blocked'):
                    raise auth.AuthError('NOT_READY', '这份修改已处理。', 409)
                value['data']['state'] = 'discarded'
                value['text'] = '已放弃这份修改，原计划保持不变。'
                db.execute('UPDATE agent_turns SET response_json=? WHERE id=? AND owner_id=?', (json.dumps(value, ensure_ascii=False), turn_id, owner_id()))
            return agent.public_turn(db.execute('SELECT * FROM agent_turns WHERE id=? AND owner_id=?', (turn_id, owner_id())).fetchone())
