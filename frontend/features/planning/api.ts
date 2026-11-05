export type Action = {text: string; kind: 'core'|'other'; minutes: number|null};
export type HabitPlanSchedule = {start_date:string;end_date:string;timezone:string;weekdays:number[];completion_standard:string;growth_threshold:number};
export type Constraints = {session_budget_minutes:number|null;habit_completion_minutes:number|null;habit_schedule?:HabitPlanSchedule|null};
export type FlowerSpecies = 'sunflower'|'daisy'|'tulip';
export type Plan = {title:string;summary:string;steps:string[];session_actions?:Action[]|null;growth_nodes?:number[]|null;flower_species?:FlowerSpecies|null;flower_reason?:string|null};
export type PlanTask = {id:string;status:'pending'|'running'|'done'|'error'|'superseded';kind:'goal'|'habit';proposal:Plan|null;question:string|null;constraints:Constraints;next_job_id?:string|null;error?:{code?:string;message:string}};
type Input = {kind:'goal'|'habit';goal:string;background:string;availability:string;session_budget_minutes:number|null;habit_completion_minutes:number|null;habit_schedule:HabitPlanSchedule|null};
const object = (v:unknown):v is Record<string,unknown> => v!==null && typeof v==='object' && !Array.isArray(v);
const nullableNumber = (v:unknown) => v===null || (typeof v==='number' && Number.isFinite(v));

export function isPlan(v:unknown):v is Plan {
  return object(v) && typeof v.title==='string' && typeof v.summary==='string'
    && Array.isArray(v.steps) && v.steps.length>0 && v.steps.every(x=>typeof x==='string')
    && (v.flower_species==null || ['sunflower','daisy','tulip'].includes(String(v.flower_species)))
    && (v.flower_reason==null || typeof v.flower_reason==='string')
    && (v.growth_nodes==null || (Array.isArray(v.growth_nodes) && v.growth_nodes.length===4 && v.growth_nodes.every(n=>typeof n==='number'&&Number.isFinite(n))))
    && (v.session_actions==null || (Array.isArray(v.session_actions) && v.session_actions.every(a=>object(a)&&typeof a.text==='string'&&['core','other'].includes(String(a.kind))&&nullableNumber(a.minutes))));
}

export function readPlanTask(value:unknown):PlanTask {
  const invalid=()=>new Error('计划状态暂时无法读取，请稍后重试。');
  if(!object(value) || typeof value.id!=='string' || !value.id || !['pending','running','done','error','superseded'].includes(String(value.status)) || !['goal','habit'].includes(String(value.kind)))throw invalid();
  const c=value.constraints;
  if(!object(c)||!nullableNumber(c.session_budget_minutes)||!nullableNumber(c.habit_completion_minutes))throw invalid();
  const s=c.habit_schedule;
  if(s!=null && (!object(s)||typeof s.start_date!=='string'||typeof s.end_date!=='string'||typeof s.timezone!=='string'||!Array.isArray(s.weekdays)||!s.weekdays.every(n=>Number.isInteger(n)&&Number(n)>=0&&Number(n)<=6)||typeof s.completion_standard!=='string'||!Number.isInteger(s.growth_threshold)))throw invalid();
  if(value.proposal!=null&&!isPlan(value.proposal))throw invalid();
  if(value.question!=null&&typeof value.question!=='string')throw invalid();
  if(value.status==='done'&&!value.proposal&&!value.question)throw invalid();
  if(value.status==='superseded'&&(typeof value.next_job_id!=='string'||!value.next_job_id))throw invalid();
  if(value.error!=null&&(!object(value.error)||typeof value.error.message!=='string'))throw invalid();
  return {...value,proposal:value.proposal??null,question:value.question??null} as PlanTask;
}

// The caller supplies the existing same-origin Cookie/CSRF adapter.
export function createPlanningApi(request:typeof globalThis.fetch) {
  async function json(path:string, options:RequestInit={}) {
    let response:Response;
    try{response=await request('/api/v1/'+path,options);}catch{throw new Error('网络暂时无法连接，已保留当前输入，请稍后重试。');}
    let data:unknown;
    try{data=await response.json();}catch{throw new Error('服务暂时无法连接，请稍后重试。');}
    if(!response.ok)throw new Error(object(data)&&object(data.error)&&typeof data.error.message==='string'?data.error.message:'操作未完成，请稍后重试。');
    return data;
  }
  async function submit(path:string,payload:unknown) {
    const data=await json(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(!object(data)||typeof data.id!=='string'||!data.id)throw new Error('服务未返回任务编号，请先重新读取计划。');
    return data.id;
  }
  return {
    create:(input:Input)=>submit('plans',input),
    get:async(id:string)=>readPlanTask(await json('plans/'+encodeURIComponent(id))),
    reply:(id:string,answer:string)=>submit('plans/'+encodeURIComponent(id)+'/reply',{answer}),
    confirm:async(id:string,plan:Plan)=>{
      const result=await json('proposals/'+encodeURIComponent(id)+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(plan)});
      if(!object(result)||typeof result.id!=='string'||!isPlan(result.plan))throw new Error('种植结果暂时无法读取，请重试确认；同一计划不会重复种植。');
      return result.id;
    },
  };
}
