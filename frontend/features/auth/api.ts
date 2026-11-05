export type Session = {
  kind: 'none' | 'guest' | 'account'; email_masked: string | null;
  guest_expires_at: string | null; csrf_token: string | null;
  cache_scope: string | null; pending_save: boolean; email_enabled: boolean;
  agreements_required?: boolean;
};
export type SavePreview = {status: 'empty' | 'pending'; operation_id?: string;
  account_masked?: string; plant_count: number; version?: string; expires_at?: string};

let csrfToken: string | null = null;
export function configureCsrf(value: string | null) { csrfToken = value; }

export async function apiFetch(input: RequestInfo | URL, options: RequestInit = {}) {
  const headers = new Headers(options.headers);
  const method = (options.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
  const target = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  const origin = typeof window === 'undefined' ? 'http://localhost' : window.location.origin;
  const url = new URL(target, origin);
  const ownApi = url.origin === origin && url.pathname.startsWith('/api/');
  if (ownApi && !['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken) headers.set('X-Bloomate-CSRF', csrfToken);
  const controller = options.signal ? null : new AbortController();
  const timer = controller ? setTimeout(()=>controller.abort(),45000) : null;
  let response: Response;
  try {
    response = await globalThis.fetch(input, {...options, signal: options.signal || controller?.signal,
      headers, credentials: ownApi ? 'same-origin' : options.credentials, cache: ownApi ? 'no-store' : options.cache});
  } finally { if(timer)clearTimeout(timer); }
  if (ownApi && response.status === 401 && typeof window !== 'undefined') window.dispatchEvent(new Event('bloomate-session-expired'));
  return response;
}

export async function authRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await apiFetch('/api/v1/auth/' + path, {
    ...options, headers: {'Content-Type': 'application/json', ...options.headers},
  });
  let data: unknown;
  try { data = await response.json(); } catch { throw new Error('服务暂时无法连接，请稍后重试。'); }
  if (!response.ok) {
    const error = data as {error?: {message?: unknown}};
    throw new Error(typeof error?.error?.message === 'string' ? error.error.message : '操作未完成，请稍后再试。');
  }
  return data as T;
}

export function validateSession(value: unknown): Session {
  const s = value as Partial<Session> | null;
  if (!s || !['none','guest','account'].includes(s.kind || '') || typeof s.pending_save !== 'boolean' || typeof s.email_enabled !== 'boolean'
    || (s.agreements_required!==undefined&&typeof s.agreements_required!=='boolean')
    || (s.kind !== 'none' && (typeof s.csrf_token !== 'string' || typeof s.cache_scope !== 'string'))
    || ![s.email_masked,s.guest_expires_at].every(item => item === null || typeof item === 'string')) {
    throw new Error('登录状态暂时无法读取，请重试。');
  }
  return s as Session;
}

export function clearPrivateCache(storage: Storage) {
  for (const key of Object.keys(storage)) if (key.startsWith('bloomate')) storage.removeItem(key);
}

export function setCacheScope(scope: string | null) {
  if (typeof window === 'undefined') return;
  try {
    const previous = sessionStorage.getItem('bloomate-cache-scope');
    if (previous !== scope) clearPrivateCache(localStorage);
    if (scope) sessionStorage.setItem('bloomate-cache-scope', scope);
    else sessionStorage.removeItem('bloomate-cache-scope');
  } catch { /* Storage restrictions must not bypass authentication or block server data. */ }
}
