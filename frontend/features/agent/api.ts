import type {Plan} from '../planning/api';
export type QueryItem = {plant_id:string;plant_title:string;completed:boolean;text?:string;completion_standard?:string;date?:string;due_date?:string|null};
export const groupNames = ['goals_today','habits_today','goals_overdue','goals_unscheduled','habits_backfill'] as const;
export type QueryGroup = typeof groupNames[number];
type ResultBase = {text:string;queried_at:number};
type QueryData = {date?:string;counts?:Record<QueryGroup,number>;plant_id?:string;title?:string;steps?:{text:string;completed:boolean}[]} & Partial<Record<QueryGroup,QueryItem[]>>;
export type AgentResult = ResultBase & ({kind:'today'|'plant'|'clarify'|'unsupported';data:QueryData}|{kind:'plan';data:PlanDraft}|{kind:'change';data:ChangeDraft}|{kind:'record';data:RecordDraft});
export type DraftFields = {kind:'goal'|'habit';goal:string;background:string;duration_days:number|null;daily_minutes:number|null;daily?:boolean;start_date:string|null;end_date:string|null;weekdays:number[]|null;completion_standard:string|null;growth_threshold:number|null;flexible_time:boolean;use_suggestions:boolean};
export type PlanDraft = {state:'collecting'|'ready'|'confirmed'|'discarded'|'superseded';version:string;fields:DraftFields;proposal:Plan|null;question:string|null;goal_dates:(string|null)[];suggestions:string[];plant_id?:string};
export type PlanConfirmation = {version:string;plan:Plan;goal_dates:(string|null)[]};
export type ActionRef = {plant_id:string;stage_id:string|null;index:number;base_hash:string};
export type ChangeCandidate = {target:ActionRef;plant_title:string;stage_title:string;step_number:number;text:string;due_date:string|null;blocked_reason:string|null};
export type ChangeChoice = {turn_id:string;version:string;target:ActionRef};
export type ChangeConfirmation = {version:string;due_date?:string|null;steps?:string[]};
export type ChangeDraft = {state:'select_task'|'need_date'|'ready'|'confirmed'|'discarded'|'superseded'|'blocked';version:string;operation:'date'|'split';request:Record<string,unknown>;choices:ChangeCandidate[];omitted?:number;target?:ActionRef;before?:ChangeCandidate;after?:{due_date?:string|null;steps?:string[]};applied?:{status:'confirmed';plant_id:string;operation:'date'|'split';result:Record<string,unknown>}};
export type RecordRef = {plant_id:string;kind:'goal'|'habit';stage_id:string|null;index:number|null;action_date:string|null;base_hash:string};
export type RecordCandidate = {target:RecordRef;plant_title:string;stage_title:string;text:string;completed:boolean|null;paused:boolean;ended:boolean;timezone:string;archived:boolean;blocked_reason:string|null;step_number?:number};
export type RecordChoice = {turn_id:string;version:string;target:RecordRef};
export type RecordConfirmation = {version:string;performed:boolean};
export type RecordApplied = {status:'confirmed';operation:'complete'|'undo'|'pause'|'resume'|'end'|'manage';plant_id:string;kind:'goal'|'habit';completed:boolean|null;action_date:string|null;paused:boolean;ended:boolean;stage_completed:boolean;history_preserved:boolean;recorded_at:number};
export type RecordDraft = {state:'select_task'|'need_date'|'clarify_report'|'ready'|'confirmed'|'already_recorded'|'discarded'|'superseded'|'blocked'|'open_original';version:string;operation:RecordApplied['operation'];request:Record<string,unknown>;anchor_at:number;choices:RecordCandidate[];omitted?:number;before?:RecordCandidate|null;target?:RecordRef;after_ref?:RecordRef;desired_completed?:boolean|null;applied?:RecordApplied};
export type AgentTurn = {id:string;session_id:string;status:'pending'|'running'|'done'|'error';text:string;created_at:number;expires_at:number;result:AgentResult|null;error:{code:string;message:string}|null};
export type AgentContext = {page:'today'|'plan'|'garden';plant_id:string|null;change_choice?:ChangeChoice;record_choice?:RecordChoice};
export type TurnRequest = {request_id:string;session_id:string|null;text:string;context:AgentContext};
export type AgentSession = {id:string;created_at:number;updated_at:number};
const object=(v:unknown):v is Record<string,unknown>=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const positive=(v:unknown):v is number=>Number.isSafeInteger(v)&&Number(v)>0;
const text=(v:unknown):v is string=>typeof v==='string'&&v.length>0;
const bad=()=>new Error('园丁回复暂时无法读取，请重新查询。');
export class AgentApiError extends Error {
  status:number;
  code?:string;
  constructor(message:string,status:number,code?:string){super(message);this.status=status;this.code=code;}
}

