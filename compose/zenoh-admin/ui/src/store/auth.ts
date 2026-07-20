import { create } from 'zustand'

interface AuthState {
  token: string | null
  role: string | null
  username: string | null
  authProvider: string | null
  setToken: (token: string, role: string, username: string, authProvider?: string) => void
  clear: () => void
  restoreSession: () => Promise<string | null>
}

// Deliberately NOT persisted (no zustand `persist` middleware): the bearer
// token lives in memory only, so it never sits in localStorage where any
// future XSS, malicious extension, or compromised dependency could read it
// after the tab closes. Session survives a reload via restoreSession(),
// which exchanges the httpOnly refresh_token cookie (never touches JS) for a
// fresh access token on app boot — see main.tsx.
export const useAuth = create<AuthState>()((set, get) => ({
  token: null,
  role: null,
  username: null,
  authProvider: null,
  setToken: (token, role, username, authProvider = 'local') => set({ token, role, username, authProvider }),
  clear: () => set({ token: null, role: null, username: null, authProvider: null }),
  restoreSession: async () => {
    try {
      const res = await fetch('/auth/refresh', { method: 'POST', credentials: 'include' })
      if (!res.ok) return null
      const data = await res.json()
      const payload = JSON.parse(atob(data.access_token.split('.')[1]))
      get().setToken(data.access_token, payload.role, payload.username, payload.auth_provider)
      return data.access_token
    } catch {
      return null
    }
  },
}))
