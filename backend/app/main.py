import json
import hashlib
import re
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, model_validator
from app.constraints import ConstraintError, validate_plan, repeats_question, asks_known_duration
from app.identity import owner_id, acting_as, run_owned
from app import auth, account_privacy, legal, flowers


def load_local_config():
    config = Path(__file__).resolve().parents[1] / '.env'
    if not config.exists():
        return
    for line in config.read_text(encoding='utf-8').splitlines():
        if not line or line.lstrip().startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        name = name.strip()
        if name in {'MODEL_ENABLED', 'MODEL_API_KEY', 'MODEL_BASE_URL', 'MODEL_NAME', 'MODEL_CALL_RESERVE_CENTS', 'MODEL_CREDENTIAL_FILE', 'CLOUDBASE_ENV_ID', 'BLOOMATE_ALLOWED_ORIGINS', 'BLOOMATE_COOKIE_SECURE', 'BLOOMATE_APP_MODE', 'BLOOMATE_EMAIL_DAILY_LIMIT', 'BLOOMATE_EMAIL_ENABLED', 'BLOOMATE_PROXY_SHARED_SECRET', 'BLOOMATE_OPERATOR_NAME', 'BLOOMATE_CONTACT_EMAIL'}:
            os.environ.setdefault(name, value.strip().strip(chr(34)).strip(chr(39)))


load_local_config()

DB = os.environ.get('BLOOMATE_DB', str(Path(__file__).resolve().parents[2] / 'data' / 'bloomate.sqlite3'))


def connect():
    from app.repository import cloud_enabled
    if cloud_enabled():
        from app.repository.cloudbase import CloudConnection
        return CloudConnection()
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB, timeout=10, factory=account_privacy.OwnedConnection)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    return connection

def migrate_habit_stages(db):
    columns={row['name'] for row in db.execute('PRAGMA table_info(habit_stages)')}
    if 'active' in columns:
        return
    db.commit()
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE habit_stages_v2 (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), start_date TEXT NOT NULL, end_date TEXT NOT NULL, timezone TEXT NOT NULL, weekdays_json TEXT NOT NULL, completion_standard TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)))''')
        db.execute('INSERT INTO habit_stages_v2 (id,owner_id,plant_id,start_date,end_date,timezone,weekdays_json,completion_standard,active) SELECT id,owner_id,plant_id,start_date,end_date,timezone,weekdays_json,completion_standard,1 FROM habit_stages')
        db.execute('DROP TABLE habit_stages')
        db.execute('ALTER TABLE habit_stages_v2 RENAME TO habit_stages')
        db.execute('CREATE UNIQUE INDEX one_current_habit_stage ON habit_stages(owner_id,plant_id) WHERE active=1')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('习惯阶段迁移外键检查失败')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')


def init_db():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, status TEXT NOT NULL, input_json TEXT NOT NULL, proposal_json TEXT, error_code TEXT);
        CREATE TABLE IF NOT EXISTS usage_reservations (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, month TEXT NOT NULL, amount_cents INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS model_call_metrics (reservation_id TEXT PRIMARY KEY, model TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, status TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER, recorded_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS plants (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id), kind TEXT NOT NULL, title TEXT NOT NULL, plan_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS goal_action_states (owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), step_index INTEGER NOT NULL, completed INTEGER NOT NULL CHECK(completed IN (0,1)), PRIMARY KEY(owner_id, plant_id, step_index));
        CREATE TABLE IF NOT EXISTS habit_stages (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), start_date TEXT NOT NULL, end_date TEXT NOT NULL, timezone TEXT NOT NULL, weekdays_json TEXT NOT NULL, completion_standard TEXT NOT NULL, UNIQUE(owner_id,plant_id));
        CREATE TABLE IF NOT EXISTS habit_occurrences (stage_id TEXT NOT NULL REFERENCES habit_stages(id), action_date TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)), recorded_at TEXT, is_backfill INTEGER NOT NULL DEFAULT 0 CHECK(is_backfill IN (0,1)), PRIMARY KEY(stage_id,action_date));
        CREATE TABLE IF NOT EXISTS habit_events (id TEXT PRIMARY KEY, stage_id TEXT NOT NULL, action_date TEXT NOT NULL, completed INTEGER NOT NULL CHECK(completed IN (0,1)), recorded_at TEXT NOT NULL, is_backfill INTEGER NOT NULL CHECK(is_backfill IN (0,1)), FOREIGN KEY(stage_id,action_date) REFERENCES habit_occurrences(stage_id,action_date));
        CREATE TABLE IF NOT EXISTS plan_adjustments (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), step_index INTEGER NOT NULL, base_hash TEXT NOT NULL, original_text TEXT NOT NULL, request_text TEXT NOT NULL, status TEXT NOT NULL, suggestion TEXT, applied_text TEXT, error_code TEXT);
        CREATE TABLE IF NOT EXISTS plant_pauses (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), timezone TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_pause ON plant_pauses(owner_id,plant_id) WHERE end_date IS NULL;
        CREATE TABLE IF NOT EXISTS habit_schedule_proposals (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), stage_id TEXT NOT NULL REFERENCES habit_stages(id), base_hash TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS growth_standards (owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), threshold INTEGER NOT NULL CHECK(threshold>0), achieved INTEGER NOT NULL DEFAULT 0 CHECK(achieved IN (0,1)), PRIMARY KEY(owner_id,plant_id));
        CREATE TABLE IF NOT EXISTS growth_events (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), event TEXT NOT NULL, threshold INTEGER NOT NULL, recorded_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS growth_suggestions (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), status TEXT NOT NULL, suggestion TEXT, error_code TEXT);
        CREATE TABLE IF NOT EXISTS habit_advice (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), status TEXT NOT NULL, difficulty TEXT NOT NULL, suggestion TEXT, error_code TEXT);
        CREATE TABLE IF NOT EXISTS goal_action_events (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), step_index INTEGER NOT NULL, step_text TEXT NOT NULL, event TEXT NOT NULL, recorded_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS goal_action_weights (owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), step_index INTEGER NOT NULL, numerator TEXT NOT NULL, denominator TEXT NOT NULL, PRIMARY KEY(owner_id,plant_id,step_index));
        CREATE TABLE IF NOT EXISTS goal_collections (owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), title TEXT NOT NULL, plan_json TEXT NOT NULL, outcome TEXT NOT NULL, confirmed_at TEXT NOT NULL, PRIMARY KEY(owner_id,plant_id));
        CREATE TABLE IF NOT EXISTS habit_collections (owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), stage_id TEXT NOT NULL REFERENCES habit_stages(id), snapshot_json TEXT NOT NULL, confirmed_at TEXT, PRIMARY KEY(owner_id,stage_id));
        CREATE TABLE IF NOT EXISTS habit_next_proposals (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), base_stage_id TEXT NOT NULL REFERENCES habit_stages(id), base_hash TEXT NOT NULL, draft_json TEXT NOT NULL, status TEXT NOT NULL, new_stage_id TEXT REFERENCES habit_stages(id));
        CREATE TABLE IF NOT EXISTS habit_lifecycle (owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), ended INTEGER NOT NULL CHECK(ended IN (0,1)), PRIMARY KEY(owner_id,plant_id));
        CREATE TABLE IF NOT EXISTS completion_notices (owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), stage_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','continue','finished')), PRIMARY KEY(owner_id,plant_id,stage_id));
        CREATE TABLE IF NOT EXISTS goal_followups (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), title TEXT NOT NULL, steps_json TEXT NOT NULL, states_json TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)), completed_at TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_current_goal_followup ON goal_followups(owner_id,plant_id) WHERE active=1;
        CREATE TABLE IF NOT EXISTS goal_followup_dates (owner_id TEXT NOT NULL, stage_id TEXT NOT NULL REFERENCES goal_followups(id), step_index INTEGER NOT NULL CHECK(step_index>=0), due_date TEXT, PRIMARY KEY(owner_id,stage_id,step_index));
        CREATE TABLE IF NOT EXISTS goal_next_proposals (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), base_stage TEXT NOT NULL, title TEXT NOT NULL, steps_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','confirmed','discarded')), new_stage_id TEXT);
        CREATE TABLE IF NOT EXISTS memory_preferences (owner_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)));
        CREATE TABLE IF NOT EXISTS user_memories (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS care_preferences (owner_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)));
        CREATE TABLE IF NOT EXISTS care_messages (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), trigger_hash TEXT NOT NULL, created_at TEXT NOT NULL, dismissed INTEGER NOT NULL DEFAULT 0 CHECK(dismissed IN (0,1)), UNIQUE(owner_id,plant_id,trigger_hash));
        CREATE TABLE IF NOT EXISTS habit_plan_revisions (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, plant_id TEXT NOT NULL REFERENCES plants(id), stage_id TEXT NOT NULL REFERENCES habit_stages(id), base_hash TEXT NOT NULL, source_json TEXT NOT NULL, status TEXT NOT NULL, suggestion_json TEXT, before_json TEXT NOT NULL, error_code TEXT);
        ''')
        db.executescript(flowers.SCHEMA)
        migrate_habit_stages(db)
        if 'next_job_id' not in {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}:
            db.execute('ALTER TABLE jobs ADD COLUMN next_job_id TEXT')
        if 'enabled' not in {row['name'] for row in db.execute('PRAGMA table_info(habit_occurrences)')}:
            db.execute('ALTER TABLE habit_occurrences ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))')
        if 'due_date' not in {row['name'] for row in db.execute('PRAGMA table_info(goal_action_states)')}:
            db.execute('ALTER TABLE goal_action_states ADD COLUMN due_date TEXT')
        if 'cancelled' not in {row['name'] for row in db.execute('PRAGMA table_info(goal_action_states)')}:
            db.execute('ALTER TABLE goal_action_states ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0 CHECK(cancelled IN (0,1))')
        if 'suggestion_steps_json' not in {row['name'] for row in db.execute('PRAGMA table_info(plan_adjustments)')}:
            db.execute('ALTER TABLE plan_adjustments ADD COLUMN suggestion_steps_json TEXT')
        if 'applied_steps_json' not in {row['name'] for row in db.execute('PRAGMA table_info(plan_adjustments)')}:
            db.execute('ALTER TABLE plan_adjustments ADD COLUMN applied_steps_json TEXT')
        db.execute("UPDATE jobs SET status='error', error_code='INTERRUPTED' WHERE status IN ('pending','running')")
        db.execute("UPDATE plan_adjustments SET status='error', error_code='INTERRUPTED' WHERE status IN ('pending','running')")
        db.execute("UPDATE growth_suggestions SET status='error', error_code='INTERRUPTED' WHERE status IN ('pending','running')")
        db.execute("UPDATE habit_advice SET status='error', error_code='INTERRUPTED' WHERE status IN ('pending','running')")
        db.execute("UPDATE habit_plan_revisions SET status='error', error_code='INTERRUPTED' WHERE status IN ('pending','running')")
        for plant in db.execute("SELECT * FROM plants WHERE kind='goal'").fetchall():
            with acting_as(plant['owner_id']):
                ensure_goal_weights(db,plant)
                if plant['owner_id']==owner_id() and not db.execute('SELECT 1 FROM goal_followups WHERE owner_id=? AND plant_id=?',(owner_id(),plant['id'])).fetchone():
                    counts=growth_counts(db,plant)
                    if counts and counts['total'] and counts['completed']>=counts['total'] and not db.execute('SELECT 1 FROM goal_collections WHERE owner_id=? AND plant_id=?',(owner_id(),plant['id'])).fetchone():
                        db.execute('INSERT INTO goal_collections VALUES (?,?,?,?,?,?)',(owner_id(),plant['id'],plant['title'],plant['plan_json'],'',datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()))
                        db.execute("INSERT OR IGNORE INTO completion_notices VALUES (?,?,'initial','pending')",(owner_id(),plant['id']))
        for plant in db.execute("SELECT * FROM plants WHERE kind='habit'").fetchall():
            with acting_as(plant['owner_id']):
                stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(plant['owner_id'],plant['id'])).fetchone()
                standard=db.execute('SELECT * FROM growth_standards WHERE owner_id=? AND plant_id=? AND achieved=1',(plant['owner_id'],plant['id'])).fetchone()
                event=db.execute("SELECT recorded_at FROM growth_events WHERE owner_id=? AND plant_id=? AND event='achieved' ORDER BY rowid DESC LIMIT 1",(plant['owner_id'],plant['id'])).fetchone()
                if stage and standard and event and plant['owner_id']==owner_id():habit_snapshot(db,plant,stage,standard,event['recorded_at'])
                if stage and plant['owner_id']==owner_id():
                    last=db.execute('SELECT recorded_at FROM habit_events WHERE stage_id=? AND completed=1 ORDER BY rowid DESC LIMIT 1',(stage['id'],)).fetchone()
                    if last:finish_habit_when_ready(db,plant['id'],stage,last['recorded_at'])


@asynccontextmanager
async def lifespan(_app):
    from app.repository import check_config
    check_config()
    init_db()
    auth.init_schema()
    account_privacy.init_schema()
    account_privacy.replay_erasures()
    auth.cleanup_expired_guests()
    import asyncio
    async def cleanup_loop():
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(account_privacy.replay_erasures)
                await asyncio.to_thread(auth.cleanup_expired_guests)
            except (sqlite3.Error, OSError, auth.AuthError):
                import logging
                logging.getLogger('bloomate').warning('Guest cleanup failed; retry scheduled. Access expiry remains enforced.')
    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)
auth.install(app)
account_privacy.install(app)
legal.install(app)
flowers.install(app)


def error(code, message, status=400):
    return JSONResponse(status_code=status, content={'error': {'code': code, 'message': message}})


@app.get('/api/health')
def health():
    from app.repository import cloud_enabled
    with connect() as db:
        db.execute('SELECT 1').fetchone()
    return {'status': 'ok', 'storage': 'cloudbase_rest' if cloud_enabled() else 'sqlite'}


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, _exc: RequestValidationError):
    return error('INVALID_INPUT', '请检查输入内容后重试。', 422)


@app.exception_handler(Exception)
async def unexpected_error(_request: Request, _exc: Exception):
    return error('INTERNAL_ERROR', '暂时无法完成操作，请稍后重试。', 500)


class FollowUp(BaseModel):
    answer: str = Field(min_length=1, max_length=1000)


class ActionUpdate(BaseModel):
    completed: bool = Field(strict=True)


class HabitStageInput(BaseModel):
    start_date: date
    end_date: date
    timezone: str = Field(min_length=1, max_length=100)
    weekdays: list[Annotated[int, Field(strict=True, ge=0, le=6)]] = Field(min_length=1, max_length=7)
    completion_standard: str = Field(min_length=1, max_length=200, pattern=r'\S')
    growth_threshold: int | None = Field(default=None, strict=True, ge=1, le=3661)

    @model_validator(mode='after')
    def valid_stage(self):
        if self.end_date < self.start_date or (self.end_date - self.start_date).days > 3660:
            raise ValueError('invalid date range')
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError('duplicate weekdays')
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('invalid timezone') from None
        return self


class PlanInput(BaseModel):
    kind: str = Field(pattern='^(goal|habit)$')
    goal: str = Field(min_length=2, max_length=500)
    background: str = Field(default='', max_length=1000)
    availability: str = Field(min_length=1, max_length=200)
    session_budget_minutes: int | None = Field(default=None, ge=1, le=720)
    habit_completion_minutes: int | None = Field(default=None, ge=1, le=720)
    habit_schedule: HabitStageInput | None = None

    @model_validator(mode='after')
    def check_user_constraints(self):
        if self.kind != 'habit' and self.habit_schedule is not None:
            raise ValueError('habit schedule applies to habits only')
        if self.habit_schedule is not None:
            dates=[self.habit_schedule.start_date + timedelta(days=i) for i in range((self.habit_schedule.end_date-self.habit_schedule.start_date).days+1)]
            planned=sum(day.weekday() in self.habit_schedule.weekdays for day in dates)
            if not planned or self.habit_schedule.growth_threshold is None or self.habit_schedule.growth_threshold>planned:
                raise ValueError('invalid habit threshold')
        if self.kind != 'habit' and self.habit_completion_minutes is not None:
            raise ValueError('completion minutes apply to habits only')
        if self.session_budget_minutes is not None and self.habit_completion_minutes is not None and self.habit_completion_minutes > self.session_budget_minutes:
            raise ValueError('completion exceeds available time')
        return self


class HabitCheckinInput(BaseModel):
    completed: bool = Field(strict=True)
    performed: bool = Field(default=False, strict=True)


class AdjustmentInput(BaseModel):
    step_index: int = Field(strict=True, ge=0)
    difficulty: str = Field(min_length=1, max_length=1000, pattern=r'\S')


class AdjustmentConfirmation(BaseModel):
    text: str | None = Field(default=None,min_length=1, max_length=500, pattern=r'\S')
    steps: list[Annotated[str,Field(min_length=1,max_length=500,pattern=r'\S')]] | None = Field(default=None,min_length=1,max_length=5)

    @model_validator(mode='after')
    def one_form(self):
        if (self.text is None)==(self.steps is None):raise ValueError('provide text or steps')
        return self


