'use client';
import {useEffect,useRef,useState} from 'react';
import {apiFetch,authRequest,Session} from './api';
import {fetchLegal,LegalInfo} from '../legal/api';

type Summary={plant_count:number;memory_count:number;linked_guest_count:number;running_work:boolean;identity_notice:string};
type Challenge={challenge_id:string;expires_at:string;resend_after_seconds:number};
const CONFIRMATION='注销Bloomate账号';

export function AccountPrivacyPanel({session,onDeleted,onPending,onBusyChange,onAccepted}:{session:Session;onDeleted:()=>void;onPending:(id:string)=>void;onBusyChange:(value:boolean)=>void;onAccepted:()=>void}) {
  const [summary,setSummary]=useState<Summary|null>(null);
  const [legal,setLegal]=useState<LegalInfo|null>(null);
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState('');
  const [deleting,setDeleting]=useState(false);
  const [confirmation,setConfirmation]=useState('');
  const [code,setCode]=useState('');
  const [challenge,setChallenge]=useState<Challenge|null>(null);
  const [cooldown,setCooldown]=useState(0);
  const [consent,setConsent]=useState(false);
  const downloadUrl=useRef<string|null>(null);
  useEffect(()=>{onBusyChange(busy);return()=>onBusyChange(false);},[busy,onBusyChange]);
  async function refresh(){
    const value=await authRequest<Summary>('account');
    if(![value.plant_count,value.memory_count,value.linked_guest_count].every(Number.isInteger)||typeof value.running_work!=='boolean'||typeof value.identity_notice!=='string')throw new Error('账号范围返回异常，请重新打开。');
    setSummary(value);
  }
  useEffect(()=>{void refresh().catch(e=>setMessage(e instanceof Error?e.message:'账号信息暂时无法读取。'));void fetchLegal().then(setLegal).catch(()=>{});return()=>{if(downloadUrl.current)URL.revokeObjectURL(downloadUrl.current);};},[]);
  useEffect(()=>{if(!cooldown)return;const timer=setTimeout(()=>setCooldown(v=>Math.max(0,v-1)),1000);return()=>clearTimeout(timer);},[cooldown]);
  async function download(){
    setBusy(true);setMessage('');
    try{
      const r=await apiFetch('/api/v1/auth/account/export',{method:'POST'});
      if(!r.ok){const d=await r.json();throw new Error(d?.error?.message||'导出未完成，请重试。');}
      const blob=await r.blob();
      if(!r.headers.get('content-type')?.includes('application/json')||blob.size>25*1024*1024)throw new Error('导出文件返回异常，请重试。');
      if(downloadUrl.current)URL.revokeObjectURL(downloadUrl.current);
      const url=URL.createObjectURL(blob);downloadUrl.current=url;
      const a=document.createElement('a');a.href=url;a.download='bloomate-personal-data.json';document.body.append(a);a.click();a.remove();
      setTimeout(()=>{URL.revokeObjectURL(url);if(downloadUrl.current===url)downloadUrl.current=null;},10000);
      setMessage('导出文件已交给浏览器下载，请妥善保管。');
    }catch(e){setMessage(e instanceof Error?e.message:'导出未完成，请稍后重试。');}finally{setBusy(false);}
  }
  async function send(){
    setBusy(true);setMessage('');
    try{
      const value=await authRequest<Challenge>('account/deletion-challenges',{method:'POST'});
      if(typeof value.challenge_id!=='string'||!Number.isFinite(Date.parse(value.expires_at))||!Number.isInteger(value.resend_after_seconds))throw new Error('验证码状态返回异常，请重试。');
      setChallenge(value);setCooldown(value.resend_after_seconds);setMessage('注销验证码已发送到当前账号邮箱，登录时的旧验证码不能复用。');
    }catch(e){setMessage(e instanceof Error?e.message:'发送未完成，请重试。');}finally{setBusy(false);}
  }
  async function erase(event:React.FormEvent){
    event.preventDefault();if(!challenge||confirmation!==CONFIRMATION)return;
    setBusy(true);setMessage('');
    try{
      const result=await authRequest<{status:string}>('account/delete',{method:'POST',body:JSON.stringify({challenge_id:challenge.challenge_id,code,confirmation})});
      if(result.status!=='succeeded')throw new Error('注销仍在处理中，请稍后查询。');
      onDeleted();
    }catch(e){
      try{const result=await authRequest<{status:string}>(`deletion-status/${challenge.challenge_id}`);if(result.status==='succeeded'){onDeleted();return;}if(result.status==='pending'){onPending(challenge.challenge_id);return;}}catch{/* Show original error without claiming deletion. */}
      setMessage(e instanceof Error?e.message:'注销未完成，请稍后重试。');
    }finally{setBusy(false);}
  }
  async function status(){
    if(!challenge)return;setBusy(true);setMessage('');
    try{const result=await authRequest<{status:string}>(`deletion-status/${challenge.challenge_id}`);if(result.status==='succeeded')onDeleted();else setMessage('注销仍在处理，账号访问已冻结，请稍后查询。');}
    catch(e){setMessage(e instanceof Error?e.message:'暂时无法查询。');}finally{setBusy(false);}
  }
  async function accept(){
    if(!consent||!legal)return;setBusy(true);setMessage('');
    try{await authRequest('account/agreements',{method:'POST',body:JSON.stringify({terms_version:legal.version,privacy_version:legal.version})});setConsent(false);setMessage('已记录本次条款版本。');onAccepted();}
    catch(e){setMessage(e instanceof Error?e.message:'记录未完成。');}finally{setBusy(false);}
  }
  return <><p className="eyebrow">YOUR ACCOUNT</p><h2 id="auth-title">账号与隐私</h2><p>{session.email_masked}</p>
    {summary?<div className="auth-save-summary">账户及关联体验共 <strong>{summary.plant_count}</strong> 株植物，<strong>{summary.memory_count}</strong> 条记忆。{summary.linked_guest_count>0&&<p>包含 {summary.linked_guest_count} 个已绑定或已迁移的体验空间。</p>}{summary.running_work&&<p>还有任务处理中，请等待完成后导出或注销。</p>}</div>:<p>正在核对数据范围…</p>}
    {!deleting?<><section className="account-section"><h3>导出个人数据</h3><p>下载本人账号的计划、行动、收藏、记忆和同意记录。文件包含个人内容，请妥善保管；未保存体验需先保存到账号。</p><button className="outline-button" type="button" disabled={busy||!summary||summary.running_work} onClick={()=>void download()}>下载我的数据（JSON）</button></section>
      <section className="account-section"><h3>服务与隐私</h3><p><a href="/terms" target="_blank" rel="noopener noreferrer">服务条款 ↗</a> · <a href="/privacy" target="_blank" rel="noopener noreferrer">隐私政策 ↗</a></p>{legal&&!legal.ready&&<p className="auth-note">运营者公示信息待补齐，当前仍为本机验收版本。</p>}
        {legal&&<><label className="auth-consent"><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)} disabled={busy}/><span>我已阅读并同意当前版本（{legal.version}）的服务条款与隐私政策。</span></label><button type="button" disabled={busy||!consent} onClick={()=>void accept()}>记录本次同意</button></>}
      </section><section className="account-section account-danger"><h3>注销Bloomate账号</h3><p>删除本应用账号及花园，撤销所有设备会话。操作无法撤销，建议先导出。</p><button className="account-danger-button" type="button" disabled={busy||!summary||summary.running_work} onClick={()=>{setDeleting(true);setMessage('');}}>查看注销范围</button></section></>:<form onSubmit={event=>void erase(event)} className="account-danger"><h3>确认注销当前Bloomate账号</h3><p>会删除当前账号及已绑定体验中的植物、计划、行动、打卡、收藏、记忆和授权记录；其他账号不受影响。</p><p>{summary?.identity_notice}</p><p>你下载的文件和管理员离线副本无法由网页即时擦除；备份恢复时先执行删除清单。详见隐私政策。</p>
        <label>新的邮箱验证码<div className="auth-code-row"><input value={code} onChange={e=>setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} required disabled={busy}/><button type="button" onClick={()=>void send()} disabled={busy||cooldown>0}>{cooldown?`${cooldown}秒后重发`:'发送注销验证码'}</button></div></label>
        {challenge&&<p className="auth-footnote">验证码有效至 {new Date(challenge.expires_at).toLocaleTimeString('zh-CN')}。</p>}
        <label>请输入“{CONFIRMATION}”<input value={confirmation} onChange={e=>setConfirmation(e.target.value)} required maxLength={40} disabled={busy} autoComplete="off"/></label>
        <button className="account-danger-button" type="submit" disabled={busy||!challenge||confirmation!==CONFIRMATION||!/^\d{6}$/.test(code)}>{busy?'正在处理…':'永久注销Bloomate账号'}</button>
        {challenge&&<button type="button" disabled={busy} onClick={()=>void status()}>查询注销结果</button>}
        <button className="auth-later" type="button" disabled={busy} onClick={()=>setDeleting(false)}>返回账号设置</button></form>}
    <p className="auth-message" role="status">{message}</p>
  </>;
}
