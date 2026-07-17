import { createFileRoute, redirect } from '@tanstack/react-router'
import { useState } from 'react'
import { Layout } from '@/components/Layout'
import { PageHeader } from '@/components/PageHeader'
import { apiFetch, errorMessage } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { notify } from '@/lib/notify'
import { Plus, X, Download } from 'lucide-react'
import { HudCorners } from '@/components/HudCorners'

export const Route = createFileRoute('/publish-builder')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: PublishBuilderPage,
})

interface Row {
  topic: string
  message: string
  count: number
  interval_s: number
}

function emptyRow(): Row {
  return { topic: '', message: '', count: 1, interval_s: 1.0 }
}

const inputClass = "w-full px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#1a1a1d] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-50"

function PublishBuilderPage() {
  const [routerEndpoint, setRouterEndpoint] = useState('')
  const [clientCn, setClientCn] = useState('')
  const [certDir, setCertDir] = useState('')
  const [rows, setRows] = useState<Row[]>([emptyRow()])
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('')

  function updateRow(i: number, patch: Partial<Row>) {
    setRows(rs => rs.map((r, idx) => idx === i ? { ...r, ...patch } : r))
  }

  function addRow() {
    setRows(rs => [...rs, emptyRow()])
  }

  function removeRow(i: number) {
    setRows(rs => rs.filter((_, idx) => idx !== i))
  }

  function validate(): string | null {
    if (!routerEndpoint.trim()) return 'Router endpoint is required'
    if (!clientCn.trim()) return 'Client name (cert CN) is required'
    if (!certDir.trim()) return 'Cert directory is required'
    if (rows.length === 0) return 'At least one row is required'
    for (const r of rows) {
      if (!r.topic.trim()) return 'Every row needs a topic'
      if (r.count < 1) return 'Count must be at least 1'
      if (r.interval_s < 0) return 'Interval must be 0 or more'
    }
    return null
  }

  async function handleGenerate() {
    const validationError = validate()
    if (validationError) { setError(validationError); return }
    setError('')
    setGenerating(true)
    try {
      const res = await apiFetch('/api/publish-script', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          router_endpoint: routerEndpoint,
          client_cn: clientCn,
          cert_dir: certDir,
          rows,
        }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(err.detail ?? res.statusText)
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'efdi_publish_script.py'
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      notify.success('Script generated')
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setGenerating(false)
    }
  }

  return (
    <Layout>
      <div className="p-6 max-w-4xl">
        <PageHeader title="Publish Script Builder" />
        <p className="text-sm text-zinc-500 mb-6">
          Add rows, then generate a ready-to-run publish script. Nothing is sent from here —
          the script only ever downloads. Message boxes accept multi-line content (JSON, XML,
          any structured payload).
        </p>

        <div className="hud-frame relative hud-enter mb-6">
          <HudCorners />
          <div className="rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-5 space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div className="space-y-1">
              <label className="text-sm text-zinc-700 dark:text-zinc-300">Router endpoint</label>
              <input className={inputClass} value={routerEndpoint} onChange={e => setRouterEndpoint(e.target.value)} placeholder="tls/<current-router>:7447" />
            </div>
            <div className="space-y-1">
              <label className="text-sm text-zinc-700 dark:text-zinc-300">Client name (cert CN)</label>
              <input className={inputClass} value={clientCn} onChange={e => setClientCn(e.target.value)} placeholder="acme" />
            </div>
            <div className="space-y-1">
              <label className="text-sm text-zinc-700 dark:text-zinc-300">Cert directory</label>
              <input className={inputClass} value={certDir} onChange={e => setCertDir(e.target.value)} placeholder="/home/acme/efdi-certs" />
            </div>
          </div>
          </div>
        </div>

        <div className="space-y-3 mb-4">
          {rows.map((row, i) => (
            <div key={i} className="hud-frame relative hud-card rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-4">
              <HudCorners />
              <div className="grid grid-cols-1 sm:grid-cols-[1fr_2fr_auto_auto_auto] gap-3 items-start">
                <div className="space-y-1">
                  <label className="text-xs text-zinc-500 uppercase tracking-wide">Topic</label>
                  <input className={inputClass} value={row.topic} onChange={e => updateRow(i, { topic: e.target.value })} placeholder="LTU/CISB/acme/status" />
                </div>
                <div className="space-y-1">
                  <label className="text-xs text-zinc-500 uppercase tracking-wide">Message</label>
                  <textarea className={`${inputClass} h-20`} value={row.message} onChange={e => updateRow(i, { message: e.target.value })} placeholder="hello world" />
                </div>
                <div className="space-y-1 w-20">
                  <label className="text-xs text-zinc-500 uppercase tracking-wide">Count</label>
                  <input type="number" min={1} className={inputClass} value={row.count} onChange={e => updateRow(i, { count: Number(e.target.value) })} />
                </div>
                <div className="space-y-1 w-24">
                  <label className="text-xs text-zinc-500 uppercase tracking-wide">Interval (s)</label>
                  <input type="number" min={0} step={0.1} className={inputClass} value={row.interval_s} onChange={e => updateRow(i, { interval_s: Number(e.target.value) })} />
                </div>
                <button onClick={() => removeRow(i)} disabled={rows.length === 1}
                  className="mt-6 p-2 rounded-md text-zinc-500 hover:text-red-600 dark:hover:text-red-400 hover:bg-zinc-200/50 dark:hover:bg-white/[0.05] disabled:opacity-30 transition-colors">
                  <X size={16} />
                </button>
              </div>
            </div>
          ))}
        </div>

        <div className="flex items-center gap-2 mb-6">
          <button onClick={addRow} className="flex items-center gap-2 px-3 py-2 rounded-md text-sm text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-white hover:bg-zinc-200/50 dark:hover:bg-white/[0.05] transition-colors">
            <Plus size={14} /> Add row
          </button>
        </div>

        {error && <p className="text-red-600 dark:text-red-400 text-sm mb-4">{error}</p>}

        <button onClick={handleGenerate} disabled={generating}
          className="flex items-center gap-2 px-4 py-2 bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm rounded-md transition-colors disabled:opacity-50">
          <Download size={14} /> {generating ? 'Generating…' : 'Generate Script'}
        </button>
      </div>
    </Layout>
  )
}