export function readResult(value:unknown):AgentResult {
  if(!object(value)||!['today','plant','clarify','unsupported','plan','change','record'].includes(String(value.kind))||!text(value.text)||!positive(value.queried_at)||!object(value.data))throw bad();
  const data=value.data;
  if(value.kind==='today'){
    if(!text(data.date)||!object(data.counts))throw bad();
    for(const group of groupNames){
      const rows=data[group];
      if(!Number.isSafeInteger(data.counts[group])||Number(data.counts[group])<0||!Array.isArray(rows)||rows.length>40||Number(data.counts[group])<rows.length)throw bad();
      for(const row of rows)if(!object(row)||!text(row.plant_id)||!text(row.plant_title)||typeof row.completed!=='boolean'||
        (group.startsWith('goals_')?!text(row.text):!text(row.completion_standard))||
        (row.due_date!==undefined&&row.due_date!==null&&!text(row.due_date))||
        (row.date!==undefined&&!text(row.date)))throw bad();
    }
  }
  if(value.kind==='plant'){
    if(!text(data.plant_id)||!text(data.title))throw bad();
    if(data.steps!==undefined&&(!Array.isArray(data.steps)||!data.steps.every(s=>object(s)&&text(s.text)&&typeof s.completed==='boolean')))throw bad();
  }
  if(value.kind==='plan')readDraft(data);
  if(value.kind==='change')readChange(data);
  if(value.kind==='record')readRecord(data);
  return value as AgentResult;
}

export function readDraft(value:unknown):PlanDraft {
  if(!object(value)||!['collecting','ready','confirmed','discarded','superseded'].includes(String(value.state))||!text(value.version)||!object(value.fields)||!Array.isArray(value.goal_dates)||!value.goal_dates.every(d=>d===null||typeof d==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(d))||!Array.isArray(value.suggestions)||!value.suggestions.every(s=>typeof s==='string'))throw bad();
  const f=value.fields;
  if(!['goal','habit'].includes(String(f.kind))||typeof f.goal!=='string'||typeof f.background!=='string'||typeof f.flexible_time!=='boolean'||typeof f.use_suggestions!=='boolean')throw bad();
  if(f.daily!==undefined&&typeof f.daily!=='boolean')throw bad();
  for(const key of ['duration_days','daily_minutes','growth_threshold'])if(f[key]!==null&&(!Number.isSafeInteger(f[key])||Number(f[key])<1))throw bad();
  for(const key of ['start_date','end_date','completion_standard'])if(f[key]!==null&&typeof f[key]!=='string')throw bad();
  if(f.weekdays!==null&&(!Array.isArray(f.weekdays)||f.weekdays.length===0||!f.weekdays.every(n=>Number.isInteger(n)&&Number(n)>=0&&Number(n)<=6)))throw bad();
  if(value.question!==null&&typeof value.question!=='string')throw bad();
  if(value.proposal!==null){
    const p=value.proposal;
    if(!object(p)||!text(p.title)||!text(p.summary)||!Array.isArray(p.steps)||!p.steps.length||p.steps.length>10||!p.steps.every(text))throw bad();
    if(p.session_actions!=null&&(!Array.isArray(p.session_actions)||!p.session_actions.every(a=>object(a)&&text(a.text)&&['core','other'].includes(String(a.kind))&&positive(a.minutes))))throw bad();
    if(p.flower_species!=null&&!['sunflower','daisy','tulip','cosmos','lavender','hydrangea'].includes(String(p.flower_species)))throw bad();
    if(p.growth_nodes!=null&&(!Array.isArray(p.growth_nodes)||p.growth_nodes.length!==4||!p.growth_nodes.every(positive)))throw bad();
  }
  if(['ready','confirmed'].includes(String(value.state))&&(value.proposal===null||f.kind==='goal'&&value.goal_dates.length!==(value.proposal as Plan).steps.length))throw bad();
  if(f.kind==='habit'&&value.goal_dates.length!==0)throw bad();
  if(value.state==='confirmed'&&!text(value.plant_id))throw bad();
  return value as PlanDraft;
}

