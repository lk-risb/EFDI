import {useAuth} from '@/store/auth'

const BASE = ''  // same origin in prod; Vite proxy in dev

// The refresh cookie is single-use and rotated on every /auth/refresh call.
// Share one in-flight refresh across concurrent callers so a second 401 does
// not try to reuse the just-rotated cookie and get rejected.
let refreshPromise: Promise<string | null> | null = null

async function refreshToken(): Promise<string | null> {
  if (refreshPromise) return refreshPromise
  refreshPromise = useAuth.getState().restoreSession()
  try {
    return await refreshPromise
  } finally {
    refreshPromise = null
  }
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { token, clear } = useAuth.getState()
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)

  let res = await fetch(`${BASE}${path}`, { ...init, headers, credentials: 'include' })

  if (res.status === 401) {
    const newToken = await refreshToken()
    if (!newToken) {
      clear()
      window.location.href = '/login'
      return res
    }
    headers.set('Authorization', `Bearer ${newToken}`)
    res = await fetch(`${BASE}${path}`, { ...init, headers, credentials: 'include' })
  }

  return res
}

export function errorMessage(e: unknown): string {
  const message = e instanceof Error ? e.message : String(e)
  return message.trim() ? message : 'Operation failed'
}

// Best available detail for a non-ok response: backend `detail`, else the
// HTTP reason phrase, else the raw status code — `??` alone lets an empty
// `statusText` (common under HTTP/2, which has no reason phrases) slip
// through as '', producing an undiagnosable "Operation failed" toast with
// no indication of whether the backend even received the request.
//
// FastAPI's own request-body validation (a field missing/wrong type on a
// Pydantic model, before a route handler ever runs — e.g. ConfigFields)
// returns `detail` as an ARRAY of {loc, msg, type} objects, not a string.
// The plain `typeof detail === 'string'` check below used to miss this
// entirely and fall through to the generic "Request failed (HTTP 422)" —
// technically correct, but useless for figuring out which field was wrong.
export function errorDetail(body: { detail?: unknown } | null | undefined, res: Response): string {
  const detail = body?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail) && detail.length) {
    const parts = detail.map(item => {
      if (item && typeof item === 'object' && 'msg' in item) {
        const loc = Array.isArray((item as { loc?: unknown }).loc)
          ? (item as { loc: unknown[] }).loc.filter(p => p !== 'body').join('.')
          : ''
        return loc ? `${loc}: ${(item as { msg: unknown }).msg}` : String((item as { msg: unknown }).msg)
      }
      return typeof item === 'string' ? item : JSON.stringify(item)
    })
    if (parts.join('').trim()) return parts.join('; ')
  }
  return res.statusText || `Request failed (HTTP ${res.status})`
}

export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await apiFetch(path, init)
  if (!res.ok) {
    const err = await res.json().catch(() => null)
    throw new Error(errorDetail(err, res))
  }
  return res.json()
}
