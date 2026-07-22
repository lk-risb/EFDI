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

export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await apiFetch(path, init)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? res.statusText)
  }
  return res.json()
}
