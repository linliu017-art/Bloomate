'use client';
import Image from 'next/image';
import {PlanCard} from './PlanCard';
import {ChangeCard} from './ChangeCard';
import {RecordCard} from './RecordCard';
import {useCallback,useEffect,useRef,useState} from 'react';
import {apiFetch} from '../auth/api';
import {useAuth} from '../auth/AuthBoundary';
import {AgentApiError,createAgentApi,groupNames,type RecordDraft,type RecordConfirmation,type ChangeDraft,type ChangeConfirmation,type PlanDraft,type PlanConfirmation,type AgentContext,type AgentResult,type AgentSession,type AgentTurn,type TurnRequest} from './api';

const api=createAgentApi(apiFetch);
const groupLabels={goals_today:'今日目标行动',habits_today:'今日习惯',goals_overdue:'逾期未完成',goals_unscheduled:'待安排日期',habits_backfill:'可补签习惯'};
const terminal=(turn:AgentTurn)=>turn.status==='done'||turn.status==='error';

function Result({result,onOpenPlant}:{result:AgentResult;onOpenPlant:(id:string)=>void}){
  return <div className="agent-result"><p>{result.text}</p>{result.kind==='today'&&groupNames.map(group=>{
    const rows=result.data[group]||[];const count=result.data.counts?.[group]||0;
    return <details key={group} open={group==='goals_today'||group==='habits_today'}><summary>{groupLabels[group]} · {count}</summary>{rows.length?<ul>{rows.map((item,index)=><li key={`${item.plant_id}-${index}`}><span className="agent-item-state">{item.completed?'已完成':'待做'}</span><div><strong>{item.text||item.completion_standard}</strong><small>{item.plant_title}{(item.date||item.due_date)?` · ${item.date||item.due_date}`:''}</small><button type="button" onClick={()=>onOpenPlant(item.plant_id)}>打开原植物 ↗</button></div></li>)}</ul>:<p className="agent-empty">没有这一类行动。</p>}{count>rows.length&&<p className="agent-empty">其余{count-rows.length}项请到今日行动查看。</p>}</details>;
  })}{result.kind==='plant'&&<><ul>{result.data.steps?.map((step,index)=><li key={index}><span className="agent-item-state">{step.completed?'已完成':'待做'}</span><span>{step.text}</span></li>)}</ul><button type="button" onClick={()=>onOpenPlant(result.data.plant_id!)}>打开植物管理 ↗</button></>}<small className="agent-query-time">查询于{new Date(result.queried_at*1000).toLocaleString('zh-CN')} · 这是当时的记录</small></div>;
}

