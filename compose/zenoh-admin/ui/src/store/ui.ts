import {create} from 'zustand'
import {persist} from 'zustand/middleware'

interface UiState {
  rowAnimations: boolean
  denseRows: boolean
  refreshIntervalMs: number
  setRowAnimations: (value: boolean) => void
  setDenseRows: (value: boolean) => void
  setRefreshIntervalMs: (value: number) => void
}

const DEFAULT_STATE = {
  rowAnimations: true,
  denseRows: false,
  refreshIntervalMs: 5000,
}

export const useUiSettings = create<UiState>()(
  persist(
    (set) => ({
      ...DEFAULT_STATE,
      setRowAnimations: (value) => set({ rowAnimations: value }),
      setDenseRows: (value) => set({ denseRows: value }),
      setRefreshIntervalMs: (value) => set({ refreshIntervalMs: value }),
    }),
    { name: 'zenoh-admin-ui' }
  )
)
