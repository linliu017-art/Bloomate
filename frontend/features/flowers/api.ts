import type {FlowerSpecies} from '../planning/api';

export const flowerNames: Record<FlowerSpecies, string> = {
  sunflower:'向日葵', daisy:'雏菊', tulip:'郁金香',
  cosmos:'波斯菊', lavender:'薰衣草', hydrangea:'绣球花',
};
export const flowerConditions: Record<FlowerSpecies, string> = {
  sunflower:'初始花种，自由选择', daisy:'初始花种，自由选择', tulip:'初始花种，自由选择',
  cosmos:'完成第一个完整目标', lavender:'第一次习惯阶段达标', hydrangea:'累计获得五份成果',
};
export const baseFlowers:FlowerSpecies[]=['sunflower','daisy','tulip'];
export type Flower = {species:FlowerSpecies;name:string;condition:string;unlocked:boolean;unlocked_at:string|null;is_new:boolean;progress:number;target:number};
export type FlowerCatalog = {flowers:Flower[];counts:{goal:number;habit:number;total:number};unread_count:number;history_pending:number};
const object=(v:unknown):v is Record<string,unknown>=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const integer=(v:unknown)=>Number.isSafeInteger(v)&&Number(v)>=0;
const dateOrNull=(v:unknown)=>v===null||(typeof v==='string'&&Number.isFinite(Date.parse(v)));

export function readFlowerCatalog(value:unknown):FlowerCatalog {
  const bad=()=>new Error('花种状态暂时无法读取，请重试。');
  if(!object(value)||!object(value.counts)||!['goal','habit','total'].every(k=>integer((value.counts as Record<string,unknown>)[k]))||!integer(value.unread_count)||!integer(value.history_pending))throw bad();
  const counts=value.counts as FlowerCatalog['counts'];
  if(counts.total!==counts.goal+counts.habit||!Array.isArray(value.flowers)||value.flowers.length!==6)throw bad();
  const ids=new Set<string>();let unread=0;
  for(const flower of value.flowers) {
    if(!object(flower)||typeof flower.species!=='string'||!Object.hasOwn(flowerNames,flower.species)||ids.has(flower.species)||typeof flower.name!=='string'||!flower.name||typeof flower.condition!=='string'||!flower.condition||typeof flower.unlocked!=='boolean'||typeof flower.is_new!=='boolean'||!dateOrNull(flower.unlocked_at)||!integer(flower.progress)||!integer(flower.target)||Number(flower.progress)>Number(flower.target))throw bad();
    ids.add(flower.species);
    const base=baseFlowers.includes(flower.species as FlowerSpecies);
    const target=base?0:flower.species==='hydrangea'?5:1;
    if(flower.target!==target||(base&&(!flower.unlocked||flower.is_new||flower.unlocked_at!==null))||(!base&&(flower.unlocked!==(flower.unlocked_at!==null)))||(flower.is_new&&!flower.unlocked))throw bad();
    if(flower.is_new)unread++;
  }
  if(unread!==value.unread_count)throw bad();
  return value as FlowerCatalog;
}

export function createFlowerApi(fetcher:typeof fetch) {
  async function json(path:string,options?:RequestInit) {
    try {
      const response=await fetcher('/api/v1/flowers'+path,options);
      let data;try{data=await response.json();}catch{throw new Error('花种服务暂时无法连接，请稍后重试。');}
      if(!response.ok)throw new Error(object(data)&&object(data.error)&&typeof data.error.message==='string'?data.error.message:'花种操作未完成，请重试。');
      return data;
    }catch(error){if(error instanceof TypeError)throw new Error('无法连接花种服务，已有成果会保留，请重试。');throw error;}
  }
  return {
    get:async()=>readFlowerCatalog(await json('')),
    seen:async(species:FlowerSpecies[])=>{
      const data=await json('/seen',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({species})});
      if(!object(data)||!integer(data.updated))throw new Error('查看记录暂未同步，请重试。');
    },
  };
}