class PauseInput(BaseModel):
    paused: bool = Field(strict=True)


class HabitScheduleInput(BaseModel):
    start_date: date | None = None
    end_date: date
    weekdays: list[Annotated[int, Field(strict=True, ge=0, le=6)]] = Field(min_length=1, max_length=7)

    @model_validator(mode='after')
    def validate_weekdays(self):
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError('duplicate weekdays')
        return self


class GrowthStandardInput(BaseModel):
    threshold: int = Field(strict=True, ge=1, le=3661)


class ActionDateInput(BaseModel):
    due_date: date | None


class HabitAdviceInput(BaseModel):
    difficulty: str = Field(min_length=1,max_length=1000,pattern=r'\S')


class ActionTextInput(BaseModel):
    text: str = Field(min_length=1,max_length=500,pattern=r'\S')


class ActionCancelInput(BaseModel):
    cancelled: bool = Field(strict=True)

class GoalCompletionInput(BaseModel):
    outcome: str = Field(default='', max_length=500)

class MemoryPreferenceInput(BaseModel):
    enabled: bool = Field(strict=True)

class CarePreferenceInput(BaseModel):
    enabled: bool = Field(strict=True)

class MemoryInput(BaseModel):
    content: str = Field(min_length=1,max_length=200,pattern=r'\S')

class FlowerChoice(BaseModel):
    species: flowers.FlowerSpecies


class SessionAction(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern='^(core|other)$')
    minutes: int = Field(ge=1, le=720)


class Plan(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    steps: list[Annotated[str, Field(min_length=1, max_length=500, pattern=r'\S')]] = Field(min_length=1, max_length=10)
    session_actions: list[SessionAction] | None = Field(default=None, max_length=10)
    growth_nodes: list[Annotated[int, Field(strict=True, ge=1, le=99)]] | None = Field(default=None, min_length=4, max_length=4)
    flower_species: flowers.FlowerSpecies | None = None
    flower_reason: str | None = Field(default=None,max_length=160)

    @model_validator(mode='after')
    def ordered_growth_nodes(self):
        if self.growth_nodes is not None and any(a >= b for a,b in zip(self.growth_nodes,self.growth_nodes[1:])):
            raise ValueError('growth nodes must be strictly increasing')
        return self


class Turn(BaseModel):
    question: str | None = Field(default=None, max_length=500)
    plan: Plan | None = None


def reserve_budget():
    # Conservative per-call reservation; provider prices and limits must be verified before enabling.
    amount = int(os.environ.get('MODEL_CALL_RESERVE_CENTS', '0'))
    if not 1 <= amount <= 5000:
        raise RuntimeError('BUDGET_NOT_CONFIGURED')
    month = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        used = db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM usage_reservations WHERE month=?', (month,)).fetchone()[0]
        if auth.owner_is_guest(owner_id()) and db.execute('SELECT count(*) FROM usage_reservations WHERE owner_id=?', (owner_id(),)).fetchone()[0] >= 10:
            raise RuntimeError('BUDGET_EXHAUSTED')
        if used + amount > 5000:
            raise RuntimeError('BUDGET_EXHAUSTED')
        reservation_id = str(uuid.uuid4())
        db.execute('INSERT INTO usage_reservations VALUES (?,?,?,?)', (reservation_id, owner_id(), month, amount))
    return reservation_id


def record_model_call(reservation_id: str, model: str, elapsed_ms: int, status: str, usage: dict | None = None):
    usage = usage if isinstance(usage, dict) else {}
    prompt_tokens = usage.get('prompt_tokens') if isinstance(usage.get('prompt_tokens'), int) else None
    completion_tokens = usage.get('completion_tokens') if isinstance(usage.get('completion_tokens'), int) else None
    with connect() as db:
        db.execute('INSERT INTO model_call_metrics VALUES (?,?,?,?,?,?,?)', (reservation_id, model, elapsed_ms, status, prompt_tokens, completion_tokens, datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()))

def with_confirmed_memory(data: dict) -> dict:
    result=dict(data)
    if data.get('kind') not in ('goal','habit'):return result
    with connect() as db:
        pref=db.execute('SELECT enabled FROM memory_preferences WHERE owner_id=?',(owner_id(),)).fetchone()
        if pref and pref['enabled']:
            rows=db.execute('SELECT content FROM user_memories WHERE owner_id=? ORDER BY created_at,id LIMIT 20',(owner_id(),)).fetchall()
            if rows:result['_confirmed_preferences']=[r['content'] for r in rows]
    return result


def generate(data: dict) -> Turn:
    if os.environ.get('BLOOMATE_TEST_MODEL') == '1':
        if data['goal'] == '需要澄清的目标' and not data.get('history'):
            return Turn(question='你希望什么时候完成？')
        limit = data.get('session_budget_minutes')
        core = data.get('habit_completion_minutes') or limit or 10
        actions = [SessionAction(text='测试核心行动', kind='core', minutes=core)] if limit or data.get('habit_completion_minutes') else None
        return Turn(plan=Plan(title=data['goal'], summary='测试提案', steps=['测试步骤'], session_actions=actions))
    if os.environ.get('MODEL_ENABLED') != '1':
        raise RuntimeError('MODEL_NOT_CONFIGURED')
    key = os.environ.get('MODEL_API_KEY', '')
    key_file = os.environ.get('MODEL_CREDENTIAL_FILE', '')
    if not key and key_file in ('.env.deepseek', '.env.qwen'):
        credential = Path(__file__).resolve().parents[1] / key_file
        if credential.is_file():
            for line in credential.read_text(encoding='utf-8').splitlines():
                if line.startswith('MODEL_API_KEY='):
                    key = line.partition('=')[2].strip()
                    break
    base = os.environ.get('MODEL_BASE_URL', '')
    model = os.environ.get('MODEL_NAME', '')
    if not key or not base.startswith('https://') or not model:
        raise RuntimeError('MODEL_NOT_CONFIGURED')
    prompt = Path(__file__).with_name('prompt.txt').read_text(encoding='utf-8')
    with connect() as flower_db:
        flower_db.execute('BEGIN IMMEDIATE')
        allowed_flowers = flowers.available_species(flower_db, owner_id())
    prompt += '\n本次本人可用花种：' + json.dumps({r[0]:r[1] for r in flowers.FLOWERS if r[0] in allowed_flowers}, ensure_ascii=False) + '。plan.flower_species只能从此集合选择，不得推荐尚未解锁的花种，也不得声称已解锁。'
    history = data.get('history', [])
    uncertain = any(any(term in item.get('answer', '') for term in ('不确定', '不知道', '不清楚', '没想好')) for item in history)
    force_plan = len(history) >= 2 or uncertain or bool(data.get('_adjustment_force_plan')) or bool(data.get('_milestone_force_plan')) or bool(data.get('_habit_advice_force_plan')) or bool(data.get('_habit_plan_revision')) or bool(data.get('_goal_followup'))
    if data.get('_adjustment_force_plan'):
        prompt += '\n你正在调整用户选中的一个未完成步骤。根据困难可把它拆成一至五条可执行小步骤，steps 只列这些替代步骤，不能假装修改其他步骤或已完成成果。若原步骤写明第几天，第一条须保留同一天标记。'
    if data.get('_milestone_force_plan'):
        prompt += '\n你正在为用户提出一个阶段成果标准建议。根据已有信息提出合理、可修改的完成次数，明确这是建议而非承诺；steps 恰好一条，不能声称用户已经同意或已经完成。'
    if data.get('_habit_advice_force_plan'):
        prompt += '\n用户的习惯遇到了困难。只给出一条针对现有周期的可执行建议，steps 恰好一条；不要假称用户已修改计划、已放弃目标或已完成行动，不能擅自降低单次完成标准。若建议调整频率或结束日期，说明需由用户另行预览并确认。'
    if data.get('_habit_plan_revision'):
        prompt += '\n用户要求将现有习惯计划调整到已确认的 habit_schedule 范围。只生成该周期内的可编辑计划，不改变周期、不编造已完成记录；本次只是提案，用户确认后才生效。'
    if force_plan:
        prompt += '\n用户已表示不确定或已澄清两轮，必须输出 plan，question 必须为 null；保留未知项为待确认，禁止继续追问或编造。'
    for attempt in range(2):
        reservation_id = reserve_budget()
        started = time.monotonic()
        try:
            payload = {'model': model, 'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': json.dumps(with_confirmed_memory(data), ensure_ascii=False)}], 'response_format': {'type': 'json_object'}, 'max_tokens': 1024}
            if model.startswith('qwen'):
                payload['enable_thinking'] = False
            elif model.startswith('deepseek'):
                payload['thinking'] = {'type': 'disabled'}
            response = httpx.post(base.rstrip('/') + '/chat/completions', headers={'Authorization': f'Bearer {key}'}, json=payload, timeout=30)
            response.raise_for_status()
            body = response.json()
            record_model_call(reservation_id, model, round((time.monotonic() - started) * 1000), 'response', body.get('usage'))
            result = Turn.model_validate_json(body['choices'][0]['message']['content'])
            if bool(result.question) == bool(result.plan):
                raise ValueError('invalid turn')
            if result.question and force_plan:
                raise ValueError('clarification limit')
            if result.question and asks_known_duration(result.question, data):
                prompt += '\n用户目标中已经明确时长，不要重复询问已知时长；如有必要只问仍未知的条件。'
                raise ValueError('known duration question')
            if result.question and repeats_question(result.question, data.get('history', [])):
                prompt += '\n刚才的追问与已问问题过于相似。不要再问相同问题；根据已有回答给可编辑的最小方案，未知信息标为待确认。'
                raise ValueError('repeated clarification')
            if data.get('_adjustment_force_plan') and result.plan and len(result.plan.steps) > 5:
                prompt += '\n调整步骤最多五条，请重新给出一至五条。'
                raise ValueError('adjustment step count')
            if (data.get('_milestone_force_plan') or data.get('_habit_advice_force_plan')) and result.plan and len(result.plan.steps) != 1:
                prompt += '\n上次 steps 不止一条。只返回一个选中步骤的替代方案，steps 数组必须恰好一条。'
                raise ValueError('adjustment step count')
            if result.plan:
                validate_plan(result.plan, data)
            return result
        except (httpx.HTTPError, KeyError, IndexError, ValueError, ValidationError) as exc:
            if isinstance(exc, ConstraintError):
                prompt += '\n上次计划未通过校验：' + str(exc) + ' 请根据用户已提供的条件修正。'
            elif isinstance(exc, ValidationError):
                prompt += '\n上次输出的字段或长度不符合要求。请重新输出有效 JSON；steps 必须为1至10条，长期计划可按周分组，session_actions 最多10项。'
                if any('growth_nodes' in e['loc'] for e in exc.errors()):
                    prompt += '\ngrowth_nodes 必须恰好4个严格递增整数，与steps的数量和计划天数无关；即使只有1条或3条步骤，也必须保留四个成长节点，例如[14,42,71,85]。'
            with connect() as db:
                recorded = db.execute('SELECT 1 FROM model_call_metrics WHERE reservation_id=?', (reservation_id,)).fetchone()
            if not recorded:
                record_model_call(reservation_id, model, round((time.monotonic() - started) * 1000), 'error')
            if attempt == 1:
                raise RuntimeError('PLAN_CONSTRAINT' if isinstance(exc, ConstraintError) else 'MODEL_FAILED') from None
    raise RuntimeError('MODEL_FAILED')


def process(job_id: str, data: dict):
    try:
        with connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=? AND owner_id=?", (job_id, owner_id()))
        result = generate(data)
        if result.plan:
            validate_plan(result.plan, data)
            with connect() as flower_db:
                flower_db.execute('BEGIN IMMEDIATE')
                flowers.require_flower(flower_db, owner_id(), result.plan.flower_species)
        with connect() as db:
            db.execute("UPDATE jobs SET status='done', proposal_json=? WHERE id=? AND owner_id=?", (result.model_dump_json(), job_id, owner_id()))
    except Exception as exc:
        code = 'FLOWER_LOCKED' if isinstance(exc, auth.AuthError) and exc.code == 'FLOWER_LOCKED' else 'PLAN_CONSTRAINT' if isinstance(exc, ConstraintError) else (str(exc) if str(exc) in ('MODEL_NOT_CONFIGURED', 'MODEL_FAILED', 'PLAN_CONSTRAINT', 'BUDGET_NOT_CONFIGURED', 'BUDGET_EXHAUSTED', 'FLOWER_LOCKED') else 'MODEL_FAILED')
        with connect() as db:
            db.execute("UPDATE jobs SET status='error', error_code=? WHERE id=? AND owner_id=?", (code, job_id, owner_id()))


@app.post('/api/v1/plans', status_code=202)
def create_plan(data: PlanInput, background_tasks: BackgroundTasks):
    if auth.owner_is_guest(owner_id()):
        with connect() as db:
            if db.execute('SELECT count(*) FROM jobs WHERE owner_id=?', (owner_id(),)).fetchone()[0] >= 10:
                return error('GUEST_LIMIT', '本次体验的规划次数已用完，请先保存到账号。', 429)
    job_id = str(uuid.uuid4())
    with connect() as db:
        db.execute('INSERT INTO jobs (id,owner_id,status,input_json) VALUES (?,?,?,?)', (job_id, owner_id(), 'pending', data.model_dump_json()))
    background_tasks.add_task(run_owned, owner_id(), process, job_id, data.model_dump(mode='json'))
    return {'id': job_id, 'status': 'pending'}


@app.get('/api/v1/plans/{job_id}')
def get_plan(job_id: str):
    with connect() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=? AND owner_id=?', (job_id, owner_id())).fetchone()
    if row is None:
        return error('NOT_FOUND', '没有找到这份计划。', 404)
    turn = json.loads(row['proposal_json']) if row['proposal_json'] else {}
    source = json.loads(row['input_json'])
    result = {'id': row['id'], 'status': row['status'], 'next_job_id': row['next_job_id'], 'proposal': turn.get('plan'), 'question': turn.get('question'), 'constraints': {'session_budget_minutes': source.get('session_budget_minutes'), 'habit_completion_minutes': source.get('habit_completion_minutes'), 'habit_schedule':source.get('habit_schedule')}, 'kind':source.get('kind')}
    if source.get('_goal_followup'):
        result['followup_time']=source.get('followup_time',{'duration_days':None,'daily_minutes':None})
        result['followup_time_required']='followup_time' not in source
    if turn.get('plan'):
        try:
            result['constraint_check'] = validate_plan(Plan.model_validate(turn['plan']), source)
        except (ConstraintError, ValidationError):
            result['constraint_check'] = {'invalid': True}
    if row['status'] == 'error':
        messages = {'MODEL_NOT_CONFIGURED': '模型尚未配置，请先完成服务商与费用确认。', 'BUDGET_NOT_CONFIGURED': '模型费用限制尚未配置。', 'BUDGET_EXHAUSTED': '已达到本月模型预算，暂时停止调用。', 'PLAN_CONSTRAINT': '计划与时间或完成标准冲突，请重新生成。', 'FLOWER_LOCKED': '园丁推荐了尚未解锁的花种，请重新生成或选择已拥有的花种。'}
        message = messages.get(row['error_code'], '生成未完成，请稍后重试。')
        result['error'] = {'code': row['error_code'], 'message': message}
    return result


@app.post('/api/v1/plans/{job_id}/reply', status_code=202)
def reply(job_id: str, data: FollowUp, background_tasks: BackgroundTasks):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM jobs WHERE id=? AND owner_id=?', (job_id, owner_id())).fetchone()
        if row is None:
            return error('NOT_FOUND', '没有找到这段对话。', 404)
        turn = json.loads(row['proposal_json']) if row['proposal_json'] else {}
        if row['status'] == 'superseded' and row['next_job_id']:
            follow = db.execute('SELECT * FROM jobs WHERE id=? AND owner_id=?', (row['next_job_id'], owner_id())).fetchone()
            previous = json.loads(follow['input_json']).get('history', []) if follow else []
            if previous and previous[-1].get('answer') == data.answer:
                return {'id': row['next_job_id'], 'status': follow['status']}
            return error('ALREADY_ANSWERED', '这条问题已经回答，请继续后续对话。', 409)
        if row['status'] != 'done' or not turn.get('question'):
            return error('NOT_READY', '这段对话暂不能继续。', 409)
        original = json.loads(row['input_json'])
        history = original.get('history', [])
        if len(history) >= 5:
            return error('TURN_LIMIT', '对话轮数已达上限，请重新开始。', 409)
        history.append({'question': turn['question'], 'answer': data.answer})
        original['history'] = history
        new_id = str(uuid.uuid4())
        db.execute("UPDATE jobs SET status='superseded', next_job_id=? WHERE id=? AND owner_id=?", (new_id, job_id, owner_id()))
        db.execute('INSERT INTO jobs (id,owner_id,status,input_json) VALUES (?,?,?,?)', (new_id, owner_id(), 'pending', json.dumps(original, ensure_ascii=False)))
    background_tasks.add_task(run_owned, owner_id(), process, new_id, original)
    return {'id': new_id, 'status': 'pending'}


