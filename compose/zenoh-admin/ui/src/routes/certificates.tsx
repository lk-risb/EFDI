import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Layout } from '@/components/Layout'
import { PageHeader } from '@/components/PageHeader'
import { apiJson } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { cn } from '@/lib/utils'
import { HudCorners } from '@/components/HudCorners'
import { Skeleton } from '@/components/Skeleton'

export const Route = createFileRoute('/certificates')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: CertificatesPage,
})

interface CertInfo {
  name: string
  expires_at: string
  days_remaining: number
}

function certState(daysRemaining: number): 'ok' | 'warn' | 'critical' {
  if (daysRemaining < 7) return 'critical'
  if (daysRemaining < 30) return 'warn'
  return 'ok'
}

function CertificatesPage() {
  const [certs, setCerts] = useState<CertInfo[] | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const data = await apiJson<{ system: { certs: CertInfo[] } }>('/api/health')
        if (!cancelled) setCerts(data.system.certs)
      } catch {
        // health endpoint unreachable — leave prior state
      }
    }
    load()
    const id = setInterval(load, 30000)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return (
    <Layout>
      <div className="p-6">
        <PageHeader title="Certificates" count={certs?.length} countLabel="tracked" />

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {certs === null ? (
            Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="hud-frame relative rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#0c0c0e] p-4 hud-card">
                <HudCorners />
                <Skeleton className="h-3 w-24 mb-3" />
                <Skeleton className="h-4 w-32 mb-2" />
                <Skeleton className="h-3 w-20" />
              </div>
            ))
          ) : certs.length === 0 ? (
            <p className="text-sm text-zinc-500 col-span-full">No certificates found. Generate one with scripts/gen-certs.sh &lt;namespace&gt;.</p>
          ) : (
            certs.map((c) => {
              const state = certState(c.days_remaining)
              const textClass = state === 'critical' ? 'text-red-600 dark:text-red-400' : state === 'warn' ? 'text-yellow-600 dark:text-yellow-400' : 'text-green-600 dark:text-green-400'
              const badgeClass = state === 'critical' ? 'border-red-300 dark:border-red-800 bg-red-100 dark:bg-red-500/10' : state === 'warn' ? 'border-yellow-300 dark:border-yellow-800 bg-yellow-100 dark:bg-yellow-500/10' : 'border-green-300 dark:border-green-800 bg-green-100 dark:bg-green-500/10'
              return (
                <div key={c.name} className="hud-frame relative rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#0c0c0e] p-4 hud-card">
                  <HudCorners />
                  <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">{c.name}</span>
                  <p className="font-mono text-sm text-zinc-800 dark:text-zinc-200 mt-2">{c.expires_at}</p>
                  <div className={cn('inline-flex items-center gap-1.5 mt-2 px-2 py-0.5 rounded border text-xs font-medium', badgeClass, textClass)}>
                    {c.days_remaining}d remaining
                  </div>
                </div>
              )
            })
          )}
        </div>
      </div>
    </Layout>
  )
}