const dateValue=(v:unknown)=>v===null||typeof v==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(v)&&Number.isFinite(Date.parse(v));
function reference(v:unknown):boolean{return object(v)&&text(v.plant_id)&&(v.stage_id===null||text(v.stage_id))&&Number.isSafeInteger(v.index)&&Number(v.index)>=0&&Number(v.index)<=99&&typeof v.base_hash==='string'&&/^[a-f0-9]{64}$/.test(v.base_hash);}
function candidate(v:unknown):boolean{return object(v)&&reference(v.target)&&text(v.plant_title)&&text(v.stage_title)&&positive(v.step_number)&&text(v.text)&&dateValue(v.due_date)&&(v.blocked_reason===null||text(v.blocked_reason));}
function readChangeApplied(value:unknown){
  if(!object(value)||value.status!=='confirmed'||!text(value.plant_id)||!['date','split'].includes(String(value.operation))||!object(value.result))throw bad();
  const r=value.result;
  if(value.operation==='date'&&(!Number.isSafeInteger(r.index)||Number(r.index)<0||!text(r.text)||typeof r.completed!=='boolean'||!dateValue(r.due_date)))throw bad();
  if(value.operation==='split'&&(r.status!=='confirmed'||!text(r.text)||!Array.isArray(r.steps)||!r.steps.length||r.steps.length>5||!r.steps.every(text)))throw bad();
  return value;
}
export function readChange(value:unknown):ChangeDraft {
  if(!object(value)||!['select_task','need_date','ready','confirmed','discarded','superseded','blocked'].includes(String(value.state))||!text(value.version)||!['date','split'].includes(String(value.operation))||!object(value.request)||value.request.operation!==value.operation||!Array.isArray(value.choices)||value.choices.length>40||!value.choices.every(candidate))throw bad();
  if(value.target!==undefined&&!reference(value.target)||value.before!==undefined&&!candidate(value.before))throw bad();
  if(value.omitted!==undefined&&(!Number.isSafeInteger(value.omitted)||Number(value.omitted)<0))throw bad();
  if(['ready','confirmed'].includes(String(value.state))){
    if(!reference(value.target)||!candidate(value.before)||!object(value.after))throw bad();
    if(value.operation==='date'&&!dateValue(value.after.due_date))throw bad();
    if(value.operation==='split'&&(!Array.isArray(value.after.steps)||!value.after.steps.length||value.after.steps.length>5||!value.after.steps.every(text)))throw bad();
  }
  if(value.state==='confirmed'){
    const applied=readChangeApplied(value.applied);
    if(applied.operation!==value.operation||applied.plant_id!==(value.target as ActionRef).plant_id)throw bad();
    const after=value.after as Record<string,unknown>;const actual=applied.result as Record<string,unknown>;
    if(value.operation==='date'?after.due_date!==actual.due_date:JSON.stringify(after.steps)!==JSON.stringify(actual.steps))throw bad();
  }
  return value as ChangeDraft;
}

const recordOps=['complete','undo','pause','resume','end','manage'];
function recordRef(v:unknown):boolean{return object(v)&&text(v.plant_id)&&['goal','habit'].includes(String(v.kind))&&(v.stage_id===null||text(v.stage_id))&&(v.index===null||Number.isSafeInteger(v.index)&&Number(v.index)>=0&&Number(v.index)<=99)&&dateValue(v.action_date)&&typeof v.base_hash==='string'&&/^[a-f0-9]{64}$/.test(v.base_hash);}
function recordCandidate(v:unknown):boolean{return object(v)&&recordRef(v.target)&&text(v.plant_title)&&text(v.stage_title)&&text(v.text)&&(v.completed===null||typeof v.completed==='boolean')&&typeof v.paused==='boolean'&&typeof v.ended==='boolean'&&text(v.timezone)&&typeof v.archived==='boolean'&&(v.blocked_reason===null||text(v.blocked_reason));}
export function readRecordApplied(v:unknown):RecordApplied{
 if(!object(v)||v.status!=='confirmed'||!recordOps.includes(String(v.operation))||!text(v.plant_id)||!['goal','habit'].includes(String(v.kind))||!dateValue(v.action_date)||typeof v.paused!=='boolean'||typeof v.ended!=='boolean'||typeof v.stage_completed!=='boolean'||typeof v.history_preserved!=='boolean'||!positive(v.recorded_at))throw bad();
 if(['complete','undo'].includes(String(v.operation))?typeof v.completed!=='boolean'||v.completed!==(v.operation==='complete'):v.completed!==null)throw bad();
 if(['complete','undo'].includes(String(v.operation))&&v.kind==='habit'&&v.action_date===null)throw bad();
 if(v.operation==='pause'&&v.paused!==true||v.operation==='resume'&&v.paused!==false||v.operation==='end'&&v.ended!==true||v.operation==='manage')throw bad();
 return v as RecordApplied;
}
export function readRecord(v:unknown):RecordDraft{
 if(!object(v)||!['select_task','need_date','clarify_report','ready','confirmed','already_recorded','discarded','superseded','blocked','open_original'].includes(String(v.state))||!text(v.version)||!recordOps.includes(String(v.operation))||!object(v.request)||v.request.operation!==v.operation||!positive(v.anchor_at)||!Array.isArray(v.choices)||v.choices.length>40||!v.choices.every(recordCandidate))throw bad();
 if(v.target!==undefined&&!recordRef(v.target)||v.before!=null&&!recordCandidate(v.before))throw bad();
 if(v.omitted!==undefined&&(!Number.isSafeInteger(v.omitted)||Number(v.omitted)<0))throw bad();
 if(v.state==='ready'||v.state==='confirmed'){
  if(!recordRef(v.target)||!recordCandidate(v.before))throw bad();
  const target=v.target as RecordRef;
  if((Object.keys(target) as (keyof RecordRef)[]).some(key=>(v.before as RecordCandidate).target[key]!==target[key]))throw bad();
  if(['complete','undo'].includes(String(v.operation))&&(target.kind==='goal'?target.index===null:target.action_date===null||target.stage_id===null))throw bad();
 }
 if(v.state==='ready'&&(['complete','undo'].includes(String(v.operation))?v.desired_completed!==(v.operation==='complete'):v.desired_completed!==null))throw bad();
 if(v.state==='confirmed'){
  const applied=readRecordApplied(v.applied);const target=v.target as RecordRef;
  if(applied.operation!==v.operation||applied.plant_id!==target.plant_id||applied.kind!==target.kind||applied.action_date!==target.action_date||!recordRef(v.after_ref))throw bad();
  const after=v.after_ref as RecordRef;
  if(['plant_id','kind','stage_id','index','action_date'].some(key=>after[key as keyof RecordRef]!==target[key as keyof RecordRef]))throw bad();
 }
 return v as RecordDraft;
}

