'use client';
import {createContext, useCallback, useContext, useEffect, useRef, useState} from 'react';
import {authRequest, configureCsrf, Session, setCacheScope, validateSession, SavePreview, clearPrivateCache} from './api';
import {AccountPrivacyPanel} from './AccountPrivacyPanel';
import {fetchLegal,LegalInfo} from '../legal/api';

type AuthContextValue = {session: Session; openLogin: () => void; openSave: () => void; openAccount:()=>void; logout: () => Promise<void>};
const AuthContext = createContext<AuthContextValue | null>(null);
export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error('Authentication boundary is required');
  return value;
}

export function AccountControls() {
  const {session, openLogin, openSave, openAccount, logout} = useAuth();
  const [error,setError] = useState('');
  const expiry = session.guest_expires_at ? new Date(session.guest_expires_at).toLocaleDateString('zh-CN') : '';
  return <div className="auth-controls">
    <span className="auth-status">{session.kind === 'guest' ? `体验中 · ${expiry}到期` : session.email_masked}
      {session.pending_save && <small>本次体验尚未保存</small>}</span>
    {session.kind === 'guest' ? <button type="button" className="auth-trigger" onClick={openLogin}>登录并保存</button>
      : <><button type="button" className="auth-trigger" onClick={openAccount}>账号与隐私</button>{session.pending_save && <button type="button" className="auth-trigger" onClick={openSave}>保存这次体验</button>}
        <button type="button" className="auth-signout" onClick={()=>{setError('');void logout().catch(e=>setError(e instanceof Error?e.message:'退出未完成，请重试。'));}}>退出</button></>}
    {error && <span role="alert">{error}</span>}
  </div>;
}

type Challenge = {challenge_id:string;expires_at:string;resend_after_seconds:number};

