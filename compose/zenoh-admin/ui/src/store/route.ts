import { create } from 'zustand'

// Published by Layout.tsx (which safely reads the router's current location
// from inside the router tree) so components rendered outside routing
// entirely can know the current path without touching the router
// instance/context directly.
interface RouteState {
  pathname: string
  setPathname: (p: string) => void
}

export const useRoute = create<RouteState>()((set) => ({
  pathname: '',
  setPathname: (pathname) => set({ pathname }),
}))
