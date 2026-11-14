"""Conversation drafts are ephemeral; only an explicit, atomic confirmation plants them."""
import hashlib
import json
import re
import uuid
from datetime import date, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import Response

from app import auth, account_privacy
from app.identity import owner_id


class DraftFields(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['goal', 'habit']
    goal: str = Field(default='', max_length=500)
    background: str = Field(default='', max_length=1000)
    duration_days: int | None = Field(default=None, strict=True, ge=1, le=3660)
    daily_minutes: int | None = Field(default=None, strict=True, ge=1, le=720)
    daily: bool = Field(default=False, strict=True)
    start_date: date | None = None
    end_date: date | None = None
    weekdays: list[Annotated[int, Field(strict=True, ge=0, le=6)]] | None = Field(default=None, min_length=1, max_length=7)
    completion_standard: str | None = Field(default=None, min_length=1, max_length=200)
    growth_threshold: int | None = Field(default=None, strict=True, ge=1, le=3661)
    flexible_time: bool = Field(default=False, strict=True)
    use_suggestions: bool = Field(default=False, strict=True)


def latest_draft(db, session_id):
    for row in db.execute("SELECT * FROM agent_turns WHERE owner_id=? AND session_id=? AND status='done' AND expires_at>? ORDER BY rowid DESC",
                          (owner_id(), session_id, auth.now())):
        result = json.loads(row['response_json'])
        if result['kind'] == 'plan':
            return row, result
    return None, None


def active_draft(session_id):
    from app import main
    with main.connect() as db:
        _, result = latest_draft(db, session_id)
    return result['data'] if result and result['data']['state'] in ('collecting', 'ready') else None


def make_draft(fields, previous, answer):
    from app import main
    fields = fields.model_copy(deep=True)
    suggestions = []
    today = main.habit_today('Asia/Shanghai')
    if fields.kind == 'goal' and fields.end_date and fields.start_date is None:
        fields.start_date = today
    if fields.use_suggestions:
        fields.flexible_time = True
        if fields.kind == 'habit':
            defaults = {'start_date': today, 'duration_days': 7, 'weekdays': list(range(7)),
                        'completion_standard': fields.goal.strip() or '完成一次所选习惯'}
            for name, value in defaults.items():
                if getattr(fields, name) is None:
                    setattr(fields, name, value)
                    suggestions.append(name)
    if fields.start_date and fields.end_date:
        actual = (fields.end_date - fields.start_date).days + 1
        if actual < 1 or actual > 3660 or fields.duration_days is not None and fields.duration_days != actual:
            return collecting(fields, '起止日期和天数有冲突，请明确采用哪个周期。', suggestions)
        fields.duration_days = actual
    if fields.kind == 'habit' and fields.start_date and fields.duration_days and fields.end_date is None:
        fields.end_date = fields.start_date + timedelta(days=fields.duration_days - 1)
    if fields.kind == 'habit' and fields.start_date and fields.end_date and fields.weekdays:
        planned = sum((fields.start_date + timedelta(days=i)).weekday() in fields.weekdays
                      for i in range((fields.end_date-fields.start_date).days+1))
        if fields.use_suggestions and fields.growth_threshold is None and planned:
            fields.growth_threshold = planned
            suggestions.append('growth_threshold')
        if not planned or len(set(fields.weekdays)) != len(fields.weekdays) or fields.growth_threshold and fields.growth_threshold > planned:
            return collecting(fields, '这个周期的计划日不足，或星期重复。请调整周期、计划星期或阶段完成次数。', suggestions)
    missing = []
    if len(fields.goal.strip()) < 2:
        missing.append('具体想做什么')
    if fields.kind == 'goal' and not fields.flexible_time:
        if fields.duration_days is None:
            missing.append('希望几天完成')
        if fields.daily_minutes is None:
            missing.append('每次可用几分钟')
    if fields.kind == 'habit':
        for name, label in [('start_date', '开始日期'), ('end_date', '结束日期或周期天数'),
                            ('weekdays', '每周哪几天做'), ('completion_standard', '单次完成标准'),
                            ('growth_threshold', '本阶段完成几次算达标')]:
            if getattr(fields, name) is None:
                missing.append(label)
    if missing:
        return collecting(fields, '还需要知道：' + '、'.join(missing) + '。可以直接回复；时间不确定也可以说明，习惯条件可让我提出建议供你确认。', suggestions)
    availability = ('希望' + str(fields.duration_days) + '天内完成；' if fields.duration_days else '完成期限不确定；')
    availability += (('每天' if fields.daily else '每次') + f'最多{fields.daily_minutes}分钟') if fields.daily_minutes else '每次时间不确定'
    schedule = None
    completion_minutes = None
    if fields.kind == 'habit':
        minutes = re.search(r'([0-9]+|[一二三四五六七八九十]+)\s*分钟', fields.completion_standard)
        if minutes:
            from app.constraints import number
            completion_minutes = number(minutes.group(1))
            if completion_minutes and (completion_minutes > 720 or fields.daily_minutes and completion_minutes > fields.daily_minutes):
                return collecting(fields, '单次完成标准超过可用时间，请调整时间上限或明确修改完成标准，我不会自动降低。', suggestions)
        schedule = {'start_date': fields.start_date.isoformat(), 'end_date': fields.end_date.isoformat(),
                    'timezone': 'Asia/Shanghai', 'weekdays': sorted(fields.weekdays),
                    'completion_standard': fields.completion_standard.strip(), 'growth_threshold': fields.growth_threshold}
    try:
        source = main.PlanInput(kind=fields.kind, goal=fields.goal.strip(), background=fields.background,
                                availability=availability, session_budget_minutes=fields.daily_minutes,
                                habit_completion_minutes=completion_minutes,
                                habit_schedule=schedule).model_dump(mode='json')
    except ValidationError:
        return collecting(fields, '条件还不能形成有效周期，请检查日期、计划星期、单次标准和阶段次数。', suggestions)
    history = []
    if previous and previous.get('source') and previous['fields']['goal'] == fields.goal and previous['fields']['kind'] == fields.kind:
        history = previous['source'].get('history', [])[-1:]
        if previous.get('question'):
            history = [*history, {'question': previous['question'], 'answer': answer}]
        if previous.get('proposal'):
            source['_agent_previous_draft'] = previous['proposal']
    source['history'] = history
    turn = main.generate(source)
    if turn.question:
        result = collecting(fields, turn.question, suggestions)
        result['data']['source'] = source
        return result
    main.validate_plan(turn.plan, source)
    with main.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        main.flowers.require_flower(db, owner_id(), turn.plan.flower_species)
    anchor = fields.start_date or today
    dates = [None] * len(turn.plan.steps)
    if fields.kind == 'goal' and fields.duration_days:
        # A visible editable suggestion, not an inferred commitment from prose.
        dates = [(anchor + timedelta(days=i*(fields.duration_days-1)//max(1,len(dates)-1))).isoformat()
                 for i in range(len(dates))]
    return {'kind': 'plan', 'queried_at': auth.now(), 'text': '草案已准备好。可以编辑，或继续告诉我怎么调整；点击确认种植后才进入你的花园。',
            'data': {'state': 'ready', 'version': str(uuid.uuid4()), 'fields': fields.model_dump(mode='json'),
                     'source': source, 'proposal': turn.plan.model_dump(mode='json'), 'question': None,
                     'goal_dates': dates if fields.kind == 'goal' else [], 'suggestions': suggestions}}


def collecting(fields, question, suggestions):
    return {'kind': 'plan', 'queried_at': auth.now(), 'text': question,
            'data': {'state': 'collecting', 'version': str(uuid.uuid4()), 'fields': fields.model_dump(mode='json'),
                     'source': None, 'proposal': None, 'question': question, 'goal_dates': [], 'suggestions': suggestions}}


def supersede_previous(db, session_id):
    row, result = latest_draft(db, session_id)
    if row and result['data']['state'] in ('ready', 'collecting'):
        result['data']['state'] = 'superseded'
        db.execute('UPDATE agent_turns SET response_json=? WHERE id=? AND owner_id=?',
                   (json.dumps(result, ensure_ascii=False), row['id'], owner_id()))


def get_draft(db, turn_id, version):
    from app import agent
    row = db.execute('SELECT * FROM agent_turns WHERE id=? AND owner_id=? AND expires_at>?', (turn_id, owner_id(), auth.now())).fetchone()
    if not row or row['status'] != 'done' or account_privacy.is_erased(agent.erased_key(row['session_id'])):
        raise auth.AuthError('NOT_FOUND', '这份草案已删除、过期或不可用。', 404)
    result = json.loads(row['response_json'])
    if result['kind'] != 'plan' or result['data']['version'] != version:
        raise auth.AuthError('DRAFT_CHANGED', '草案已变化，请重新读取后确认。', 409)
    return row, result


def require_latest(db, row):
    latest, _ = latest_draft(db, row['session_id'])
    if not latest or latest['id'] != row['id'] or db.execute("SELECT 1 FROM agent_turns WHERE session_id=? AND owner_id=? AND status IN ('pending','running')", (row['session_id'], owner_id())).fetchone():
        raise auth.AuthError('DRAFT_CHANGED', '园丁正在处理新要求，或已有新版草案，请重新读取。', 409)


class VersionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str = Field(min_length=1, max_length=80)


def install(app):
    from app import agent, main

    class Confirmation(VersionInput):
        plan: main.Plan
        goal_dates: list[date | None] = Field(max_length=10)

    @app.post('/api/v1/agent/turns/{turn_id}/confirm')
    def confirm(turn_id: str, data: Confirmation):
        agent.cleanup()
        fingerprint = hashlib.sha256(data.model_dump_json().encode()).hexdigest()
        with main.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row, result = get_draft(db, turn_id, data.version)
            draft = result['data']
            if draft['state'] == 'confirmed':
                if draft.get('confirmation_hash') != fingerprint:
                    raise auth.AuthError('REQUEST_CHANGED', '这份草案已按另一版本种植，请打开原植物。', 409)
                return draft['plant']
            require_latest(db, row)
            if draft['state'] != 'ready':
                raise auth.AuthError('NOT_READY', '请先补齐条件或使用最新草案。', 409)
            if len(data.goal_dates) != (len(data.plan.steps) if draft['fields']['kind'] == 'goal' else 0):
                raise auth.AuthError('INVALID_DATES', '步骤和日期数量不一致，请重新检查。', 422)
            fields = DraftFields.model_validate(draft['fields'])
            if fields.kind == 'goal' and fields.duration_days:
                start = fields.start_date or date.fromisoformat(draft['goal_dates'][0])
                end = start + timedelta(days=fields.duration_days-1)
                if any(d is not None and not start <= d <= end for d in data.goal_dates):
                    raise auth.AuthError('PLAN_CONSTRAINT', '步骤日期超出这份草案的周期，请调整日期或重新描述周期。', 409)
            # Only canonical business inputs survive chat deletion; no full conversation is copied.
            source = main.PlanInput.model_validate(draft['source']).model_dump(mode='json')
            try:
                main.validate_plan(data.plan, source)
            except main.ConstraintError as failure:
                raise auth.AuthError('PLAN_CONSTRAINT', str(failure), 409) from None
            job_id = str(uuid.uuid4())
            db.execute('INSERT INTO jobs (id,owner_id,status,input_json,proposal_json) VALUES (?,?,?,?,?)',
                       (job_id, owner_id(), 'done', json.dumps(source, ensure_ascii=False), main.Turn(plan=data.plan).model_dump_json()))
            plant = main.confirm_plan_in_transaction(db, job_id, data.plan)
            if isinstance(plant, Response):
                failure = json.loads(plant.body)['error']
                raise auth.AuthError(failure['code'], failure['message'], plant.status_code)
            if fields.kind == 'goal':
                for i, day in enumerate(data.goal_dates):
                    db.execute('INSERT INTO goal_action_states (owner_id,plant_id,step_index,completed,due_date) VALUES (?,?,?,0,?)',
                               (owner_id(), plant['id'], i, day.isoformat() if day else None))
            draft.update(state='confirmed', proposal=data.plan.model_dump(mode='json'),
                         goal_dates=[d.isoformat() if d else None for d in data.goal_dates],
                         plant_id=plant['id'], plant=plant, confirmation_hash=fingerprint)
            result['text'] = '已种植到你的花园，今日行动与原页面已同步。'
            db.execute('UPDATE agent_turns SET response_json=? WHERE id=? AND owner_id=?', (json.dumps(result, ensure_ascii=False), turn_id, owner_id()))
            return plant

    @app.post('/api/v1/agent/turns/{turn_id}/discard')
    def discard(turn_id: str, data: VersionInput):
        agent.cleanup()
        with main.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row, result = get_draft(db, turn_id, data.version)
            if result['data']['state'] == 'discarded':
                return agent.public_turn(row)
            require_latest(db, row)
            if result['data']['state'] not in ('ready', 'collecting'):
                raise auth.AuthError('NOT_READY', '这份草案已种植或已有新版。', 409)
            result['data']['state'] = 'discarded'
            db.execute('UPDATE agent_turns SET response_json=? WHERE id=? AND owner_id=?', (json.dumps(result, ensure_ascii=False), turn_id, owner_id()))
            return agent.public_turn(db.execute('SELECT * FROM agent_turns WHERE id=? AND owner_id=?', (turn_id, owner_id())).fetchone())
