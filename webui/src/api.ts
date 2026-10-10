export class ApiError extends Error {
  status: number
  busy: boolean
  missing: boolean
  payload: any
  constructor(status: number, message: string, payload: any) {
    super(message)
    this.status = status
    this.busy = status === 423 || !!payload?.busy
    this.missing = !!payload?.missing
    this.payload = payload
  }
}

type Params = Record<string, string | number | boolean | null | undefined>

function qs(params?: Params): string {
  if (!params) return ''
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v))
  const s = p.toString()
  return s ? `?${s}` : ''
}

async function request<T>(method: string, path: string, body?: unknown, params?: Params): Promise<T> {
  const init: RequestInit = { method, headers: { 'X-StockData-UI': '1' } }
  if (body !== undefined) {
    ;(init.headers as Record<string, string>)['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }
  let res: Response
  try {
    res = await fetch(`/api${path}${qs(params)}`, init)
  } catch {
    throw new ApiError(0, 'The backend is not reachable. Is the Stock Data server running?', null)
  }
  const text = await res.text()
  let data: any = null
  try { data = text ? JSON.parse(text) : null } catch { data = { error: text.slice(0, 300) } }
  if (!res.ok) {
    let msg = data?.error
    if (!msg && Array.isArray(data?.detail)) msg = data.detail.map((d: any) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ')
    throw new ApiError(res.status, msg || `Request failed (${res.status})`, data)
  }
  return data as T
}

export const api = {
  get: <T = any>(path: string, params?: Params) => request<T>('GET', path, undefined, params),
  post: <T = any>(path: string, body: unknown = {}, params?: Params) => request<T>('POST', path, body, params),
  put: <T = any>(path: string, body: unknown = {}) => request<T>('PUT', path, body),
  del: <T = any>(path: string) => request<T>('DELETE', path),
  url: (path: string, params?: Params) => `/api${path}${qs(params)}`,
}
