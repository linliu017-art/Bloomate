'use client';
import {useRef,useState} from 'react';
import {AgentApiError,type RecordDraft,type RecordCandidate,type RecordConfirmation} from './api';

const states={select_task:'请选择行动',need_date:'请补充日期',clarify_report:'请明确实际情况',ready:'等待核对',confirmed:'已执行',already_recorded:'无需重复操作',discarded:'已放弃',superseded:'已有新版',blocked:'暂不能操作',open_original:'到原植物继续'};
export function RecordCard({value,busy,onChoose,onConfirm,onDiscard,onOpenPlant}:{value:RecordDraft;busy:boolean;onChoose:(item:RecordCandidate)=>void;onConfirm:(body:RecordConfirmation)=>Promise<void>;onDiscard:()=>Promise<void>;onOpenPlant:(id:string)=>void}){
  const [error,setError]=useState(''),[uncertain,setUncertain]=useState(false),[stale,setStale]=useState(false);
  const locked=useRef(false),pending=useRef<RecordConfirmation|null>(null);
  const before=value.before;
  const title={complete:'记录完成',undo:'撤销完成',pause:'暂停植物',resume:'恢复植物',end:before?.target.kind==='goal'?'完成整个目标':'结束这段习惯',manage:'周期与下一阶段'}[value.operation];
  async function confirm(){
    if(busy||locked.current)return;locked.current=true;setError('');
    const body=pending.current||{version:value.version,performed:value.operation==='complete'};pending.current=body;
    try{await onConfirm(body);pending.current=null;setUncertain(false);}
    catch(e){if(e instanceof AgentApiError&&['PLAN_CHANGED','RECORD_STALE'].includes(e.code||''))setStale(true);const unknown=!(e instanceof AgentApiError)||e.status>=500||e.status===408;setUncertain(unknown);if(!unknown)pending.current=null;setError(e instanceof Error?e.message:'记录结果尚未确认，请按原内容重试。');}
    finally{locked.current=false;}
  }
  async function discard(){if(busy||locked.current)return;locked.current=true;setError('');try{await onDiscard();}catch(e){setError(e instanceof Error?e.message:'暂时无法放弃，请重新读取。');}finally{locked.current=false;}}
  return <article className="agent-plan-card agent-record-card" aria-label="行动记录核对">
    <div className="agent-plan-title"><strong>{title}</strong><span>{states[value.state]}</span></div>
    {value.state==='select_task'&&<div className="agent-task-choices">{value.choices.map(item=><button type="button" disabled={busy} key={`${item.target.plant_id}-${item.target.stage_id}-${item.target.index}-${item.target.action_date}`} onClick={()=>onChoose(item)}><small>{item.target.kind==='goal'?'目标':'习惯'} · {item.plant_title} · {item.stage_title}</small><strong>{item.text}</strong><span>{item.target.action_date|| (item.step_number?`步骤${item.step_number}`:'植物管理')} · {item.completed===null?'核对条件':item.completed?'已记录完成':'未记录完成'} · 选择这项 →</span></button>)}</div>}
    {!!value.omitted&&<p className="agent-plan-note">其余行动请打开具体植物后再问。</p>}
    {before&&<dl className="agent-plan-facts"><div><dt>植物 / 阶段</dt><dd>{before.plant_title} / {before.stage_title}</dd></div><div><dt>行动 / 标准</dt><dd>{before.text}</dd></div>{before.target.action_date&&<div><dt>记录日期</dt><dd>{before.target.action_date} · {before.timezone}</dd></div>}{before.completed!==null&&<div><dt>原状态</dt><dd>{before.completed?'已记录完成':'未记录完成'}</dd></div>}{['pause','resume'].includes(value.operation)&&<div><dt>原状态</dt><dd>{before.paused?'已暂停':'进行中'}</dd></div>}</dl>}
    {value.state==='need_date'&&<p>在下方告诉园丁具体日期，例如“昨天确实完成了”。</p>}
    {value.state==='clarify_report'&&<p>请直接说明哪项行动确实完成了，或告诉园丁还没有完成。这里没有新增记录。</p>}
    {stale&&<p role="status">实际记录已经变化，请在下方重新描述，按最新状态核对。</p>}
    {value.state==='ready'&&!stale&&<><p className="agent-plan-note">{value.operation==='complete'?'点击下方按钮，表示你确认这项行动在上述日期或这一步确实已完成。':value.operation==='undo'?'撤销完成状态，既有收藏仍按更正规则保留，不会重新发放成果。':value.operation==='resume'?'恢复后仍按原计划日继续；已经过去的暂停日不会变成可新增打卡的日期。':'请核对对象后执行，原页面会同步更新。'}</p>{uncertain&&<p role="status">上次结果尚未收到，请按原内容重试；同一操作不会重复执行。</p>}<div className="agent-plan-buttons"><button type="button" disabled={busy} onClick={()=>void confirm()}>{busy?'正在处理…':uncertain?'重试本次记录':value.operation==='complete'&&before?.target.kind==='habit'?'确认当天达标并记录':title}</button><button type="button" disabled={busy||uncertain} onClick={()=>void discard()}>放弃本次操作</button></div></>}
    {value.state==='confirmed'&&value.applied&&<><p>{value.applied.operation==='complete'?'已记录完成。':value.applied.operation==='undo'?'已撤销这次完成状态。':value.applied.operation==='pause'?'已暂停。':value.applied.operation==='resume'?'已恢复。':'已确认结束，成果仍保留。'}{value.applied.stage_completed?' 当前阶段已达标，成果已按原规则保存。':''}</p><small className="agent-query-time">操作于{new Date(value.applied.recorded_at*1000).toLocaleString('zh-CN')}，后续变化以原页面为准。</small><button type="button" onClick={()=>onOpenPlant(value.applied!.plant_id)}>{value.applied.stage_completed?'查看成果与下一步 ↗':'打开原植物 ↗'}</button></>}
    {value.state==='open_original'&&before&&<button type="button" onClick={()=>onOpenPlant(before.target.plant_id)}>打开周期与阶段管理 ↗</button>}
    {value.state==='superseded'&&<p>请使用后面的新版核对卡，这一份不会再执行。</p>}
    {value.state==='discarded'&&<p>未执行这次待办，原记录保持不变。</p>}
    {['select_task','need_date','clarify_report','blocked','open_original','already_recorded'].includes(value.state)&&<button type="button" disabled={busy} onClick={()=>void discard()}>放弃本次操作</button>}
    {error&&<p className="agent-error" role="alert">{error}</p>}
  </article>;
}
