import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface ThemeState {
  theme: 'light' | 'dark'
  toggleTheme: () => void
}

function systemPrefersDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia('(prefers-color-scheme: dark)').matches
}

function applyThemeClass(theme: 'light' | 'dark') {
  document.documentElement.classList.toggle('dark', theme === 'dark')
}

export const useTheme = create<ThemeState>()(
  persist(
    (set, get) => ({
      theme: systemPrefersDark() ? 'dark' : 'light',
      toggleTheme: () => {
        const next = get().theme === 'dark' ? 'light' : 'dark'
        applyThemeClass(next)
        set({ theme: next })
      },
    }),
    {
      name: 'zenoh-admin-theme',
      onRehydrateStorage: () => (state) => {
        if (state) applyThemeClass(state.theme)
      },
    }
  )
)

applyThemeClass(useTheme.getState().theme)
