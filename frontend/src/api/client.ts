export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/** Placeholder identity, matching the backend's X-Username header (spec 3.1). */
let username = ''

export function setUsername(value: string): void {
  username = value
}

export function getUsername(): string {
  return username
}

interface RequestInitLite {
  method?: string
  body?: unknown
}

function readDetail(payload: unknown): string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (item as { msg?: string }).msg ?? '')
        .filter(Boolean)
        .join('；')
    }
    if (detail && typeof detail === 'object' && 'message' in detail) {
      return String((detail as { message: unknown }).message)
    }
  }
  return ''
}

export async function request<T>(path: string, init: RequestInitLite = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      method: init.method ?? 'GET',
      headers: { 'Content-Type': 'application/json', 'X-Username': username },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    })
  } catch {
    throw new ApiError('无法连接服务，请检查后端是否启动', 0)
  }

  let payload: unknown = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    throw new ApiError(readDetail(payload) || `请求失败（${response.status}）`, response.status)
  }
  return payload as T
}