export function readTurn(value:unknown):AgentTurn {
  if(!object(value)||!text(value.id)||!text(value.session_id)||!['pending','running','done','error'].includes(String(value.status))||!text(value.text)||!positive(value.created_at)||!positive(value.expires_at)||Number(value.expires_at)<=Number(value.created_at))throw bad();
  if(value.status==='done')readResult(value.result);
  else if(value.result!==null)throw bad();
  if(value.status==='error'){
    if(!object(value.error)||!text(value.error.code)||!text(value.error.message))throw bad();
  }else if(value.error!==null)throw bad();
  return value as AgentTurn;
}

export function createAgentApi(fetcher:typeof fetch){
  async function request(path:string,options?:RequestInit):Promise<unknown>{
    try{
      const response=await fetcher('/api/v1/agent'+path,options);
      let data:unknown;try{data=await response.json();}catch{throw new Error('园丁服务暂时无法连接，请稍后重新读取。');}
      if(!response.ok)throw new AgentApiError(object(data)&&object(data.error)&&text(data.error.message)?data.error.message:'园丁查询未完成，请重试。',response.status,object(data)&&object(data.error)&&typeof data.error.code==='string'?data.error.code:undefined);
      return data;
    }catch(error){if(error instanceof TypeError||error instanceof DOMException&&error.name==='AbortError')throw new Error('连接中断，查询可能已受理；重新读取不会重复提交。');throw error;}
  }
  return {
    sessions:async()=>{
      const data=await request('/sessions');
      if(!object(data)||!Array.isArray(data.sessions)||!data.sessions.every(s=>object(s)&&text(s.id)&&positive(s.created_at)&&positive(s.updated_at)))throw bad();
      return data.sessions as AgentSession[];
    },
    history:async(id:string)=>{const data=await request('/sessions/'+encodeURIComponent(id));if(!object(data)||data.id!==id||!Array.isArray(data.turns))throw bad();return data.turns.map(readTurn);},
    create:async(body:TurnRequest)=>readTurn(await request('/turns',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})),
    turn:async(id:string)=>readTurn(await request('/turns/'+encodeURIComponent(id))),
    confirm:async(id:string,body:PlanConfirmation)=>{
      const data=await request('/turns/'+encodeURIComponent(id)+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(!object(data)||!text(data.id))throw bad();return data.id;
    },
    discard:async(id:string,version:string)=>readTurn(await request('/turns/'+encodeURIComponent(id)+'/discard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version})})),
    changeConfirm:async(id:string,body:ChangeConfirmation)=>{
      const data=await request('/changes/'+encodeURIComponent(id)+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const applied=readChangeApplied(data);if(applied.operation!==('steps' in body?'split':'date'))throw bad();return applied.plant_id as string;
    },
    changeDiscard:async(id:string,version:string)=>readTurn(await request('/changes/'+encodeURIComponent(id)+'/discard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version})})),
    recordConfirm:async(id:string,body:RecordConfirmation)=>readRecordApplied(await request('/records/'+encodeURIComponent(id)+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})),
    recordDiscard:async(id:string,version:string)=>readTurn(await request('/records/'+encodeURIComponent(id)+'/discard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version})})),
    remove:async(id:string)=>{const data=await request('/sessions/'+encodeURIComponent(id),{method:'DELETE'});if(!object(data)||data.deleted!==true)throw bad();},
  };
}
