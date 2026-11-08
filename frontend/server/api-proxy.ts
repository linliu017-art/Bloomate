/** Server-only runtime proxy. Never forward platform credentials to the browser. */
export async function proxyApi(request: Request): Promise<Response> {
  const target = process.env.BLOOMATE_BACKEND_URL || 'http://127.0.0.1:8000';
  let upstream: URL;
  try {
    upstream = new URL(target);
    const local = ['127.0.0.1', 'localhost'].includes(upstream.hostname);
    if (upstream.username || upstream.password || upstream.search || upstream.hash ||
        upstream.pathname !== '/' || (upstream.protocol !== 'https:' && !(local && upstream.protocol === 'http:'))) {
      throw new Error('Invalid upstream');
    }
  } catch {
    return failure('PROXY_NOT_CONFIGURED', 503);
  }
  const incoming = new URL(request.url);
  if (!incoming.pathname.startsWith('/api/')) return failure('NOT_FOUND', 404);
  const headers = new Headers();
  // Explicit allowlist excludes x-cloudbase-context and other platform secrets.
  for (const name of ['accept', 'content-type', 'cookie', 'origin', 'sec-fetch-site', 'x-bloomate-csrf']) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const secret = process.env.BLOOMATE_PROXY_SHARED_SECRET;
  if (secret && request.headers.get('x-envoy-external-address')) {
    headers.set('x-proxy-secret', secret);
    headers.set('x-client-ip', request.headers.get('x-envoy-external-address')!);
  }
  const url = new URL(incoming.pathname + incoming.search, upstream);
  try {
    const body = ['GET', 'HEAD'].includes(request.method) ? undefined : await request.arrayBuffer();
    const response = await fetch(url, {
      method: request.method, headers, body, redirect: 'manual', cache: 'no-store',
      signal: AbortSignal.timeout(120_000),
    });
    const outgoing = new Headers({ 'Cache-Control': 'no-store', 'Vary': 'Cookie', 'X-Content-Type-Options': 'nosniff' });
    for (const name of ['content-type', 'content-disposition', 'retry-after']) {
      const value = response.headers.get(name);
      if (value) outgoing.set(name, value);
    }
    for (const cookie of response.headers.getSetCookie()) outgoing.append('set-cookie', cookie);
    return new Response(response.body, { status: response.status, headers: outgoing });
  } catch {
    return failure('UPSTREAM_UNAVAILABLE', 503);
  }
}

function failure(code: string, status: number): Response {
  return Response.json({ error: { code, message: '暂时无法连接服务，请稍后重试；输入内容仍会保留。' } },
    { status, headers: { 'Cache-Control': 'no-store' } });
}
