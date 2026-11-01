'use client';
import { useCallback, useEffect, useState } from 'react';

type Action = {text: string; kind: 'core'|'other'; minutes: number|null};
type Constraints = {session_budget_minutes: number|null; habit_completion_minutes: number|null};
type Plan = { title: string; summary: string; steps: string[]; session_actions?: Action[]|null };
type Plant = { id: string; title: string; kind: string; plan: Plan; constraints?: Constraints };
type GoalAction = {index: number; text: string; completed: boolean; due_date:string|null; cancelled:boolean};
type TodayGoal = {plant_title:string; index:number; text:string; due_date:string|null; completed:boolean};
type TodayHabit = {plant_title:string; date:string; completed:boolean; completion_standard:string};
type Today = {date:string;goals_today:TodayGoal[];goals_overdue:TodayGoal[];goals_unscheduled:TodayGoal[];habits_today:TodayHabit[]};

function TodayActions() {
  const [today,setToday]=useState<Today|null>(null);const [error,setError]=useState('');
  const load=useCallback(async()=>{const r=await fetch('/api/v1/today');if(!r.ok)throw new Error();setToday(await r.json());setError('');},[]);
  useEffect(()=>{const refresh=()=>{void load().catch(()=>setError('今日行动暂时无法读取。'));};refresh();window.addEventListener('bloomate-progress',refresh);return()=>window.removeEventListener('bloomate-progress',refresh);},[load]);
  return <section className="card"><h2>今日行动</h2>{error && <p role="status">{error}</p>}{today && <><p>目标按北京时间 {today.date}；习惯按各自周期时区计算。</p><h3>今天</h3>{today.goals_today.length+today.habits_today.length===0 ? <p>今天没有已安排的行动。</p> : <ul>{today.goals_today.map((a,i)=><li key={`g${i}`}>{a.plant_title}：{a.text}{a.completed?'（已完成）':''}</li>)}{today.habits_today.map((a,i)=><li key={`h${i}`}>{a.plant_title}：{a.completion_standard}{a.completed?'（已完成）':''}</li>)}</ul>}<h3>逾期未完成</h3>{today.goals_overdue.length?<ul>{today.goals_overdue.map((a,i)=><li key={i}>{a.due_date} · {a.plant_title}：{a.text}</li>)}</ul>:<p>没有逾期步骤。</p>}<h3>待安排日期</h3>{today.goals_unscheduled.length?<ul>{today.goals_unscheduled.map((a,i)=><li key={i}>{a.plant_title}：{a.text}</li>)}</ul>:<p>没有待安排的目标步骤。</p>}<p className="note">完成、打卡和日期调整可在下方对应植物中操作。</p></>}</section>;
}
type Adjustment = {id:string; status:string; original_text:string; suggestion:string|null; suggestion_steps:string[]|null; step_index:number; error?:{message:string}};

