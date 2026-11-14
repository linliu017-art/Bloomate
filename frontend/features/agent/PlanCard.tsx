'use client';
import {useRef,useState} from 'react';
import {AgentApiError,type PlanDraft,type PlanConfirmation} from './api';

export function PlanCard({draft,busy,onConfirm,onDiscard,onOpenPlant}:{draft:PlanDraft;busy:boolean;onConfirm:(body:PlanConfirmation)=>Promise<void>;onDiscard:()=>Promise<void>;onOpenPlant:(id:string)=>void}){
  const [plan,setPlan]=useState(draft.proposal);
  const [dates,setDates]=useState(draft.goal_dates);
  const [error,setError]=useState(''),[uncertain,setUncertain]=useState(false);
  const pending=useRef<PlanConfirmation|null>(null),locked=useRef(false);
  const fields=draft.fields;
  const kind=fields.kind==='goal'?'目标':'习惯';
  async function confirm(){
    if(!plan||locked.current||busy)return;
    locked.current=true;setError('');
    const body=pending.current||{version:draft.version,plan,goal_dates:dates};pending.current=body;
    try{await onConfirm(body);pending.current=null;setUncertain(false);}
    catch(e){const unknown=!(e instanceof AgentApiError)||e.status>=500||e.status===408;setUncertain(unknown);if(!unknown)pending.current=null;setError(e instanceof Error?e.message:'种植结果尚未确认，请按原内容重试。');}
    finally{locked.current=false;}
  }
  async function discard(){
    if(locked.current||busy)return;locked.current=true;setError('');
    try{await onDiscard();}catch(e){setError(e instanceof Error?e.message:'暂时无法放弃草案，请重新读取。');}finally{locked.current=false;}
  }
  return <article className="agent-plan-card" aria-label={`${kind}草案`}>
    <div className="agent-plan-title"><strong>{kind}草案</strong><span>{{collecting:'等待补充',ready:'等待确认',confirmed:'已种植',discarded:'已放弃',superseded:'已有新版'}[draft.state]}</span></div>
    <dl className="agent-plan-facts"><div><dt>想做的事</dt><dd>{fields.goal||'待补充'}</dd></div>{fields.background&&<div><dt>背景</dt><dd>{fields.background}</dd></div>}<div><dt>可用时间</dt><dd>{fields.daily_minutes?`${fields.daily?'每天':'每次'}最多${fields.daily_minutes}分钟`:'时间不确定'}{fields.duration_days?` · ${fields.duration_days}天`:''}</dd></div>
    {fields.kind==='habit'&&<><div><dt>周期</dt><dd>{fields.start_date||'待补充'} 至 {fields.end_date||'待补充'} · 北京时间</dd></div><div><dt>计划日</dt><dd>{fields.weekdays?.map(n=>'周'+'一二三四五六日'[n]).join('、')||'待补充'}</dd></div><div><dt>完成标准</dt><dd>{fields.completion_standard||'待补充'}</dd></div><div><dt>阶段达标</dt><dd>{fields.growth_threshold?`累计完成${fields.growth_threshold}次`:'待补充'}</dd></div></>}
    </dl>
    {draft.suggestions.length>0&&<p className="agent-plan-note">部分缺失条件按你接受建议的请求补全，请核对上方明细后再确认。</p>}
    {draft.state==='collecting'&&<p>直接在下方回复园丁，继续补充条件。</p>}
    {plan&&draft.state==='ready'&&<form onSubmit={e=>{e.preventDefault();void confirm();}}>
      <fieldset disabled={busy||uncertain}><legend className="sr-only">编辑{kind}草案</legend>
      <label>计划名称<input value={plan.title} maxLength={100} required onChange={e=>setPlan({...plan,title:e.target.value})}/></label>
      <label>计划说明<textarea value={plan.summary} rows={3} maxLength={500} required onChange={e=>setPlan({...plan,summary:e.target.value})}/></label>
      <div className="agent-plan-steps">{plan.steps.map((step,i)=><div key={i}><label>步骤{i+1}<textarea rows={2} value={step} maxLength={500} required onChange={e=>setPlan({...plan,steps:plan.steps.map((s,j)=>j===i?e.target.value:s)})}/></label>{fields.kind==='goal'&&<label>建议日期<input type="date" value={dates[i]||''} onChange={e=>setDates(dates.map((d,j)=>j===i?e.target.value||null:d))}/></label>}</div>)}</div>
      </fieldset>
      {!!plan.session_actions?.length&&<details><summary>单次时间安排</summary><ul>{plan.session_actions.map((a,i)=><li key={i}>{a.text} · {a.minutes}分钟</li>)}</ul></details>}
      <p className="agent-plan-note">{fields.kind==='goal'?'日期是可修改的安排建议，留空后在今日行动的待安排中查看。':'按上方周期和标准生成打卡日，现有打卡与成长规则继续适用。'} 时间条件需要调整时，直接告诉园丁生成新版草案。</p>
      {uncertain&&<p role="status">上次确认结果尚未收到，请按原内容重试；同一草案不会重复种植。</p>}
      <div className="agent-plan-buttons"><button type="submit" disabled={busy}>{busy?'正在处理…':uncertain?'重试确认种植':'确认种植'}</button><button type="button" disabled={busy||uncertain} onClick={()=>void discard()}>放弃草案</button></div>
    </form>}
    {draft.state==='collecting'&&<button type="button" disabled={busy} onClick={()=>void discard()}>放弃草案</button>}
    {draft.state==='confirmed'&&draft.plant_id&&<button type="button" onClick={()=>onOpenPlant(draft.plant_id!)}>打开已种植的{kind} ↗</button>}
    {draft.state==='superseded'&&<p>请使用后面的新版草案，这一版不会再种植。</p>}
    {error&&<p className="agent-error" role="alert">{error}</p>}
  </article>;
}
