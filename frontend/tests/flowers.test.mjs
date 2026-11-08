import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFlowerCatalog,createFlowerApi,flowerNames,flowerConditions,baseFlowers} from '../features/flowers/api.ts';
import {isPlan} from '../features/planning/api.ts';

function fixture(){return {counts:{goal:0,habit:0,total:0},unread_count:0,history_pending:0,flowers:Object.keys(flowerNames).map(species=>({species,name:flowerNames[species],condition:flowerConditions[species],unlocked:baseFlowers.includes(species),unlocked_at:null,is_new:false,progress:0,target:baseFlowers.includes(species)?0:species==='hydrangea'?5:1}))};}

test('catalog transport rejects false success, duplicate flowers and invented grants',()=>{
  assert.equal(readFlowerCatalog(fixture()).flowers.length,6);
  for(const mutate of [d=>{d.flowers[5]={...d.flowers[0]};},d=>{d.flowers[3].is_new=true;},d=>{d.flowers[3].unlocked=true;},d=>{d.flowers[5].progress=6;},d=>{d.counts.total=1;},d=>{d.flowers[5].unlocked_at='bad';},d=>{d.flowers[3].target=0;},d=>{d.unread_count=1;}]){const d=fixture();mutate(d);assert.throws(()=>readFlowerCatalog(d),/无法读取/);}
  const d=fixture();d.counts={goal:1,habit:0,total:1};Object.assign(d.flowers[3],{unlocked:true,unlocked_at:'2026-11-08T10:00:00Z',is_new:true,progress:1});d.unread_count=1;
  assert.equal(readFlowerCatalog(d).flowers[3].species,'cosmos');
});

test('seen acknowledgment sends only the flowers actually viewed, never a broad clear',async()=>{
  const calls=[];const api=createFlowerApi(async(url,options)=>{calls.push({url,options});return new Response(JSON.stringify(options?{updated:1}:fixture()));});
  assert.equal((await api.get()).counts.total,0);
  await api.seen(['cosmos']);
  assert.equal(calls[1].url,'/api/v1/flowers/seen');assert.equal(calls[1].options.method,'POST');
  assert.deepEqual(JSON.parse(calls[1].options.body),{species:['cosmos']});
});

test('network and provider errors remain retryable without fabricating a zero catalog',async()=>{
  await assert.rejects(createFlowerApi(async()=>{throw new TypeError('offline');}).get(),/已有成果会保留/);
  await assert.rejects(createFlowerApi(async()=>new Response('bad gateway',{status:502})).get(),/暂时无法连接/);
  await assert.rejects(createFlowerApi(async()=>new Response(JSON.stringify({error:{code:'DB_CONFLICT',message:'内容刚刚有更新'}}),{status:409})).get(),/内容刚刚有更新/);
  await assert.rejects(createFlowerApi(async()=>new Response('{}')).seen(['cosmos']),/暂未同步/);
});

test('new flower plans survive transport validation while invented species remain invalid',()=>{
  for(const species of ['cosmos','lavender','hydrangea'])assert.equal(isPlan({title:'新计划',summary:'说明',steps:['行动'],flower_species:species}),true);
  assert.equal(isPlan({title:'新计划',summary:'说明',steps:['行动'],flower_species:'invented'}),false);
});
