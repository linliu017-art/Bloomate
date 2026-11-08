import {test} from 'node:test';
import assert from 'node:assert/strict';
import {proxyApi} from '../server/api-proxy.ts';

test('runtime proxy reads current upstream and secret, strips platform credentials, and preserves cookies',async()=>{
  const original=globalThis.fetch;
  const oldUrl=process.env.BLOOMATE_BACKEND_URL,oldSecret=process.env.BLOOMATE_PROXY_SHARED_SECRET;
  try {
    process.env.BLOOMATE_BACKEND_URL='https://backend.example.com';
    process.env.BLOOMATE_PROXY_SHARED_SECRET='test-proxy-secret';
    globalThis.fetch=async(url,options)=>{
      assert.equal(String(url),'https://backend.example.com/api/v1/auth/guest');
      assert.equal(options.headers.get('x-cloudbase-context'),null);
      assert.equal(options.headers.get('x-client-ip'),'203.0.113.2');
      assert.equal(options.headers.get('x-proxy-secret'),'test-proxy-secret');
      assert.equal(options.headers.get('origin'),'https://web.example.com');
      const headers=new Headers({'content-type':'application/json'});
      headers.append('set-cookie','one=1; HttpOnly');headers.append('set-cookie','two=2; Secure');
      return new Response('{}',{status:201,headers});
    };
    const response=await proxyApi(new Request('https://web.example.com/api/v1/auth/guest',{
      method:'POST',headers:{origin:'https://web.example.com','x-cloudbase-context':'private',
        'x-envoy-external-address':'203.0.113.2','x-client-ip':'attacker','x-proxy-secret':'attacker'}}));
    assert.equal(response.status,201);assert.equal(response.headers.getSetCookie().length,2);
    process.env.BLOOMATE_BACKEND_URL='https://second.example.com';
    globalThis.fetch=async(url)=>{assert.equal(new URL(url).hostname,'second.example.com');throw new Error('private');};
    const failed=await proxyApi(new Request('https://web.example.com/api/v1/garden'));
    assert.equal(failed.status,503);assert.equal((await failed.json()).error.code,'UPSTREAM_UNAVAILABLE');
    process.env.BLOOMATE_BACKEND_URL='http://public.example.com';
    assert.equal((await proxyApi(new Request('https://web.example.com/api/v1/garden'))).status,503);
  } finally {
    globalThis.fetch=original;
    if(oldUrl===undefined)delete process.env.BLOOMATE_BACKEND_URL;else process.env.BLOOMATE_BACKEND_URL=oldUrl;
    if(oldSecret===undefined)delete process.env.BLOOMATE_PROXY_SHARED_SECRET;else process.env.BLOOMATE_PROXY_SHARED_SECRET=oldSecret;
  }
});
