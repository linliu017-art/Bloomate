'use client';
import {useRef,useState} from 'react';
import {AgentApiError,type ChangeDraft,type ChangeCandidate,type ChangeConfirmation} from './api';

const labels={select_task:'请选择步骤',need_date:'等待日期',ready:'等待确认',confirmed:'已修改',discarded:'已放弃',superseded:'已有新版',blocked:'暂不能修改'};
export function ChangeCard({value,busy,onChoose,onConfirm,onDiscard,onOpenPlant}:{value:ChangeDraft;busy:boolean;onChoose:(candidate:ChangeCandidate)=>void;onConfirm:(body:ChangeConfirmation)=>Promise<void>;onDiscard:()=>Promise<void>;onOpenPlant:(id:string)=>void}){
  const [date,setDate]=useState(value.after?.due_date||''),[steps,setSteps]=useState(value.after?.steps?.join('\n')||'');
  const [error,setError]=useState(''),[uncertain,setUncertain]=useState(false),[stale,setStale]=useState(false);
  const pending=useRef<ChangeConfirmation|null>(null),locked=useRef(false);
  async function confirm(){
    if(busy||locked.current)return;
    const proposed=steps.split('\n').map(s=>s.trim()).filter(Boolean);
    if(value.operation==='split'&&(!proposed.length||proposed.length>5)){setError('请保留1至5条小步骤，每行一条。');return;}
    const body=pending.current||{version:value.version,...(value.operation==='date'?{due_date:date||null}:{steps:proposed})};
    locked.current=true;pending.current=body;setError('');
    try{await onConfirm(body);pending.current=null;setUncertain(false);}
    catch(e){if(e instanceof AgentApiError&&['PLAN_CHANGED','CHANGE_STALE'].includes(e.code||''))setStale(true);const unknown=!(e instanceof AgentApiError)||e.status>=500||e.status===408;setUncertain(unknown);if(!unknown)pending.current=null;setError(e instanceof Error?e.message:'修改结果尚未确认，请按原内容重试。');}
    finally{locked.current=false;}
  }
  async function discard(){
    if(busy||locked.current)return;locked.current=true;setError('');
    try{await onDiscard();}catch(e){setError(e instanceof Error?e.message:'暂时无法放弃修改，请重新读取。');}finally{locked.current=false;}
  }
  return <article className="agent-plan-card agent-change-card" aria-label="目标修改预览">
    <div className="agent-plan-title"><strong>{value.operation==='date'?'修改日期':'拆小步骤'}</strong><span>{labels[value.state]}</span></div>
    {value.state==='select_task'&&<><div className="agent-task-choices">{value.choices.map(item=><button key={`${item.target.plant_id}-${item.target.stage_id}-${item.target.index}`} type="button" disabled={busy} onClick={()=>onChoose(item)}><small>{item.plant_title} · {item.stage_title} · 步骤{item.step_number}</small><strong>{item.text}</strong><span>{item.due_date||'待安排日期'} · 选择这一步 →</span></button>)}</div>{!!value.omitted&&<p className="agent-plan-note">其余步骤请打开具体植物后再问园丁。</p>}</>}
    {value.before&&<dl className="agent-plan-facts"><div><dt>目标 / 阶段</dt><dd>{value.before.plant_title} / {value.before.stage_title}</dd></div><div><dt>原步骤</dt><dd>{value.before.text}</dd></div><div><dt>原日期</dt><dd>{value.before.due_date||'待安排'}</dd></div></dl>}
    {value.state==='need_date'&&<p>直接在下方回复日期，或说“清空日期”。目标日期按北京时间。</p>}
    {stale&&<p role="status">原计划已经变化，请在下方重新描述要调整的步骤，生成最新预览。</p>}{value.state==='ready'&&!stale&&<form onSubmit={e=>{e.preventDefault();void confirm();}}><fieldset disabled={busy||uncertain}><legend className="sr-only">编辑修改建议</legend>{value.operation==='date'?<label>新日期（留空取消安排）<input type="date" value={date} onChange={e=>setDate(e.target.value)}/></label>:<label>替代小步骤（每行一条，最多5条）<textarea rows={6} maxLength={2500} value={steps} required onChange={e=>setSteps(e.target.value)}/></label>}</fieldset><p className="agent-plan-note">{value.operation==='date'?'确认后更新这一步的安排，其他步骤保持不变。':'第一条沿用原日期，新增小步骤进入待安排。已有完成记录和成长份额保留。'} 原计划已变化时，这份建议会被拒绝，需要重新预览。</p>{uncertain&&<p role="status">上次结果尚未收到，请按原内容重试；同一份修改不会重复执行。</p>}<div className="agent-plan-buttons"><button type="submit" disabled={busy}>{busy?'正在处理…':uncertain?'重试确认修改':'确认修改'}</button><button type="button" disabled={busy||uncertain} onClick={()=>void discard()}>放弃修改</button></div></form>}
    {value.state==='confirmed'&&<>{value.operation==='date'?<p>新日期：{value.after?.due_date||'已取消安排'}</p>:<ol>{value.after?.steps?.map((step,i)=><li key={i}>{step}</li>)}</ol>}<button type="button" onClick={()=>onOpenPlant(value.applied!.plant_id)}>打开修改后的目标 ↗</button></>}
    {['select_task','need_date','blocked'].includes(value.state)&&<button type="button" disabled={busy} onClick={()=>void discard()}>放弃修改</button>}
    {value.state==='superseded'&&<p>请查看后面的新版预览，这份建议不会再执行。</p>}
    {value.state==='discarded'&&<p>未采用这份修改，原计划保持不变。</p>}
    {error&&<p className="agent-error" role="alert">{error}</p>}
  </article>;
}
