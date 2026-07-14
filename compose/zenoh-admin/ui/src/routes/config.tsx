import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Layout } from '@/components/Layout'
import { apiJson, apiFetch, errorMessage } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { notify } from '@/lib/notify'
import { Save, RotateCw } from 'lucide-react'
import { HudCorners } from '@/components/HudCorners'

export const Route = createFileRoute('/config')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'admin' && role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: ConfigPage,
})

interface ConfigFields {
  mtls_port: number
  local_tcp_port: number
  fabric_endpoint: string
  partner_namespace: string
  inbound_namespace: string
  namespace_prefix: string
  verify_name_on_connect: boolean
  plugins_loading_enabled: boolean
}

const EMPTY_FIELDS: ConfigFields = {
  mtls_port: 7447,
  local_tcp_port: 7448,
  fabric_endpoint: '',
  partner_namespace: '',
  inbound_namespace: '',
  namespace_prefix: '',
  verify_name_on_connect: false,
  plugins_loading_enabled: true,
}

interface FederatedChild {
  id: string
  name: string
  namespace: string
}

function Field({ label, help, children }: { label: string; help?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <label className="text-sm text-zinc-700 dark:text-zinc-300">{label}</label>
      {children}
      {help && <p className="text-xs text-zinc-500">{help}</p>}
    </div>
  )
}

// Known-good fabric endpoints seen in this pod's history — one-click fill,
// still just host/port under the hood (scheme is always tls, never exposed).
const FABRIC_PRESETS = [
  { label: 'EFDI backbone', host: 'zenoh.efdi.netbird.efdi-backbone.net', port: 7447 },
  { label: 'Sandbox (nbio.fairytail.eu)', host: 'nbio.fairytail.eu', port: 7447 },
]

function parseFabricEndpoint(v: string): { host: string; port: number } {
  const m = v.match(/^tls\/(.+):(\d+)$/)
  return m ? { host: m[1], port: Number(m[2]) } : { host: v, port: 7447 }
}

