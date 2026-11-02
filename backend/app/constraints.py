"""Deterministic checks for explicit numeric constraints; text checks are limited."""
import re
import json
from datetime import date
from difflib import SequenceMatcher


class ConstraintError(ValueError):
    pass


def number(text):
    if text.isdigit():
        return int(text)
    digits = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if text == '十':
        return 10
    if '十' in text:
        before, after = text.split('十', 1)
        return (digits.get(before, 1) * 10) + digits.get(after, 0)
    return digits.get(text)


def validate_plan(plan, source):
    budget = source.get('session_budget_minutes')
    completion = source.get('habit_completion_minutes')
    actions = plan.session_actions
    source_text = json.dumps(source, ensure_ascii=False)
    for part in [plan.summary, *plan.steps, *[a.text for a in (plan.session_actions or [])]]:
        for match in re.finditer(r'你(?:已|已经)(?:确定|选定|选择|拥有|完成)', part):
            if match.group(0) not in source_text:
                raise ConstraintError('计划包含未确认的用户事实，请修改后再确认。')
    goal_context = ' '.join([source.get('goal', ''), source.get('background', ''), *[item.get('answer', '') for item in source.get('history', [])]])
    availability = source.get('availability', '')
    schedule=source.get('habit_schedule')
    if source.get('kind')=='habit' and schedule:
        duration=(date.fromisoformat(str(schedule['end_date']))-date.fromisoformat(str(schedule['start_date']))).days+1
        text='\n'.join([plan.summary,*plan.steps])
        for match in re.finditer(r'第\s*([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*(天|日|周|星期)',text):
            value=number(match.group(1))
            if value is not None and ((value-1)*7+1 if match.group(2) in ('周','星期') else value)>duration:
                raise ConstraintError('计划包含超出所选周期的日期或周数，请重新生成。')
        for match in re.finditer(r'([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*(天|日|周|星期)\s*(?:习惯|计划|周期|安排|训练)',text):
            value=number(match.group(1))
            if value is not None and (value*7 if match.group(2) in ('周','星期') else value)>duration:
                raise ConstraintError('计划时长超过所选习惯周期，请重新生成。')
    daily = re.search(r'每天|每日|天天|一天\s*(?:一|1|两|2|半|[0-9]+)\s*(?:小?时|分钟)', availability)
    if source.get('kind') == 'goal' and not source.get('_goal_followup') and daily and re.search(r'(?:一|1|七|7)\s*(?:周|星期|天|日)', goal_context):
        missing_days = [n for n in range(1, 8) if not any(re.search(rf'第\s*(?:{n}|{ "一二三四五六七"[n-1] })\s*天', step) for step in plan.steps)]
        if missing_days:
            raise ConstraintError('请分别列出第1天到第7天的计划，覆盖完整一周。')
    # The initial goal form records an explicitly supplied duration in availability.
    # Check common numbered-day/week statements; free-form schedules still need review.
    initial_days = re.search(r'希望\s*([0-9]{1,4})\s*天内完成', availability) if source.get('kind') == 'goal' and not source.get('_goal_followup') else None
    if initial_days:
        limit = int(initial_days.group(1))
        text = '\n'.join([plan.summary, *plan.steps])
        for match in re.finditer(r'第\s*([0-9]{1,4}|[一二三四五六七八九十]{1,3})\s*(天|日|周|星期)', text):
            value = number(match.group(1))
            if value is not None and ((value - 1) * 7 + 1 if match.group(2) in ('周', '星期') else value) > limit:
                raise ConstraintError('计划包含超出你填写的完成期限的日期，请调整。')
        for match in re.finditer(r'([0-9]{1,4}|[一二三四五六七八九十]{1,3})\s*(天|日|周|星期)\s*(?:的)?(?:计划|周期|安排|完成)', text):
            value = number(match.group(1))
            if value is not None and value * (7 if match.group(2) in ('周', '星期') else 1) > limit:
                raise ConstraintError('计划时长超过你填写的完成期限，请调整。')
    if source.get('_goal_followup'):
        limit=(source.get('followup_time') or {}).get('duration_days')
        if limit is not None:
            text='\n'.join([plan.summary,*plan.steps])
            for match in re.finditer(r'第\s*([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*(天|日|周|星期)',text):
                value=number(match.group(1))
                if value is not None and ((value-1)*7+1 if match.group(2) in ('周','星期') else value)>limit:
                    raise ConstraintError('后续阶段包含超出用户指定周期的日期，请调整。')
            for match in re.finditer(r'([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*(天|日|周|星期)\s*(?:的)?(?:计划|周期|安排|完成)',text):
                value=number(match.group(1))
                if value is not None and value*(7 if match.group(2) in ('周','星期') else 1)>limit:
                    raise ConstraintError('后续阶段计划时长超过用户指定周期，请调整。')
    # Catch a common self-contradiction; broader semantic consistency still needs review.
    if re.search(r'不(?:另行|额外|再)?(?:安排|进行|做)[^。；;\n]{0,15}记录', plan.summary):
        for step in plan.steps:
            if re.search(r'(?:并|然后|之后|最后|只)\s*(?:记下|记录)', step) and not re.search(r'(?:先确认|额外时间|可选|不(?:用|必|需|要))', step):
                raise ConstraintError('计划说明不安排记录，但步骤要求记录，请修改后再确认。')
    for claim in ('并非要', '不再想要', '不打算', '不愿意'):
        if claim not in source_text and any(claim in part for part in [plan.summary, *plan.steps]):
            raise ConstraintError('计划包含未确认的用户意愿，请修改后再确认。')
    # A stated N-week total cannot equal more than 7*N days without explicit extra days.
    for step in plan.steps:
        match = re.search(r'第\s*([0-9]{1,2}|[一二三四五六七八九十]{1,2})\s*周[^。；;\n]{0,80}?(?:凑满|累计满|达到|满)\s*([0-9]{1,3})\s*天', step)
        if match:
            week = number(match.group(1))
            target = int(match.group(2))
            if week is not None and target > week * 7 and not re.search(r'(?:额外|另加|加上|另外|及)\s*[0-9一二三四五六七八九十]+\s*天', step):
                raise ConstraintError('计划中的周数和天数不一致，请调整后再确认。')
    if budget is not None or completion is not None:
        if not actions:
            raise ConstraintError('请先提供可核对的单次时间安排。')
        total = sum(action.minutes for action in actions)
        text = '\n'.join([plan.summary, *plan.steps, *[action.text for action in actions]])
        if budget is not None and (re.search(r'不占用(?:这|该|原有|当天|单次)?\s*[0-9一二三四五六七八九十]+\s*分钟', text) or re.search(r'不另占时间[^。；;\n]{0,40}(?:开始前|结束后|读完后|白天)', text)):
            raise ConstraintError('计划将部分时间安排在预算之外，请调整后再确认。')
        if budget is not None and total > budget:
            raise ConstraintError('单次安排超过你填写的可用时间，请调整后再确认。')
        if completion is not None:
            core = sum(action.minutes for action in actions if action.kind == 'core')
            for action in actions:
                if action.kind == 'core' and re.search(r'(?:(?:用|花费?|预留|分配|安排)\s*[0-9一二三四五六七八九十]+\s*分钟[^。；;\n]{0,12}(?:准备|整理|记录|打勾|收尾|热身)|(?:准备|整理|记录|打勾|收尾|热身)\s*[0-9一二三四五六七八九十]+\s*分钟)', action.text):
                    raise ConstraintError('核心行动中包含单列的准备或整理时间，请分开并重新确认。')
            if core < completion:
                raise ConstraintError('核心行动时间少于你填写的完成标准，请调整后再确认。')
            # Recognise common affirmative formulations only; cannot prove arbitrary text safe.
            pattern = r'([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*分钟([^。；;\n]{0,10}?)(?:即算|就算|算作|算|视为|视作|即为|即可|就可|便可)([^。；;\n]{0,6}?)(?:完成|达标)'
            for match in re.finditer(pattern, text):
                bridge = match.group(2)
                if '不' in bridge or '不能' in bridge:
                    continue
                claimed = number(match.group(1))
                if claimed is not None and claimed < completion:
                    raise ConstraintError('文字中的完成时间与您填写的完成标准冲突，请修改后再确认。')
    return {
        'budget_checked': budget is not None,
        'completion_checked': completion is not None,
        'session_total_minutes': sum(action.minutes for action in actions) if actions else None,
    }


def repeats_question(question, history):
    normalized = re.sub(r"[^\w\u4e00-\u9fff]", "", question).lower()
    if not normalized:
        return False
    for item in history:
        old = re.sub(r"[^\w\u4e00-\u9fff]", "", item.get('question', '')).lower()
        if old:
            a = {normalized[i:i+2] for i in range(len(normalized)-1)}
            b = {old[i:i+2] for i in range(len(old)-1)}
            overlap = len(a & b)
            similar = overlap >= 8 and overlap / max(1, min(len(a), len(b))) >= 0.55
            if SequenceMatcher(None, normalized, old).ratio() >= 0.74 or similar:
                return True
    return False


def asks_known_duration(question, source):
    goal = source.get('goal', '')
    target = re.search(r'(?:[0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*分钟', goal)
    return bool(target and re.search(r'(?:总时长|演讲时长|分享时长|视频时长|时长是否)', question))