@app.post('/api/v1/proposals/{job_id}/confirm')
def confirm(job_id: str, edited: Plan | None = None):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM jobs WHERE id=? AND owner_id=?', (job_id, owner_id())).fetchone()
        if row is None:
            return error('NOT_FOUND', '没有找到这份计划。', 404)
        if json.loads(row['input_json']).get('_goal_followup'):
            return error('WRONG_KIND','后续阶段建议请在原目标中确认。',409)
        if row['status'] != 'done' or not row['proposal_json'] or not json.loads(row['proposal_json']).get('plan'):
            return error('NOT_READY', '计划尚未准备好。', 409)
        plant = db.execute('SELECT * FROM plants WHERE job_id=? AND owner_id=?', (job_id, owner_id())).fetchone()
        if not plant and auth.owner_is_guest(owner_id()) and db.execute('SELECT count(*) FROM plants WHERE owner_id=?', (owner_id(),)).fetchone()[0] >= 1:
            return error('GUEST_LIMIT', '先保存当前植物到账号，就可以继续种植。', 409)
        if not plant:
            plant_id = str(uuid.uuid4())
            data = json.loads(row['input_json'])
            plan = edited.model_dump(exclude_none=True) if edited else json.loads(row['proposal_json'])['plan']
            try:
                validate_plan(Plan.model_validate(plan), data)
            except ConstraintError as exc:
                return error('PLAN_CONSTRAINT', str(exc), 409)
            flowers.require_flower(db, owner_id(), plan.get('flower_species'))
            db.execute('INSERT INTO plants (id,owner_id,job_id,kind,title,plan_json) VALUES (?,?,?,?,?,?)', (plant_id, owner_id(), job_id, data['kind'], plan['title'], json.dumps(plan, ensure_ascii=False)))
            plant = db.execute('SELECT * FROM plants WHERE id=?', (plant_id,)).fetchone()
            if plant['kind']=='goal': ensure_goal_weights(db,plant)
            elif data.get('habit_schedule'):
                schedule=HabitStageInput.model_validate(data['habit_schedule'])
                stage_id=str(uuid.uuid4())
                db.execute('INSERT INTO habit_stages (id,owner_id,plant_id,start_date,end_date,timezone,weekdays_json,completion_standard) VALUES (?,?,?,?,?,?,?,?)',(stage_id,owner_id(),plant_id,schedule.start_date.isoformat(),schedule.end_date.isoformat(),schedule.timezone,json.dumps(sorted(schedule.weekdays)),schedule.completion_standard.strip()))
                dates=[schedule.start_date+timedelta(days=i) for i in range((schedule.end_date-schedule.start_date).days+1)]
                db.executemany('INSERT INTO habit_occurrences (stage_id,action_date) VALUES (?,?)',[(stage_id,day.isoformat()) for day in dates if day.weekday() in schedule.weekdays])
                db.execute('INSERT INTO growth_standards (owner_id,plant_id,threshold,achieved) VALUES (?,?,?,0)',(owner_id(),plant_id,schedule.growth_threshold))
                growth_event(db,plant_id,'standard_confirmed',schedule.growth_threshold)
        return {'id': plant['id'], 'title': plant['title'], 'kind': plant['kind'], 'plan': json.loads(plant['plan_json'])}


@app.get('/api/v1/garden')
def garden():
    with connect() as db:
        rows = db.execute('SELECT plants.id,plants.kind,plants.title,plants.plan_json,plants.created_at,jobs.input_json FROM plants JOIN jobs ON plants.job_id=jobs.id WHERE plants.owner_id=? AND jobs.owner_id=? ORDER BY plants.created_at DESC', (owner_id(),owner_id())).fetchall()
    return {'plants': [{'id': r['id'], 'kind': r['kind'], 'title': r['title'], 'plan': json.loads(r['plan_json']), 'created_at': r['created_at'], 'constraints': {'session_budget_minutes': json.loads(r['input_json']).get('session_budget_minutes'), 'habit_completion_minutes': json.loads(r['input_json']).get('habit_completion_minutes')}} for r in rows]}

@app.get('/api/v1/memory')
def get_memory():
    with connect() as db:
        pref=db.execute('SELECT enabled FROM memory_preferences WHERE owner_id=?',(owner_id(),)).fetchone()
        rows=db.execute('SELECT id,content,created_at,updated_at FROM user_memories WHERE owner_id=? ORDER BY created_at,id',(owner_id(),)).fetchall()
        return {'enabled':bool(pref and pref['enabled']),'items':[dict(r) for r in rows]}

def care_now():
    return datetime.now(ZoneInfo('Asia/Shanghai'))

def care_is_enabled(db):
    pref=db.execute('SELECT enabled FROM care_preferences WHERE owner_id=?',(owner_id(),)).fetchone()
    return bool(pref['enabled']) if pref else True

@app.get('/api/v1/care')
def get_care():
    with connect() as db:
        enabled=care_is_enabled(db)
        rows=db.execute('SELECT m.id,m.plant_id,m.created_at,p.title FROM care_messages m JOIN plants p ON p.id=m.plant_id AND p.owner_id=m.owner_id WHERE m.owner_id=? AND m.dismissed=0 ORDER BY m.created_at DESC',(owner_id(),)).fetchall() if enabled else []
        return {'enabled':enabled,'messages':[{'id':r['id'],'plant_id':r['plant_id'],'plant_title':r['title'],'created_at':r['created_at'],'text':'之前有两次计划行动没有完成记录。需要的话，我们可以一起看看怎么调整。'} for r in rows]}

@app.put('/api/v1/care/preference')
def set_care_preference(data: CarePreferenceInput):
    with connect() as db:
        db.execute('INSERT INTO care_preferences VALUES (?,?) ON CONFLICT(owner_id) DO UPDATE SET enabled=excluded.enabled',(owner_id(),int(data.enabled)))
        return {'enabled':data.enabled}

