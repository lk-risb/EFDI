import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider, createRouter } from '@tanstack/react-router'
import { ShellSession } from './components/ShellSession'
import { routeTree } from './routeTree.gen'
import { useBranding } from './store/branding'
import { useAuth } from './store/auth'
import './index.css'
import './store/theme'

useBranding.getState().fetchBranding()

const router = createRouter({ routeTree })

// The auth token is in-memory only (not persisted — see store/auth.ts), so a
// reload always starts token-less. Try to silently re-establish the session
// from the httpOnly refresh cookie before the router's beforeLoad guards run,
// so a refresh doesn't look like a logged-out state.
useAuth.getState().restoreSession().finally(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <RouterProvider router={router} />
      <ShellSession />
    </StrictMode>
  )
})
