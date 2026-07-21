import { createFileRoute, redirect } from '@tanstack/react-router'
import { useAuth } from '@/store/auth'

export const Route = createFileRoute('/topology')({
  beforeLoad: () => {
    if (!useAuth.getState().token) throw redirect({ to: '/login' })
    throw redirect({ to: '/network' })
  },
})