@app.post('/api/v1/care/refresh')
def refresh_care():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not care_is_enabled(db):return {'created':0}
        now=care_now();created=0
        for plant in db.execute('SELECT * FROM plants WHERE owner_id=?',(owner_id(),)).fetchall():
            if db.execute('SELECT 1 FROM plant_pauses WHERE owner_id=? AND plant_id=? AND end_date IS NULL',(owner_id(),plant['id'])).fetchone():continue
            actions=[]
            if plant['kind']=='habit':
                if db.execute('SELECT 1 FROM habit_lifecycle WHERE owner_id=? AND plant_id=? AND ended=1',(owner_id(),plant['id'])).fetchone():continue
                stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant['id'])).fetchone()
                if not stage:continue
                standard=db.execute('SELECT achieved FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant['id'])).fetchone()
                if standard and standard['achieved']:continue
                day=now.astimezone(ZoneInfo(stage['timezone'])).date().isoformat()
                for r in db.execute('SELECT action_date,completed FROM habit_occurrences WHERE stage_id=? AND enabled=1 AND action_date<? ORDER BY action_date',(stage['id'],day)):
                    if not is_paused_day(db,plant['id'],r['action_date']):actions.append((f"habit:{stage['id']}:{r['action_date']}",bool(r['completed'])))
            elif plant['kind']=='goal':
                if db.execute('SELECT 1 FROM goal_collections WHERE owner_id=? AND plant_id=?',(owner_id(),plant['id'])).fetchone():continue
                today=now.date().isoformat()
                for r in db.execute('SELECT step_index,due_date,completed FROM goal_action_states WHERE owner_id=? AND plant_id=? AND cancelled=0 AND due_date IS NOT NULL AND due_date<? ORDER BY due_date,step_index',(owner_id(),plant['id'],today)):
                    if not is_paused_day(db,plant['id'],r['due_date']):actions.append((f"goal:{r['step_index']}:{r['due_date']}",bool(r['completed'])))
            if len(actions)<2 or actions[-1][1] or actions[-2][1]:continue
            fingerprint=hashlib.sha256(json.dumps([actions[-2][0],actions[-1][0]],ensure_ascii=False).encode()).hexdigest()
            if db.execute('SELECT 1 FROM care_messages WHERE owner_id=? AND plant_id=? AND trigger_hash=?',(owner_id(),plant['id'],fingerprint)).fetchone():continue
            latest=db.execute('SELECT created_at FROM care_messages WHERE owner_id=? AND plant_id=? ORDER BY created_at DESC LIMIT 1',(owner_id(),plant['id'])).fetchone()
            if latest and now-datetime.fromisoformat(latest['created_at'])<timedelta(days=7):continue
            db.execute('INSERT INTO care_messages (id,owner_id,plant_id,trigger_hash,created_at) VALUES (?,?,?,?,?)',(str(uuid.uuid4()),owner_id(),plant['id'],fingerprint,now.isoformat()));created+=1
        return {'created':created}

@app.post('/api/v1/care/{message_id}/dismiss')
def dismiss_care(message_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT id FROM care_messages WHERE owner_id=? AND id=?',(owner_id(),message_id)).fetchone()
        if not row:return error('NOT_FOUND','没有找到这条留言。',404)
        db.execute('UPDATE care_messages SET dismissed=1 WHERE owner_id=? AND id=?',(owner_id(),message_id))
        return {'dismissed':True}

@app.put('/api/v1/memory/preference')
def set_memory_preference(data: MemoryPreferenceInput):
    with connect() as db:
        db.execute('INSERT INTO memory_preferences VALUES (?,?) ON CONFLICT(owner_id) DO UPDATE SET enabled=excluded.enabled',(owner_id(),int(data.enabled)))
        return {'enabled':data.enabled}

@app.post('/api/v1/memory/items',status_code=201)
def add_memory(data: MemoryInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        pref=db.execute('SELECT enabled FROM memory_preferences WHERE owner_id=?',(owner_id(),)).fetchone()
        if not pref or not pref['enabled']:return error('MEMORY_DISABLED','请先开启园丁记忆。',409)
        if db.execute('SELECT count(*) FROM user_memories WHERE owner_id=?',(owner_id(),)).fetchone()[0]>=20:return error('MEMORY_LIMIT','最多保存20条记忆，请先删除不需要的内容。',409)
        now=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat();memory_id=str(uuid.uuid4());content=data.content.strip()
        db.execute('INSERT INTO user_memories VALUES (?,?,?,?,?)',(memory_id,owner_id(),content,now,now))
        return {'id':memory_id,'content':content,'created_at':now,'updated_at':now}

@app.put('/api/v1/memory/items/{memory_id}')
def edit_memory(memory_id: str,data: MemoryInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        pref=db.execute('SELECT enabled FROM memory_preferences WHERE owner_id=?',(owner_id(),)).fetchone()
        if not pref or not pref['enabled']:return error('MEMORY_DISABLED','请先开启园丁记忆。',409)
        row=db.execute('SELECT id FROM user_memories WHERE id=? AND owner_id=?',(memory_id,owner_id())).fetchone()
        if not row:return error('NOT_FOUND','没有找到这条记忆。',404)
        now=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat();content=data.content.strip()
        db.execute('UPDATE user_memories SET content=?,updated_at=? WHERE id=? AND owner_id=?',(content,now,memory_id,owner_id()))
        return {'id':memory_id,'content':content,'updated_at':now}

@app.delete('/api/v1/memory/items/{memory_id}')
def delete_memory(memory_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT id FROM user_memories WHERE id=? AND owner_id=?',(memory_id,owner_id())).fetchone()
        if not row:return error('NOT_FOUND','没有找到这条记忆。',404)
        db.execute('DELETE FROM user_memories WHERE id=? AND owner_id=?',(memory_id,owner_id()))
        return {'deleted':True}

@app.get('/api/v1/plants/{plant_id}/collection')
def get_goal_collection(plant_id: str):
    with connect() as db:
        plant=db.execute('SELECT kind FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant: return error('NOT_FOUND','没有找到这株植物。',404)
        if plant['kind']!='goal': return error('WRONG_KIND','该收藏仅用于目标。',409)
        saved=db.execute('SELECT title,plan_json,outcome,confirmed_at FROM goal_collections WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        return {'collection': {'title':saved['title'],'plan':json.loads(saved['plan_json']),'outcome':saved['outcome'],'confirmed_at':saved['confirmed_at']} if saved else None}

@app.put('/api/v1/plants/{plant_id}/flower')
def choose_existing_flower(plant_id: str, choice: FlowerChoice):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT plan_json FROM plants WHERE owner_id=? AND id=?',(owner_id(),plant_id)).fetchone()
        if not plant: return error('NOT_FOUND','没有找到这株植物。',404)
        plan=json.loads(plant['plan_json'])
        if plan.get('flower_species'):
            if plan['flower_species']!=choice.species: return error('FLOWER_ALREADY_CHOSEN','这株植物已有品种。',409)
            return {'species':plan['flower_species']}
        flowers.require_flower(db,owner_id(),choice.species)
        plan['flower_species']=choice.species
        db.execute('UPDATE plants SET plan_json=? WHERE owner_id=? AND id=?',(json.dumps(plan,ensure_ascii=False),owner_id(),plant_id))
        return {'species':choice.species}

@app.post('/api/v1/plants/{plant_id}/collection')
def confirm_goal_collection(plant_id: str, data: GoalCompletionInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant: return error('NOT_FOUND','没有找到这株植物。',404)
        if plant['kind']!='goal': return error('WRONG_KIND','该收藏仅用于目标。',409)
        saved=db.execute('SELECT confirmed_at,outcome FROM goal_collections WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        if saved:
            flowers.record(db,owner_id(),'goal',plant_id,saved['confirmed_at'])
            if not saved['outcome'] and data.outcome.strip():db.execute('UPDATE goal_collections SET outcome=? WHERE owner_id=? AND plant_id=?',(data.outcome.strip(),owner_id(),plant_id))
            return {'confirmed_at':saved['confirmed_at']}
        counts=growth_counts(db,plant)
        if not counts or counts['total']==0 or counts['completed']<counts['total']:
            return error('INCOMPLETE','请先完成所有未取消的步骤，再确认目标完成。',409)
        confirmed_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
        db.execute('INSERT INTO goal_collections VALUES (?,?,?,?,?,?)',(owner_id(),plant_id,plant['title'],plant['plan_json'],data.outcome.strip(),confirmed_at))
        flowers.record(db,owner_id(),'goal',plant_id,confirmed_at)
        return {'confirmed_at':confirmed_at}


@app.get('/api/v1/plants/{plant_id}/actions')
def get_goal_actions(plant_id: str):
    with connect() as db:
        plant = db.execute('SELECT kind, plan_json FROM plants WHERE id=? AND owner_id=?', (plant_id, owner_id())).fetchone()
        if not plant:
            return error('NOT_FOUND', '没有找到这个目标。', 404)
        if plant['kind'] != 'goal':
            return error('WRONG_KIND', '习惯打卡将在后续阶段提供。', 409)
        states = {r['step_index']: r for r in db.execute('SELECT step_index, completed, due_date, cancelled FROM goal_action_states WHERE owner_id=? AND plant_id=?', (owner_id(), plant_id))}
        steps = json.loads(plant['plan_json'])['steps']
        return {'actions': [{'index': i, 'text': step, 'completed': bool(states[i]['completed']) if i in states else False, 'due_date':states[i]['due_date'] if i in states else None, 'cancelled':bool(states[i]['cancelled']) if i in states else False} for i, step in enumerate(steps)]}


@app.put('/api/v1/plants/{plant_id}/actions/{index}')
def set_goal_action(plant_id: str, index: int, update: ActionUpdate):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant = db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?', (plant_id, owner_id())).fetchone()
        if not plant:
            return error('NOT_FOUND', '没有找到这个目标。', 404)
        if plant['kind'] != 'goal':
            return error('WRONG_KIND', '习惯打卡将在后续阶段提供。', 409)
        if update.completed and db.execute('SELECT 1 FROM plant_pauses WHERE owner_id=? AND plant_id=? AND end_date IS NULL',(owner_id(),plant_id)).fetchone():
            return error('PAUSED','目标已暂停，恢复后再记录完成。',409)
        steps = json.loads(plant['plan_json'])['steps']
        if index < 0 or index >= len(steps):
            return error('NOT_FOUND', '没有找到这个步骤。', 404)
        ensure_goal_weights(db,plant)
        if db.execute('SELECT 1 FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone():return error('STAGE_ARCHIVED','请在当前阶段记录行动。',409)
        existing=db.execute('SELECT cancelled,completed FROM goal_action_states WHERE owner_id=? AND plant_id=? AND step_index=?',(owner_id(),plant_id,index)).fetchone()
        if update.completed and existing and existing['cancelled']:
            return error('CANCELLED','已取消的步骤需要先恢复。',409)
        db.execute('INSERT INTO goal_action_states (owner_id,plant_id,step_index,completed) VALUES (?,?,?,?) ON CONFLICT(owner_id,plant_id,step_index) DO UPDATE SET completed=excluded.completed', (owner_id(),plant_id,index,int(update.completed)))
        reconcile_growth(db,plant_id)
        stage_completed=False
        counts=growth_counts(db,plant)
        if update.completed and (not existing or not existing['completed']) and counts['total']>0 and counts['completed']>=counts['total']:
            saved=db.execute('SELECT 1 FROM goal_collections WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
            if not saved:
                confirmed_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
                db.execute('INSERT INTO goal_collections VALUES (?,?,?,?,?,?)',(owner_id(),plant_id,plant['title'],plant['plan_json'],'',confirmed_at))
                db.execute("INSERT OR IGNORE INTO completion_notices VALUES (?,?,'initial','pending')",(owner_id(),plant_id))
                stage_completed=True
        state=db.execute('SELECT due_date,cancelled FROM goal_action_states WHERE owner_id=? AND plant_id=? AND step_index=?',(owner_id(),plant_id,index)).fetchone()
        return {'index': index, 'text': steps[index], 'completed': update.completed,'due_date':state['due_date'],'cancelled':bool(state['cancelled']),'stage_completed':stage_completed}


@app.put('/api/v1/plants/{plant_id}/actions/{index}/date')
def set_goal_action_date(plant_id: str,index: int,data: ActionDateInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这个目标。',404)
        if plant['kind']!='goal':return error('WRONG_KIND','习惯的日期由周期管理。',409)
        steps=json.loads(plant['plan_json'])['steps']
        if index<0 or index>=len(steps):return error('NOT_FOUND','没有找到这个步骤。',404)
        due=data.due_date.isoformat() if data.due_date else None
        db.execute('INSERT INTO goal_action_states (owner_id,plant_id,step_index,completed,due_date) VALUES (?,?,?,0,?) ON CONFLICT(owner_id,plant_id,step_index) DO UPDATE SET due_date=excluded.due_date',(owner_id(),plant_id,index,due))
        row=db.execute('SELECT completed,due_date,cancelled FROM goal_action_states WHERE owner_id=? AND plant_id=? AND step_index=?',(owner_id(),plant_id,index)).fetchone()
        return {'index':index,'text':steps[index],'completed':bool(row['completed']),'due_date':row['due_date'],'cancelled':bool(row['cancelled'])}


def manual_action_event(db,plant_id,index,text,event):
    db.execute('INSERT INTO goal_action_events VALUES (?,?,?,?,?,?,?)',(str(uuid.uuid4()),owner_id(),plant_id,index,text,event,datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()))


def validate_manual_plan(db,plant,plan):
    source=json.loads(db.execute('SELECT input_json FROM jobs WHERE id=? AND owner_id=?',(plant['job_id'],owner_id())).fetchone()['input_json'])
    try:validate_plan(Plan.model_validate(plan),source)
    except (ConstraintError,ValidationError):return False
    return True


@app.post('/api/v1/plants/{plant_id}/actions',status_code=201)
def add_goal_action(plant_id: str,data: ActionTextInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这个目标。',404)
        if plant['kind']!='goal':return error('WRONG_KIND','习惯行动由周期管理。',409)
        plan=json.loads(plant['plan_json']);index=len(plan['steps']);plan['steps'].append(data.text)
        if not validate_manual_plan(db,plant,plan):return error('PLAN_CONSTRAINT','步骤过多或计划不符合约束，请修改后重试。',409)
        ensure_goal_weights(db,plant)
        db.execute('UPDATE plants SET plan_json=? WHERE id=? AND owner_id=?',(json.dumps(plan,ensure_ascii=False),plant_id,owner_id()))
        db.execute('INSERT INTO goal_action_weights VALUES (?,?,?,?,?)',(owner_id(),plant_id,index,'0','1'))
        rebalance_goal_weights(db,plant_id,plan)
        manual_action_event(db,plant_id,index,data.text,'added')
        return {'index':index,'text':data.text,'completed':False,'due_date':None,'cancelled':False}


@app.put('/api/v1/plants/{plant_id}/actions/{index}/text')
def edit_goal_action(plant_id: str,index: int,data: ActionTextInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这个目标。',404)
        if plant['kind']!='goal':return error('WRONG_KIND','习惯行动由周期管理。',409)
        plan=json.loads(plant['plan_json'])
        if index<0 or index>=len(plan['steps']):return error('NOT_FOUND','没有找到这个步骤。',404)
        state=db.execute('SELECT * FROM goal_action_states WHERE owner_id=? AND plant_id=? AND step_index=?',(owner_id(),plant_id,index)).fetchone()
        if state and (state['completed'] or state['cancelled']):return error('ACTION_LOCKED','已完成或取消的步骤，请先撤销或恢复。',409)
        previous=plan['steps'][index];plan['steps'][index]=data.text
        if not validate_manual_plan(db,plant,plan):return error('PLAN_CONSTRAINT','修改后的步骤不符合计划约束。',409)
        if previous!=data.text:
            db.execute('UPDATE plants SET plan_json=? WHERE id=? AND owner_id=?',(json.dumps(plan,ensure_ascii=False),plant_id,owner_id()))
            manual_action_event(db,plant_id,index,previous,'edited_before');manual_action_event(db,plant_id,index,data.text,'edited_after')
        return {'index':index,'text':data.text,'completed':False,'due_date':state['due_date'] if state else None,'cancelled':False}


@app.put('/api/v1/plants/{plant_id}/actions/{index}/cancel')
def cancel_goal_action(plant_id: str,index: int,data: ActionCancelInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这个目标。',404)
        if plant['kind']!='goal':return error('WRONG_KIND','习惯行动由周期管理。',409)
        steps=json.loads(plant['plan_json'])['steps']
        if index<0 or index>=len(steps):return error('NOT_FOUND','没有找到这个步骤。',404)
        state=db.execute('SELECT * FROM goal_action_states WHERE owner_id=? AND plant_id=? AND step_index=?',(owner_id(),plant_id,index)).fetchone()
        if data.cancelled and state and state['completed']:return error('ALREADY_COMPLETED','请先撤销完成，再取消步骤。',409)
        old=bool(state['cancelled']) if state else False
        if old!=data.cancelled:
            ensure_goal_weights(db,plant)
            db.execute('INSERT INTO goal_action_states (owner_id,plant_id,step_index,completed,cancelled) VALUES (?,?,?,0,?) ON CONFLICT(owner_id,plant_id,step_index) DO UPDATE SET cancelled=excluded.cancelled',(owner_id(),plant_id,index,int(data.cancelled)))
            rebalance_goal_weights(db,plant_id,json.loads(plant['plan_json']))
            manual_action_event(db,plant_id,index,steps[index],'cancelled' if data.cancelled else 'restored')
        return {'index':index,'text':steps[index],'completed':bool(state['completed']) if state else False,'due_date':state['due_date'] if state else None,'cancelled':data.cancelled}


def habit_today(timezone: str) -> date:
    return datetime.now(ZoneInfo(timezone)).date()

def habit_snapshot(db, plant, stage, standard, confirmed_at):
    existing=db.execute('SELECT 1 FROM habit_collections WHERE owner_id=? AND stage_id=?',(owner_id(),stage['id'])).fetchone()
    if existing: return
    entries=[dict(r) for r in db.execute('SELECT action_date,completed,enabled FROM habit_occurrences WHERE stage_id=? ORDER BY action_date',(stage['id'],))]
    snapshot={'title':plant['title'],'flower_species':json.loads(plant['plan_json']).get('flower_species'),'start_date':stage['start_date'],'end_date':stage['end_date'],'timezone':stage['timezone'],'weekdays':json.loads(stage['weekdays_json']),'completion_standard':stage['completion_standard'],'threshold':standard['threshold'],'completed':sum(bool(r['completed']) for r in entries),'entries':entries}
    db.execute('INSERT INTO habit_collections VALUES (?,?,?,?,?)',(owner_id(),plant['id'],stage['id'],json.dumps(snapshot,ensure_ascii=False),confirmed_at))
    if snapshot['completed'] >= snapshot['threshold']:
        flowers.record(db,owner_id(),'habit',stage['id'],confirmed_at)

def finish_habit_when_ready(db, plant_id, stage, confirmed_at=None):
    plant=db.execute('SELECT * FROM plants WHERE owner_id=? AND id=?',(owner_id(),plant_id)).fetchone()
    standard=db.execute('SELECT * FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
    counts=growth_counts(db,plant) if plant else None
    if not standard or not counts or counts['completed']<standard['threshold']:
        return False
    existing=db.execute('SELECT 1 FROM habit_collections WHERE owner_id=? AND stage_id=?',(owner_id(),stage['id'])).fetchone()
    if not standard['achieved']:
        db.execute('UPDATE growth_standards SET achieved=1 WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id))
        growth_event(db,plant_id,'achieved',standard['threshold'])
    if existing:
        return False
    habit_snapshot(db,plant,stage,standard,confirmed_at or datetime.now(ZoneInfo(stage['timezone'])).isoformat())
    db.execute("INSERT OR IGNORE INTO completion_notices VALUES (?,?,?,'pending')",(owner_id(),plant_id,stage['id']))
    return True

class CompletionChoice(BaseModel):
    choice: Literal['finish','continue']

class GoalNextInput(BaseModel):
    title: str = Field(min_length=1,max_length=200)
    steps: list[Annotated[str,Field(min_length=1,max_length=500)]] = Field(min_length=1,max_length=20)
    generation_id: str | None = Field(default=None,max_length=100)

    @model_validator(mode='after')
    def nonblank(self):
        self.title=self.title.strip()
        self.steps=[s.strip() for s in self.steps]
        if not self.title or any(not s for s in self.steps):raise ValueError('内容不能为空')
        return self

class GoalNextGenerateInput(BaseModel):
    title: str = Field(min_length=1,max_length=100,pattern=r'\S')
    details: str = Field(default='',max_length=1000)
    duration_days: int | None = Field(default=None,strict=True,ge=1,le=3650)
    daily_minutes: int | None = Field(default=None,strict=True,ge=1,le=720)

@app.post('/api/v1/plants/{plant_id}/goal-next-generation',status_code=202)
def generate_next_goal(plant_id: str,data: GoalNextGenerateInput,background_tasks: BackgroundTasks):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute("SELECT * FROM plants WHERE owner_id=? AND id=? AND kind='goal'",(owner_id(),plant_id)).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这个目标。',404)
        active=db.execute('SELECT * FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone()
        collection=db.execute('SELECT * FROM goal_collections WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        if (active and not active['completed_at']) or (not active and not collection):return error('NOT_ELIGIBLE','先完成当前阶段。',409)
        base=active['id'] if active else 'initial'
        original=json.loads(plant['plan_json'])
        previous={'title':active['title'],'steps':json.loads(active['steps_json'])} if active else {'title':collection['title'],'steps':json.loads(collection['plan_json'])['steps']}
        source=db.execute('SELECT input_json FROM jobs WHERE owner_id=? AND id=?',(owner_id(),plant['job_id'])).fetchone()
        inputs=json.loads(source['input_json']) if source else {}
        context={'原目标':plant['title'],'原计划概要':original.get('summary'),'上一阶段':previous,'用户原先提供的背景':inputs.get('background',''),'用户对新阶段的补充':data.details.strip()}
        time_details={'duration_days':data.duration_days,'daily_minutes':data.daily_minutes}
        availability=f'新阶段每天最多 {data.daily_minutes} 分钟' if data.daily_minutes is not None else '新阶段每天可用时间不确定，由园丁提出可修改建议'
        model_data={'kind':'goal','goal':data.title.strip(),'background':json.dumps(context,ensure_ascii=False),'availability':availability,'session_budget_minutes':data.daily_minutes,'followup_time':time_details,'history':[],'_goal_followup':True,'_followup_plant_id':plant_id,'_followup_base_stage':base}
        job_id=str(uuid.uuid4())
        db.execute('INSERT INTO jobs (id,owner_id,status,input_json) VALUES (?,?,?,?)',(job_id,owner_id(),'pending',json.dumps(model_data,ensure_ascii=False)))
    background_tasks.add_task(run_owned, owner_id(), process,job_id,model_data)
    return {'id':job_id,'status':'pending'}

@app.get('/api/v1/plants/{plant_id}/goal-stages')
def goal_stages(plant_id: str):
    with connect() as db:
        plant=db.execute('SELECT kind FROM plants WHERE owner_id=? AND id=?',(owner_id(),plant_id)).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这株植物。',404)
        if plant['kind']!='goal':return error('WRONG_KIND','此功能仅用于目标。',409)
        rows=db.execute('SELECT * FROM goal_followups WHERE owner_id=? AND plant_id=? ORDER BY rowid',(owner_id(),plant_id)).fetchall()
        stages=[]
        for r in rows:
            steps=json.loads(r['steps_json'])
            dates={v['step_index']:v['due_date'] for v in db.execute('SELECT step_index,due_date FROM goal_followup_dates WHERE owner_id=? AND stage_id=?',(owner_id(),r['id']))}
            stages.append({'id':r['id'],'title':r['title'],'steps':steps,'states':json.loads(r['states_json']),'dates':[dates.get(i) for i in range(len(steps))],'active':bool(r['active']),'completed_at':r['completed_at']})
        return {'stages':stages}

@app.put('/api/v1/goal-stages/{stage_id}/actions/{index}/date')
def set_goal_followup_action_date(stage_id: str,index: int,data: ActionDateInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        stage=db.execute('SELECT * FROM goal_followups WHERE owner_id=? AND id=? AND active=1',(owner_id(),stage_id)).fetchone()
        if not stage:return error('NOT_FOUND','没有找到当前阶段。',404)
        steps=json.loads(stage['steps_json']);states=json.loads(stage['states_json'])
        if index<0 or index>=len(steps):return error('NOT_FOUND','没有找到这个步骤。',404)
        due=data.due_date.isoformat() if data.due_date else None
        db.execute('INSERT INTO goal_followup_dates VALUES (?,?,?,?) ON CONFLICT(owner_id,stage_id,step_index) DO UPDATE SET due_date=excluded.due_date',(owner_id(),stage_id,index,due))
        return {'index':index,'text':steps[index],'completed':bool(states[index]),'due_date':due}

@app.post('/api/v1/plants/{plant_id}/goal-next-proposals',status_code=201)
def propose_next_goal(plant_id: str, data: GoalNextInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute("SELECT id FROM plants WHERE owner_id=? AND id=? AND kind='goal'",(owner_id(),plant_id)).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这个目标。',404)
        active=db.execute('SELECT * FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone()
        base=active['id'] if active else 'initial'
        ready=bool(active['completed_at']) if active else bool(db.execute('SELECT 1 FROM goal_collections WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone())
        if not ready:return error('NOT_ELIGIBLE','先完成当前阶段。',409)
        if data.generation_id:
            job=db.execute('SELECT status,input_json,proposal_json FROM jobs WHERE owner_id=? AND id=?',(owner_id(),data.generation_id)).fetchone()
            if not job:return error('NOT_FOUND','没有找到园丁建议。',404)
            source=json.loads(job['input_json'])
            if not source.get('_goal_followup') or source.get('_followup_plant_id')!=plant_id or source.get('_followup_base_stage')!=base:return error('PLAN_CHANGED','原阶段已变化，请重新生成建议。',409)
            if job['status']!='done' or not job['proposal_json'] or not json.loads(job['proposal_json']).get('plan'):return error('NOT_READY','园丁建议尚未准备好。',409)
        proposal=str(uuid.uuid4())
        db.execute('INSERT INTO goal_next_proposals VALUES (?,?,?,?,?,?,?,NULL)',(proposal,owner_id(),plant_id,base,data.title,json.dumps(data.steps,ensure_ascii=False),'pending'))
        return {'id':proposal,'status':'pending','title':data.title,'steps':data.steps}

@app.get('/api/v1/goal-next-proposals/{proposal_id}')
def get_goal_next_proposal(proposal_id: str):
    with connect() as db:
        row=db.execute('SELECT * FROM goal_next_proposals WHERE owner_id=? AND id=?',(owner_id(),proposal_id)).fetchone()
        if not row:return error('NOT_FOUND','没有找到阶段计划。',404)
        return {'id':row['id'],'status':row['status'],'title':row['title'],'steps':json.loads(row['steps_json'])}

@app.post('/api/v1/goal-next-proposals/{proposal_id}/{choice}')
def resolve_goal_next_proposal(proposal_id: str, choice: Literal['confirm','discard']):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        p=db.execute('SELECT * FROM goal_next_proposals WHERE owner_id=? AND id=?',(owner_id(),proposal_id)).fetchone()
        if not p:return error('NOT_FOUND','没有找到阶段计划。',404)
        if p['status']=='confirmed' and choice=='confirm':return {'status':'confirmed','stage_id':p['new_stage_id']}
        if p['status']!='pending':return error('NOT_READY','这份计划已处理。',409)
        if choice=='discard':
            db.execute("UPDATE goal_next_proposals SET status='discarded' WHERE id=?",(proposal_id,))
            return {'status':'discarded'}
        current=db.execute('SELECT * FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),p['plant_id'])).fetchone()
        if (current['id'] if current else 'initial')!=p['base_stage'] or (current and not current['completed_at']):return error('PLAN_CHANGED','原阶段已变化，请重新预览。',409)
        if current:db.execute('UPDATE goal_followups SET active=0 WHERE id=? AND owner_id=?',(current['id'],owner_id()))
        steps=json.loads(p['steps_json']);stage_id=str(uuid.uuid4())
        db.execute('INSERT INTO goal_followups VALUES (?,?,?,?,?,?,1,NULL)',(stage_id,owner_id(),p['plant_id'],p['title'],p['steps_json'],json.dumps([False]*len(steps))))
        db.execute("UPDATE goal_next_proposals SET status='confirmed',new_stage_id=? WHERE id=?",(stage_id,proposal_id))
        return {'status':'confirmed','stage_id':stage_id}

@app.put('/api/v1/goal-stages/{stage_id}/actions/{index}')
def set_goal_followup_action(stage_id: str,index: int,data: ActionUpdate):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM goal_followups WHERE owner_id=? AND id=? AND active=1',(owner_id(),stage_id)).fetchone()
        if not row:return error('NOT_FOUND','没有找到当前阶段。',404)
        if data.completed and db.execute('SELECT 1 FROM plant_pauses WHERE owner_id=? AND plant_id=? AND end_date IS NULL',(owner_id(),row['plant_id'])).fetchone():return error('PAUSED','目标已暂停，请先恢复。',409)
        states=json.loads(row['states_json']);steps=json.loads(row['steps_json'])
        if index<0 or index>=len(states):return error('NOT_FOUND','没有找到这个步骤。',404)
        states[index]=data.completed
        completed_at=row['completed_at'] or (datetime.now(ZoneInfo('Asia/Shanghai')).isoformat() if all(states) else None)
        db.execute('UPDATE goal_followups SET states_json=?,completed_at=? WHERE id=? AND owner_id=?',(json.dumps(states),completed_at,stage_id,owner_id()))
        newly_completed=bool(completed_at and not row['completed_at'])
        if newly_completed:db.execute("INSERT OR IGNORE INTO completion_notices VALUES (?,?,?,'pending')",(owner_id(),row['plant_id'],stage_id))
        return {'index':index,'text':steps[index],'completed':data.completed,'stage_completed':newly_completed}

@app.get('/api/v1/plants/{plant_id}/completion-notice')
def get_completion_notice(plant_id: str):
    with connect() as db:
        plant=db.execute('SELECT kind FROM plants WHERE owner_id=? AND id=?',(owner_id(),plant_id)).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这株植物。',404)
        stage=db.execute('SELECT id FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone() if plant['kind']=='habit' else None
        follow=db.execute('SELECT id FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone() if plant['kind']=='goal' else None
        stage_id=stage['id'] if stage else (follow['id'] if follow else 'initial' if plant['kind']=='goal' else None)
        row=db.execute('SELECT status FROM completion_notices WHERE owner_id=? AND plant_id=? AND stage_id=?',(owner_id(),plant_id,stage_id)).fetchone() if stage_id else None
        return {'status':row['status'] if row else None}

@app.get('/api/v1/completion-notices')
def pending_completion_notices():
    with connect() as db:
        results=[]
        for row in db.execute("SELECT n.plant_id,n.stage_id,p.kind,p.title FROM completion_notices n JOIN plants p ON p.id=n.plant_id AND p.owner_id=n.owner_id WHERE n.owner_id=? AND n.status='pending' ORDER BY n.rowid",(owner_id(),)):
            if row['kind']=='habit':
                stage=db.execute('SELECT id FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),row['plant_id'])).fetchone()
                current=stage['id'] if stage else None
            else:
                stage=db.execute('SELECT id FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),row['plant_id'])).fetchone()
                current=stage['id'] if stage else 'initial'
            if current==row['stage_id']:results.append({'plant_id':row['plant_id'],'kind':row['kind'],'title':row['title']})
        return {'notices':results}

@app.get('/api/v1/collections')
def get_collections():
    with connect() as db:
        items=[]
        for plant in db.execute('SELECT * FROM plants WHERE owner_id=? ORDER BY created_at DESC',(owner_id(),)):
            plant_id=plant['id']
            if plant['kind']=='habit':
                rows=db.execute('SELECT * FROM habit_collections WHERE owner_id=? AND plant_id=? ORDER BY rowid',(owner_id(),plant_id)).fetchall()
                for index,row in enumerate(rows):
                    snap=json.loads(row['snapshot_json'])
                    current=[dict(r) for r in db.execute('SELECT action_date,completed,enabled FROM habit_occurrences WHERE stage_id=? ORDER BY action_date',(row['stage_id'],))]
                    items.append({'id':row['stage_id'],'plant_id':plant_id,'kind':'habit','title':snap.get('title',plant['title']),'stage_number':index+1,'species':snap.get('flower_species'),'completed_at':row['confirmed_at'],'changed':current!=snap.get('entries',[]),'details':{'start_date':snap.get('start_date'),'end_date':snap.get('end_date'),'completion_standard':snap.get('completion_standard'),'completed':snap.get('completed'),'threshold':snap.get('threshold')}})
            elif plant['kind']=='goal':
                saved=db.execute('SELECT * FROM goal_collections WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
                if saved:
                    plan=json.loads(saved['plan_json'])
                    items.append({'id':'initial:'+plant_id,'plant_id':plant_id,'kind':'goal','title':saved['title'],'stage_number':1,'species':plan.get('flower_species'),'completed_at':saved['confirmed_at'],'changed':False,'details':{'summary':plan.get('summary'),'steps':plan.get('steps',[]),'outcome':saved['outcome']}})
                rows=db.execute('SELECT * FROM goal_followups WHERE owner_id=? AND plant_id=? ORDER BY rowid',(owner_id(),plant_id)).fetchall()
                species=json.loads(plant['plan_json']).get('flower_species')
                for index,row in enumerate(rows):
                    if row['completed_at']:
                        items.append({'id':row['id'],'plant_id':plant_id,'kind':'goal','title':row['title'],'stage_number':index+2,'species':species,'completed_at':row['completed_at'],'changed':not all(json.loads(row['states_json'])),'details':{'steps':json.loads(row['steps_json'])}})
        items.sort(key=lambda i:i['completed_at'] or '',reverse=True)
        return {'collections':items}

@app.post('/api/v1/plants/{plant_id}/completion-notice')
def choose_completion_notice(plant_id: str, data: CompletionChoice):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT kind FROM plants WHERE owner_id=? AND id=?',(owner_id(),plant_id)).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这株植物。',404)
        stage=db.execute('SELECT id FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone() if plant['kind']=='habit' else db.execute('SELECT id FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone()
        stage_id=stage['id'] if stage else ('initial' if plant['kind']=='goal' else None)
        row=db.execute('SELECT status FROM completion_notices WHERE owner_id=? AND plant_id=? AND stage_id=?',(owner_id(),plant_id,stage_id)).fetchone()
        if not row:return error('NOT_FOUND','没有找到完成提示。',404)
        if data.choice=='finish' and plant['kind']=='habit':
            db.execute('INSERT INTO habit_lifecycle VALUES (?,?,1) ON CONFLICT(owner_id,plant_id) DO UPDATE SET ended=1',(owner_id(),plant_id))
        status='finished' if data.choice=='finish' else 'continue'
        db.execute('UPDATE completion_notices SET status=? WHERE owner_id=? AND plant_id=? AND stage_id=?',(status,owner_id(),plant_id,stage_id))
        if data.choice=='finish' and plant['kind']=='goal':
            flowers.record(db,owner_id(),'goal',plant_id,flowers.timestamp())
        return {'status':status}

@app.get('/api/v1/plants/{plant_id}/habit-stages')
def list_habit_stages(plant_id: str):
    with connect() as db:
        plant=db.execute('SELECT * FROM plants WHERE owner_id=? AND id=?',(owner_id(),plant_id)).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这个习惯。',404)
        if plant['kind']!='habit':return error('WRONG_KIND','此功能仅适用于习惯。',409)
        ended=db.execute('SELECT ended FROM habit_lifecycle WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        collections=[]
        for row in db.execute('SELECT * FROM habit_collections WHERE owner_id=? AND plant_id=? ORDER BY rowid',(owner_id(),plant_id)):
            snap=json.loads(row['snapshot_json'])
            now=[dict(r) for r in db.execute('SELECT action_date,completed,enabled FROM habit_occurrences WHERE stage_id=? ORDER BY action_date',(row['stage_id'],))]
            active=db.execute('SELECT active FROM habit_stages WHERE id=? AND owner_id=?',(row['stage_id'],owner_id())).fetchone()
            collections.append({'stage_id':row['stage_id'],'snapshot':snap,'confirmed_at':row['confirmed_at'],'changed':now!=snap.get('entries',[]),'active':bool(active and active['active']),'current_entries':now})
        return {'ended':bool(ended and ended['ended']),'collections':collections}

@app.put('/api/v1/habit-stages/{stage_id}/occurrences/{action_date}')
def correct_archived_habit_occurrence(stage_id: str, action_date: date, update: HabitCheckinInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND id=?',(owner_id(),stage_id)).fetchone()
        if not stage:return error('NOT_FOUND','没有找到这个阶段。',404)
        if stage['active']:return error('USE_CURRENT_STAGE','请在当前阶段中修改打卡。',409)
        if not db.execute('SELECT 1 FROM habit_collections WHERE owner_id=? AND stage_id=?',(owner_id(),stage_id)).fetchone():return error('NOT_FOUND','没有找到该阶段收藏。',404)
        occurrence=db.execute('SELECT * FROM habit_occurrences WHERE stage_id=? AND action_date=? AND enabled=1',(stage_id,action_date.isoformat())).fetchone()
        if not occurrence:return error('NOT_SCHEDULED','这一天不是计划日。',404)
        if update.completed and not update.performed:return error('CONFIRM_REQUIRED','请确认当天确实完成了习惯。',422)
        if action_date>habit_today(stage['timezone']):return error('OUTSIDE_WINDOW','不能记录未来的行动。',409)
        if update.completed and is_paused_day(db,stage['plant_id'],action_date.isoformat()):return error('PAUSED','暂停日不能新增打卡。',409)
        if bool(occurrence['completed'])!=update.completed:
            recorded_at=datetime.now(ZoneInfo(stage['timezone'])).isoformat()
            db.execute('UPDATE habit_occurrences SET completed=?,recorded_at=?,is_backfill=? WHERE stage_id=? AND action_date=?',(int(update.completed),recorded_at,int(update.completed),stage_id,action_date.isoformat()))
            db.execute('INSERT INTO habit_events VALUES (?,?,?,?,?,?)',(str(uuid.uuid4()),stage_id,action_date.isoformat(),int(update.completed),recorded_at,int(update.completed)))
        return {'date':action_date.isoformat(),'completed':update.completed,'history_preserved':True}

@app.post('/api/v1/plants/{plant_id}/habit-end')
def end_habit(plant_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone()
        if not stage:return error('NOT_FOUND','没有找到当前习惯周期。',404)
        standard=db.execute('SELECT * FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        captured=db.execute('SELECT 1 FROM habit_collections WHERE owner_id=? AND stage_id=?',(owner_id(),stage['id'])).fetchone()
        if not standard or (not standard['achieved'] and not captured):return error('NOT_ELIGIBLE','请先确认阶段成果。',409)
        db.execute('INSERT INTO habit_lifecycle VALUES (?,?,1) ON CONFLICT(owner_id,plant_id) DO UPDATE SET ended=1',(owner_id(),plant_id))
        db.execute("UPDATE completion_notices SET status='finished' WHERE owner_id=? AND plant_id=? AND stage_id=? AND status='pending'",(owner_id(),plant_id,stage['id']))
        return {'ended':True}

@app.post('/api/v1/plants/{plant_id}/next-stage-proposals',status_code=201)
def propose_next_habit_stage(plant_id: str, data: HabitStageInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone()
        if not stage:return error('NOT_FOUND','没有找到当前习惯周期。',404)
        standard=db.execute('SELECT * FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        captured=db.execute('SELECT 1 FROM habit_collections WHERE owner_id=? AND stage_id=?',(owner_id(),stage['id'])).fetchone()
        if not standard or (not standard['achieved'] and not captured):return error('NOT_ELIGIBLE','请先确认阶段成果。',409)
        if data.timezone!=stage['timezone'] or data.start_date<=habit_today(stage['timezone']):return error('INVALID_SCHEDULE','新周期需从明天或以后开始，并使用原时区。',422)
        dates=[data.start_date+timedelta(days=i) for i in range((data.end_date-data.start_date).days+1)]
        count=sum(day.weekday() in data.weekdays for day in dates)
        if not count or data.growth_threshold is None or data.growth_threshold>count:return error('INVALID_STANDARD','请设置不超过计划日数量的阶段标准。',422)
        proposal=str(uuid.uuid4());draft=data.model_dump(mode='json')
        base=hashlib.sha256(f"{habit_schedule_hash(db,stage)}:{standard['threshold']}:{standard['achieved']}".encode()).hexdigest()
        db.execute('INSERT INTO habit_next_proposals VALUES (?,?,?,?,?,?,?,NULL)',(proposal,owner_id(),plant_id,stage['id'],base,json.dumps(draft,ensure_ascii=False),'pending'))
        return {'id':proposal,'status':'pending','after':draft,'planned_count':count}

@app.post('/api/v1/habit-next-proposals/{proposal_id}/confirm')
def confirm_next_habit_stage(proposal_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        proposal=db.execute('SELECT * FROM habit_next_proposals WHERE owner_id=? AND id=?',(owner_id(),proposal_id)).fetchone()
        if not proposal:return error('NOT_FOUND','没有找到这份新阶段计划。',404)
        if proposal['status']=='confirmed':return {'status':'confirmed','stage_id':proposal['new_stage_id']}
        if proposal['status']!='pending':return error('NOT_READY','这份计划已经放弃。',409)
        stage=db.execute('SELECT * FROM habit_stages WHERE id=? AND owner_id=? AND active=1',(proposal['base_stage_id'],owner_id())).fetchone()
        standard=db.execute('SELECT * FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),proposal['plant_id'])).fetchone()
        if not stage or not standard:return error('PLAN_CHANGED','原阶段已变化，请重新预览。',409)
        captured=db.execute('SELECT 1 FROM habit_collections WHERE owner_id=? AND stage_id=?',(owner_id(),stage['id'])).fetchone()
        if not standard['achieved'] and not captured:return error('PLAN_CHANGED','原阶段已变化，请重新预览。',409)
        base=hashlib.sha256(f"{habit_schedule_hash(db,stage)}:{standard['threshold']}:{standard['achieved']}".encode()).hexdigest()
        if base!=proposal['base_hash']:return error('PLAN_CHANGED','原阶段已变化，请重新预览。',409)
        draft=HabitStageInput.model_validate_json(proposal['draft_json'])
        if draft.start_date<=habit_today(stage['timezone']):return error('PLAN_CHANGED','新阶段开始日期已到，请重新预览。',409)
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(proposal['plant_id'],owner_id())).fetchone()
        last=db.execute("SELECT recorded_at FROM growth_events WHERE owner_id=? AND plant_id=? AND event='achieved' ORDER BY rowid DESC LIMIT 1",(owner_id(),plant['id'])).fetchone()
        habit_snapshot(db,plant,stage,standard,last['recorded_at'] if last else None)
        db.execute('UPDATE habit_stages SET active=0 WHERE id=? AND owner_id=?',(stage['id'],owner_id()))
        stage_id=str(uuid.uuid4())
        db.execute('INSERT INTO habit_stages (id,owner_id,plant_id,start_date,end_date,timezone,weekdays_json,completion_standard) VALUES (?,?,?,?,?,?,?,?)',(stage_id,owner_id(),plant['id'],draft.start_date.isoformat(),draft.end_date.isoformat(),draft.timezone,json.dumps(sorted(draft.weekdays)),draft.completion_standard.strip()))
        dates=[draft.start_date+timedelta(days=i) for i in range((draft.end_date-draft.start_date).days+1)]
        db.executemany('INSERT INTO habit_occurrences (stage_id,action_date) VALUES (?,?)',[(stage_id,d.isoformat()) for d in dates if d.weekday() in draft.weekdays])
        db.execute('UPDATE growth_standards SET threshold=?,achieved=0 WHERE owner_id=? AND plant_id=?',(draft.growth_threshold,owner_id(),plant['id']))
        growth_event(db,plant['id'],'standard_confirmed',draft.growth_threshold)
        db.execute('INSERT INTO habit_lifecycle VALUES (?,?,0) ON CONFLICT(owner_id,plant_id) DO UPDATE SET ended=0',(owner_id(),plant['id']))
        db.execute("UPDATE habit_next_proposals SET status='confirmed',new_stage_id=? WHERE id=? AND owner_id=?",(stage_id,proposal_id,owner_id()))
        return {'status':'confirmed','stage_id':stage_id}

@app.get('/api/v1/habit-next-proposals/{proposal_id}')
def read_next_habit_proposal(proposal_id: str):
    with connect() as db:
        row=db.execute('SELECT * FROM habit_next_proposals WHERE owner_id=? AND id=?',(owner_id(),proposal_id)).fetchone()
        if not row:return error('NOT_FOUND','没有找到这份新阶段计划。',404)
        return {'id':row['id'],'status':row['status'],'after':json.loads(row['draft_json']),'stage_id':row['new_stage_id']}

@app.post('/api/v1/habit-next-proposals/{proposal_id}/discard')
def discard_next_habit_proposal(proposal_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT status FROM habit_next_proposals WHERE owner_id=? AND id=?',(owner_id(),proposal_id)).fetchone()
        if not row:return error('NOT_FOUND','没有找到这份新阶段计划。',404)
        if row['status']=='confirmed':return error('NOT_READY','已确认的阶段不能放弃。',409)
        db.execute("UPDATE habit_next_proposals SET status='discarded' WHERE owner_id=? AND id=?",(owner_id(),proposal_id))
        return {'status':'discarded'}


@app.post('/api/v1/plants/{plant_id}/habit-stage', status_code=201)
def create_habit_stage(plant_id: str, data: HabitStageInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant = db.execute('SELECT kind FROM plants WHERE id=? AND owner_id=?', (plant_id, owner_id())).fetchone()
        if not plant:
            return error('NOT_FOUND', '没有找到这个习惯。', 404)
        if plant['kind'] != 'habit':
            return error('WRONG_KIND', '目标不能设置习惯周期。', 409)
        if db.execute('SELECT 1 FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1', (owner_id(), plant_id)).fetchone():
            return error('STAGE_EXISTS', '习惯已有周期，暂不能重复设置。', 409)
        if db.execute('SELECT 1 FROM plant_pauses WHERE owner_id=? AND plant_id=? AND timezone<>? LIMIT 1',(owner_id(),plant_id,data.timezone)).fetchone():
            return error('TIMEZONE_CONFLICT','现有暂停记录使用其他时区，请保持时区一致。',409)
        dates = [data.start_date + timedelta(days=i) for i in range((data.end_date - data.start_date).days + 1)]
        dates = [day.isoformat() for day in dates if day.weekday() in data.weekdays]
        if not dates:
            return error('NO_PLANNED_DAYS', '选择的时间范围内没有计划日。', 422)
        if data.growth_threshold is not None and data.growth_threshold > len(dates):
            return error('INVALID_STANDARD','阶段标准不能超过计划日数量。',422)
        stage_id = str(uuid.uuid4())
        db.execute('INSERT INTO habit_stages (id,owner_id,plant_id,start_date,end_date,timezone,weekdays_json,completion_standard) VALUES (?,?,?,?,?,?,?,?)', (stage_id, owner_id(), plant_id, data.start_date.isoformat(), data.end_date.isoformat(), data.timezone, json.dumps(sorted(data.weekdays)), data.completion_standard.strip()))
        db.executemany('INSERT INTO habit_occurrences (stage_id,action_date) VALUES (?,?)', [(stage_id, day) for day in dates])
        if data.growth_threshold is not None:
            db.execute('INSERT INTO growth_standards (owner_id,plant_id,threshold,achieved) VALUES (?,?,?,0)',(owner_id(),plant_id,data.growth_threshold))
            growth_event(db,plant_id,'standard_confirmed',data.growth_threshold)
    return {'id':stage_id, 'planned_count':len(dates)}


@app.get('/api/v1/plants/{plant_id}/habit-stage')
def get_habit_stage(plant_id: str):
    with connect() as db:
        plant = db.execute('SELECT kind FROM plants WHERE id=? AND owner_id=?', (plant_id, owner_id())).fetchone()
        if not plant:
            return error('NOT_FOUND', '没有找到这个习惯。', 404)
        if plant['kind'] != 'habit':
            return error('WRONG_KIND', '目标没有习惯周期。', 409)
        stage = db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1', (owner_id(),plant_id)).fetchone()
        if not stage:
            return {'stage':None, 'occurrences':[]}
        today = habit_today(stage['timezone'])
        rows = db.execute('SELECT action_date,completed,recorded_at,is_backfill FROM habit_occurrences WHERE stage_id=? AND enabled=1 AND action_date BETWEEN ? AND ? ORDER BY action_date DESC', (stage['id'], (today-timedelta(days=3)).isoformat(), today.isoformat())).fetchall()
        status='upcoming' if today.isoformat()<stage['start_date'] else ('review_due' if today.isoformat()>stage['end_date'] else 'active')
        if db.execute('SELECT 1 FROM habit_lifecycle WHERE owner_id=? AND plant_id=? AND ended=1',(owner_id(),plant_id)).fetchone():status='ended'
        return {'stage':{'id':stage['id'],'start_date':stage['start_date'],'end_date':stage['end_date'],'timezone':stage['timezone'],'weekdays':json.loads(stage['weekdays_json']),'completion_standard':stage['completion_standard'],'today':today.isoformat(),'status':status},'occurrences':[{'date':r['action_date'],'completed':bool(r['completed']),'recorded_at':r['recorded_at'],'is_backfill':bool(r['is_backfill']),'paused':is_paused_day(db,plant_id,r['action_date'])} for r in rows]}


@app.put('/api/v1/plants/{plant_id}/habit-occurrences/{action_date}')
def set_habit_occurrence(plant_id: str, action_date: date, update: HabitCheckinInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        stage = db.execute('SELECT s.* FROM habit_stages s JOIN plants p ON p.id=s.plant_id AND p.owner_id=s.owner_id WHERE s.owner_id=? AND s.plant_id=? AND p.kind=? AND s.active=1', (owner_id(),plant_id,'habit')).fetchone()
        if not stage:
            return error('NOT_FOUND', '没有找到这个习惯周期。', 404)
        ended=db.execute('SELECT ended FROM habit_lifecycle WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        if ended and ended['ended'] and update.completed:return error('HABIT_ENDED','这个习惯已结束，请先开启下一阶段。',409)
        occurrence = db.execute('SELECT * FROM habit_occurrences WHERE stage_id=? AND action_date=? AND enabled=1', (stage['id'],action_date.isoformat())).fetchone()
        if not occurrence:
            return error('NOT_SCHEDULED', '这一天不是习惯的计划日。', 404)
        today = habit_today(stage['timezone'])
        if action_date > today or (update.completed and action_date < today-timedelta(days=3)):
            return error('OUTSIDE_WINDOW', '只能打卡今天或补签之前三个自然日。', 409)
        if update.completed and not update.performed:
            return error('CONFIRM_REQUIRED', '请确认当天确实完成了习惯。', 422)
        if update.completed and not occurrence['completed'] and is_paused_day(db,plant_id,action_date.isoformat()):
            return error('PAUSED', '这一天是暂停日，不能新增打卡。', 409)
        stage_completed=False
        if bool(occurrence['completed']) != update.completed:
            recorded_at = datetime.now(ZoneInfo(stage['timezone'])).isoformat()
            backfill = action_date < today if update.completed else bool(occurrence['is_backfill'])
            db.execute('UPDATE habit_occurrences SET completed=?, recorded_at=?, is_backfill=? WHERE stage_id=? AND action_date=?', (int(update.completed),recorded_at,int(backfill),stage['id'],action_date.isoformat()))
            db.execute('INSERT INTO habit_events VALUES (?,?,?,?,?,?)', (str(uuid.uuid4()),stage['id'],action_date.isoformat(),int(update.completed),recorded_at,int(backfill)))
            reconcile_growth(db,plant_id)
            if update.completed:stage_completed=finish_habit_when_ready(db,plant_id,stage)
        row = db.execute('SELECT * FROM habit_occurrences WHERE stage_id=? AND action_date=?',(stage['id'],action_date.isoformat())).fetchone()
        return {'date':row['action_date'],'completed':bool(row['completed']),'recorded_at':row['recorded_at'],'is_backfill':bool(row['is_backfill']),'paused':is_paused_day(db,plant_id,row['action_date']),'stage_completed':stage_completed}


def adjustment_base(db, plant):
    states = [(row['step_index'],row['completed'],row['due_date'],row['cancelled']) for row in db.execute('SELECT step_index,completed,due_date,cancelled FROM goal_action_states WHERE owner_id=? AND plant_id=? AND (completed=1 OR due_date IS NOT NULL OR cancelled=1) ORDER BY step_index',(owner_id(),plant['id']))]
    payload = json.dumps({'plan':json.loads(plant['plan_json']),'completed':states},ensure_ascii=False,sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def process_adjustment(adjustment_id: str, model_data: dict):
    try:
        with connect() as db:
            db.execute("UPDATE plan_adjustments SET status='running' WHERE id=? AND owner_id=? AND status='pending'",(adjustment_id,owner_id()))
        turn = generate(model_data)
        if not turn.plan or not 1 <= len(turn.plan.steps) <= 5:
            raise RuntimeError('MODEL_FAILED')
        with connect() as db:
            db.execute("UPDATE plan_adjustments SET status='done', suggestion=?, suggestion_steps_json=? WHERE id=? AND owner_id=? AND status='running'",(turn.plan.steps[0],json.dumps(turn.plan.steps,ensure_ascii=False),adjustment_id,owner_id()))
    except Exception as exc:
        code = str(exc) if str(exc) in ('MODEL_NOT_CONFIGURED','MODEL_FAILED','PLAN_CONSTRAINT','BUDGET_NOT_CONFIGURED','BUDGET_EXHAUSTED') else 'MODEL_FAILED'
        with connect() as db:
            db.execute("UPDATE plan_adjustments SET status='error', error_code=? WHERE id=? AND owner_id=? AND status='running'",(code,adjustment_id,owner_id()))


@app.post('/api/v1/plants/{plant_id}/adjustments', status_code=202)
def create_adjustment(plant_id: str, data: AdjustmentInput, background_tasks: BackgroundTasks):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant = db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant:
            return error('NOT_FOUND','没有找到这个目标。',404)
        if plant['kind'] != 'goal':
            return error('WRONG_KIND','习惯调整将在后续阶段提供。',409)
        steps = json.loads(plant['plan_json'])['steps']
        if data.step_index >= len(steps):
            return error('NOT_FOUND','没有找到这个步骤。',404)
        done = db.execute('SELECT completed,cancelled FROM goal_action_states WHERE owner_id=? AND plant_id=? AND step_index=?',(owner_id(),plant_id,data.step_index)).fetchone()
        if done and done['completed']:
            return error('ALREADY_COMPLETED','已完成的步骤不能直接调整。',409)
        if done and done['cancelled']:
            return error('CANCELLED','已取消的步骤需要先恢复。',409)
        source_row = db.execute('SELECT input_json FROM jobs WHERE id=? AND owner_id=?',(plant['job_id'],owner_id())).fetchone()
        source = json.loads(source_row['input_json'])
        proposal_id = str(uuid.uuid4())
        db.execute('INSERT INTO plan_adjustments (id,owner_id,plant_id,step_index,base_hash,original_text,request_text,status) VALUES (?,?,?,?,?,?,?,?)',(proposal_id,owner_id(),plant_id,data.step_index,adjustment_base(db,plant),steps[data.step_index],data.difficulty,'pending'))
        model_data = {'kind':'adjustment','goal':'调整计划中的一个步骤','background':json.dumps({'目标':plant['title'],'完整步骤':steps,'选中步骤':steps[data.step_index],'用户遇到的困难':data.difficulty},ensure_ascii=False),'availability':source.get('availability','不确定'),'session_budget_minutes':source.get('session_budget_minutes'),'habit_completion_minutes':None,'_adjustment_force_plan':True}
    background_tasks.add_task(run_owned, owner_id(), process_adjustment,proposal_id,model_data)
    return {'id':proposal_id,'status':'pending'}


@app.get('/api/v1/adjustments/{adjustment_id}')
def get_adjustment(adjustment_id: str):
    with connect() as db:
        row=db.execute('SELECT * FROM plan_adjustments WHERE id=? AND owner_id=?',(adjustment_id,owner_id())).fetchone()
        if not row:
            return error('NOT_FOUND','没有找到这份调整建议。',404)
        result={'id':row['id'],'plant_id':row['plant_id'],'step_index':row['step_index'],'status':row['status'],'original_text':row['original_text'],'suggestion':row['suggestion'],'suggestion_steps':json.loads(row['suggestion_steps_json']) if row['suggestion_steps_json'] else ([row['suggestion']] if row['suggestion'] else None),'applied_text':row['applied_text'],'applied_steps':json.loads(row['applied_steps_json']) if row['applied_steps_json'] else ([row['applied_text']] if row['applied_text'] else None)}
        if row['status']=='error': result['error']={'code':row['error_code'],'message':'调整建议生成失败，请稍后重试。'}
        return result


@app.post('/api/v1/adjustments/{adjustment_id}/confirm')
def confirm_adjustment(adjustment_id: str, confirmation: AdjustmentConfirmation):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM plan_adjustments WHERE id=? AND owner_id=?',(adjustment_id,owner_id())).fetchone()
        if not row: return error('NOT_FOUND','没有找到这份调整建议。',404)
        proposed=confirmation.steps if confirmation.steps is not None else [confirmation.text]
        day=re.match(r'^\s*(第\s*(?:[1-9]|[一二三四五六七])\s*天)',row['original_text'])
        if day and not any(day.group(1).replace(' ','') in step.replace(' ','') for step in proposed):
            proposed[0]=day.group(1)+'：'+proposed[0]
        if row['status']=='confirmed':
            applied=json.loads(row['applied_steps_json']) if row['applied_steps_json'] else [row['applied_text']]
            if applied != proposed: return error('ALREADY_CONFIRMED','这份建议已确认，无法重复修改。',409)
            return {'status':'confirmed','text':row['applied_text'],'steps':applied}
        if row['status']!='done': return error('NOT_READY','这份调整建议不能确认。',409)
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(row['plant_id'],owner_id())).fetchone()
        if not plant or adjustment_base(db,plant)!=row['base_hash']:
            return error('PLAN_CHANGED','计划或完成状态已经改变，请重新生成建议。',409)
        plan=json.loads(plant['plan_json'])
        index=row['step_index'];plan['steps'][index:index+1]=proposed
        source=json.loads(db.execute('SELECT input_json FROM jobs WHERE id=? AND owner_id=?',(plant['job_id'],owner_id())).fetchone()['input_json'])
        try: validate_plan(Plan.model_validate(plan),source)
        except (ConstraintError,ValidationError): return error('PLAN_CONSTRAINT','调整后的计划未通过约束检查，请修改后重试。',409)
        ensure_goal_weights(db,plant)
        shift=len(proposed)-1
        if shift:
            original_weight=db.execute('SELECT numerator,denominator FROM goal_action_weights WHERE owner_id=? AND plant_id=? AND step_index=?',(owner_id(),plant['id'],index)).fetchone()
            share=Fraction(int(original_weight['numerator']),int(original_weight['denominator']))/len(proposed)
            weights=db.execute('SELECT step_index FROM goal_action_weights WHERE owner_id=? AND plant_id=? AND step_index>? ORDER BY step_index DESC',(owner_id(),plant['id'],index)).fetchall()
            for weight in weights:db.execute('UPDATE goal_action_weights SET step_index=? WHERE owner_id=? AND plant_id=? AND step_index=?',(weight['step_index']+shift,owner_id(),plant['id'],weight['step_index']))
            db.execute('UPDATE goal_action_weights SET numerator=?,denominator=? WHERE owner_id=? AND plant_id=? AND step_index=?',(str(share.numerator),str(share.denominator),owner_id(),plant['id'],index))
            for i in range(1,len(proposed)):db.execute('INSERT INTO goal_action_weights VALUES (?,?,?,?,?)',(owner_id(),plant['id'],index+i,str(share.numerator),str(share.denominator)))
            existing=db.execute('SELECT step_index FROM goal_action_states WHERE owner_id=? AND plant_id=? AND step_index>? ORDER BY step_index DESC',(owner_id(),plant['id'],index)).fetchall()
            for state in existing: db.execute('UPDATE goal_action_states SET step_index=? WHERE owner_id=? AND plant_id=? AND step_index=?',(state['step_index']+shift,owner_id(),plant['id'],state['step_index']))
        db.execute('UPDATE plants SET plan_json=? WHERE id=? AND owner_id=?',(json.dumps(plan,ensure_ascii=False),plant['id'],owner_id()))
        db.execute("UPDATE plan_adjustments SET status='confirmed',applied_text=?,applied_steps_json=? WHERE id=? AND owner_id=?",(proposed[0],json.dumps(proposed,ensure_ascii=False),adjustment_id,owner_id()))
        return {'status':'confirmed','text':proposed[0],'steps':proposed}


@app.post('/api/v1/adjustments/{adjustment_id}/discard')
def discard_adjustment(adjustment_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT status FROM plan_adjustments WHERE id=? AND owner_id=?',(adjustment_id,owner_id())).fetchone()
        if not row: return error('NOT_FOUND','没有找到这份调整建议。',404)
        if row['status']=='discarded': return {'status':'discarded'}
        if row['status']!='done': return error('NOT_READY','这份建议不能放弃。',409)
        db.execute("UPDATE plan_adjustments SET status='discarded' WHERE id=? AND owner_id=?",(adjustment_id,owner_id()))
        return {'status':'discarded'}


def is_paused_day(db, plant_id: str, day: str) -> bool:
    return db.execute('SELECT 1 FROM plant_pauses WHERE owner_id=? AND plant_id=? AND start_date<=? AND (end_date IS NULL OR end_date>=?) LIMIT 1',(owner_id(),plant_id,day,day)).fetchone() is not None


def pause_view(db, plant):
    pause=db.execute('SELECT * FROM plant_pauses WHERE owner_id=? AND plant_id=? AND end_date IS NULL',(owner_id(),plant['id'])).fetchone()
    stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant['id'])).fetchone()
    today=habit_today(stage['timezone'] if stage else 'Asia/Shanghai')
    future=0;future_dates=[]
    if stage:
        future=db.execute('SELECT COUNT(*) FROM habit_occurrences WHERE stage_id=? AND enabled=1 AND action_date>?',(stage['id'],today.isoformat())).fetchone()[0]
        future_dates=[r['action_date'] for r in db.execute('SELECT action_date FROM habit_occurrences WHERE stage_id=? AND enabled=1 AND action_date>? ORDER BY action_date LIMIT 10',(stage['id'],today.isoformat()))]
    standard=db.execute('SELECT threshold,achieved FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant['id'])).fetchone()
    counts=growth_counts(db,plant)
    return {'paused':bool(pause),'since':pause['start_date'] if pause else None,'end_date':stage['end_date'] if stage else None,'future_days':future if stage else None,'future_dates':future_dates,'today':today.isoformat(),'threshold':standard['threshold'] if standard else None,'completed':counts['completed'] if counts else None,'achieved':bool(standard['achieved']) if standard else False}


@app.get('/api/v1/plants/{plant_id}/pause')
def get_pause(plant_id: str):
    with connect() as db:
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant: return error('NOT_FOUND','没有找到这株植物。',404)
        return pause_view(db,plant)


@app.put('/api/v1/plants/{plant_id}/pause')
def set_pause(plant_id: str, update: PauseInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant: return error('NOT_FOUND','没有找到这株植物。',404)
        current=db.execute('SELECT * FROM plant_pauses WHERE owner_id=? AND plant_id=? AND end_date IS NULL',(owner_id(),plant_id)).fetchone()
        if update.paused and not current:
            stage=db.execute('SELECT timezone FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone()
            timezone=stage['timezone'] if stage else 'Asia/Shanghai'
            db.execute('INSERT INTO plant_pauses (id,owner_id,plant_id,timezone,start_date) VALUES (?,?,?,?,?)',(str(uuid.uuid4()),owner_id(),plant_id,timezone,habit_today(timezone).isoformat()))
        elif not update.paused and current:
            today=habit_today(current['timezone']).isoformat()
            if today < current['start_date']: return error('CLOCK_CHANGED','当前日期早于暂停日期，请检查系统时间。',409)
            db.execute('UPDATE plant_pauses SET end_date=? WHERE id=? AND owner_id=?',(today,current['id'],owner_id()))
        return pause_view(db,plant)


def habit_schedule_hash(db, stage, include_start=True):
    rows=[tuple(r) for r in db.execute('SELECT action_date,enabled,completed,recorded_at,is_backfill FROM habit_occurrences WHERE stage_id=? ORDER BY action_date',(stage['id'],))]
    pauses=[tuple(r) for r in db.execute('SELECT start_date,end_date FROM plant_pauses WHERE owner_id=? AND plant_id=? ORDER BY start_date,id',(owner_id(),stage['plant_id']))]
    fields={'end':stage['end_date'],'weekdays':stage['weekdays_json'],'timezone':stage['timezone'],'occurrences':rows,'pauses':pauses}
    if include_start:fields['start']=stage['start_date']
    payload=json.dumps(fields,ensure_ascii=False,sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()

def habit_revision_hash(db, plant, stage):
    standard=db.execute('SELECT threshold,achieved FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant['id'])).fetchone()
    payload={'plan':json.loads(plant['plan_json']),'stage':habit_schedule_hash(db,stage),'standard':dict(standard) if standard else None}
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def process_habit_plan_revision(revision_id: str, source: dict):
    try:
        with connect() as db:db.execute("UPDATE habit_plan_revisions SET status='running' WHERE owner_id=? AND id=? AND status='pending'",(owner_id(),revision_id))
        turn=generate(source)
        if not turn.plan:raise RuntimeError('MODEL_FAILED')
        validate_plan(turn.plan,source)
        with connect() as db:db.execute("UPDATE habit_plan_revisions SET status='done',suggestion_json=? WHERE owner_id=? AND id=? AND status='running'",(turn.plan.model_dump_json(),owner_id(),revision_id))
    except Exception as exc:
        code='PLAN_CONSTRAINT' if isinstance(exc,ConstraintError) else (str(exc) if str(exc) in ('MODEL_NOT_CONFIGURED','MODEL_FAILED','BUDGET_NOT_CONFIGURED','BUDGET_EXHAUSTED','PLAN_CONSTRAINT') else 'MODEL_FAILED')
        with connect() as db:db.execute("UPDATE habit_plan_revisions SET status='error',error_code=? WHERE owner_id=? AND id=? AND status='running'",(code,owner_id(),revision_id))

@app.post('/api/v1/plants/{plant_id}/habit-plan-revisions',status_code=202)
def create_habit_plan_revision(plant_id: str, background_tasks: BackgroundTasks):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT * FROM plants WHERE owner_id=? AND id=?',(owner_id(),plant_id)).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这个习惯。',404)
        if plant['kind']!='habit':return error('WRONG_KIND','此功能仅适用于习惯。',409)
        stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone()
        if not stage:return error('NO_STAGE','请先确认习惯周期。',409)
        original=db.execute('SELECT input_json FROM jobs WHERE owner_id=? AND id=?',(owner_id(),plant['job_id'])).fetchone()
        if not original:return error('NOT_FOUND','没有找到原始计划。',404)
        source=json.loads(original['input_json']);standard=db.execute('SELECT threshold FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        source['kind']='habit';source['goal']=source.get('goal') or plant['title'];source['_habit_plan_revision']=True
        source['habit_schedule']={'start_date':stage['start_date'],'end_date':stage['end_date'],'timezone':stage['timezone'],'weekdays':json.loads(stage['weekdays_json']),'completion_standard':stage['completion_standard'],'growth_threshold':standard['threshold'] if standard else None}
        revision_id=str(uuid.uuid4())
        db.execute('INSERT INTO habit_plan_revisions (id,owner_id,plant_id,stage_id,base_hash,source_json,status,before_json) VALUES (?,?,?,?,?,?,?,?)',(revision_id,owner_id(),plant_id,stage['id'],habit_revision_hash(db,plant,stage),json.dumps(source,ensure_ascii=False),'pending',plant['plan_json']))
    background_tasks.add_task(run_owned, owner_id(), process_habit_plan_revision,revision_id,source)
    return {'id':revision_id,'status':'pending'}

@app.get('/api/v1/habit-plan-revisions/{revision_id}')
def get_habit_plan_revision(revision_id: str):
    with connect() as db:
        row=db.execute('SELECT * FROM habit_plan_revisions WHERE owner_id=? AND id=?',(owner_id(),revision_id)).fetchone()
        if not row:return error('NOT_FOUND','没有找到这份计划建议。',404)
        response={'id':row['id'],'status':row['status'],'before':json.loads(row['before_json']),'suggestion':json.loads(row['suggestion_json']) if row['suggestion_json'] else None}
        if row['status']=='error':response['error']={'code':row['error_code'],'message':'计划建议未能生成，请稍后重试。'}
        return response

@app.post('/api/v1/habit-plan-revisions/{revision_id}/confirm')
def confirm_habit_plan_revision(revision_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM habit_plan_revisions WHERE owner_id=? AND id=?',(owner_id(),revision_id)).fetchone()
        if not row:return error('NOT_FOUND','没有找到这份计划建议。',404)
        if row['status']=='confirmed':return {'status':'confirmed'}
        if row['status']!='done' or not row['suggestion_json']:return error('NOT_READY','计划建议尚不能确认。',409)
        plant=db.execute('SELECT * FROM plants WHERE owner_id=? AND id=?',(owner_id(),row['plant_id'])).fetchone()
        stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND id=? AND active=1',(owner_id(),row['stage_id'])).fetchone()
        if not plant or not stage or habit_revision_hash(db,plant,stage)!=row['base_hash']:return error('PLAN_CHANGED','习惯或周期已变化，请重新生成建议。',409)
        suggestion=Plan.model_validate_json(row['suggestion_json']);plan=json.loads(plant['plan_json'])
        plan['summary']=suggestion.summary;plan['steps']=suggestion.steps
        if suggestion.session_actions is None:plan.pop('session_actions',None)
        else:plan['session_actions']=[a.model_dump() for a in suggestion.session_actions]
        try:validate_plan(Plan.model_validate(plan),json.loads(row['source_json']))
        except (ConstraintError,ValidationError):return error('PLAN_CONSTRAINT','建议与已确认条件冲突，请重新生成。',409)
        db.execute('UPDATE plants SET plan_json=? WHERE owner_id=? AND id=?',(json.dumps(plan,ensure_ascii=False),owner_id(),plant['id']))
        db.execute("UPDATE habit_plan_revisions SET status='confirmed' WHERE owner_id=? AND id=?",(owner_id(),revision_id))
        return {'status':'confirmed'}

@app.post('/api/v1/habit-plan-revisions/{revision_id}/discard')
def discard_habit_plan_revision(revision_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT status FROM habit_plan_revisions WHERE owner_id=? AND id=?',(owner_id(),revision_id)).fetchone()
        if not row:return error('NOT_FOUND','没有找到这份计划建议。',404)
        if row['status']=='discarded':return {'status':'discarded'}
        if row['status']!='done':return error('NOT_READY','当前建议不能放弃。',409)
        db.execute("UPDATE habit_plan_revisions SET status='discarded' WHERE owner_id=? AND id=?",(owner_id(),revision_id))
        return {'status':'discarded'}


@app.post('/api/v1/plants/{plant_id}/habit-schedule-proposals',status_code=201)
def create_schedule_proposal(plant_id: str, data: HabitScheduleInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        stage=db.execute('SELECT s.* FROM habit_stages s JOIN plants p ON p.id=s.plant_id AND p.owner_id=s.owner_id WHERE s.owner_id=? AND s.plant_id=? AND p.kind=? AND s.active=1',(owner_id(),plant_id,'habit')).fetchone()
        if not stage: return error('NOT_FOUND','没有找到习惯周期。',404)
        today=habit_today(stage['timezone']); old_start=date.fromisoformat(stage['start_date']);start=data.start_date or old_start
        start_changed=start!=old_start
        if start_changed and (today>=old_start or start<today):
            return error('START_LOCKED','仅尚未开始的周期可以调整开始日期，新日期不能早于今天。',409)
        if data.end_date < today or data.end_date < start or (data.end_date-start).days > 3660:
            return error('INVALID_SCHEDULE','结束日期需不早于今天和开始日期，且周期不超过10年。',422)
        effective=min(old_start,start) if start_changed else max(today+timedelta(days=1),start)
        future=[effective+timedelta(days=i) for i in range(max(0,(data.end_date-effective).days+1))]
        count=sum(day>=start and day.weekday() in data.weekdays for day in future)
        if start_changed and count==0:
            return error('NO_PLANNED_DAYS','新的周期范围内没有计划日，请调整日期或星期。',422)
        before={'start_date':stage['start_date'],'end_date':stage['end_date'],'weekdays':json.loads(stage['weekdays_json'])}
        standard=db.execute('SELECT threshold FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone();counts=growth_counts(db,db.execute('SELECT * FROM plants WHERE owner_id=? AND id=?',(owner_id(),plant_id)).fetchone())
        after={'start_date':start.isoformat(),'end_date':data.end_date.isoformat(),'weekdays':sorted(data.weekdays),'effective_date':effective.isoformat(),'preview_date':today.isoformat(),'includes_today':effective==today,'future_count':count,'threshold':standard['threshold'] if standard else None,'completed':counts['completed'] if counts else None}
        proposal_id=str(uuid.uuid4())
        db.execute('INSERT INTO habit_schedule_proposals VALUES (?,?,?,?,?,?,?,?)',(proposal_id,owner_id(),plant_id,stage['id'],habit_schedule_hash(db,stage),json.dumps(before),json.dumps(after),'pending'))
        return {'id':proposal_id,'status':'pending','before':before,'after':after}


@app.get('/api/v1/habit-schedule-proposals/{proposal_id}')
def get_schedule_proposal(proposal_id: str):
    with connect() as db:
        row=db.execute('SELECT * FROM habit_schedule_proposals WHERE id=? AND owner_id=?',(proposal_id,owner_id())).fetchone()
        if not row: return error('NOT_FOUND','没有找到这份周期调整。',404)
        return {'id':row['id'],'plant_id':row['plant_id'],'status':row['status'],'before':json.loads(row['before_json']),'after':json.loads(row['after_json'])}


@app.post('/api/v1/habit-schedule-proposals/{proposal_id}/confirm')
def confirm_schedule_proposal(proposal_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM habit_schedule_proposals WHERE id=? AND owner_id=?',(proposal_id,owner_id())).fetchone()
        if not row: return error('NOT_FOUND','没有找到这份周期调整。',404)
        if row['status']=='confirmed': return {'status':'confirmed'}
        if row['status']!='pending': return error('NOT_READY','这份周期调整无法确认。',409)
        stage=db.execute('SELECT * FROM habit_stages WHERE id=? AND owner_id=?',(row['stage_id'],owner_id())).fetchone()
        after=json.loads(row['after_json'])
        if stage and 'preview_date' not in after:
            for previous in db.execute("SELECT before_json,after_json FROM habit_schedule_proposals WHERE stage_id=? AND owner_id=? AND status='confirmed'",(stage['id'],owner_id())):
                before_change=json.loads(previous['before_json']);after_change=json.loads(previous['after_json'])
                if before_change.get('start_date') and after_change.get('start_date') != before_change['start_date']:
                    return error('PLAN_CHANGED','周期开始日期已经改变，请重新预览。',409)
        if not stage or not stage['active'] or habit_schedule_hash(db,stage,include_start='preview_date' in after)!=row['base_hash']: return error('PLAN_CHANGED','周期或打卡状态已经改变，请重新预览。',409)
        today=habit_today(stage['timezone']);old_start=date.fromisoformat(stage['start_date']);new_start=date.fromisoformat(after.get('start_date',stage['start_date']))
        if after.get('preview_date',today.isoformat()) != today.isoformat() or (new_start!=old_start and (today>=old_start or new_start<today)):
            return error('PLAN_CHANGED','日期已经变化，请重新预览。',409)
        expected=min(old_start,new_start) if new_start!=old_start else max(today+timedelta(days=1),old_start)
        if after['effective_date'] != expected.isoformat():return error('PLAN_CHANGED','日期已经变化，请重新预览。',409)
        effective=date.fromisoformat(after['effective_date']); end=date.fromisoformat(after['end_date'])
        db.execute('UPDATE habit_occurrences SET enabled=0 WHERE stage_id=? AND action_date>=?',(stage['id'],effective.isoformat()))
        valid=[effective+timedelta(days=i) for i in range(max(0,(end-effective).days+1))]
        db.executemany('INSERT INTO habit_occurrences (stage_id,action_date,enabled) VALUES (?,?,1) ON CONFLICT(stage_id,action_date) DO UPDATE SET enabled=1',[(stage['id'],day.isoformat()) for day in valid if day>=new_start and day.weekday() in after['weekdays']])
        db.execute('UPDATE habit_stages SET start_date=?,end_date=?,weekdays_json=? WHERE id=? AND owner_id=?',(new_start.isoformat(),after['end_date'],json.dumps(after['weekdays']),stage['id'],owner_id()))
        db.execute("UPDATE habit_schedule_proposals SET status='confirmed' WHERE id=? AND owner_id=?",(proposal_id,owner_id()))
        return {'status':'confirmed'}


@app.post('/api/v1/habit-schedule-proposals/{proposal_id}/discard')
def discard_schedule_proposal(proposal_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT status FROM habit_schedule_proposals WHERE id=? AND owner_id=?',(proposal_id,owner_id())).fetchone()
        if not row: return error('NOT_FOUND','没有找到这份周期调整。',404)
        if row['status']=='discarded': return {'status':'discarded'}
        if row['status']!='pending': return error('NOT_READY','这份周期调整无法放弃。',409)
        db.execute("UPDATE habit_schedule_proposals SET status='discarded' WHERE id=? AND owner_id=?",(proposal_id,owner_id()))
        return {'status':'discarded'}


def growth_counts(db, plant):
    if plant['kind']=='goal':
        current=db.execute('SELECT states_json FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant['id'])).fetchone()
        if current:
            states=json.loads(current['states_json'])
            return {'completed':sum(bool(s) for s in states),'total':len(states)}
        cancelled=db.execute('SELECT count(*) FROM goal_action_states WHERE owner_id=? AND plant_id=? AND cancelled=1',(owner_id(),plant['id'])).fetchone()[0]
        total=len(json.loads(plant['plan_json'])['steps'])-cancelled
        completed=db.execute('SELECT count(*) FROM goal_action_states WHERE owner_id=? AND plant_id=? AND completed=1',(owner_id(),plant['id'])).fetchone()[0]
    else:
        stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant['id'])).fetchone()
        if not stage: return None
        rows=db.execute('SELECT action_date,completed FROM habit_occurrences WHERE stage_id=? AND enabled=1',(stage['id'],)).fetchall()
        today=habit_today(stage['timezone']).isoformat()
        total=sum(bool(r['completed']) or r['action_date']>today or not is_paused_day(db,plant['id'],r['action_date']) for r in rows)
        completed=sum(bool(r['completed']) for r in rows)
    return {'completed':completed,'total':total}


def ensure_goal_weights(db,plant):
    owner=plant['owner_id'];plant_id=plant['id'];steps=json.loads(plant['plan_json'])['steps']
    existing=db.execute('SELECT step_index FROM goal_action_weights WHERE owner_id=? AND plant_id=? ORDER BY step_index',(owner,plant_id)).fetchall()
    if existing:
        if [r['step_index'] for r in existing] != list(range(len(steps))):
            raise RuntimeError('inconsistent goal weights')
        return
    cancelled={r['step_index'] for r in db.execute('SELECT step_index FROM goal_action_states WHERE owner_id=? AND plant_id=? AND cancelled=1',(owner,plant_id))}
    active=len(steps)-len(cancelled)
    for index in range(len(steps)):
        weight=Fraction(0,1) if index in cancelled or not active else Fraction(1,active)
        db.execute('INSERT INTO goal_action_weights VALUES (?,?,?,?,?)',(owner,plant_id,index,str(weight.numerator),str(weight.denominator)))


def goal_earned_fraction(db,plant_id):
    rows=db.execute('SELECT w.numerator,w.denominator FROM goal_action_weights w JOIN goal_action_states s ON s.owner_id=w.owner_id AND s.plant_id=w.plant_id AND s.step_index=w.step_index WHERE w.owner_id=? AND w.plant_id=? AND s.completed=1 AND s.cancelled=0',(owner_id(),plant_id)).fetchall()
    return sum((Fraction(int(r['numerator']),int(r['denominator'])) for r in rows),Fraction(0,1))


def rebalance_goal_weights(db,plant_id,plan):
    earned=goal_earned_fraction(db,plant_id)
    if earned>1:raise RuntimeError('invalid earned shares')
    states={r['step_index']:r for r in db.execute('SELECT step_index,completed,cancelled FROM goal_action_states WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id))}
    pending=[i for i in range(len(plan['steps'])) if i not in states or (not states[i]['completed'] and not states[i]['cancelled'])]
    weight=(Fraction(1,1)-earned)/len(pending) if pending else Fraction(0,1)
    for i in range(len(plan['steps'])):
        if i in states and states[i]['completed']:continue
        assigned=weight if i in pending else Fraction(0,1)
        db.execute('UPDATE goal_action_weights SET numerator=?,denominator=? WHERE owner_id=? AND plant_id=? AND step_index=?',(str(assigned.numerator),str(assigned.denominator),owner_id(),plant_id,i))


def growth_event(db, plant_id, event, threshold):
    db.execute('INSERT INTO growth_events VALUES (?,?,?,?,?,?)',(str(uuid.uuid4()),owner_id(),plant_id,event,threshold,datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()))


def reconcile_growth(db, plant_id):
    standard=db.execute('SELECT * FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
    if not standard or not standard['achieved']: return
    plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
    counts=growth_counts(db,plant) if plant else None
    if not counts or counts['completed'] < standard['threshold']:
        db.execute('UPDATE growth_standards SET achieved=0 WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id))
        growth_event(db,plant_id,'revoked',standard['threshold'])


@app.get('/api/v1/plants/{plant_id}/growth')
def get_growth(plant_id: str):
    with connect() as db:
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant: return error('NOT_FOUND','没有找到这株植物。',404)
        counts=growth_counts(db,plant)
        standard=db.execute('SELECT * FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        follow=db.execute('SELECT 1 FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone() if plant['kind']=='goal' else None
        progress=Fraction(counts['completed'],counts['total']) if follow and counts and counts['total'] else goal_earned_fraction(db,plant['id']) if plant['kind']=='goal' else None
        return {'counts':counts,'threshold':standard['threshold'] if standard else None,'achieved':bool(standard['achieved']) if standard else False,'eligible':bool(standard and counts and counts['completed']>=standard['threshold']),'progress_percent':round(float(progress*100),1) if progress is not None else None}


@app.put('/api/v1/plants/{plant_id}/growth/standard')
def set_growth_standard(plant_id: str, data: GrowthStandardInput):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant: return error('NOT_FOUND','没有找到这株植物。',404)
        counts=growth_counts(db,plant)
        if not counts: return error('NO_STAGE','请先设置习惯周期。',409)
        if data.threshold>counts['total']: return error('INVALID_STANDARD','阶段标准不能超过当前计划中可完成的次数。',422)
        old=db.execute('SELECT * FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        if old and old['threshold']==data.threshold:
            if plant['kind']=='habit':
                stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone()
                if stage:finish_habit_when_ready(db,plant_id,stage)
            current=db.execute('SELECT achieved FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
            return {'threshold':old['threshold'],'achieved':bool(current['achieved'])}
        db.execute('INSERT INTO growth_standards (owner_id,plant_id,threshold,achieved) VALUES (?,?,?,0) ON CONFLICT(owner_id,plant_id) DO UPDATE SET threshold=excluded.threshold,achieved=0',(owner_id(),plant_id,data.threshold))
        growth_event(db,plant_id,'standard_confirmed',data.threshold)
        if plant['kind']=='habit':
            stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone()
            if stage:finish_habit_when_ready(db,plant_id,stage)
        current=db.execute('SELECT achieved FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        return {'threshold':data.threshold,'achieved':bool(current['achieved'])}


@app.post('/api/v1/plants/{plant_id}/growth/confirm')
def confirm_growth(plant_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant: return error('NOT_FOUND','没有找到这株植物。',404)
        standard=db.execute('SELECT * FROM growth_standards WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id)).fetchone()
        counts=growth_counts(db,plant)
        if not standard or not counts: return error('NO_STANDARD','请先确认阶段标准。',409)
        if counts['completed']<standard['threshold']: return error('NOT_ELIGIBLE','完成次数尚未达到阶段标准。',409)
        if not standard['achieved']:
            db.execute('UPDATE growth_standards SET achieved=1 WHERE owner_id=? AND plant_id=?',(owner_id(),plant_id))
            growth_event(db,plant_id,'achieved',standard['threshold'])
        if plant['kind']=='habit':
            stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone()
            if stage:habit_snapshot(db,plant,stage,standard,datetime.now(ZoneInfo(stage['timezone'])).isoformat())
        return {'achieved':True,'completed':counts['completed'],'threshold':standard['threshold']}


def process_growth_suggestion(suggestion_id: str, model_data: dict):
    try:
        with connect() as db: db.execute("UPDATE growth_suggestions SET status='running' WHERE id=? AND owner_id=? AND status='pending'",(suggestion_id,owner_id()))
        turn=generate(model_data)
        if not turn.plan or len(turn.plan.steps)!=1: raise RuntimeError('MODEL_FAILED')
        with connect() as db: db.execute("UPDATE growth_suggestions SET status='done',suggestion=? WHERE id=? AND owner_id=? AND status='running'",(turn.plan.steps[0],suggestion_id,owner_id()))
    except Exception as exc:
        code=str(exc) if str(exc) in ('MODEL_NOT_CONFIGURED','MODEL_FAILED','PLAN_CONSTRAINT','BUDGET_NOT_CONFIGURED','BUDGET_EXHAUSTED') else 'MODEL_FAILED'
        with connect() as db: db.execute("UPDATE growth_suggestions SET status='error',error_code=? WHERE id=? AND owner_id=? AND status='running'",(code,suggestion_id,owner_id()))


@app.post('/api/v1/plants/{plant_id}/growth/suggestions',status_code=202)
def create_growth_suggestion(plant_id: str, background_tasks: BackgroundTasks):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant: return error('NOT_FOUND','没有找到这株植物。',404)
        counts=growth_counts(db,plant)
        if not counts or counts['total']<1: return error('NO_STAGE','请先设置习惯周期。',409)
        stage=db.execute('SELECT start_date,end_date,weekdays_json,completion_standard FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone() if plant['kind']=='habit' else None
        data={'kind':'milestone','goal':'提出阶段成果标准建议','background':json.dumps({'目标或习惯':plant['title'],'计划说明':json.loads(plant['plan_json']).get('summary'),'类型':plant['kind'],'计划次数上限':counts['total'],'已完成次数':counts['completed'],'阶段':dict(stage) if stage else None},ensure_ascii=False),'availability':'以已确认计划为准','_milestone_force_plan':True}
        suggestion_id=str(uuid.uuid4());db.execute("INSERT INTO growth_suggestions (id,owner_id,plant_id,status) VALUES (?,?,?,'pending')",(suggestion_id,owner_id(),plant_id))
    background_tasks.add_task(run_owned, owner_id(), process_growth_suggestion,suggestion_id,data)
    return {'id':suggestion_id,'status':'pending'}


@app.get('/api/v1/growth/suggestions/{suggestion_id}')
def get_growth_suggestion(suggestion_id: str):
    with connect() as db:
        row=db.execute('SELECT * FROM growth_suggestions WHERE id=? AND owner_id=?',(suggestion_id,owner_id())).fetchone()
        if not row: return error('NOT_FOUND','没有找到这份阶段标准建议。',404)
        result={'id':row['id'],'status':row['status'],'suggestion':row['suggestion']}
        if row['status']=='error': result['error']={'code':row['error_code'],'message':'建议生成失败，请稍后重试或自行填写标准。'}
        return result


@app.get('/api/v1/today')
def get_today_actions():
    with connect() as db:
        today=habit_today('Asia/Shanghai').isoformat(); planned=[]; overdue=[]; unscheduled=[]; habits=[]; backfill=[]
        plants=db.execute('SELECT * FROM plants WHERE owner_id=? ORDER BY created_at DESC',(owner_id(),)).fetchall()
        for plant in plants:
            if db.execute('SELECT 1 FROM plant_pauses WHERE owner_id=? AND plant_id=? AND end_date IS NULL',(owner_id(),plant['id'])).fetchone():continue
            if plant['kind']=='goal':
                follow=db.execute('SELECT * FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant['id'])).fetchone()
                if follow:
                    dates={r['step_index']:r['due_date'] for r in db.execute('SELECT step_index,due_date FROM goal_followup_dates WHERE owner_id=? AND stage_id=?',(owner_id(),follow['id']))}
                    for i,(step,done) in enumerate(zip(json.loads(follow['steps_json']),json.loads(follow['states_json']))):
                        due=dates.get(i)
                        item={'plant_id':plant['id'],'plant_title':follow['title'],'stage_id':follow['id'],'index':i,'text':step,'due_date':due,'completed':bool(done)}
                        if due==today:planned.append(item)
                        elif due and due<today and not done:overdue.append(item)
                        elif due is None and not done:unscheduled.append(item)
                    continue
                states={r['step_index']:r for r in db.execute('SELECT * FROM goal_action_states WHERE owner_id=? AND plant_id=?',(owner_id(),plant['id']))}
                for i,text in enumerate(json.loads(plant['plan_json'])['steps']):
                    row=states.get(i);due=row['due_date'] if row else None;done=bool(row['completed']) if row else False
                    if row and row['cancelled']:continue
                    item={'plant_id':plant['id'],'plant_title':plant['title'],'index':i,'text':text,'due_date':due,'completed':done}
                    if due==today:planned.append(item)
                    elif due and due<today and not done:overdue.append(item)
                    elif due is None and not done:unscheduled.append(item)
            elif plant['kind']=='habit':
                if db.execute('SELECT 1 FROM habit_lifecycle WHERE owner_id=? AND plant_id=? AND ended=1',(owner_id(),plant['id'])).fetchone():continue
                stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant['id'])).fetchone()
                if not stage:continue
                day=habit_today(stage['timezone']).isoformat()
                row=db.execute('SELECT completed FROM habit_occurrences WHERE stage_id=? AND action_date=? AND enabled=1',(stage['id'],day)).fetchone()
                if row and not is_paused_day(db,plant['id'],day):habits.append({'plant_id':plant['id'],'plant_title':plant['title'],'date':day,'completed':bool(row['completed']),'completion_standard':stage['completion_standard']})
                first=(date.fromisoformat(day)-timedelta(days=3)).isoformat()
                for occurrence in db.execute('SELECT action_date FROM habit_occurrences WHERE stage_id=? AND enabled=1 AND completed=0 AND action_date>=? AND action_date<? ORDER BY action_date DESC',(stage['id'],first,day)):
                    if not is_paused_day(db,plant['id'],occurrence['action_date']):backfill.append({'plant_id':plant['id'],'plant_title':plant['title'],'date':occurrence['action_date'],'completed':False,'completion_standard':stage['completion_standard']})
        return {'date':today,'goals_today':planned,'goals_overdue':overdue,'goals_unscheduled':unscheduled,'habits_today':habits,'habits_backfill':backfill}


def process_habit_advice(advice_id: str, data: dict):
    try:
        with connect() as db:db.execute("UPDATE habit_advice SET status='running' WHERE id=? AND owner_id=? AND status='pending'",(advice_id,owner_id()))
        turn=generate(data)
        if not turn.plan or len(turn.plan.steps)!=1:raise RuntimeError('MODEL_FAILED')
        with connect() as db:db.execute("UPDATE habit_advice SET status='done',suggestion=? WHERE id=? AND owner_id=? AND status='running'",(turn.plan.steps[0],advice_id,owner_id()))
    except Exception as exc:
        code=str(exc) if str(exc) in ('MODEL_NOT_CONFIGURED','MODEL_FAILED','PLAN_CONSTRAINT','BUDGET_NOT_CONFIGURED','BUDGET_EXHAUSTED') else 'MODEL_FAILED'
        with connect() as db:db.execute("UPDATE habit_advice SET status='error',error_code=? WHERE id=? AND owner_id=? AND status='running'",(code,advice_id,owner_id()))


@app.post('/api/v1/plants/{plant_id}/habit-advice',status_code=202)
def create_habit_advice(plant_id: str,data: HabitAdviceInput,background_tasks: BackgroundTasks):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plant=db.execute('SELECT * FROM plants WHERE id=? AND owner_id=?',(plant_id,owner_id())).fetchone()
        if not plant:return error('NOT_FOUND','没有找到这个习惯。',404)
        if plant['kind']!='habit':return error('WRONG_KIND','目标请使用步骤调整。',409)
        stage=db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),plant_id)).fetchone()
        if not stage:return error('NO_STAGE','请先确认习惯周期。',409)
        counts=growth_counts(db,plant)
        details={'习惯':plant['title'],'开始日期':stage['start_date'],'结束日期':stage['end_date'],'时区':stage['timezone'],'每周计划日':json.loads(stage['weekdays_json']),'每次完成标准':stage['completion_standard'],'已完成次数':counts['completed'],'遇到的困难':data.difficulty}
        model_data={'kind':'habit_advice','goal':'提出一条习惯遇阻调整建议','background':json.dumps(details,ensure_ascii=False),'availability':'以用户确认的周期为准','_habit_advice_force_plan':True}
        advice_id=str(uuid.uuid4());db.execute("INSERT INTO habit_advice (id,owner_id,plant_id,status,difficulty) VALUES (?,?,?,'pending',?)",(advice_id,owner_id(),plant_id,data.difficulty))
    background_tasks.add_task(run_owned, owner_id(), process_habit_advice,advice_id,model_data)
    return {'id':advice_id,'status':'pending'}


@app.get('/api/v1/habit-advice/{advice_id}')
def get_habit_advice(advice_id: str):
    with connect() as db:
        row=db.execute('SELECT * FROM habit_advice WHERE id=? AND owner_id=?',(advice_id,owner_id())).fetchone()
        if not row:return error('NOT_FOUND','没有找到这份习惯建议。',404)
        result={'id':row['id'],'status':row['status'],'suggestion':row['suggestion']}
        if row['status']=='error':result['error']={'code':row['error_code'],'message':'建议生成失败，请稍后重试。'}
        return result
