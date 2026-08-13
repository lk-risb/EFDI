import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { CheckCircle2, Clock3, RotateCw, ShieldAlert } from 'lucide-react'
import { Layout } from '@/components/Layout'
import { PageHeader } from '@/components/PageHeader'
import { HudCorners } from '@/components/HudCorners'
import { StatusPill } from '@/components/StatusPill'
import { apiJson, errorMessage } from '@/lib/api'
import { notify } from '@/lib/notify'
import { useAuth } from '@/store/auth'

export const Route = createFileRoute('/changes')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'admin' && role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: ChangesPage,
})

interface ConfigRevision {
  id: string
  target_namespace: string
  version: number
  source: string
  state: string
  config_sha256: string
  detail: string | null
  created_at: string
  completed_at: string | null
}

function stateTone(state: string): 'ok' | 'warn' | 'bad' {
  if (state === 'applied') return 'ok'
  if (state === 'pending' || state === 'validating') return 'warn'
  return 'bad'
}

function ChangesPage() {
  const [revisions, setRevisions] = useState<ConfigRevision[] | null>(null)

  async function load() {
    try {
      setRevisions(await apiJson<ConfigRevision[]>('/api/config-revisions'))
    } catch (error) {
      notify.error(errorMessage(error))
    }
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 5000)
    return () => clearInterval(timer)
  }, [])

  const pending = revisions?.filter(item => item.state === 'pending' || item.state === 'validating').length ?? 0
  const failed = revisions?.filter(item => !['pending', 'validating', 'applied'].includes(item.state)).length ?? 0

  return (
    <Layout>
      <div className="mx-auto max-w-7xl p-6">
        <PageHeader title="Configuration Changes" count={revisions?.length} countLabel="revisions" actions={
          <button onClick={load} className="flex items-center gap-2 rounded-md border border-zinc-300 px-3 py-2 text-sm text-zinc-600 dark:border-white/10 dark:text-zinc-300">
            <RotateCw size={14} /> Refresh
          </button>
        } />

        <div className="mb-5 grid gap-3 sm:grid-cols-3">
          <div className="hud-card hud-glass border border-zinc-200 dark:border-white/10 p-4"><p className="hud-label text-[10px] text-zinc-500">Pending</p><p className="mt-2 flex items-center gap-2 text-2xl font-display"><Clock3 size={18} className="text-amber-500" />{pending}</p></div>
          <div className="hud-card hud-glass border border-zinc-200 dark:border-white/10 p-4"><p className="hud-label text-[10px] text-zinc-500">Applied</p><p className="mt-2 flex items-center gap-2 text-2xl font-display"><CheckCircle2 size={18} className="text-green-500" />{revisions?.filter(item => item.state === 'applied').length ?? 0}</p></div>
          <div className="hud-card hud-glass border border-zinc-200 dark:border-white/10 p-4"><p className="hud-label text-[10px] text-zinc-500">Attention</p><p className="mt-2 flex items-center gap-2 text-2xl font-display"><ShieldAlert size={18} className="text-red-500" />{failed}</p></div>
        </div>

        <div className="hud-frame relative overflow-hidden rounded-md border border-zinc-200 bg-white dark:border-white/10 dark:bg-[#0c0c0e]">
          <HudCorners />
          <div className="grid grid-cols-[minmax(10rem,1.2fr)_7rem_9rem_minmax(9rem,1fr)] gap-3 border-b border-zinc-200 px-4 py-3 text-[10px] text-zinc-500 dark:border-white/10 hud-label">
            <span>Managed router</span><span>State</span><span>Delivery</span><span>Revision</span>
          </div>
          {revisions === null ? (
            <p className="p-5 text-sm text-zinc-500">Loading change ledger…</p>
          ) : revisions.length === 0 ? (
            <p className="p-5 text-sm text-zinc-500">No configuration revisions have been created.</p>
          ) : revisions.map(revision => (
            <div key={revision.id} className="grid grid-cols-[minmax(10rem,1.2fr)_7rem_9rem_minmax(9rem,1fr)] gap-3 border-b border-zinc-100 px-4 py-3 text-xs last:border-0 dark:border-white/5">
              <div className="min-w-0"><p className="truncate font-mono text-zinc-800 dark:text-zinc-200">{revision.target_namespace}</p><p className="mt-1 text-zinc-500">{new Date(revision.created_at).toLocaleString()}</p>{revision.detail && <p className="mt-1 truncate text-red-500" title={revision.detail}>{revision.detail}</p>}</div>
              <div><StatusPill text={revision.state} tone={stateTone(revision.state)} /></div>
              <span className="text-zinc-600 dark:text-zinc-400">{revision.source}</span>
              <div className="min-w-0 font-mono text-zinc-500"><p>v{revision.version}</p><p className="truncate" title={revision.config_sha256}>{revision.config_sha256.slice(0, 16)}…</p></div>
            </div>
          ))}
        </div>
      </div>
    </Layout>
  )
}