export function Gardener({context,onOpenPlant}:{context:AgentContext;onOpenPlant:(id:string)=>void}){
  const {session}=useAuth();
  const [open,setOpen]=useState(false),[collapsed,setCollapsed]=useState(false);
  const [sessions,setSessions]=useState<AgentSession[]>([]),[sessionId,setSessionId]=useState<string|null>(null);
  const [turns,setTurns]=useState<AgentTurn[]>([]),[draft,setDraft]=useState(''),[busy,setBusy]=useState(false),[loading,setLoading]=useState(false);
  const [error,setError]=useState(''),[stale,setStale]=useState(false),[waitingId,setWaitingId]=useState<string|null>(null);
  const [retry,setRetry]=useState(false);
  const dialog=useRef<HTMLDialogElement>(null),launcher=useRef<HTMLButtonElement>(null),input=useRef<HTMLTextAreaElement>(null),log=useRef<HTMLDivElement>(null);
  const epoch=useRef(0),pending=useRef<TurnRequest|null>(null),sending=useRef(false),nearBottom=useRef(true);
  const invalidate=useCallback(()=>{epoch.current++;},[]);
  const valid=(version:number)=>epoch.current===version;
  function upsert(turn:AgentTurn){setTurns(previous=>previous.some(t=>t.id===turn.id)?previous.map(t=>t.id===turn.id?turn:t):[...previous,turn]);}
  async function poll(id:string,version:number){
    setWaitingId(id);
    for(let attempt=0;attempt<75;attempt++){
      if(!valid(version))return;
      if(document.hidden){await new Promise(resolve=>setTimeout(resolve,2000));continue;}
      const turn=await api.turn(id);if(!valid(version))return;upsert(turn);
      if(terminal(turn)){setWaitingId(null);if(turn.status==='done')setStale(false);return;}
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    if(valid(version))setError('查询仍可能在处理，可以稍后重新读取结果。');
  }
  async function load(id:string|null,version:number){
    setLoading(true);setError('');
    try{
      const list=await api.sessions();if(!valid(version))return;setSessions(list);
      const selected=id&&list.some(s=>s.id===id)?id:list[0]?.id||null;
      setSessionId(selected);const rows=selected?await api.history(selected):[];if(!valid(version))return;setTurns(rows);pending.current=null;setRetry(false);
      const running=rows.find(t=>!terminal(t));
      setWaitingId(running?.id||null);
      if(running){setBusy(true);await poll(running.id,version);}
    }catch(e){if(valid(version))setError(e instanceof Error?e.message:'暂时无法读取对话。');}
    finally{if(valid(version)){setLoading(false);setBusy(false);sending.current=false;}}
  }
  const latestLoad=useRef(load),latestSession=useRef(sessionId);
  useEffect(()=>{latestLoad.current=load;latestSession.current=sessionId;});
  useEffect(()=>{
    if(open){dialog.current?.showModal();const version=++epoch.current;void latestLoad.current(latestSession.current,version);input.current?.focus();}
    else{invalidate();dialog.current?.close();sending.current=false;setBusy(false);setLoading(false);}
    return invalidate;
  },[open,session.cache_scope,invalidate]);
  useEffect(()=>{if(open&&nearBottom.current)log.current?.scrollTo({top:log.current.scrollHeight,behavior:'smooth'});},[turns,open]);
  useEffect(()=>{const changed=()=>setStale(true);window.addEventListener('bloomate-progress',changed);return()=>window.removeEventListener('bloomate-progress',changed);},[]);
  function close(){setOpen(false);requestAnimationFrame(()=>launcher.current?.focus());}
  async function send(text:string,repeat=false,override?:AgentContext){
    if(sending.current||!text.trim()||waitingId&&!repeat)return;
    sending.current=true;setBusy(true);setError('');nearBottom.current=true;const version=epoch.current;
    const request=repeat&&pending.current?pending.current:{request_id:crypto.randomUUID(),text:text.trim(),session_id:sessionId,context:{...(override||context)}};
    pending.current=request;
    try{
      const turn=await api.create(request);if(!valid(version))return;
      setSessionId(turn.session_id);upsert(turn);setDraft('');pending.current=null;setRetry(false);
      if(!terminal(turn))await poll(turn.id,version);else setWaitingId(null);
      const rows=await api.history(turn.session_id);if(valid(version))setTurns(rows);
      const list=await api.sessions();if(valid(version))setSessions(list);
    }catch(e){if(valid(version)){if(e instanceof AgentApiError&&e.status<500&&e.status!==408){pending.current=null;if(e.status===404)setWaitingId(null);}setError(e instanceof Error?e.message:'查询未完成。');setRetry(!!pending.current);}}
    finally{if(valid(version)){setBusy(false);sending.current=false;}}
  }
  function resultVersion(id:string){const value=turns.find(t=>t.id===id)?.result;return value&&(value.kind==='plan'||value.kind==='change'||value.kind==='record')?value.data.version:'';}
  async function resolveDraft(id:string,body:PlanConfirmation|null){
    if(sending.current||busy)throw new Error('园丁正在处理上一项，请稍后再操作。');
    const version=epoch.current;sending.current=true;setBusy(true);setError('');
    try{
      if(body){await api.confirm(id,body);if(!valid(version))return;window.dispatchEvent(new Event('bloomate-progress'));}
      else{await api.discard(id,resultVersion(id));if(!valid(version))return;}
      await load(sessionId,version);
    }finally{if(valid(version)){setBusy(false);sending.current=false;}}
  }
  async function resolveChange(id:string,body:ChangeConfirmation|null){
    if(sending.current||busy)throw new Error('园丁正在处理上一项，请稍后操作。');
    const version=epoch.current;sending.current=true;setBusy(true);setError('');
    try{
      if(body){await api.changeConfirm(id,body);if(!valid(version))return;window.dispatchEvent(new Event('bloomate-progress'));}
      else{await api.changeDiscard(id,resultVersion(id));if(!valid(version))return;}
      await load(sessionId,version);
    }finally{if(valid(version)){setBusy(false);sending.current=false;}}
  }
  async function resolveRecord(id:string,body:RecordConfirmation|null){
    if(sending.current||busy)throw new Error('园丁正在处理上一项，请稍后操作。');
    const version=epoch.current;sending.current=true;setBusy(true);setError('');
    try{
      if(body){await api.recordConfirm(id,body);if(!valid(version))return;window.dispatchEvent(new Event('bloomate-progress'));}
      else{await api.recordDiscard(id,resultVersion(id));if(!valid(version))return;}
      await load(sessionId,version);
    }finally{if(valid(version)){setBusy(false);sending.current=false;}}
  }
  async function restore(){const version=epoch.current;setBusy(true);setError('');sending.current=true;try{if(waitingId)await poll(waitingId,version);else await load(sessionId,version);}catch(e){if(valid(version))setError(e instanceof Error?e.message:'暂时无法读取。');}finally{if(valid(version)){setBusy(false);sending.current=false;}}}
  async function remove(){if(!sessionId||busy||!window.confirm('删除这段园丁对话？对应记录将清除，已确认计划和打卡不受影响。'))return;setBusy(true);const version=epoch.current;try{await api.remove(sessionId);if(!valid(version))return;setSessionId(null);setTurns([]);setWaitingId(null);pending.current=null;setRetry(false);setSessions(await api.sessions());}catch(e){if(valid(version))setError(e instanceof Error?e.message:'删除未完成。');}finally{if(valid(version))setBusy(false);}}
  function newConversation(){if(busy)return;setSessionId(null);setTurns([]);setWaitingId(null);pending.current=null;setRetry(false);setError('');setDraft('');input.current?.focus();}
  return <><div className={'gardener-launcher'+(collapsed?' is-collapsed':'')}><button className="gardener-minimize" type="button" aria-label={collapsed?'展开园丁形象':'收起园丁形象'} onClick={()=>setCollapsed(!collapsed)}>{collapsed?'+':'−'}</button><button type="button" className="gardener-open" ref={launcher} aria-label="和园丁聊聊" onClick={()=>setOpen(true)}>{!collapsed&&<Image src="/gardener/companion.png" width={92} height={120} alt="抱着小盆栽的园丁"/>}<span>找园丁聊聊</span></button></div>
    <dialog className="agent-dialog" ref={dialog} aria-labelledby="agent-heading" onCancel={event=>{event.preventDefault();close();}}>
      <div className="agent-shell"><header className="agent-heading"><Image src="/gardener/companion.png" width={43} height={57} alt=""/><div><h2 id="agent-heading">你的园丁</h2><p>把想做的事，变成下一步行动。</p></div><button type="button" aria-label="关闭园丁对话" onClick={close}>×</button></header>
      <div className="agent-toolbar"><label className="sr-only" htmlFor="agent-history">选择园丁对话</label><select id="agent-history" value={sessionId||''} disabled={busy||loading} onChange={e=>{if(!e.target.value)newConversation();else void load(e.target.value,++epoch.current);}}><option value="">新对话</option>{sessions.map(s=><option key={s.id} value={s.id}>{new Date(s.created_at*1000).toLocaleString('zh-CN')}</option>)}</select><button type="button" disabled={busy||loading} onClick={newConversation}>新对话</button><button type="button" disabled={!sessionId||busy||loading} onClick={()=>void remove()}>删除对话</button></div>
      <p className="agent-scope">{context.plant_id?'当前已选中花园里的植物。':'当前查看：'+({today:'今日行动',plan:'制定计划',garden:'我的花园'}[context.page])} 可以查计划、调整步骤，也能核对完成与打卡。</p>
      <div className="agent-log" ref={log} role="log" aria-label="园丁对话记录" aria-live="polite" onScroll={()=>{if(log.current)nearBottom.current=log.current.scrollHeight-log.current.scrollTop-log.current.clientHeight<70;}}>
      {loading&&<p role="status">正在读取对话…</p>}{!loading&&!turns.length&&<div className="agent-welcome"><span aria-hidden="true">✿</span><h3>今天想从哪里开始？</h3><p>告诉我想完成什么或想养成什么习惯，我会帮你拆成草案。也可以查询行动、把目标步骤改期，或一起把困难步骤拆小，再记录实际完成的行动。</p></div>}
      {turns.map(turn=><section className="agent-turn" key={turn.id}><p className="agent-user">{turn.text}</p>{turn.result?turn.result.kind==='record'?<><p>{turn.result.text}</p><RecordCard key={turn.result.data.version} value={turn.result.data as RecordDraft} busy={busy||loading} onChoose={item=>void send('选择了「'+item.text+'」',false,{page:context.page,plant_id:item.target.plant_id,record_choice:{turn_id:turn.id,version:resultVersion(turn.id),target:item.target}})} onConfirm={body=>resolveRecord(turn.id,body)} onDiscard={()=>resolveRecord(turn.id,null)} onOpenPlant={id=>{close();onOpenPlant(id);}}/></>:turn.result.kind==='change'?<><p>{turn.result.text}</p><ChangeCard key={turn.result.data.version} value={turn.result.data as ChangeDraft} busy={busy||loading} onChoose={item=>void send('选择了「'+item.text+'」',false,{page:context.page,plant_id:item.target.plant_id,change_choice:{turn_id:turn.id,version:resultVersion(turn.id),target:item.target}})} onConfirm={body=>resolveChange(turn.id,body)} onDiscard={()=>resolveChange(turn.id,null)} onOpenPlant={id=>{close();onOpenPlant(id);}}/></>:turn.result.kind==='plan'?<><p>{turn.result.text}</p><PlanCard key={turn.result.data.version} draft={turn.result.data as PlanDraft} busy={busy||loading} onConfirm={body=>resolveDraft(turn.id,body)} onDiscard={()=>resolveDraft(turn.id,null)} onOpenPlant={id=>{close();onOpenPlant(id);}}/></>:<Result result={turn.result} onOpenPlant={id=>{close();onOpenPlant(id);}}/>:turn.error?<p className="agent-error" role="status">{turn.error.message}</p>:<p role="status">{turn.status==='pending'?'请求已受理，等待园丁开始…':'园丁正在理解条件或生成结果…'}</p>}</section>)}
      </div>
      <div className="agent-compose">{stale&&turns.length>0&&<p className="agent-refresh-note">行动记录已有更新，重新查询可看到最新进度。</p>}{error&&<p className="agent-error" role="alert">{error}</p>}<div className="agent-recovery">{busy&&<button type="button" onClick={()=>{epoch.current++;sending.current=false;setBusy(false);setLoading(false);setError('已停止等待，后台查询未必停止；可重新读取结果。');}}>停止等待</button>}{!busy&&waitingId&&<button type="button" onClick={()=>void restore()}>重新读取结果</button>}{!busy&&retry&&<button type="button" onClick={()=>void send(pending.current?.text||draft,true)}>重试这次提交</button>}{!busy&&error&&!waitingId&&!retry&&<button type="button" onClick={()=>void restore()}>重新读取对话</button>}</div>
      <div className="agent-prompts"><button type="button" disabled={busy||loading||!!waitingId||retry} onClick={()=>{setDraft('我已经完成了：');input.current?.focus();}}>记录完成</button><button type="button" disabled={busy||loading||!!waitingId||retry} onClick={()=>void send('撤销刚才那次记录')}>撤销刚才</button><button type="button" disabled={busy||loading||!!waitingId||retry} onClick={()=>{setDraft('我想制定一个新目标：');input.current?.focus();}}>制定新目标</button><button type="button" disabled={busy||loading||!!waitingId||retry} onClick={()=>{setDraft('我想养成一个习惯：');input.current?.focus();}}>养成新习惯</button><button type="button" disabled={busy||loading||!!waitingId||retry} onClick={()=>void send('今天要做什么')}>今天要做什么</button><button type="button" disabled={busy||loading||!!waitingId||retry||!context.plant_id} onClick={()=>void send('这株植物进展怎样')}>这株植物的进展</button></div>
      <form onSubmit={e=>{e.preventDefault();void send(draft);}}><label className="sr-only" htmlFor="agent-input">告诉园丁你想做什么</label><textarea id="agent-input" ref={input} maxLength={500} rows={2} placeholder="例如：我想3天内整理一份分享提纲，每次10分钟…" value={draft} disabled={busy||loading||retry||!!waitingId} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void send(draft);}}}/><button type="submit" disabled={busy||loading||retry||!!waitingId||!draft.trim()}>{busy?'处理中…':'发送'}</button></form><p className="agent-privacy">对话保留30天，可提前删除；游客仍保留7天。输入及必要上下文会交由模型处理，请勿填写敏感信息。长期偏好记忆仍默认关闭。</p></div></div>
    </dialog></>;
}
