'use client';
import {useEffect,useState} from 'react';
import Link from 'next/link';
import {fetchLegal,LegalInfo} from './api';

export function LegalDocument({kind}:{kind:'terms'|'privacy'}) {
  const [info,setInfo]=useState<LegalInfo|null>(null);
  const [error,setError]=useState('');
  const [attempt,setAttempt]=useState(0);
  useEffect(()=>{let alive=true;setError('');void fetchLegal().then(data=>{if(alive)setInfo(data);}).catch(e=>{if(alive)setError(e instanceof Error?e.message:'暂时无法读取。');});return()=>{alive=false;};},[attempt]);
  const doc=info?.[kind];
  return <main className="legal-page"><Link className="legal-back" href="/">✿ Bloomate · 返回首页</Link>
    <p className="eyebrow">{kind==='privacy'?'YOUR PRIVACY':'OUR AGREEMENT'}</p>
    <h1>{doc?.title||(kind==='privacy'?'隐私政策':'服务条款')}</h1>
    {info&&<><p className="legal-meta">更新日期 {info.updated_at} · 版本 {info.version}</p>
      {!info.ready&&<p className="auth-note" role="status">运营者公示信息待补齐，当前仅用于本机受控验收，尚非公开服务生效文本。</p>}</>}
    {error?<div role="alert"><p>{error}</p><button type="button" onClick={()=>setAttempt(v=>v+1)}>重新读取</button></div>:!doc?<p role="status">正在读取…</p>:<>
      {doc.sections.map(section=><section key={section.heading}><h2>{section.heading}</h2>{section.paragraphs.map((p,index)=><p key={index}>{p}</p>)}</section>)}
      <section><h2>相关服务说明</h2>{info?.sources.map(s=><p key={s.url}><a href={s.url} target="_blank" rel="noopener noreferrer">{s.label} ↗</a></p>)}</section>
      <nav aria-label="法律文件"><a href={kind==='privacy'?'/terms':'/privacy'}>{kind==='privacy'?'查看服务条款':'查看隐私政策'}</a></nav></>}
  </main>;
}
