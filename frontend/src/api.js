/**
 * 后端通信层。
 *
 * V1 在这里留了一个从未被调用的 stream()：真正的实时更新走的是轮询。
 * V2 直接删掉它——死代码比没有代码更贵。
 */

const CSRF_KEY = 'shuqing-csrf';
const WORKSPACE_KEY = 'shuqing-workspace';

export class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

export function workspaceId() {
  return localStorage.getItem(WORKSPACE_KEY) || 'default';
}

export function rememberWorkspace(value) {
  if (value) localStorage.setItem(WORKSPACE_KEY, value);
}

function headers(extra = {}) {
  const csrf = sessionStorage.getItem(CSRF_KEY) || '';
  return {
    'X-Workspace-Id': workspaceId(),
    ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
    ...extra,
  };
}

export async function api(path, options = {}) {
  const init = { ...options, headers: headers(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData) && typeof options.body !== 'string') {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  const type = response.headers.get('content-type') || '';
  const payload = type.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok || (payload && payload.ok === false)) {
    throw new ApiError(payload?.error || `请求失败 (${response.status})`, response.status, payload);
  }
  if (payload?.csrf_token) sessionStorage.setItem(CSRF_KEY, payload.csrf_token);
  return payload;
}

export const get = (path) => api(path);
export const post = (path, body) => api(path, { method: 'POST', body });
export const put = (path, body) => api(path, { method: 'PUT', body });
export const patch = (path, body) => api(path, { method: 'PATCH', body });
export const remove = (path, body) => api(path, { method: 'DELETE', body });

export function upload(path, file, extra = {}) {
  const form = new FormData();
  form.append('file', file);
  Object.entries(extra).forEach(([key, value]) => form.append(key, value));
  return api(path, { method: 'POST', body: form });
}

/** 把 workspace_id 附到查询串上，避免每个调用点都写一遍。 */
export function withWorkspace(path, id = workspaceId()) {
  const url = new URL(path, location.origin);
  url.searchParams.set('workspace_id', id);
  return `${url.pathname}${url.search}`;
}

/** 触发浏览器下载，并保持同源凭据与 CSRF 头。 */
export async function download(path, filename) {
  const response = await fetch(path, { headers: headers() });
  if (!response.ok) {
    const message = (await response.json().catch(() => ({}))).error || '下载失败';
    throw new ApiError(message, response.status);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename || path.split('/').pop() || 'download';
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
