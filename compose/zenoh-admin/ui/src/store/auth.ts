import { create } from 'zustand'

interface AuthState {
  token: string | null
  role: string | null
  username: string | null
  setToken: (token: string, role: string, username: string) => void
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
  setToken: (token, role, username) => set({ token, role, username }),
  clear: () => set({ token: null, role: null, username: null }),
  restoreSession: async () => {
    try {
      const res = await fetch('/auth/refresh', { method: 'POST', credentials: 'include' })
      if (!res.ok) return null
      const data = await res.json()
      const payload = JSON.parse(atob(data.access_token.split('.')[1]))
      get().setToken(data.access_token, payload.role, payload.username)
      return data.access_token
    } catch {
      return null
    }
  },
}))
