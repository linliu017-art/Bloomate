'use client';
import {createContext,useCallback,useContext,useEffect,useRef,useState} from 'react';
import Image from 'next/image';
import {apiFetch} from '../auth/api';
import type {FlowerSpecies} from '../planning/api';
import {baseFlowers,createFlowerApi,flowerNames,flowerConditions,type FlowerCatalog} from './api';

const api=createFlowerApi(apiFetch);
type FlowersContext={catalog:FlowerCatalog|null;error:string;reload:()=>Promise<void>;openAtlas:()=>void};
const Context=createContext<FlowersContext|null>(null);
const useFlowers=()=>{const value=useContext(Context);if(!value)throw new Error('Flower provider required');return value;};

export function FlowerPicture({species,phase='bloom',className='',alt=''}:{species:FlowerSpecies;phase?:string;className?:string;alt?:string}) {
  const [failed,setFailed]=useState(false);
  if(failed)return <span className={'flower-image-fallback '+className} role="img" aria-label={flowerNames[species]+'图片暂未加载'}><span aria-hidden="true">✿</span><small>{flowerNames[species]}</small></span>;
  return <Image className={className} src={`/flowers/${species}-${phase}.png`} alt={alt} width={240} height={330} sizes="(max-width:600px) 130px, 210px" onError={()=>setFailed(true)}/>;
}

export function FlowerProvider({children}:{children:React.ReactNode}) {
  const [catalog,setCatalog]=useState<FlowerCatalog|null>(null);const [error,setError]=useState('');
  const [open,setOpen]=useState(false);const [seenError,setSeenError]=useState('');const [seenRetry,setSeenRetry]=useState(0);
  const dialog=useRef<HTMLDialogElement>(null);const previousFocus=useRef<HTMLElement|null>(null);
  const mounted=useRef(false);const requestVersion=useRef(0);const marking=useRef(false);
  const reload=useCallback(async()=>{const version=++requestVersion.current;try{const data=await api.get();if(mounted.current&&requestVersion.current===version){setCatalog(data);setError('');}}catch(e){if(mounted.current&&requestVersion.current===version)setError(e instanceof Error?e.message:'花种暂时无法读取。');}},[]);
  useEffect(()=>{mounted.current=true;void reload();const refresh=()=>void reload();window.addEventListener('bloomate-progress',refresh);return()=>{mounted.current=false;window.removeEventListener('bloomate-progress',refresh);};},[reload]);
  const close=()=>{setOpen(false);setSeenError('');};
  useEffect(()=>{
    if(open&&!dialog.current?.open){dialog.current?.showModal();dialog.current?.querySelector<HTMLButtonElement>('.flower-atlas-close')?.focus();}
    if(!open&&dialog.current?.open){dialog.current.close();if(previousFocus.current?.isConnected)previousFocus.current.focus();}
  },[open]);
  useEffect(()=>{
    if(!open||!catalog||!dialog.current?.open||marking.current||seenError)return;
    const seen=catalog.flowers.filter(f=>f.is_new).map(f=>f.species);if(!seen.length)return;
    marking.current=true;
    void api.seen(seen).then(()=>{if(mounted.current){setCatalog(current=>current?{...current,flowers:current.flowers.map(f=>seen.includes(f.species)?{...f,is_new:false}:f),unread_count:current.flowers.filter(f=>f.is_new&&!seen.includes(f.species)).length}:current);}}).catch(()=>{if(mounted.current)setSeenError('新花提示暂未同步，解锁记录仍保留。');}).finally(()=>{marking.current=false;});
  },[open,catalog,seenError,seenRetry]);
  function openAtlas(){previousFocus.current=document.activeElement as HTMLElement|null;setOpen(true);setSeenError('');void reload();}
  const owned=catalog?.flowers.filter(f=>f.unlocked).length;
  return <Context.Provider value={{catalog,error,reload,openAtlas}}>{children}
    <dialog ref={dialog} className="flower-atlas" aria-labelledby="flower-atlas-title" onCancel={e=>{e.preventDefault();close();}} onClick={e=>{if(e.target===e.currentTarget){const rect=e.currentTarget.getBoundingClientRect();if(e.clientX<rect.left||e.clientX>rect.right||e.clientY<rect.top||e.clientY>rect.bottom)close();}}}>
      <div className="flower-atlas-heading"><div><span className="eyebrow">A GARDEN OF POSSIBILITIES</span><h2 id="flower-atlas-title">让努力，开出新的花。</h2><p>{catalog?`已拥有 ${owned} / 6 种花 · 已收获 ${catalog.counts.total} 份成果`:'每一份成果，都让花园多一种可能。'}</p></div><button className="flower-atlas-close" type="button" aria-label="关闭花种图鉴" onClick={close}>×</button></div>
      {error&&<p className="flower-service-error" role="alert">{error} <button type="button" onClick={()=>void reload()}>重新读取</button></p>}
      {!catalog&&!error&&<p role="status">正在翻开你的花种图鉴…</p>}
      {catalog&&<><div className="flower-atlas-grid">{catalog.flowers.map(f=><article key={f.species} className={'flower-atlas-card'+(f.unlocked?' is-owned':' is-locked')}><div className="flower-atlas-art"><FlowerPicture species={f.species}/><span className="flower-card-state">{f.unlocked?'已拥有':'待解锁'}</span></div><div className="flower-atlas-copy"><h3>{f.name}</h3><p>{f.condition}</p>{f.target>0&&<div className="flower-unlock-progress"><span aria-hidden="true"><i style={{width:`${100*f.progress/f.target}%`}}/></span><small>{f.unlocked?'已经解锁':`${f.progress} / ${f.target} ${f.species==='cosmos'?'个完整目标':f.species==='lavender'?'个习惯阶段':'份成果'}`}</small></div>}{f.unlocked_at&&<time dateTime={f.unlocked_at}>{new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'numeric',day:'numeric'}).format(new Date(f.unlocked_at))} 解锁</time>}</div></article>)}</div>
      {catalog.history_pending>0&&<p className="flower-atlas-note">有 {catalog.history_pending} 条历史成果的信息不完整，尚未计入；已有解锁不受影响。</p>}
      <p className="flower-atlas-note">完成一个完整目标或一个习惯阶段，记一份成果。原有三种花随时可选，新花在下次种植时使用。</p></>}
      {seenError&&<p role="status" className="flower-service-error">{seenError} <button type="button" onClick={()=>{setSeenError('');setSeenRetry(v=>v+1);}}>重试同步</button></p>}
      <button className="flower-atlas-done" type="button" onClick={close}>回到我的花园</button>
    </dialog>
  </Context.Provider>;
}

