import {test, afterEach} from 'node:test';
import assert from 'node:assert/strict';
import {apiFetch, authRequest, configureCsrf, validateSession, clearPrivateCache} from '../features/auth/api.ts';

const originalFetch=globalThis.fetch;
afterEach(()=>{globalThis.fetch=originalFetch;configureCsrf(null);});

test('same-origin mutations carry CSRF, credentials and no shared cache',async()=>{
  configureCsrf('test-csrf');
  let seen;
  globalThis.fetch=async(_input,options)=>{seen=options;return new Response('{}');};
  await apiFetch('/api/v1/plants/a/actions/0',{method:'PUT',body:'{}'});
  assert.equal(seen.headers.get('X-Bloomate-CSRF'),'test-csrf');
  assert.equal(seen.credentials,'same-origin');assert.equal(seen.cache,'no-store');
});
test('never forwards the CSRF credential to external APIs',async()=>{
  configureCsrf('test-csrf');let seen;
  globalThis.fetch=async(_input,options)=>{seen=options;return new Response('{}');};
  await apiFetch('https://outside.invalid/api/v1/log',{method:'POST'});
  assert.equal(seen.headers.has('X-Bloomate-CSRF'),false);
});
test('logout clears the in-memory CSRF value for subsequent mutations',async()=>{
  configureCsrf('old-csrf');configureCsrf(null);let seen;
  globalThis.fetch=async(_input,options)=>{seen=options;return new Response('{}');};
  await apiFetch('/api/v1/auth/guest',{method:'POST'});
  assert.equal(seen.headers.has('X-Bloomate-CSRF'),false);
});
test('provider-not-configured error stays an error, never a sent state',async()=>{
  globalThis.fetch=async()=>new Response(JSON.stringify({error:{code:'AUTH_NOT_CONFIGURED',message:'邮箱未配置'}}),{status:503});
  await assert.rejects(authRequest('email/challenges',{method:'POST'}),/邮箱未配置/);
});
test('network or malformed response cannot masquerade as authentication success',async()=>{
  globalThis.fetch=async()=>new Response('bad gateway',{status:502});
  await assert.rejects(authRequest('session'),/无法连接/);
  assert.throws(()=>validateSession({kind:'account',pending_save:false,email_enabled:true,email_masked:'a***@x.com',guest_expires_at:null}),/无法读取/);
  assert.throws(()=>validateSession({kind:'admin'}),/无法读取/);
});
test('clears only Bloomate private task and draft keys, leaving unrelated app data',()=>{
  const storage={'bloomate-active-job':'private-job','bloomate-memory-draft':'private-text','unrelated':'keep'};
  Object.defineProperty(storage,'removeItem',{value:key=>delete storage[key]});
  clearPrivateCache(storage);
  assert.deepEqual(Object.keys(storage),['unrelated']);assert.equal(storage.unrelated,'keep');
});
