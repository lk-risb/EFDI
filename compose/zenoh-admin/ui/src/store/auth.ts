import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface AuthState {
  token: string | null
  role: string | null
  username: string | null
  setToken: (token: string, role: string, username: string) => void
  clear: () => void
}

export const useAuth = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      role: null,
      username: null,
      setToken: (token, role, username) => set({ token, role, username }),
      clear: () => set({ token: null, role: null, username: null }),
    }),
    { name: 'zenoh-admin-auth' }
  )
)
