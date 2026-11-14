"""Explicit, owner-scoped action records reuse existing completion and lifecycle rules."""
from datetime import date, datetime, timedelta
import json
import re
import uuid
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from app import auth, account_privacy, agent_changes
from app.identity import owner_id


class RecordRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plant_id: str = Field(min_length=1, max_length=80)
    kind: Literal['goal', 'habit']
    stage_id: str | None = Field(default=None, max_length=80)
    index: int | None = Field(default=None, strict=True, ge=0, le=99)
    action_date: date | None = None
    base_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class RecordChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    turn_id: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=80)
    target: RecordRef


class RecordRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['complete', 'undo', 'pause', 'resume', 'end', 'manage']
    kind: Literal['goal', 'habit'] | None = None
    plant_title: str | None = Field(default=None, min_length=1, max_length=100)
    step_number: int | None = Field(default=None, strict=True, ge=1, le=100)
    step_text: str | None = Field(default=None, min_length=1, max_length=500)
    action_date: date | None = None
    relative_day: Literal['today', 'yesterday', 'day_before_yesterday', 'three_days_ago'] | None = None
    backfill: bool = Field(default=False, strict=True)
    undo_last: bool = Field(default=False, strict=True)
    certainty: Literal['reported', 'instruction', 'uncertain'] = 'uncertain'


def latest(db, session_id, successful=False):
    for row in db.execute("SELECT * FROM agent_turns WHERE owner_id=? AND session_id=? AND status='done' AND expires_at>? ORDER BY rowid DESC", (owner_id(), session_id, auth.now())):
        value = json.loads(row['response_json'])
        if value['kind'] == 'record' and (not successful or value['data']['state'] == 'confirmed'):
            return row, value
    return None, None


def active_record(session_id):
    from app import main
    with main.connect() as db:
        _, value = latest(db, session_id)
    return value['data'] if value and value['data']['state'] in ('select_task', 'need_date', 'clarify_report', 'ready') else None


def response(request, state, text, anchor_at, **values):
    return {'kind': 'record', 'queried_at': auth.now(), 'text': text,
            'data': {'state': state, 'version': str(uuid.uuid4()), 'operation': request.operation,
                     'request': request.model_dump(mode='json'), 'anchor_at': anchor_at, 'choices': [], **values}}


