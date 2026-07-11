import { create } from 'zustand'

export interface Notification {
  id: string
  type: 'success' | 'error'
  message: string
  timestamp: number
}

interface NotificationState {
  items: Notification[]
  push: (type: Notification['type'], message: string) => void
  clear: () => void
}

const MAX_ITEMS = 20

export const useNotifications = create<NotificationState>((set) => ({
  items: [],
  push: (type, message) =>
    set((s) => ({
      items: [{ id: crypto.randomUUID(), type, message, timestamp: Date.now() }, ...s.items].slice(0, MAX_ITEMS),
    })),
  clear: () => set({ items: [] }),
}))
