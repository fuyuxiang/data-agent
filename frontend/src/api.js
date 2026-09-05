const defaultHeaders = () => {
  const workspaceId = localStorage.getItem('meridian-workspace') || 'default';
  return { 'X-Workspace-Id': workspaceId };
};

export class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

export async function api(path, options = {}) {
  const headers = { ...defaultHeaders(), ...(options.headers || {}) };
  const init = { ...options, headers };
  if (options.body && !(options.body instanceof FormData) && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  const type = response.headers.get('content-type') || '';
  const payload = type.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok || (payload && payload.ok === false)) {
    throw new ApiError(payload?.error || `请求失败 (${response.status})`, response.status, payload);
  }
  return payload;
}

export async function stream(path, payload, onEvent, signal) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { ...defaultHeaders(), 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok || !response.body) {
    let message = `请求失败 (${response.status})`;
    try { message = (await response.json()).error || message; } catch { /* no-op */ }
    throw new ApiError(message, response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop() || '';
    for (const block of blocks) {
      let event = 'message';
      const data = [];
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        if (line.startsWith('data:')) data.push(line.slice(5).trim());
      }
      if (data.length) {
        let parsed;
        try { parsed = JSON.parse(data.join('\n')); } catch { parsed = { content: data.join('\n') }; }
        await onEvent(event, parsed);
      }
    }
    if (done) break;
  }
}

export function withWorkspace(path, workspaceId) {
  const url = new URL(path, location.origin);
  url.searchParams.set('workspace_id', workspaceId || localStorage.getItem('meridian-workspace') || 'default');
  return `${url.pathname}${url.search}`;
}