def candidates(db, plant, request, anchor_at):
    from app import main
    pid, kind = plant['id'], plant['kind']
    pauses = [tuple(r) for r in db.execute('SELECT start_date,end_date FROM plant_pauses WHERE owner_id=? AND plant_id=? ORDER BY start_date,id', (owner_id(), pid))]
    paused = any(r[1] is None for r in pauses)
    stage = db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=? AND active=1', (owner_id(), pid)).fetchone() if kind == 'habit' else None
    ended_row = db.execute('SELECT ended FROM habit_lifecycle WHERE owner_id=? AND plant_id=?', (owner_id(), pid)).fetchone()
    ended = bool(ended_row and ended_row['ended'])
    base = {'plant': pid, 'plan': plant['plan_json'], 'pauses': pauses, 'ended': ended}
    if request.operation in ('pause', 'resume', 'end', 'manage'):
        standard = db.execute('SELECT threshold,achieved FROM growth_standards WHERE owner_id=? AND plant_id=?', (owner_id(), pid)).fetchone()
        captured = bool(stage and db.execute('SELECT 1 FROM habit_collections WHERE owner_id=? AND stage_id=?', (owner_id(), stage['id'])).fetchone())
        base.update(stage={k:stage[k] for k in ('id','start_date','end_date','active')} if stage else None, standard=tuple(standard) if standard else None, captured=captured)
        blocked = None
        follow = db.execute('SELECT * FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1',(owner_id(),pid)).fetchone() if kind=='goal' else None
        if kind=='goal':
            notice=db.execute('SELECT status FROM completion_notices WHERE owner_id=? AND plant_id=? AND stage_id=?',(owner_id(),pid,follow['id'] if follow else 'initial')).fetchone()
            counts=main.growth_counts(db,plant)
            ended=bool(notice and notice['status']=='finished')
            base.update(goal_phase={k:follow[k] for k in ('id','steps_json','states_json','completed_at')} if follow else main.adjustment_base(db,plant),notice=notice['status'] if notice else None)
            if request.operation=='end' and (not notice or not counts or not counts['total'] or counts['completed']<counts['total']):
                blocked='请先实际完成当前目标阶段的有效步骤，再确认完成整个目标。'
        if request.operation == 'end' and kind=='habit' and (not stage or not (standard and standard['achieved'] or captured)):
            blocked = '结束阶段沿用原规则：请先达成并保留阶段成果；暂时不继续可以先暂停。'
        ref = RecordRef(plant_id=pid, kind=kind, stage_id=stage['id'] if stage else follow['id'] if follow else None, base_hash=agent_changes.digest(base))
        return [{'target': ref.model_dump(mode='json'), 'plant_title': plant['title'], 'stage_title': '当前植物',
                 'text': plant['title'], 'completed': None, 'paused': paused, 'ended': ended,
                 'timezone': stage['timezone'] if stage else 'Asia/Shanghai', 'archived': False, 'blocked_reason': blocked}]
    rows = []
    if kind == 'goal':
        follow = db.execute('SELECT * FROM goal_followups WHERE owner_id=? AND plant_id=? AND active=1', (owner_id(), pid)).fetchone()
        if follow:
            steps, states = json.loads(follow['steps_json']), json.loads(follow['states_json'])
            dates = [tuple(r) for r in db.execute('SELECT step_index,due_date FROM goal_followup_dates WHERE owner_id=? AND stage_id=? ORDER BY step_index', (owner_id(), follow['id']))]
            base.update(follow={k:follow[k] for k in ('id','steps_json','states_json','completed_at')}, dates=dates)
            entries = [{'completed':bool(done), 'cancelled':False} for done in states]
        else:
            steps = json.loads(plant['plan_json'])['steps']
            states = {r['step_index']: dict(r) for r in db.execute('SELECT step_index,completed,cancelled,due_date FROM goal_action_states WHERE owner_id=? AND plant_id=?', (owner_id(), pid))}
            base.update(initial=main.adjustment_base(db, plant), captured=bool(db.execute('SELECT 1 FROM goal_collections WHERE owner_id=? AND plant_id=?', (owner_id(), pid)).fetchone()))
            entries = [states.get(i, {'completed':False,'cancelled':False}) for i in range(len(steps))]
        for i, text in enumerate(steps):
            blocked = '目标已暂停，请先恢复再新增完成记录。' if paused and request.operation == 'complete' else '已取消步骤请先在原页面恢复。' if entries[i]['cancelled'] and request.operation == 'complete' else None
            ref = RecordRef(plant_id=pid, kind=kind, stage_id=follow['id'] if follow else None, index=i, base_hash=agent_changes.digest(base))
            rows.append({'target':ref.model_dump(mode='json'),'plant_title':plant['title'],'stage_title':follow['title'] if follow else '初始阶段','text':text,
                         'step_number':i+1,'completed':bool(entries[i]['completed']),'paused':paused,'ended':False,'timezone':'Asia/Shanghai','archived':False,'blocked_reason':blocked})
        return rows
    dated = request.action_date is not None or request.relative_day is not None
    needs_date = not dated and (request.backfill or request.operation=='undo')
    stages = db.execute('SELECT * FROM habit_stages WHERE owner_id=? AND plant_id=?' + ('' if dated else ' AND active=1') + ' ORDER BY active DESC,rowid DESC', (owner_id(), pid)).fetchall()
    for stage in stages:
        day = request.action_date
        if day is None:
            day = datetime.fromtimestamp(anchor_at, ZoneInfo(stage['timezone'])).date() - timedelta(days={None:0,'today':0,'yesterday':1,'day_before_yesterday':2,'three_days_ago':3}[request.relative_day])
        occurrence = db.execute('SELECT * FROM habit_occurrences WHERE stage_id=? AND action_date=? AND enabled=1', (stage['id'], day.isoformat())).fetchone()
        if not occurrence and not stage['active']:
            continue
        captured = bool(db.execute('SELECT 1 FROM habit_collections WHERE owner_id=? AND stage_id=?', (owner_id(), stage['id'])).fetchone())
        snapshot = {**base, 'schedule':main.habit_schedule_hash(db, stage), 'stage':{k:stage[k] for k in ('id','active','completion_standard')}, 'captured':captured}
        today = main.habit_today(stage['timezone'])
        blocked = '这一天不是该习惯的计划日。' if not occurrence else '不能记录未来的行动。' if day > today else '新增打卡只限今天或之前三个自然日。' if request.operation == 'complete' and day < today-timedelta(days=3) else '这个习惯已结束，请在原页面开启下一阶段。' if request.operation == 'complete' and ended and stage['active'] else '这一天是暂停日，不能新增打卡。' if request.operation == 'complete' and main.is_paused_day(db, pid, day.isoformat()) and not occurrence['completed'] else '这个历史阶段尚无收藏，不能通过园丁更正。' if not stage['active'] and not captured else None
        if needs_date:
            blocked=None
        ref = RecordRef(plant_id=pid, kind=kind, stage_id=stage['id'], action_date=None if needs_date else day, base_hash=agent_changes.digest(snapshot))
        rows.append({'target':ref.model_dump(mode='json'),'plant_title':plant['title'],'stage_title':('当前阶段' if stage['active'] else '已收藏阶段')+' '+stage['start_date']+'至'+stage['end_date'],
                     'text':stage['completion_standard'],'completed':bool(occurrence['completed']) if occurrence and not needs_date else None,'paused':paused,'ended':ended,
                     'timezone':stage['timezone'],'archived':not bool(stage['active']),'blocked_reason':blocked})
    return rows