function AdjustmentEditor({plantId, actions, onChanged}: {plantId:string; actions:GoalAction[]; onChanged:()=>Promise<void>}) {
  const [selected,setSelected]=useState(''); const [difficulty,setDifficulty]=useState('');
  const [draft,setDraft]=useState<Adjustment|null>(null); const [edited,setEdited]=useState('');
  const [busy,setBusy]=useState(false); const [message,setMessage]=useState('');
  const storageKey=`bloomate-adjustment-${plantId}`;
  const poll=useCallback(async (id:string) => {
    for (let i=0;i<75;i++) {
      const response=await fetch(`/api/v1/adjustments/${id}`); const data:Adjustment=await response.json();
      if (!response.ok) throw new Error('无法读取调整建议。');
      if (data.status==='error') throw new Error(data.error?.message || '建议生成失败，请重试。');
      if (data.status==='done') {setDraft(data);setEdited((data.suggestion_steps || (data.suggestion?[data.suggestion]:[])).join('\n'));return;}
      if (data.status==='confirmed' || data.status==='discarded') {localStorage.removeItem(storageKey);return;}
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    throw new Error('等待时间较长，刷新后可继续查看。');
  },[storageKey]);
  useEffect(()=>{const id=localStorage.getItem(storageKey);if(id) {setBusy(true);void poll(id).catch(e=>setMessage(e instanceof Error?e.message:'暂时无法读取建议。')).finally(()=>setBusy(false));}},[poll,storageKey]);
  async function create(event:React.FormEvent<HTMLFormElement>) {
    event.preventDefault();setBusy(true);setMessage('');
    try {const response=await fetch(`/api/v1/plants/${plantId}/adjustments`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({step_index:Number(selected),difficulty})});const data=await response.json();if(!response.ok) throw new Error(data.error?.message || '无法生成建议。');localStorage.setItem(storageKey,data.id);await poll(data.id);}
    catch(e){setMessage(e instanceof Error?e.message:'无法生成建议。');}finally{setBusy(false);}
  }
  async function finish(confirm:boolean) {
    if(!draft)return;setBusy(true);setMessage('');
    try {const steps=edited.split('\n').map(s=>s.trim());if(confirm && (steps.length>5 || steps.some(s=>!s || s.length>500)))throw new Error('请填写一至五条步骤，每条不超过500字且不能留空。');const response=await fetch(`/api/v1/adjustments/${draft.id}/${confirm?'confirm':'discard'}`,{method:'POST',headers:{'Content-Type':'application/json'},...(confirm?{body:JSON.stringify({steps})}:{})});const data=await response.json();if(!response.ok) throw new Error(data.error?.message || '操作失败。');localStorage.removeItem(storageKey);setDraft(null);setDifficulty('');setSelected('');if(confirm){await onChanged();window.dispatchEvent(new Event('bloomate-progress'));}setMessage(confirm?'计划已更新。':'建议已放弃，原计划保持不变。');}
    catch(e){setMessage(e instanceof Error?e.message:'操作失败，请重试。');}finally{setBusy(false);}
  }
  return <div><details><summary>调整一个未完成步骤</summary>{draft ? <div><p>原步骤：{draft.original_text}</p><label>调整建议（可编辑，每行一条）<textarea value={edited} maxLength={2500} onChange={e=>setEdited(e.target.value)}/></label><p className="note">可拆为一至五条；其他步骤及已完成记录保留，原日期只给第一条。</p><button type="button" disabled={busy || !edited.trim()} onClick={()=>void finish(true)}>确认调整</button> <button type="button" disabled={busy} onClick={()=>void finish(false)}>放弃建议</button></div> : <form onSubmit={create}><label>选择未完成步骤<select value={selected} required onChange={e=>setSelected(e.target.value)}><option value="">请选择</option>{actions.filter(a=>!a.completed && !a.cancelled).map(a=><option key={a.index} value={a.index}>{a.text}</option>)}</select></label><label>遇到了什么困难？<textarea required maxLength={1000} value={difficulty} onChange={e=>setDifficulty(e.target.value)} placeholder="例如：这个步骤太耗时"/></label><button type="submit" disabled={busy || !actions.some(a=>!a.completed && !a.cancelled)}>{busy?'正在生成建议…':'生成调整建议'}</button></form>}</details>{message && <p role="status">{message}</p>}</div>;
}
type HabitStage = {start_date:string; end_date:string; timezone:string; weekdays:number[]; completion_standard:string; today:string;status:'upcoming'|'active'|'review_due'};
type Occurrence = {date:string; completed:boolean; recorded_at:string|null; is_backfill:boolean; paused:boolean};
type PauseState = {paused:boolean; since:string|null; end_date:string|null; future_days:number|null; future_dates:string[]; today:string;threshold:number|null;completed:number|null;achieved:boolean};
type ScheduleProposal = {id:string; status:string; before:{end_date:string; weekdays:number[]}; after:{end_date:string; weekdays:number[]; effective_date:string; future_count:number;threshold?:number|null;completed?:number|null}};
const weekdayNames=['周一','周二','周三','周四','周五','周六','周日'];

function HabitAdvice({plantId}:{plantId:string}) {
  const [difficulty,setDifficulty]=useState('');const [advice,setAdvice]=useState('');const [busy,setBusy]=useState(false);const [message,setMessage]=useState('');
  const storageKey=`bloomate-habit-advice-${plantId}`;
  const poll=useCallback(async(id:string)=>{for(let i=0;i<75;i++){const r=await fetch(`/api/v1/habit-advice/${id}`);const d=await r.json();if(!r.ok)throw new Error('无法读取习惯建议。');if(d.status==='error'){localStorage.removeItem(storageKey);throw new Error(d.error?.message || '建议生成失败。');}if(d.status==='done'){setAdvice(d.suggestion);return;}await new Promise(resolve=>setTimeout(resolve,1000));}throw new Error('建议仍在生成，刷新后可继续查看。');},[storageKey]);
  useEffect(()=>{const id=localStorage.getItem(storageKey);if(id){setBusy(true);void poll(id).catch(e=>setMessage(e instanceof Error?e.message:'无法读取建议。')).finally(()=>setBusy(false));}},[poll,storageKey]);
  async function create(event:React.FormEvent<HTMLFormElement>){event.preventDefault();setBusy(true);setMessage('');try{const r=await fetch(`/api/v1/plants/${plantId}/habit-advice`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({difficulty})});const d=await r.json();if(!r.ok)throw new Error(d.error?.message || '无法生成建议。');localStorage.setItem(storageKey,d.id);await poll(d.id);}catch(e){setMessage(e instanceof Error?e.message:'无法生成建议。');}finally{setBusy(false);}}
  return <div><details><summary>遇到困难，问问园丁</summary><form onSubmit={create}><label>遇到了什么困难？<textarea required maxLength={1000} value={difficulty} onChange={e=>setDifficulty(e.target.value)} placeholder="例如：最近只有每周三天有时间"/></label><button type="submit" disabled={busy}>{busy?'正在生成建议…':'生成习惯建议'}</button></form>{advice && <div><label>园丁建议（可编辑，仅供参考）<textarea value={advice} maxLength={500} onChange={e=>setAdvice(e.target.value)}/></label><p className="note">建议不会自动修改周期；如需调整，请在下方预览并确认。编辑文字不会保存。</p></div>}</details>{message && <p role="status">{message}</p>}</div>;
}
type Growth = {counts:{completed:number;total:number}|null;threshold:number|null;achieved:boolean;eligible:boolean;progress_percent:number|null};

