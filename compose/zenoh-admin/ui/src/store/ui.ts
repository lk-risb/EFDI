import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface UiState {
  showCorners: boolean
  rowAnimations: boolean
  denseRows: boolean
  refreshIntervalMs: number
  setShowCorners: (value: boolean) => void
  setRowAnimations: (value: boolean) => void
  setDenseRows: (value: boolean) => void
  setRefreshIntervalMs: (value: number) => void
}

const DEFAULT_STATE = {
  showCorners: true,
  rowAnimations: true,
  denseRows: false,
  refreshIntervalMs: 5000,
}

export const useUiSettings = create<UiState>()(
  persist(
    (set) => ({
      ...DEFAULT_STATE,
      setShowCorners: (value) => set({ showCorners: value }),
      setRowAnimations: (value) => set({ rowAnimations: value }),
      setDenseRows: (value) => set({ denseRows: value }),
      setRefreshIntervalMs: (value) => set({ refreshIntervalMs: value }),
    }),
    { name: 'zenoh-admin-ui' }
  )
)

