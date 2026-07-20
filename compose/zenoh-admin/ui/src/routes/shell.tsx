import { createFileRoute, redirect } from '@tanstack/react-router'
import { Layout } from '@/components/Layout'
import { useAuth } from '@/store/auth'

export const Route = createFileRoute('/shell')({
  beforeLoad: () => {
    const { token, role, authProvider } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'superadmin' || authProvider !== 'local') throw redirect({ to: '/' })
  },
  component: ShellPage,
})

function ShellPage() {
  return <Layout>{null}</Layout>
}