function GrowthCard({plantId,kind}: {plantId:string;kind:string}) {
  const [growth,setGrowth]=useState<Growth|null>(null);const [suggestion,setSuggestion]=useState('');const [busy,setBusy]=useState(false);const [message,setMessage]=useState('');
  const storageKey=`bloomate-growth-suggestion-${plantId}`;
  const load=useCallback(async()=>{const r=await fetch(`/api/v1/plants/${plantId}/growth`);if(!r.ok)throw new Error('成长状态暂时无法读取。');setGrowth(await r.json());},[plantId]);
  const poll=useCallback(async(id:string)=>{for(let i=0;i<75;i++){const r=await fetch(`/api/v1/growth/suggestions/${id}`);const d=await r.json();if(!r.ok)throw new Error('无法读取阶段建议。');if(d.status==='error'){localStorage.removeItem(storageKey);throw new Error(d.error?.message || '建议生成失败。');}if(d.status==='done'){setSuggestion(d.suggestion);return;}await new Promise(resolve=>setTimeout(resolve,1000));}throw new Error('建议仍在生成，刷新后可继续查看。');},[storageKey]);
  useEffect(()=>{void load().catch(()=>setMessage('成长状态暂时无法读取。'));const refresh=()=>{void load().catch(()=>setMessage('成长状态暂时无法读取。'));};window.addEventListener('bloomate-progress',refresh);const id=localStorage.getItem(storageKey);if(id){setBusy(true);void poll(id).catch(e=>setMessage(e instanceof Error?e.message:'无法读取建议。')).finally(()=>setBusy(false));}return()=>window.removeEventListener('bloomate-progress',refresh);},[load,poll,storageKey]);
  async function suggest(){setBusy(true);setMessage('');try{const r=await fetch(`/api/v1/plants/${plantId}/growth/suggestions`,{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.error?.message || '无法生成建议。');localStorage.setItem(storageKey,d.id);await poll(d.id);}catch(e){setMessage(e instanceof Error?e.message:'无法生成建议。');}finally{setBusy(false);}}
  async function standard(event:React.FormEvent<HTMLFormElement>){event.preventDefault();const fields=new FormData(event.currentTarget);setBusy(true);setMessage('');try{const r=await fetch(`/api/v1/plants/${plantId}/growth/standard`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({threshold:Number(fields.get('threshold'))})});const d=await r.json();if(!r.ok)throw new Error(d.error?.message || '无法保存标准。');await load();setMessage('阶段标准已确认。');}catch(e){setMessage(e instanceof Error?e.message:'无法保存标准。');}finally{setBusy(false);}}
  async function confirm(){setBusy(true);setMessage('');try{const r=await fetch(`/api/v1/plants/${plantId}/growth/confirm`,{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.error?.message || '无法确认成果。');await load();setMessage('阶段成果已确认，植物发芽了。');}catch(e){setMessage(e instanceof Error?e.message:'无法确认成果。');}finally{setBusy(false);}}
  if(!growth)return <p>正在读取成长状态…</p>;
  if(!growth.counts)return <p>先设置习惯周期，再确认阶段标准。</p>;
  return <div><strong>阶段成长</strong><p>{growth.achieved?'🌿 阶段成果已确认，植物已发芽':'🌱 种子正在等待阶段成果'}</p>{kind==='goal' && growth.progress_percent!==null && <p>计划步骤进度：{growth.progress_percent}%（按暂定份额计算，不代表目标已完成）</p>}<p>已完成 {growth.counts.completed} {kind==='habit'?'次打卡':'个步骤'}{growth.threshold ? `，阶段标准 ${growth.threshold} 次` : '，尚未确认阶段标准'}</p><button type="button" disabled={busy || growth.counts.total===0} onClick={()=>void suggest()}>{busy?'处理中…':'让园丁建议阶段标准'}</button>{suggestion && <p>园丁建议：{suggestion}（仅供参考，由你决定。）</p>}<form onSubmit={standard}><label>阶段需要完成多少次？<input name="threshold" type="number" required min={1} max={growth.counts.total} defaultValue={growth.threshold ?? ''} key={growth.threshold ?? 'new'}/></label><p className="note">可选 1 至 {growth.counts.total} 次；修改标准后需要重新确认成果。</p><button type="submit" disabled={busy || growth.counts.total===0}>确认阶段标准</button></form><button type="button" disabled={busy || !growth.eligible || growth.achieved} onClick={()=>void confirm()}>确认阶段成果</button>{message && <p role="status">{message}</p>}</div>;
}

function HabitScheduleEditor({plantId,stage,onChanged}: {plantId:string;stage:HabitStage;onChanged:()=>Promise<void>}) {
  const [days,setDays]=useState(stage.weekdays);const [draft,setDraft]=useState<ScheduleProposal|null>(null);
  const [busy,setBusy]=useState(false);const [message,setMessage]=useState('');const storageKey=`bloomate-schedule-${plantId}`;
  useEffect(()=>{const id=localStorage.getItem(storageKey);if(id) void fetch(`/api/v1/habit-schedule-proposals/${id}`).then(async r=>{if(!r.ok)throw new Error();const data:ScheduleProposal=await r.json();if(data.status==='pending')setDraft(data);else localStorage.removeItem(storageKey);}).catch(()=>setMessage('暂时无法读取周期调整。'));},[storageKey]);
  async function propose(event:React.FormEvent<HTMLFormElement>) {
    event.preventDefault();const fields=new FormData(event.currentTarget);setBusy(true);setMessage('');
    try {const response=await fetch(`/api/v1/plants/${plantId}/habit-schedule-proposals`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({end_date:fields.get('end_date'),weekdays:days})});const data=await response.json();if(!response.ok)throw new Error(data.error?.message || '无法预览周期。');setDraft(data);localStorage.setItem(storageKey,data.id);}
    catch(e){setMessage(e instanceof Error?e.message:'无法预览周期。');}finally{setBusy(false);}
  }
  async function finish(confirm:boolean) {
    if(!draft)return;setBusy(true);setMessage('');
    try {const response=await fetch(`/api/v1/habit-schedule-proposals/${draft.id}/${confirm?'confirm':'discard'}`,{method:'POST'});const data=await response.json();if(!response.ok)throw new Error(data.error?.message || '操作失败。');localStorage.removeItem(storageKey);setDraft(null);if(confirm){await onChanged();window.dispatchEvent(new Event('bloomate-progress'));}setMessage(confirm?'周期已更新。':'已放弃，原周期保持不变。');}
    catch(e){setMessage(e instanceof Error?e.message:'操作失败。');}finally{setBusy(false);}
  }
  return <div><details><summary>调整未来习惯周期</summary>{draft ? <div><p>原周期：截至 {draft.before.end_date}，{draft.before.weekdays.map(d=>weekdayNames[d]).join('、')}</p><p>新周期：截至 {draft.after.end_date}，{draft.after.weekdays.map(d=>weekdayNames[d]).join('、')}</p><p>从 {draft.after.effective_date} 起生效，之后有 {draft.after.future_count} 个计划日；今天和历史记录不变。</p>{draft.after.threshold != null && <p>当前阶段标准 {draft.after.threshold} 次，已完成 {draft.after.completed ?? 0} 次；今天与可补签日期请另外核对。</p>}<button type="button" disabled={busy} onClick={()=>void finish(true)}>确认周期调整</button> <button type="button" disabled={busy} onClick={()=>void finish(false)}>放弃周期调整</button></div> : <form onSubmit={propose}><label>新的结束日期<input type="date" name="end_date" required defaultValue={stage.end_date}/></label><fieldset><legend>新的每周计划日</legend>{weekdayNames.map((name,i)=><label key={i}><input type="checkbox" checked={days.includes(i)} onChange={e=>setDays(old=>e.target.checked?[...old,i]:old.filter(d=>d!==i))}/>{name}</label>)}</fieldset><p className="note">只影响未来日期，完成标准和时区不变。</p><button type="submit" disabled={busy || !days.length}>预览周期调整</button></form>}</details>{message && <p role="status">{message}</p>}</div>;
}