def pool(db, request, anchor_at, plant_id=None):
    plants = db.execute('SELECT * FROM plants WHERE owner_id=?' + (' AND id=?' if plant_id else '') + ' ORDER BY created_at DESC,id', (owner_id(),plant_id) if plant_id else (owner_id(),)).fetchall()
    if request.kind:
        plants = [p for p in plants if p['kind'] == request.kind]
    if request.plant_title:
        exact = [p for p in plants if p['title'] == request.plant_title]
        if exact:
            plants = exact
    rows = []
    for plant in plants:
        rows.extend(candidates(db, plant, request, anchor_at))
        if sum(not r['blocked_reason'] for r in rows) >= 41:
            break
    return rows


def select(request, rows, anchor_at, text='请选择具体任务或习惯，我再帮你核对记录。'):
    eligible = [r for r in rows if not r['blocked_reason']]
    if not eligible:
        return response(request,'blocked', rows[0]['blocked_reason'] if len(rows)==1 else '没有符合当前条件的可记录行动。请打开具体植物核对计划日、阶段或暂停状态。',anchor_at,
                        before=rows[0] if len(rows)==1 else None)
    return response(request,'select_task',text,anchor_at,choices=eligible[:40],omitted=max(0,len(eligible)-40))


def prepare(request, context, session_id, user_text, target=None, anchor_at=None):
    from app import main
    anchor_at = anchor_at or auth.now()
    request = request.model_copy(deep=True)
    if request.operation == 'complete' and re.search(r'(?:还没|尚未|没有|并未|没|未)(?:有)?(?:实际)?(?:完成|做完|达标|读完|打卡)|(?:不确定|好像|可能).{0,8}(?:完成|做完|达标)|(?:准备|打算|想要).{0,6}(?:完成|做完|打卡)',user_text):
        request.certainty = 'uncertain'
    with main.connect() as db:
        if request.undo_last:
            _, previous = latest(db, session_id, successful=True)
            if not previous:
                return response(request,'blocked','本会话没有可撤销的实际记录，请明确说哪一项、哪一天要撤销。',anchor_at)
            data = previous['data']
            if data['operation'] not in ('complete','undo'):
                return response(request,'open_original','最近一次操作是暂停、恢复或结束。请明确说要暂停或恢复；结束后的下一阶段请在原植物确认。',anchor_at,before=data['before'])
            target = RecordRef.model_validate(data['after_ref'])
            request.operation = 'undo' if data['applied']['completed'] else 'complete'
            request.kind = target.kind;request.action_date = target.action_date;request.relative_day = None
            request.step_number = target.index+1 if target.index is not None else None
            request.certainty = 'reported';request.undo_last = False
        lookup_request = request
        if target:
            lookup_request = request.model_copy(update={'kind':target.kind,'action_date':target.action_date,'relative_day':None})
        rows = pool(db, lookup_request, anchor_at, target.plant_id if target else context.plant_id)
        if target:
            chosen = next((r for r in rows if r['target']==target.model_dump(mode='json')),None)
            if not chosen:
                return select(request, rows, anchor_at,'原记录或阶段已变化，请重新选择，不能按旧编号撤销或记录。')
        else:
            matching = rows
            if request.step_number is not None:
                matching = [r for r in matching if r.get('step_number')==request.step_number]
            if request.step_text:
                matching = [r for r in matching if r['text'].strip()==request.step_text.strip()]
            if request.plant_title:
                exact = [r for r in matching if r['plant_title']==request.plant_title]
                matching = exact
            chosen = matching[0] if len(matching)==1 and (context.plant_id or request.plant_title or request.step_text) else None
            if not chosen and not request.step_number and not request.step_text and not request.plant_title:
                previous = active_record(session_id)
                old = previous.get('target') if previous else None
                if old and (not context.plant_id or context.plant_id==old['plant_id']):
                    # Dates are part of the new utterance, so match the plant/step first.
                    chosen = next((r for r in rows if all(r['target'][k]==old[k] for k in ('plant_id','kind','stage_id','index'))),None)
            if request.operation == 'complete' and request.certainty == 'uncertain':
                return response(request,'clarify_report','你是已经实际完成了吗？请明确告诉我完成情况，我不会根据计划推断。',anchor_at,
                                **({'target':chosen['target'],'before':chosen} if chosen else {}))
            if not chosen:
                return select(request,rows,anchor_at)
        if request.operation == 'manage':
            return response(request,'open_original','请在原植物里调整周期或继续下一阶段，补充条件、预览并确认。这里还没有替你改变周期或新建阶段。',anchor_at,before=chosen,target=chosen['target'])
        if chosen['blocked_reason']:
            return response(request,'blocked',chosen['blocked_reason'],anchor_at,before=chosen,target=chosen['target'])
        if chosen['target']['kind']=='habit' and request.operation in ('complete','undo') and not request.action_date and not request.relative_day and (request.backfill or request.operation=='undo'):
            return response(request,'need_date','要补签或更正哪一天？请明确日期；新增打卡只限今天和之前三个自然日。',anchor_at,before=chosen,target=chosen['target'])
        if request.operation == 'complete' and request.certainty == 'uncertain':
            return response(request,'clarify_report','请明确当天确实完成后再记录。',anchor_at,before=chosen,target=chosen['target'])
        already = chosen['completed']==(request.operation=='complete') if request.operation in ('complete','undo') else chosen['paused']==(request.operation=='pause') if request.operation in ('pause','resume') else chosen['ended'] if request.operation=='end' else False
        if already:
            return response(request,'already_recorded','实际状态已经符合这次请求，无需重复记录；没有新增事件或重复发放成果。',anchor_at,before=chosen,target=chosen['target'])
        return response(request,'ready','请核对目标、步骤或日期，再点击对应按钮记录。只有实际完成的行动才标记完成。',anchor_at,before=chosen,target=chosen['target'],
                        desired_completed=request.operation=='complete' if request.operation in ('complete','undo') else None)


