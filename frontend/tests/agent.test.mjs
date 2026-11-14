import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readTurn,readResult,createAgentApi} from '../features/agent/api.ts';
const counts={goals_today:1,goals_overdue:0,goals_unscheduled:0,habits_today:0,habits_backfill:0};
const result={kind:'today',text:'真实记录',queried_at:100,data:{date:'2026-11-14',counts,goals_today:[{plant_id:'p',plant_title:'目标',text:'一步',completed:false}],goals_overdue:[],goals_unscheduled:[],habits_today:[],habits_backfill:[]}};
const turn={id:'t',session_id:'s',status:'done',text:'今天做什么',created_at:100,expires_at:1000,result,error:null};
test('agent transport rejects unknown states and false successes',()=>{
  assert.equal(readTurn(turn).result.kind,'today');
  assert.throws(()=>readTurn({...turn,status:'completed'}));
  assert.throws(()=>readTurn({...turn,result:null}));
  assert.throws(()=>readTurn({...turn,status:'error',error:{code:'x',message:'失败'}}));
  assert.throws(()=>readTurn({...turn,expires_at:99}));
});
test('query cards validate task ownership references, counts and completion types',()=>{
  assert.throws(()=>readResult({...result,data:{...result.data,goals_today:[{completed:true}]}}));
  assert.throws(()=>readResult({...result,data:{...result.data,counts:{...counts,goals_today:0}}}));
  assert.throws(()=>readResult({...result,data:{...result.data,goals_today:[{...result.data.goals_today[0],completed:'true'}]}}));
  assert.throws(()=>readResult({kind:'plant',text:'x',queried_at:100,data:{title:'没有编号'}}));
});
test('exact idempotency key and context survive a submission retry',async()=>{
  const calls=[];const api=createAgentApi(async(path,options)=>{calls.push({path,options});return new Response(JSON.stringify(turn));});
  const body={request_id:'stable-request-id-1',session_id:'s',text:'今天做什么',context:{page:'today',plant_id:null}};
  await api.create(body);await api.create(body);
  assert.deepEqual(JSON.parse(calls[0].options.body),body);
  assert.equal(calls[0].options.body,calls[1].options.body);
});
test('agent restoration and deletion use owner-scoped server APIs and surface failures',async()=>{
  const paths=[];const api=createAgentApi(async(path,options)=>{paths.push([path,options?.method||'GET']);return new Response(JSON.stringify(path.includes('/turns/')?turn:options?.method==='DELETE'?{deleted:true}:{id:'s',turns:[turn]}));});
  assert.equal((await api.history('s'))[0].id,'t');
  await api.turn('t');await api.remove('s');
  assert.deepEqual(paths,[['/api/v1/agent/sessions/s','GET'],['/api/v1/agent/turns/t','GET'],['/api/v1/agent/sessions/s','DELETE']]);
  await assert.rejects(createAgentApi(async()=>new Response(JSON.stringify({error:{code:'BUDGET_EXHAUSTED',message:'预算已用完'}}),{status:429})).sessions(),/预算已用完/);
  await assert.rejects(createAgentApi(async()=>{throw new TypeError('network');}).sessions(),/可能已受理/);
});

const fields={kind:'goal',goal:'整理提纲',background:'',duration_days:3,daily_minutes:10,start_date:null,end_date:null,weekdays:null,completion_standard:null,growth_threshold:null,flexible_time:false,use_suggestions:false};
const draft={state:'ready',version:'version-1',fields,proposal:{title:'提纲',summary:'说明',steps:['整理一条'],session_actions:[{text:'整理',kind:'core',minutes:10}]},question:null,goal_dates:['2026-11-14'],suggestions:[]};
test('plan results validate state, conditions, steps and date alignment',()=>{
  const result={kind:'plan',text:'等待确认',queried_at:100,data:draft};
  assert.equal(readResult(result).data.state,'ready');
  for(const data of [{...draft,state:'auto_planted'},{...draft,proposal:null},{...draft,goal_dates:[]},{...draft,fields:{...fields,daily_minutes:'10'}},{...draft,state:'confirmed'}])assert.throws(()=>readResult({...result,data}));
  assert.equal(readResult({...result,data:{...draft,state:'collecting',proposal:null,goal_dates:[]}}).data.state,'collecting');
});
test('explicit confirm sends edited body unchanged for idempotent retries and discard restores state',async()=>{
  const calls=[];const api=createAgentApi(async(path,options)=>{calls.push([path,options]);return new Response(JSON.stringify(path.endsWith('/confirm')?{id:'planted'}:{...turn,result:{kind:'plan',text:'已放弃',queried_at:100,data:{...draft,state:'discarded'}}}));});
  const body={version:draft.version,plan:{...draft.proposal,title:'编辑后的标题'},goal_dates:draft.goal_dates};
  assert.equal(await api.confirm('turn id',body),'planted');await api.confirm('turn id',body);
  assert.equal(calls[0][0],'/api/v1/agent/turns/turn%20id/confirm');assert.equal(calls[0][1].body,calls[1][1].body);
  assert.deepEqual(JSON.parse(calls[0][1].body),body);
  assert.equal((await api.discard('t',draft.version)).result.data.state,'discarded');
});