export function FlowerAtlasTrigger() {
  const {catalog,openAtlas}=useFlowers();
  return <button type="button" className="flower-atlas-trigger" onClick={openAtlas}><span aria-hidden="true">✿</span> 花种图鉴 {catalog&&<small>{catalog.flowers.filter(f=>f.unlocked).length}/6</small>}{!!catalog?.unread_count&&<span className="flower-new-indicator">新</span>}</button>;
}

export function FlowerRewardNotice({completionKind}:{completionKind?:'goal'|'habit'}) {
  const {catalog,openAtlas}=useFlowers();const fresh=catalog?.flowers.filter(f=>f.is_new)??[];
  if(!fresh.length){if(completionKind==='goal'&&catalog&&!catalog.flowers.find(f=>f.species==='cosmos')?.unlocked)return <p className="flower-completion-hint">选择完成整个目标后，还能解锁波斯菊；继续阶段会沿用这株植物。</p>;return null;}
  return <div className="flower-reward-notice" role="status"><span className="flower-reward-symbol" aria-hidden="true">✿</span><div><strong>新花种已解锁 · {fresh.map(f=>f.name).join('、')}</strong><p>已经为你留下，下次种植时可以选择。</p></div><button type="button" onClick={openAtlas}>查看花种 <span aria-hidden="true">↗</span></button></div>;
}

export function FlowerSelector({value,onChange}:{value:FlowerSpecies;onChange:(species:FlowerSpecies)=>void}) {
  const {catalog,error,reload}=useFlowers();const [hint,setHint]=useState('');
  return <><div className="flower-picker flower-picker-expanded" role="group" aria-label="选择花种">{(Object.keys(flowerNames) as FlowerSpecies[]).map(species=>{
    const flower=catalog?.flowers.find(f=>f.species===species);const allowed=baseFlowers.includes(species)||flower?.unlocked===true;
    return <button type="button" key={species} className={'flower-option'+(value===species?' active':'')+(allowed?'':' locked')} aria-pressed={value===species} aria-disabled={!allowed} onClick={()=>{if(allowed){onChange(species);setHint('');}else setHint(catalog?`${flowerNames[species]}：${flowerConditions[species]}，当前进度 ${flower?.progress??0}/${flower?.target??1}。`:'花种权限正在核对，请稍后重新读取。');}}><span className="flower-option-art" aria-hidden="true"><FlowerPicture species={species}/></span><strong>{flowerNames[species]}{value===species&&' ✓'}</strong><small>{allowed?'可以选择':catalog?flowerConditions[species]:'正在核对解锁状态…'}</small></button>;
  })}</div>{hint&&<p role="status" className="flower-selection-hint">{hint}</p>}{error&&<p role="alert" className="flower-service-error">{error} <button type="button" onClick={()=>void reload()}>重新读取花种</button></p>}</>;
}