def from_choice(choice, context, session_id):
    from app import main
    with main.connect() as db:
        row = db.execute("SELECT * FROM agent_turns WHERE id=? AND owner_id=? AND session_id=? AND status='done' AND expires_at>?", (choice.turn_id,owner_id(),session_id,auth.now())).fetchone()
        if not row:
            raise auth.AuthError('NOT_FOUND','这份选择已删除、过期或不可用。',404)
        value = json.loads(row['response_json']);data=value['data']
        if value['kind']!='record' or data.get('version')!=choice.version or data.get('state')!='select_task' or not any(r['target']==choice.target.model_dump(mode='json') for r in data['choices']):
            raise auth.AuthError('RECORD_STALE','请选择最新卡片中的行动。',409)
    return prepare(RecordRequest.model_validate(data['request']),context,session_id,row['user_text'],choice.target,data['anchor_at'])


def supersede_previous(db, session_id):
    row,value = latest(db,session_id)
    if row and value['data']['state'] in ('select_task','need_date','clarify_report','ready'):
        value['data']['state']='superseded'
        db.execute('UPDATE agent_turns SET response_json=? WHERE id=? AND owner_id=?',(json.dumps(value,ensure_ascii=False),row['id'],owner_id()))


class Confirmation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:str=Field(min_length=1,max_length=80)
    performed:bool=Field(default=False,strict=True)