function Toggle({ label, help, checked, disabled, onChange }: {
  label: string; help?: string; checked: boolean; disabled: boolean; onChange: (v: boolean) => void
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <p className="text-sm text-zinc-700 dark:text-zinc-300">{label}</p>
        {help && <p className="text-xs text-zinc-500 mt-0.5">{help}</p>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`shrink-0 relative w-10 h-6 rounded-full transition-colors disabled:opacity-50 ${checked ? 'bg-accent-fill' : 'bg-zinc-300 dark:bg-zinc-700'}`}
      >
        <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform ${checked ? 'translate-x-4' : ''}`} />
      </button>
    </div>
  )
}

function ConfigPage() {
  const { role } = useAuth()
  const [fields, setFields] = useState<ConfigFields>(EMPTY_FIELDS)
  const [path, setPath] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const canWrite = role === 'superadmin'
  const [children, setChildren] = useState<FederatedChild[]>([])
  const [target, setTarget] = useState<string>('local')

  async function load() {
    setLoading(true)
    try {
      const data = await apiJson<{ fields: ConfigFields; path: string }>('/api/config')
      setFields(data.fields)
      setPath(data.path)
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  useEffect(() => {
    apiJson<FederatedChild[]>('/api/federation').then(setChildren).catch(() => {})
  }, [])

  function set<K extends keyof ConfigFields>(key: K, value: ConfigFields[K]) {
    setFields(f => ({ ...f, [key]: value }))
  }

  async function handleSave() {
    if (!canWrite) return
    setSaving(true)
    try {
      const res = target === 'local'
        ? await apiFetch('/api/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(fields),
          })
        : await apiFetch(`/api/federation/${target}/push-config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(fields),
          })
      const body = await res.json().catch(() => ({ detail: res.statusText }))
      if (!res.ok) throw new Error(body.detail ?? res.statusText)
      if (target === 'local') {
        const result = body.restarted ? 'Config written, zenoh-router restarted' : `Config written, restart failed: ${body.restart_error}`
        notify.success(body.native_process_restart_required
          ? `${result}. Restart native bridge scripts to apply the new namespace prefix.`
          : result)
      } else {
        notify.success(`Config pushed to child (version ${body.version})`)
      }
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setSaving(false)
    }
  }

  const inputClass = "w-full px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#1a1a1d] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-50"

  return (
    <Layout>
      <div className="p-6 max-w-2xl">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-xl font-semibold">Zenoh Config</h1>
            {path && <p className="text-xs text-zinc-500 font-mono mt-1">{path}</p>}
          </div>
          <div className="flex items-center gap-2">
            <button onClick={load} disabled={loading}
              className="flex items-center gap-2 px-3 py-2 rounded-md text-sm text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-white hover:bg-zinc-200/50 dark:hover:bg-white/[0.05] transition-colors disabled:opacity-50">
              <RotateCw size={14} /> Reload
            </button>
            {canWrite && (
              <button onClick={handleSave} disabled={saving || loading}
                className="flex items-center gap-2 px-4 py-2 bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm rounded-md transition-colors disabled:opacity-50">
                <Save size={14} /> {saving ? 'Saving…' : 'Save & Restart'}
              </button>
            )}
          </div>
        </div>
        {children.length > 0 && (
          <div className="mb-4 space-y-1">
            <label className="text-sm text-zinc-700 dark:text-zinc-300">Push target</label>
            <select value={target} onChange={e => setTarget(e.target.value)}
              className="w-full sm:w-64 px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#1a1a1d] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm focus:outline-none focus:ring-2 focus:ring-accent-ring">
              <option value="local">This pod</option>
              {children.map(c => <option key={c.id} value={c.id}>{c.name} ({c.namespace})</option>)}
            </select>
          </div>
        )}
        {!canWrite && (
          <p className="text-xs text-yellow-600 dark:text-yellow-400 mb-3">Your role can view but not edit this config.</p>
        )}
        <div className="hud-frame relative rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-5 space-y-4">
          <HudCorners />
          <Field label="Local mTLS port" help="Mesh-facing listen port for bridges, audit-sink (default 7447)">
            <input type="number" min={1} max={65535} disabled={!canWrite} className={inputClass}
              value={fields.mtls_port} onChange={e => set('mtls_port', Number(e.target.value))} />
          </Field>
          <Field label="Local TCP port" help="Plaintext local-only listen port for bridges + this GUI (default 7448)">
            <input type="number" min={1} max={65535} disabled={!canWrite} className={inputClass}
              value={fields.local_tcp_port} onChange={e => set('local_tcp_port', Number(e.target.value))} />
          </Field>
          <Field label="Fabric endpoint" help="The peer endpoint this pod dials out to (always mTLS — scheme is fixed)">
            <div className="flex flex-wrap gap-2 mb-2">
              {FABRIC_PRESETS.map(p => (
                <button key={p.label} type="button" disabled={!canWrite}
                  onClick={() => set('fabric_endpoint', `tls/${p.host}:${p.port}`)}
                  className="px-2.5 py-1 rounded-full text-xs border border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-white hover:border-zinc-500 transition-colors disabled:opacity-50">
                  {p.label}
                </button>
              ))}
            </div>
            <div className="flex gap-2">
              <div className="flex-1 flex items-center gap-2 bg-zinc-200 dark:bg-[#1a1a1d] border border-zinc-300 dark:border-white/10 rounded-md px-3 focus-within:ring-2 focus-within:ring-accent-ring">
                <span className="text-zinc-500 text-sm shrink-0">tls://</span>
                <input type="text" disabled={!canWrite} placeholder="host or NetBird name"
                  className="flex-1 py-2 bg-transparent text-zinc-900 dark:text-white text-sm font-mono focus:outline-none disabled:opacity-50"
                  value={parseFabricEndpoint(fields.fabric_endpoint).host}
                  onChange={e => set('fabric_endpoint', `tls/${e.target.value}:${parseFabricEndpoint(fields.fabric_endpoint).port}`)} />
              </div>
              <input type="number" min={1} max={65535} disabled={!canWrite}
                className={`${inputClass} w-28`}
                value={parseFabricEndpoint(fields.fabric_endpoint).port}
                onChange={e => set('fabric_endpoint', `tls/${parseFabricEndpoint(fields.fabric_endpoint).host}:${e.target.value}`)} />
            </div>
          </Field>
          <Field label="Org namespace prefix" help="Your org's topic prefix — first segment fixed, rest is any depth (e.g. LTU/CISB, LTU/CISB/LTK). Saving restarts the router and all bridges.">
            <input type="text" disabled={!canWrite} className={inputClass}
              value={fields.namespace_prefix} onChange={e => set('namespace_prefix', e.target.value)} />
          </Field>
          <Field label="Partner namespace" help="This pod's first-party publish/subscribe prefix (its slot)">
            <input type="text" disabled={!canWrite} className={inputClass}
              value={fields.partner_namespace} onChange={e => set('partner_namespace', e.target.value)} />
          </Field>
          <Field label="Inbound namespace" help="Bilateral prefix the fabric publishes TO this pod">
            <input type="text" disabled={!canWrite} className={inputClass}
              value={fields.inbound_namespace} onChange={e => set('inbound_namespace', e.target.value)} />
          </Field>
          <div className="pt-2 border-t border-zinc-200 dark:border-white/10 space-y-4">
            <Toggle label="Verify name on connect" disabled={!canWrite}
              help="Verify the fabric endpoint's cert SAN against the DNS name dialed. Off by default: the gateway cert SAN binds the mesh IP, not the DNS name — turning this on can break the fabric connection."
              checked={fields.verify_name_on_connect} onChange={v => set('verify_name_on_connect', v)} />
            <Toggle label="Storage plugin loading" disabled={!canWrite}
              help="Whether the storage_manager plugin loads at all. Off means new subscribers no longer get a last-known value via get() — publish/subscribe still works."
              checked={fields.plugins_loading_enabled} onChange={v => set('plugins_loading_enabled', v)} />
          </div>
        </div>
      </div>
    </Layout>
  )
}