function HabitActions({plantId,paused}: {plantId:string;paused:boolean}) {
  const [stage, setStage] = useState<HabitStage|null>(null);
  const [occurrences, setOccurrences] = useState<Occurrence[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [standard, setStandard] = useState(''); const [days, setDays] = useState([0,1,2,3,4,5,6]);
  const load = useCallback(async () => {
    const response = await fetch(`/api/v1/plants/${plantId}/habit-stage`);
    if (!response.ok) throw new Error('习惯周期暂时无法读取。');
    const data = await response.json(); setStage(data.stage); setOccurrences(data.occurrences);
  }, [plantId]);
  useEffect(() => {void load().catch(() => setError('习惯周期暂时无法读取，请刷新重试。')).finally(() => setLoading(false));}, [load,paused]);
  async function create(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); const fields = new FormData(event.currentTarget); setBusy(true); setError('');
    try {
      const response = await fetch(`/api/v1/plants/${plantId}/habit-stage`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start_date:fields.get('start_date'),end_date:fields.get('end_date'),timezone:'Asia/Shanghai',weekdays:days,completion_standard:standard})});
      const data = await response.json(); if (!response.ok) throw new Error(data.error?.message || '无法设置周期，请检查日期和星期。');
      await load(); window.dispatchEvent(new Event('bloomate-progress'));
    } catch (e) {setError(e instanceof Error ? e.message : '设置失败，请重试。');} finally {setBusy(false);}
  }
  async function toggle(item:Occurrence) {
    setBusy(true); setError('');
    try {
      const response = await fetch(`/api/v1/plants/${plantId}/habit-occurrences/${item.date}`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({completed:!item.completed,performed:!item.completed})});
      const data = await response.json(); if (!response.ok) throw new Error(data.error?.message || '保存失败。');
      setOccurrences(current => current.map(entry => entry.date === data.date ? data : entry)); window.dispatchEvent(new Event('bloomate-progress'));
    } catch (e) {setError(e instanceof Error ? e.message : '保存失败，请重试。');} finally {setBusy(false);}
  }
  if (loading) return <p>正在读取习惯周期…</p>;
  return <div><strong>习惯打卡</strong>{stage ? <div><p>{stage.start_date} 至 {stage.end_date} · {stage.timezone}</p><p>{stage.status==='review_due'?'周期已到期，等待复盘；不会自动达成成果。':stage.status==='upcoming'?'周期尚未开始。':'周期进行中。'}</p><p>每周计划日：{stage.weekdays.map(day=>weekdayNames[day]).join('、')}</p><p>每次完成标准：{stage.completion_standard}</p>{occurrences.length ? <ul>{occurrences.map(item => <li key={item.date}>{item.date}{item.is_backfill && item.completed ? '（补签）' : ''}{item.paused ? '（暂停日）' : ''} <button type="button" disabled={busy || (item.paused && !item.completed)} onClick={() => void toggle(item)}>{item.completed ? '已完成，撤销' : item.date === stage.today ? '确认今天已完成' : '确认当天已完成并补签'}</button></li>)}</ul> : <p>今天和最近三天没有计划日。</p>}<p className="note">补签仅用于实际做过、但忘记记录的行动。</p><HabitAdvice plantId={plantId}/><HabitScheduleEditor key={`${stage.end_date}-${stage.weekdays.join(',')}`} plantId={plantId} stage={stage} onChanged={load}/></div> : <form onSubmit={create}><p>设置开始日期、结束日期和计划日，再开始打卡。日期按北京时间计算；确认后可预览并调整未来计划日。</p><label>开始日期<input name="start_date" type="date" required/></label><label>结束日期<input name="end_date" type="date" required/></label><label>每次怎样算完成？<input required maxLength={200} value={standard} onChange={e=>setStandard(e.target.value)} placeholder="例如：读满20分钟"/></label><fieldset><legend>每周计划日</legend>{weekdayNames.map((name,index)=><label key={index}><input type="checkbox" checked={days.includes(index)} onChange={e=>setDays(current=>e.target.checked?[...current,index]:current.filter(n=>n!==index))}/>{name}</label>)}</fieldset><button type="submit" disabled={busy || days.length===0}>确认习惯周期</button></form>}{error && <p role="status">{error}</p>}</div>;
}

