import { useAuth } from '@/store/auth'

const BASE = ''  // same origin in prod; Vite proxy in dev

async function refreshToken(): Promise<string | null> {
  const res = await fetch(`${BASE}/auth/refresh`, { method: 'POST', credentials: 'include' })
  if (!res.ok) return null
  const data = await res.json()
  const payload = JSON.parse(atob(data.access_token.split('.')[1]))
  useAuth.getState().setToken(data.access_token, payload.role, payload.username)
  return data.access_token
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

export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await apiFetch(path, init)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? res.statusText)
  }
  return res.json()
}
