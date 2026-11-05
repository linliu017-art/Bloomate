import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readPlanTask,createPlanningApi} from '../features/planning/api.ts';

const base={id:'job-1',kind:'habit',status:'done',proposal:null,question:'你想从哪天开始？',constraints:{session_budget_minutes:null,habit_completion_minutes:null,habit_schedule:null}};
test('a restored question stays a question, never a ready-to-plant proposal',()=>{
  const task=readPlanTask(base);assert.equal(task.proposal,null);assert.equal(task.question,base.question);
  assert.throws(()=>readPlanTask({...base,question:null}),/无法读取/);
});
test('unknown states and malformed plans cannot become successful previews',()=>{
  assert.throws(()=>readPlanTask({...base,status:'completed'}),/无法读取/);
  assert.throws(()=>readPlanTask({...base,proposal:{title:'bad',summary:'bad',steps:[null]}}),/无法读取/);
  assert.throws(()=>readPlanTask({...base,status:'superseded',next_job_id:null}),/无法读取/);
  assert.throws(()=>readPlanTask({...base,constraints:{...base.constraints,habit_schedule:{weekdays:[8]}}}),/无法读取/);
});
test('the edited plan and exact habit constraints reach their respective endpoints',async()=>{
  const calls=[];const plan={title:'真实编辑',summary:'说明',steps:['阅读十分钟'],flower_species:'daisy'};
  const api=createPlanningApi(async(path,options)=>{calls.push({path,options});return new Response(JSON.stringify(path.includes('confirm')?{id:'plant-1',plan}:{id:'job-2'}));});
  const schedule={start_date:'2026-11-05',end_date:'2026-11-05',timezone:'Asia/Shanghai',weekdays:[3],completion_standard:'阅读十分钟',growth_threshold:1};
  await api.create({kind:'habit',goal:'阅读',background:'',availability:'每次十分钟',session_budget_minutes:10,habit_completion_minutes:10,habit_schedule:schedule});
  assert.deepEqual(JSON.parse(calls[0].options.body).habit_schedule,schedule);
  assert.equal(await api.confirm('job-2',plan),'plant-1');assert.deepEqual(JSON.parse(calls[1].options.body),plan);
});
test('failed confirmation and malformed transport remain recoverable errors',async()=>{
  const api=createPlanningApi(async()=>new Response(JSON.stringify({error:{code:'PLAN_CONSTRAINT',message:'时间超过上限'}}),{status:409}));
  await assert.rejects(api.confirm('x',{title:'a',summary:'b',steps:['c']}),/时间超过上限/);
  await assert.rejects(createPlanningApi(async()=>new Response('bad')).get('x'),/无法连接/);
  await assert.rejects(createPlanningApi(async()=>{throw new TypeError('Failed to fetch');}).get('x'),/已保留当前输入/);
});