function GoalActions({plantId,paused}: {plantId: string;paused:boolean}) {
  const [actions, setActions] = useState<GoalAction[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {const r=await fetch(`/api/v1/plants/${plantId}/actions`);if(!r.ok)throw new Error('无法读取计划步骤。');const data=await r.json();setActions(data.actions);},[plantId]);
  useEffect(() => {
    let active = true;
    fetch(`/api/v1/plants/${plantId}/actions`).then(async r => {
      if (!r.ok) throw new Error();
      const data = await r.json(); if (active) setActions(data.actions);
    }).catch(() => {if (active) setError('计划步骤暂时无法读取，请刷新重试。');}).finally(() => {if (active) setLoading(false);});
    return () => {active = false;};
  }, [plantId]);
  async function toggle(action: GoalAction) {
    setBusy(true); setError('');
    try {
      const response = await fetch(`/api/v1/plants/${plantId}/actions/${action.index}`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({completed:!action.completed})});
      if (!response.ok) throw new Error();
      const updated: GoalAction = await response.json();
      setActions(current => current.map(item => item.index === updated.index ? updated : item)); window.dispatchEvent(new Event('bloomate-progress'));
    } catch {setError('保存失败，请稍后重试。');}
    finally {setBusy(false);}
  }
  async function schedule(event:React.FormEvent<HTMLFormElement>, action:GoalAction) {
    event.preventDefault();const fields=new FormData(event.currentTarget);setBusy(true);setError('');
    try {const response=await fetch(`/api/v1/plants/${plantId}/actions/${action.index}/date`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({due_date:fields.get('due_date') || null})});if(!response.ok)throw new Error();const updated:GoalAction=await response.json();setActions(current=>current.map(item=>item.index===updated.index?updated:item));window.dispatchEvent(new Event('bloomate-progress'));}
    catch{setError('日期保存失败，请稍后重试。');}finally{setBusy(false);}
  }
  async function manual(event:React.FormEvent<HTMLFormElement>,index:number|null) {
    event.preventDefault();const form=event.currentTarget;const fields=new FormData(form);setBusy(true);setError('');
    try{const response=await fetch(index===null?`/api/v1/plants/${plantId}/actions`:`/api/v1/plants/${plantId}/actions/${index}/text`,{method:index===null?'POST':'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:fields.get('text')})});const data=await response.json();if(!response.ok)throw new Error(data.error?.message || '保存失败。');await refresh();if(index===null)form.reset();window.dispatchEvent(new Event('bloomate-progress'));}
    catch(e){setError(e instanceof Error?e.message:'保存失败。');}finally{setBusy(false);}
  }
  async function cancel(action:GoalAction) {
    setBusy(true);setError('');try{const response=await fetch(`/api/v1/plants/${plantId}/actions/${action.index}/cancel`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({cancelled:!action.cancelled})});const data=await response.json();if(!response.ok)throw new Error(data.error?.message || '操作失败。');setActions(current=>current.map(item=>item.index===data.index?data:item));window.dispatchEvent(new Event('bloomate-progress'));}catch(e){setError(e instanceof Error?e.message:'操作失败。');}finally{setBusy(false);}
  }
  return <div><strong>计划步骤</strong>{loading ? <p>正在读取步骤…</p> : <><ul>{actions.map(action => <li key={action.index}>{action.text}{action.cancelled?'（已取消）':''} <button type="button" disabled={busy || action.cancelled || (paused && !action.completed)} onClick={() => void toggle(action)}>{action.completed ? '已完成，撤销' : '标记完成'}</button> <button type="button" disabled={busy || action.completed} onClick={()=>void cancel(action)}>{action.cancelled?'恢复步骤':'取消步骤'}</button>{!action.cancelled && <><form onSubmit={event=>void schedule(event,action)}><label>计划日期<input type="date" name="due_date" defaultValue={action.due_date || ''} key={action.due_date || 'empty'}/></label><button type="submit" disabled={busy}>保存日期</button><span className="note">留空可清除日期</span></form>{!action.completed && <details><summary>手动修改步骤</summary><form onSubmit={event=>void manual(event,action.index)}><label>步骤内容<input name="text" required maxLength={500} defaultValue={action.text} key={action.text}/></label><button type="submit" disabled={busy}>保存步骤</button></form></details>}</>}</li>)}</ul><details><summary>新增步骤</summary><form onSubmit={event=>void manual(event,null)}><label>新步骤<input name="text" required maxLength={500}/></label><button type="submit" disabled={busy}>添加步骤</button></form></details><AdjustmentEditor plantId={plantId} actions={actions} onChanged={refresh}/></>}{error && <p role="status">{error}</p>}</div>;
}

function PlantCard({plant}: {plant:Plant}) {
  const [pause,setPause]=useState<PauseState|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState('');
  useEffect(()=>{void fetch(`/api/v1/plants/${plant.id}/pause`).then(async r=>{if(!r.ok)throw new Error();setPause(await r.json());}).catch(()=>setError('暂时无法读取暂停状态。'));},[plant.id]);
  async function togglePause() {
    if(!pause)return;setBusy(true);setError('');
    try {const response=await fetch(`/api/v1/plants/${plant.id}/pause`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({paused:!pause.paused})});const data=await response.json();if(!response.ok)throw new Error(data.error?.message || '操作失败。');setPause(data);window.dispatchEvent(new Event('bloomate-progress'));}
    catch(e){setError(e instanceof Error?e.message:'操作失败。');}finally{setBusy(false);}
  }
  return <article><span>🌱</span><div><strong>{plant.title}</strong><p>{plant.kind==='goal'?'目标':'习惯'} · {pause?.paused?'已暂停':'已种下'}</p>{pause && <div>{pause.paused && <p>暂停从 {pause.since} 开始。{pause.end_date ? `截止日期仍为 ${pause.end_date}，之后有 ${pause.future_days} 个计划日。` : '已有进展会保留。'}恢复不会自动延长期限；暂停及恢复当天按暂停日处理。{pause.threshold !== null && ` 阶段标准 ${pause.threshold} 次，已完成 ${pause.completed ?? 0} 次。`}{pause.future_dates.length > 0 && ` 之后计划日（最多显示10个）：${pause.future_dates.join('、')}。`}</p>}<button type="button" disabled={busy} onClick={()=>void togglePause()}>{pause.paused?'恢复':'暂停'}</button></div>}{error && <p role="status">{error}</p>}{plant.constraints?.session_budget_minutes && <p>每次最多 {plant.constraints.session_budget_minutes} 分钟{plant.constraints.habit_completion_minutes ? ` · 核心行动 ${plant.constraints.habit_completion_minutes} 分钟` : ''}</p>}{plant.plan.session_actions?.length ? <p>单次安排：{plant.plan.session_actions.map(a=>`${a.text} ${a.minutes}分钟`).join('、')}</p> : null}{plant.kind==='goal' ? <GoalActions plantId={plant.id} paused={pause?.paused || false}/> : <><ul>{plant.plan.steps.map((s,i)=><li key={i}>{s}</li>)}</ul><HabitActions plantId={plant.id} paused={pause?.paused || false}/></>}<GrowthCard plantId={plant.id} kind={plant.kind}/></div></article>;
}