const target={plant_id:'p',stage_id:null,index:0,base_hash:'a'.repeat(64)};
const candidate={target,plant_title:'目标',stage_title:'初始阶段',step_number:1,text:'第一步',due_date:null,blocked_reason:null};
const change={state:'ready',version:'change-v1',operation:'date',request:{operation:'date'},choices:[],target,before:candidate,after:{due_date:'2026-11-15'}};
test('change previews validate selection ownership references, states and actual results',()=>{
 const r={kind:'change',text:'预览',queried_at:100,data:change};assert.equal(readResult(r).data.operation,'date');
 for(const data of [{...change,state:'executing'},{...change,target:{...target,base_hash:'bad'}},{...change,after:{}},{...change,state:'confirmed'},{...change,choices:[{...candidate,target:{...target,index:-1}}]}])assert.throws(()=>readResult({...r,data}));
 assert.equal(readResult({...r,data:{...change,operation:'split',request:{operation:'split'},after:{steps:['小步1','小步2']}}}).data.after.steps.length,2);
});
test('change choice and exact confirmation bodies survive transport retries',async()=>{
 const calls=[];const api=createAgentApi(async(path,options)=>{calls.push([path,options]);return new Response(JSON.stringify(path.includes('/changes/')?{status:'confirmed',plant_id:'p',operation:'date',result:{index:0,text:'第一步',completed:false,due_date:'2026-11-15'}}:{...turn,result:{kind:'change',text:'选择步骤',queried_at:100,data:{...change,state:'select_task',choices:[candidate]}}}));});
 const context={page:'today',plant_id:'p',change_choice:{turn_id:'old',version:'v',target}};
 await api.create({request_id:'stable-change-choice',session_id:'s',text:'选择第一步',context});assert.deepEqual(JSON.parse(calls[0][1].body).context,context);
 const body={version:'change-v1',due_date:'2026-11-15'};await api.changeConfirm('t',body);await api.changeConfirm('t',body);
 assert.equal(calls[1][0],'/api/v1/agent/changes/t/confirm');assert.equal(calls[1][1].body,calls[2][1].body);
 await assert.rejects(createAgentApi(async()=>new Response(JSON.stringify({error:{code:'PLAN_CHANGED',message:'原计划已变化'}}),{status:409})).changeConfirm('t',body),/原计划已变化/);
});

test('change confirmation rejects malformed success payloads',async()=>{
 const api=createAgentApi(async()=>new Response(JSON.stringify({status:'confirmed',plant_id:'p',operation:'date',result:{}})));
 await assert.rejects(api.changeConfirm('t',{version:'v',due_date:'2026-11-15'}),/无法读取/);
});

const recordRef={plant_id:'p',kind:'habit',stage_id:'stage',index:null,action_date:'2026-11-14',base_hash:'b'.repeat(64)};
const recordBefore={target:recordRef,plant_title:'阅读',stage_title:'当前阶段',text:'阅读10分钟',completed:false,paused:false,ended:false,timezone:'Asia/Shanghai',archived:false,blocked_reason:null};
const recordDraft={state:'ready',version:'record-v1',operation:'complete',request:{operation:'complete'},anchor_at:100,choices:[],target:recordRef,before:recordBefore,desired_completed:true};
const appliedRecord={status:'confirmed',operation:'complete',plant_id:'p',kind:'habit',completed:true,action_date:'2026-11-14',paused:false,ended:false,stage_completed:false,history_preserved:false,recorded_at:101};
test('record previews validate object/date and never confuse uncertain reports with success',()=>{
 const r={kind:'record',text:'核对记录',queried_at:100,data:recordDraft};assert.equal(readResult(r).data.state,'ready');
 for(const data of [{...recordDraft,target:{...recordRef,action_date:null}},{...recordDraft,desired_completed:false},{...recordDraft,state:'confirmed'},{...recordDraft,choices:[{...recordBefore,completed:'true'}]}])assert.throws(()=>readResult({...r,data}));
 assert.equal(readResult({...r,data:{...recordDraft,state:'clarify_report'}}).data.state,'clarify_report');
 assert.equal(readResult({...r,data:{...recordDraft,state:'confirmed',applied:appliedRecord,after_ref:{...recordRef,base_hash:'c'.repeat(64)}}}).data.applied.completed,true);
});
test('record confirmation sends exact explicit performance and rejects malformed actual outcomes',async()=>{
 const calls=[];const api=createAgentApi(async(path,options)=>{calls.push([path,options]);return new Response(JSON.stringify(appliedRecord));});
 const body={version:'record-v1',performed:true};await api.recordConfirm('t',body);await api.recordConfirm('t',body);
 assert.equal(calls[0][0],'/api/v1/agent/records/t/confirm');assert.equal(calls[0][1].body,calls[1][1].body);assert.deepEqual(JSON.parse(calls[0][1].body),body);
 for(const result of [{...appliedRecord,completed:false},{...appliedRecord,action_date:null},{status:'confirmed'}])await assert.rejects(createAgentApi(async()=>new Response(JSON.stringify(result))).recordConfirm('t',body),/无法读取/);
});
test('record facts and lifecycle outcomes must refer to the same actual object and operation',async()=>{
 const r={kind:'record',text:'核对',queried_at:100,data:recordDraft};
 assert.throws(()=>readResult({...r,data:{...recordDraft,before:{...recordBefore,target:{...recordRef,plant_id:'another'}}}}));
 assert.throws(()=>readResult({...r,data:{...recordDraft,state:'confirmed',applied:appliedRecord,after_ref:{...recordRef,action_date:'2026-11-13'}}}));
 for(const value of [{...appliedRecord,operation:'pause',completed:null,paused:false},{...appliedRecord,operation:'resume',completed:null,paused:true},{...appliedRecord,operation:'end',completed:null,ended:false}])await assert.rejects(createAgentApi(async()=>new Response(JSON.stringify(value))).recordConfirm('t',{version:'v',performed:false}),/无法读取/);
});
