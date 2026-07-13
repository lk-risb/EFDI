import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Layout } from '@/components/Layout'
import { PageHeader } from '@/components/PageHeader'
import { apiJson, apiFetch, errorMessage } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { notify } from '@/lib/notify'
import { Trash2 } from 'lucide-react'
import { HudCorners } from '@/components/HudCorners'

export const Route = createFileRoute('/federation')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: FederationPage,
})

interface FederatedChild {
  id: string
  name: string
  namespace: string
  created_by: string
  created_at: string
  last_status: string | null
  last_status_version: number | null
  last_status_at: string | null
  last_status_error: string | null
}

function StatusBadge({ child }: { child: FederatedChild }) {
  if (!child.last_status) {
    return <span className="text-xs text-zinc-500">No push yet</span>
  }
  const color = child.last_status === 'ok'
    ? 'text-green-600 dark:text-green-400'
    : child.last_status === 'rejected' || child.last_status === 'rolled_back'
      ? 'text-red-600 dark:text-red-400'
      : 'text-zinc-500'
  return (
    <span className={`text-xs ${color}`} title={child.last_status_error ?? undefined}>
      {child.last_status} (v{child.last_status_version}){child.last_status_at ? ` — ${new Date(child.last_status_at).toLocaleString()}` : ''}
    </span>
  )
}

const inputClass = "w-full px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#1a1a1d] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-ring"

function FederationPage() {
  const [children, setChildren] = useState<FederatedChild[] | null>(null)
  const [name, setName] = useState('')
  const [namespace, setNamespace] = useState('')
  const [creating, setCreating] = useState(false)

  async function load() {
    try {
      const data = await apiJson<FederatedChild[]>('/api/federation')
      setChildren(data)
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  useEffect(() => {
    load()
    // Poll for status updates — Task 7's subscriber writes last_status
    // asynchronously (up to ~30s after a push, per the child's health-check
    // window), so this page can't rely on a one-shot load to show the
    // outcome. 5s matches this codebase's existing dashboard poll cadence
    // (compose/zenoh-admin/ui/src/routes/index.tsx's /api/health poll).
    const interval = setInterval(load, 5000)
    return () => clearInterval(interval)
  }, [])

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    try {
      const res = await apiFetch('/api/federation', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, namespace }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(err.detail ?? res.statusText)
      }
      setName(''); setNamespace('')
      notify.success('Federated child added')
      await load()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setCreating(false)
    }
  }

  async function handleDelete(id: string) {
    try {
      const res = await apiFetch(`/api/federation/${id}`, { method: 'DELETE' })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(err.detail ?? res.statusText)
      }
      notify.success('Federated child removed')
      await load()
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  return (
    <Layout>
      <div className="p-6 max-w-2xl">
        <PageHeader title="Federation" count={children?.length} countLabel="children" />

        <div className="hud-frame relative hud-enter mb-6">
          <HudCorners />
          <form onSubmit={handleCreate} className="rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-5 space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="space-y-1">
              <label className="text-sm text-zinc-700 dark:text-zinc-300">Name</label>
              <input className={inputClass} value={name} onChange={e => setName(e.target.value)} placeholder="City pod — Vilnius" required />
            </div>
            <div className="space-y-1">
              <label className="text-sm text-zinc-700 dark:text-zinc-300">Namespace</label>
              <input className={inputClass} value={namespace} onChange={e => setNamespace(e.target.value)} placeholder="release/vilnius" required />
            </div>
          </div>
          <button type="submit" disabled={creating}
            className="px-4 py-2 bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm rounded-md transition-colors disabled:opacity-50">
            {creating ? 'Adding…' : 'Add federated child'}
          </button>
          </form>
        </div>

        <div className="space-y-2">
          {children === null ? (
            <p className="text-sm text-zinc-500">Loading…</p>
          ) : children.length === 0 ? (
            <p className="text-sm text-zinc-500">No federated children yet.</p>
          ) : (
            children.map(c => (
              <div key={c.id} className="hud-frame relative hud-card flex items-center justify-between rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-4">
                <HudCorners />
                <div>
                  <p className="text-sm font-medium text-zinc-800 dark:text-zinc-200">{c.name}</p>
                  <p className="text-xs font-mono text-zinc-500">{c.namespace}</p>
                  <StatusBadge child={c} />
                </div>
                <button onClick={() => handleDelete(c.id)}
                  className="p-2 rounded-md text-zinc-500 hover:text-red-600 dark:hover:text-red-400 hover:bg-zinc-200/50 dark:hover:bg-white/[0.05] transition-colors">
                  <Trash2 size={16} />
                </button>
              </div>
            ))
          )}
        </div>
      </div>
    </Layout>
  )
}