export default function Home() {
  const [kind, setKind] = useState<'goal'|'habit'>('goal');
  const [goal, setGoal] = useState('');
  const [background, setBackground] = useState('');
  const [availability, setAvailability] = useState('');
  const [budget, setBudget] = useState('');
  const [completion, setCompletion] = useState('');
  const [constraints, setConstraints] = useState<Constraints>({session_budget_minutes:null,habit_completion_minutes:null});
  const [plan, setPlan] = useState<Plan|null>(null);
  const [job, setJob] = useState('');
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState('');
  const [plants, setPlants] = useState<Plant[]>([]);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const loadGarden = useCallback(async () => {
    try { const res = await fetch('/api/v1/garden'); if (!res.ok) throw new Error(); const data = await res.json(); setPlants(data.plants); }
    catch { setMessage('花园暂时无法连接，请检查本地服务。'); }
  }, []);
  const poll = useCallback(async function pollJob(jobId: string): Promise<void> {
    setJob(jobId); localStorage.setItem('bloomate-active-job', jobId);
    for (let i = 0; i < 75; i++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      const response = await fetch(`/api/v1/plans/${jobId}`); const result = await response.json();
      if (!response.ok || result.status === 'error') throw new Error(result.error?.message || '计划生成失败。');
      if (result.status === 'superseded' && result.next_job_id) return pollJob(result.next_job_id);
      if (result.status === 'done') { setPlan(result.proposal); setQuestion(result.question || ''); setConstraints(result.constraints); return; }
    }
    throw new Error('等待时间较长，请稍后再试。');
  }, []);
  useEffect(() => {
    void loadGarden();
    const saved = localStorage.getItem('bloomate-active-job');
    if (saved) {
      setJob(saved);
      fetch(`/api/v1/plans/${saved}`).then(r => r.json()).then(result => {
        if (result.status === 'done') { setPlan(result.proposal); setQuestion(result.question || ''); setConstraints(result.constraints); }
        else if (result.status === 'error') setMessage(result.error?.message || '上一次生成中断，请重新开始。');
        else if (result.status === 'superseded' && result.next_job_id) void poll(result.next_job_id).catch(() => setMessage('上一次对话暂时无法读取。'));
        else if (result.status === 'pending' || result.status === 'running') void poll(saved).catch(() => setMessage('上一次对话尚未完成，请稍后重试。'));
      }).catch(() => setMessage('上一次对话暂时无法读取。'));
    }
  }, [loadGarden, poll]);
  async function propose(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (kind === 'habit' && budget && completion && Number(completion) > Number(budget)) { setMessage('核心行动的完成分钟数超过可用时间，请调整后继续。'); return; }
    setBusy(true); setMessage(''); setPlan(null);
    try {
      const response = await fetch('/api/v1/plans', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({kind, goal, background, availability, session_budget_minutes:budget ? Number(budget) : null, habit_completion_minutes:kind==='habit' && completion ? Number(completion) : null}) });
      const body = await response.json(); if (!response.ok) throw new Error(body.error?.message || '无法创建计划。');
      setQuestion(''); await poll(body.id);
    } catch (error) { setMessage(error instanceof Error ? error.message : '暂时无法生成计划。'); }
    finally { setBusy(false); }
  }
  async function reply(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setMessage('');
    try {
      const res = await fetch(`/api/v1/plans/${job}/reply`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({answer})});
      const data = await res.json(); if (!res.ok) throw new Error(data.error?.message || '暂时无法继续对话。');
      setAnswer(''); setQuestion(''); await poll(data.id);
    } catch (error) { setMessage(error instanceof Error ? error.message : '暂时无法继续对话。'); }
    finally { setBusy(false); }
  }
  async function confirm() {
    if (plan?.session_actions?.some(a => a.minutes === null || a.minutes < 1)) { setMessage('请为每项行动填写大于零的分钟数。'); return; }
    setBusy(true); setMessage('');
    try {
      const res = await fetch(`/api/v1/proposals/${job}/confirm`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(plan)}); const data = await res.json();
      if (!res.ok) throw new Error(data.error?.message || '种植失败。');
      setPlan(null); localStorage.removeItem('bloomate-active-job'); setMessage('种子已种下。每一步都值得被看见。'); await loadGarden(); window.dispatchEvent(new Event('bloomate-progress'));
    } catch (error) { setMessage(error instanceof Error ? error.message : '种植失败，请重试。'); }
    finally { setBusy(false); }
  }
  function changeKind(nextKind: 'goal'|'habit') {
    if (nextKind === kind || busy) return;
    setKind(nextKind);
    setGoal(''); setBackground(''); setAvailability(''); setBudget(''); setCompletion('');
    setQuestion(''); setAnswer(''); setPlan(null); setJob(''); setMessage('');
    setConstraints({session_budget_minutes:null, habit_completion_minutes:null});
    localStorage.removeItem('bloomate-active-job');
  }
  return <main><header><div className="brand"><span>✿</span> Bloomate</div><div className="notice">本机体验中 · 尚未保存到账户</div></header>
    <section className="intro"><div className="eyebrow">YOUR LITTLE GARDEN</div><h1>把想做的事，<br/><em>慢慢种成花。</em></h1><p>你好，我是你的园丁伙伴。告诉我你想前进的方向，我们一起找到可以开始的一小步。</p></section>
    <TodayActions/><div className="columns"><section className="card"><div className="card-top"><span className="icon">✧</span><div><h2>和园丁聊聊</h2><p>从一个想法开始就好</p></div></div>
      <p className="privacy">本机测试使用 DeepSeek：你填写的内容会发送至该服务处理，请勿输入敏感信息。</p><form onSubmit={propose}><div className="choice"><button type="button" disabled={busy} className={kind==='goal'?'active':''} onClick={()=>changeKind('goal')}>完成一个目标</button><button type="button" disabled={busy} className={kind==='habit'?'active':''} onClick={()=>changeKind('habit')}>培养一个习惯</button></div>
      <label>你想做什么？<textarea required minLength={2} maxLength={500} value={goal} onChange={e=>setGoal(e.target.value)} placeholder={kind==='goal'?'例如：一个月内准备好产品经理面试':'例如：养成每天阅读的习惯'}/></label>
      <label>现在的情况 <small>选填</small><input maxLength={1000} value={background} onChange={e=>setBackground(e.target.value)} placeholder="已有的基础、遇到的困难…"/></label>
      <label>可以投入多少时间？<input required maxLength={200} value={availability} onChange={e=>setAvailability(e.target.value)} placeholder="例如：每天 20 分钟"/></label>
      <label>每次最多多少分钟？ <small>选填，填入后可自动核对数字</small><input type="number" min={1} max={720} step={1} value={budget} onChange={e=>setBudget(e.target.value)} placeholder="例如：20"/></label>
      {kind==='habit' && <label>每次核心行动要做满多少分钟？ <small>选填</small><input type="number" min={1} max={720} step={1} value={completion} onChange={e=>setCompletion(e.target.value)} placeholder="例如：读满10分钟"/></label>}
      <button className="primary" disabled={busy}>{busy?'园丁正在想一想…':'一起制定计划  →'}</button></form>
      {message && <div className="message" role="status">{message}</div>}
      {question && <form className="proposal" onSubmit={reply}><div className="eyebrow">园丁想再了解一点</div><p>{question}</p><label>你的回答<textarea value={answer} onChange={e=>setAnswer(e.target.value)} required maxLength={1000}/></label><button className="primary" disabled={busy}>继续对话</button></form>}
      {plan && <div className="proposal"><div className="eyebrow">一份给你的建议</div><label>计划名称<input value={plan.title} maxLength={100} onChange={e=>setPlan({...plan, title:e.target.value})}/></label><label>计划说明<textarea value={plan.summary} maxLength={500} onChange={e=>setPlan({...plan, summary:e.target.value})}/></label><label>全程行动步骤（每行一条）<textarea value={plan.steps.join("\n")} onChange={e=>setPlan({...plan, steps:e.target.value.split("\n")})}/></label><div className="check-summary"><strong>确认前检查</strong><p>每次可用时间：{constraints.session_budget_minutes === null ? '未填写，需自行核对' : `${constraints.session_budget_minutes} 分钟`}</p>{constraints.habit_completion_minutes !== null && <p>你填写的核心行动完成标准：{constraints.habit_completion_minutes} 分钟</p>}{plan.session_actions?.length ? <div><p>第1次行动示例，合计：{plan.session_actions.reduce((sum,a)=>sum+(a.minutes ?? 0),0)} 分钟</p>{plan.session_actions.map((action,i)=><div className="action-row" key={i}><input aria-label={`第${i+1}项动作`} value={action.text} onChange={e=>setPlan({...plan,session_actions:plan.session_actions!.map((a,n)=>n===i?{...a,text:e.target.value}:a)})}/><input aria-label={`第${i+1}项分钟`} type="number" min={1} max={720} value={action.minutes ?? ''} onChange={e=>setPlan({...plan,session_actions:plan.session_actions!.map((a,n)=>n===i?{...a,minutes:e.target.value === '' ? null : Number(e.target.value)}:a)})}/><span>{action.kind==='core'?'核心行动':'准备/其他'}</span></div>)}</div> : <p>没有可自动核对的单次时间安排，请自行核对。</p>}<p className="note">程序只核对上面的单次安排及部分常见表述，无法检查全部计划的时间或事实；请逐条确认。</p></div><button className="primary" onClick={confirm} disabled={busy}>确认计划，种下种子</button><p className="note">确认前，这只是一份建议。</p></div>}
    </section><section className="card garden"><div className="card-top"><span className="icon">❀</span><div><h2>我的小花园</h2><p>每一株，都有你的故事</p></div></div>
    {plants.length===0?<div className="empty"><div className="sprout">🌱</div><h3>这里还空着一小块土地</h3><p>和园丁聊聊，种下属于你的第一株植物吧。</p></div>:<div className="plant-list">{plants.map(plant=><PlantCard key={plant.id} plant={plant}/>)}</div>}
    </section></div><footer>慢一点也没关系，你正在生长。 ✿</footer></main>;
}