def get_record(db, turn_id, version):
    from app import agent
    row=db.execute("SELECT * FROM agent_turns WHERE id=? AND owner_id=? AND status='done' AND expires_at>?",(turn_id,owner_id(),auth.now())).fetchone()
    if not row or account_privacy.is_erased(agent.erased_key(row['session_id'])):
        raise auth.AuthError('NOT_FOUND','这份记录已删除、过期或不可用。',404)
    value=json.loads(row['response_json'])
    if value['kind']!='record' or value['data']['version']!=version:
        raise auth.AuthError('RECORD_STALE','请重新读取这份记录。',409)
    return row,value


def require_latest(db,row):
    current,_=latest(db,row['session_id'])
    if not current or current['id']!=row['id'] or db.execute("SELECT 1 FROM agent_turns WHERE owner_id=? AND session_id=? AND status IN ('pending','running')",(owner_id(),row['session_id'])).fetchone():
        raise auth.AuthError('RECORD_STALE','已有新版或园丁正在处理新要求，请重新核对。',409)


def install(app):
    from app import main,agent,agent_planning

    @app.post('/api/v1/agent/records/{turn_id}/confirm')
    def confirm(turn_id:str,data:Confirmation):
        agent.cleanup();fingerprint=agent_changes.digest(data.model_dump())
        with main.connect() as db:
            db.execute('BEGIN IMMEDIATE');row,value=get_record(db,turn_id,data.version);record=value['data']
            if record['state']=='confirmed':
                if record['confirmation_hash']!=fingerprint:
                    raise auth.AuthError('REQUEST_CHANGED','这次操作已按另一内容处理，请查看实际记录。',409)
                return record['applied']
            require_latest(db,row)
            if record['state']!='ready':
                raise auth.AuthError('NOT_READY','请使用已核对的最新记录卡。',409)
            request=RecordRequest.model_validate(record['request']);target=RecordRef.model_validate(record['target'])
            if request.operation=='complete' and not data.performed:
                raise auth.AuthError('CONFIRM_REQUIRED','请确认这项行动当天确实已完成。',422)
            source=request.model_copy(update={'kind':target.kind,'action_date':target.action_date,'relative_day':None})
            current=next((r for r in pool(db,source,record['anchor_at'],target.plant_id) if r['target']==target.model_dump(mode='json')),None)
            if not current or current['blocked_reason']:
                raise auth.AuthError('PLAN_CHANGED','原行动、日期窗口或阶段已变化，请重新核对。',409)
            operation=request.operation
            if operation in ('complete','undo'):
                wanted=operation=='complete'
                if target.kind=='goal':
                    applied=main.record_followup_in_transaction(db,target.stage_id,target.index,main.ActionUpdate(completed=wanted)) if target.stage_id else main.record_goal_in_transaction(db,target.plant_id,target.index,main.ActionUpdate(completed=wanted))
                else:
                    update=main.HabitCheckinInput(completed=wanted,performed=data.performed)
                    applied=main.record_archived_habit_in_transaction(db,target.stage_id,target.action_date,update) if current['archived'] else main.record_habit_in_transaction(db,target.plant_id,target.action_date,update)
            elif operation in ('pause','resume'):
                applied=main.pause_in_transaction(db,target.plant_id,main.PauseInput(paused=operation=='pause'))
            elif operation=='end':
                applied=main.end_habit_in_transaction(db,target.plant_id) if target.kind=='habit' else main.completion_choice_in_transaction(db,target.plant_id,main.CompletionChoice(choice='finish'))
            else:
                raise auth.AuthError('NOT_READY','周期与下一阶段请在原植物确认。',409)
            if isinstance(applied,Response):
                failure=json.loads(applied.body)['error'];raise auth.AuthError(failure['code'],failure['message'],applied.status_code)
            after=next((r for r in pool(db,source,record['anchor_at'],target.plant_id) if all(r['target'][k]==target.model_dump(mode='json')[k] for k in ('plant_id','kind','stage_id','index','action_date'))),None)
            if not after:
                raise RuntimeError('BUSINESS_RESULT_INVALID')
            result={'status':'confirmed','operation':operation,'plant_id':target.plant_id,'kind':target.kind,
                    'completed':applied.get('completed') if operation in ('complete','undo') else None,
                    'action_date':target.action_date.isoformat() if target.action_date else None,'paused':after['paused'],'ended':after['ended'],
                    'stage_completed':bool(applied.get('stage_completed')),'history_preserved':bool(applied.get('history_preserved')) or operation=='undo',
                    'recorded_at':auth.now()}
            if operation in ('complete','undo') and result['completed']!=(operation=='complete'):
                raise RuntimeError('BUSINESS_RESULT_INVALID')
            record.update(state='confirmed',applied=result,after_ref=after['target'],confirmation_hash=fingerprint)
            value['text']='已按实际状态记录，今日行动、植物成长与原页面已同步。'
            db.execute('UPDATE agent_turns SET response_json=? WHERE id=? AND owner_id=?',(json.dumps(value,ensure_ascii=False),turn_id,owner_id()))
            return result

    @app.post('/api/v1/agent/records/{turn_id}/discard')
    def discard(turn_id:str,data:agent_planning.VersionInput):
        agent.cleanup()
        with main.connect() as db:
            db.execute('BEGIN IMMEDIATE');row,value=get_record(db,turn_id,data.version)
            if value['data']['state']!='discarded':
                require_latest(db,row)
                if value['data']['state']=='confirmed':
                    raise auth.AuthError('NOT_READY','已经记录的行动请用撤销处理，不能放弃回滚。',409)
                value['data']['state']='discarded';value['text']='已放弃这次待办，原业务记录不变。'
                db.execute('UPDATE agent_turns SET response_json=? WHERE id=? AND owner_id=?',(json.dumps(value,ensure_ascii=False),turn_id,owner_id()))
            return agent.public_turn(db.execute('SELECT * FROM agent_turns WHERE id=? AND owner_id=?',(turn_id,owner_id())).fetchone())