export function AuthBoundary({children}: {children: React.ReactNode}) {
  const [session,setSession] = useState<Session|null>(null);
  const [loading,setLoading] = useState(true);
  const [error,setError] = useState('');
  const [modal,setModal] = useState<'login'|'save'|'account'|'erasure'|null>(null);
  const [legal,setLegal] = useState<LegalInfo|null>(null);
  const [legalError,setLegalError] = useState('');
  const [erasureReceipt,setErasureReceipt] = useState<string|null>(null);
  const [email,setEmail] = useState('');
  const [code,setCode] = useState('');
  const [consent,setConsent] = useState(false);
  const [challenge,setChallenge] = useState<Challenge|null>(null);
  const [cooldown,setCooldown] = useState(0);
  const [busy,setBusy] = useState(false);
  const [message,setMessage] = useState('');
  const [preview,setPreview] = useState<SavePreview|null>(null);
  const [saveConfirmed,setSaveConfirmed] = useState(false);
  const [revision,setRevision] = useState(0);
  const dialog = useRef<HTMLDialogElement>(null);
  const previousFocus = useRef<HTMLElement|null>(null);
  const generation = useRef(0);
  const operation = useRef<{id:string;version:string}|null>(null);
  const reloadLegal=useCallback(async()=>{setLegalError('');try{setLegal(await fetchLegal());}catch(e){setLegalError(e instanceof Error?e.message:'条款暂时无法读取。');}},[]);
  useEffect(()=>{void reloadLegal();try{const receipt=sessionStorage.getItem('bloomate-erasure-receipt');if(receipt&&/^[a-f0-9]{48}$/.test(receipt)){setErasureReceipt(receipt);setModal('erasure');}}catch{/* The receipt may also be kept manually. */}},[reloadLegal]);

  const loadSession = useCallback(async () => {
    const result = validateSession(await authRequest<unknown>('session'));
    configureCsrf(result.csrf_token);setCacheScope(result.cache_scope);setSession(result);
    if(result.agreements_required)setModal('account');
    return result;
  },[]);
  useEffect(()=>{
    let alive=true;
    void loadSession().catch(e=>{if(alive)setError(e instanceof Error?e.message:'登录状态暂时无法读取。');}).finally(()=>{if(alive)setLoading(false);});
    const expired=()=>{generation.current++;configureCsrf(null);setSession(null);setModal(null);setError('登录或体验已到期，请重新进入。');};
    window.addEventListener('bloomate-session-expired',expired);
    const pageShow=(event:PageTransitionEvent)=>{if(event.persisted){setSession(null);setLoading(true);void loadSession().catch(()=>expired()).finally(()=>setLoading(false));}};
    window.addEventListener('pageshow',pageShow);
    return ()=>{alive=false;window.removeEventListener('bloomate-session-expired',expired);window.removeEventListener('pageshow',pageShow);};
  },[loadSession]);
  useEffect(()=>{
    if(!cooldown)return;
    const timer=setTimeout(()=>setCooldown(v=>Math.max(0,v-1)),1000);
    return ()=>clearTimeout(timer);
  },[cooldown]);
  useEffect(()=>{
    if(modal && dialog.current && !dialog.current.open){
      const active=document.activeElement as HTMLElement|null;
      if(active&&active!==document.body&&!dialog.current.contains(active))previousFocus.current=active;
      dialog.current.showModal();
    }
    if(modal)dialog.current?.querySelector<HTMLElement>('input:not([disabled]),button.primary')?.focus();
    if(!modal && dialog.current?.open){
      dialog.current.close();
      const target=previousFocus.current?.isConnected?previousFocus.current:document.querySelector<HTMLElement>('.auth-trigger,.auth-entry-actions button');
      target?.focus();
    }
  },[modal]);

  async function start(login=false) {
    setBusy(true);setError('');
    try {
      const result=validateSession(await authRequest<unknown>('guest',{method:'POST'}));
      configureCsrf(result.csrf_token);setCacheScope(result.cache_scope);setSession(result);
      if(login){setMessage('');setModal('login');}
    }catch(e){setError(e instanceof Error?e.message:'暂时无法开始体验。');}finally{setBusy(false);}
  }
  async function prepareSave() {
    setModal('save');setBusy(true);setMessage('');setSaveConfirmed(false);
    try{
      const data=await authRequest<SavePreview>('guest-save/preview',{method:'POST'});
      if(!['empty','pending'].includes(data.status)||!Number.isInteger(data.plant_count))throw new Error('保存范围返回异常，请重试。');
      setPreview(data);
      operation.current = data.operation_id && data.version ? {id:data.operation_id,version:data.version}:null;
      if(data.status==='empty'){setModal(null);await loadSession();}
    }catch(e){setMessage(e instanceof Error?e.message:'暂时无法读取保存范围。');}finally{setBusy(false);}
  }
  async function send() {
    if(!legal){setMessage('请先重新读取服务条款和隐私政策。');return;}
    if(!consent){setMessage('请先阅读并勾选服务与隐私说明。');return;}
    if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())){setMessage('请填写有效的邮箱。');return;}
    const version=generation.current;setBusy(true);setMessage('');
    try {
      const data=await authRequest<Challenge>('email/challenges',{method:'POST',body:JSON.stringify({email:email.trim(),terms_version:legal.version,privacy_version:legal.version})});
      if(version!==generation.current)return;
      if(typeof data.challenge_id!=='string'||!Number.isFinite(Date.parse(data.expires_at))||!Number.isInteger(data.resend_after_seconds))throw new Error('邮件服务返回异常，请稍后重试。');
      setChallenge(data);setCooldown(data.resend_after_seconds);setMessage('验证码已发送，请查看邮箱及垃圾邮件。');
    }catch(e){if(version===generation.current)setMessage(e instanceof Error?e.message:'邮件暂时无法发送。');}finally{setBusy(false);}
  }
  async function verify(event:React.FormEvent<HTMLFormElement>){
    event.preventDefault();
    if(!consent||!challenge){setMessage('请先同意说明并获取验证码。');return;}
    if(Date.parse(challenge.expires_at)<=Date.now()){setMessage('验证码已过期，请重新获取。');return;}
    setBusy(true);setMessage('');
    try{
      await authRequest('email/verify',{method:'POST',body:JSON.stringify({challenge_id:challenge.challenge_id,code})});
      const result=await loadSession();setCode('');setChallenge(null);
      if(result.pending_save)await prepareSave();else setModal(null);
    }catch(e){setMessage(e instanceof Error?e.message:'验证未完成，请重试。');}finally{setBusy(false);}
  }
  async function save(){
    const op=operation.current;if(!op)return;
    setBusy(true);setMessage('');
    try{
      const result=await authRequest<{status:string}>(`guest-save/${op.id}/confirm`,{method:'POST',body:JSON.stringify({version:op.version})});
      if(result.status!=='succeeded')throw new Error('保存仍在处理中，请稍后核对。');
      await loadSession();setSaveConfirmed(true);setRevision(value=>value+1);window.dispatchEvent(new Event('bloomate-progress'));
    }catch(e){
      // A lost response must not cause a second transfer or clear the original operation.
      try{
        const status=await authRequest<{status:string}>(`guest-save/${op.id}`);
        if(status.status==='succeeded'){await loadSession();setSaveConfirmed(true);setRevision(value=>value+1);window.dispatchEvent(new Event('bloomate-progress'));return;}
      }catch{/* Keep the same operation available for a user-triggered retry. */}
      setMessage(e instanceof Error?e.message:'保存未完成，体验仍保留，请重试。');
    }finally{setBusy(false);}
  }
  async function logout(){
    await authRequest('logout',{method:'POST'});
    generation.current++;configureCsrf(null);setCacheScope(null);
    try{clearPrivateCache(localStorage);}catch{/* No private UI remains mounted. */}
    setSession(null);setModal(null);setEmail('');setConsent(false);setCooldown(0);setCode('');setChallenge(null);setPreview(null);operation.current=null;setMessage('');setError('已退出，账号中的花园仍然保留。');
  }
  function erased(){
    generation.current++;configureCsrf(null);setCacheScope(null);
    try{clearPrivateCache(localStorage);sessionStorage.removeItem('bloomate-erasure-receipt');}catch{/* Unmount private UI regardless of browser storage. */}
    setSession(null);setModal(null);setEmail('');setCode('');setChallenge(null);setPreview(null);setConsent(false);setErasureReceipt(null);operation.current=null;setMessage('');setError('Bloomate账号及花园已注销。共享认证身份和其他产品账号未删除。');
  }
  function erasurePending(id:string){
    generation.current++;configureCsrf(null);setCacheScope(null);setSession(null);setErasureReceipt(id);setModal('erasure');
    try{clearPrivateCache(localStorage);sessionStorage.setItem('bloomate-erasure-receipt',id);}catch{/* Keep the visible receipt when storage is unavailable. */}
    setMessage('注销正在处理，账号访问已冻结；请保留回执，稍后查询。');
  }
  async function queryErasure(){
    if(!erasureReceipt)return;setBusy(true);setMessage('');
    try{const result=await authRequest<{status:string}>(`deletion-status/${erasureReceipt}`);if(result.status==='succeeded')erased();else setMessage('注销仍在处理中，账号访问已冻结，请稍后查询。');}
    catch(e){setMessage(e instanceof Error?e.message:'暂时无法查询，请保留回执。');}finally{setBusy(false);}
  }

  const current=session?.kind!=='none'?session:null;
  return <>
    {current ? <AuthContext.Provider value={{session:current,openLogin:()=>{setMessage('');setModal('login');},openSave:()=>void prepareSave(),openAccount:()=>setModal('account'),logout}}>
      <div key={`${current.cache_scope}:${revision}`}>{current.agreements_required?<main className="auth-welcome"><h1>请阅读更新后的条款</h1><p>你仍可导出数据或注销账号；继续使用前请先阅读并同意当前版本。</p><button onClick={()=>setModal('account')}>打开账号与隐私</button></main>:children}</div>
    </AuthContext.Provider> : <main className="auth-welcome"><div className="brand"><span>✿</span> Bloomate</div><section><p className="eyebrow">A LITTLE BEGINNING</p><h1>先种下一个小小的想法。</h1><p>可以先体验，种下第一株后，再用邮箱保存你的花园。</p>{loading?<p role="status">正在读取登录状态…</p>:<div className="auth-entry-actions"><button type="button" disabled={busy} className="primary" onClick={()=>void start()}>开始体验</button><button type="button" disabled={busy} className="outline-button" onClick={()=>void start(true)}>登录已有账号</button></div>}<p role="status">{error}</p><p className="auth-footnote">体验内容临时保存在服务端，7天到期；不会自动保存到账户。</p></section></main>}
    <dialog className="auth-dialog" ref={dialog} aria-labelledby="auth-title" onCancel={event=>{event.preventDefault();if(!busy)setModal(null);}}>
      <button className="auth-close" type="button" aria-label="关闭账号窗口" disabled={busy} onClick={()=>setModal(null)}>×</button>
      {modal==='login' && <><p className="eyebrow">SAVE YOUR GARDEN</p><h2 id="auth-title">把这份开始留下来。</h2><p>邮箱验证码登录，首次验证自动注册。</p>
        <form onSubmit={event=>void verify(event)}><label>邮箱<input type="email" autoComplete="email" required maxLength={254} disabled={busy} value={email} onChange={event=>{generation.current++;setEmail(event.target.value);setCode('');setChallenge(null);setMessage('');}}/></label>
        <label>邮箱验证码<div className="auth-code-row"><input inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} required disabled={busy} value={code} onChange={event=>{setCode(event.target.value);setMessage('');}}/><button type="button" disabled={busy||cooldown>0||!legal} onClick={()=>void send()}>{busy?'请稍候…':cooldown?`${cooldown}秒后重发`:'获取验证码'}</button></div></label>
        {challenge && <p className="auth-footnote">验证码有效至 {new Date(challenge.expires_at).toLocaleTimeString('zh-CN')}。</p>}
        <label className="auth-consent"><input type="checkbox" checked={consent} onChange={event=>{setConsent(event.target.checked);setMessage('');}}/><span>我已阅读并同意<a href="/terms" target="_blank" rel="noopener noreferrer">服务条款</a>与<a href="/privacy" target="_blank" rel="noopener noreferrer">隐私政策</a>。园丁记忆默认关闭。</span></label>
        {legal&&!legal.ready&&<p className="auth-note">运营者公示信息待补齐，当前仅供本机受控验收。</p>}
        {legalError&&<p role="alert">{legalError} <button type="button" onClick={()=>void reloadLegal()}>重新读取条款</button></p>}
        {!session?.email_enabled && <p className="auth-note">邮箱登录服务尚未配置。你可以继续体验；此时无法发送真实验证码。</p>}
        <p className="auth-message" role="status">{message}</p><button className="primary" type="submit" disabled={busy||!challenge||!consent}>{busy?'正在验证…':'验证邮箱并继续'}</button></form></>}
      {modal==='save' && <><p className="eyebrow">KEEP IT GROWING</p><h2 id="auth-title">{saveConfirmed?'花园已经保存。':'把这次体验加入你的账号？'}</h2>{saveConfirmed?<><p>植物、记录及本次园丁对话已保存到账户，下次用同一个邮箱登录即可继续。</p><button type="button" className="primary" onClick={()=>{setModal(null);window.location.reload();}}>回到我的花园</button></>:<><p>确认后追加到 {preview?.account_masked||session?.email_masked}，不会覆盖原有花园。</p>{preview?<div className="auth-save-summary">本次包含 <strong>{preview.plant_count}</strong> 株植物，以及已确认的计划、打卡、成长记录和本次园丁对话。</div>:<p role="status">{busy?'正在核对保存范围…':'暂时无法读取保存范围。'}</p>}<p className="auth-message" role="status">{message}</p><button type="button" className="primary" disabled={busy||!operation.current} onClick={()=>void save()}>{busy?'正在保存…':message?'重试保存':'确认保存我的花园'}</button><button className="auth-later" type="button" disabled={busy} onClick={()=>setModal(null)}>稍后保存</button>{!operation.current&&!busy&&<button type="button" onClick={()=>void prepareSave()}>重新读取保存范围</button>}</>}</>}
      {modal==='account'&&session?.kind==='account'&&<AccountPrivacyPanel session={session} onDeleted={erased} onPending={erasurePending} onBusyChange={setBusy} onAccepted={()=>{void loadSession().then(()=>setRevision(v=>v+1));}}/>}
      {modal==='erasure'&&<><h2 id="auth-title">注销处理进度</h2><p>请保留回执，刷新页面后仍可在此浏览器查询。</p><code className="erasure-receipt">{erasureReceipt}</code><p className="auth-message" role="status">{message}</p><button className="primary" disabled={busy} onClick={()=>void queryErasure()}>查询注销结果</button></>}
    </dialog>
    <nav className="legal-footer" aria-label="服务与隐私"><a href="/terms" target="_blank" rel="noopener noreferrer">服务条款</a><a href="/privacy" target="_blank" rel="noopener noreferrer">隐私政策</a></nav>
  </>;
}
