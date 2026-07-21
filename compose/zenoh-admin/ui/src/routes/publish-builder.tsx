import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Layout } from '@/components/Layout'
import { PageHeader } from '@/components/PageHeader'
import { apiFetch, apiJson, errorMessage } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { notify } from '@/lib/notify'
import { Plus, X, Download, FileCode2, ShieldCheck, Waypoints } from 'lucide-react'
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

interface PublishProfile {
  id: string
  label: string
  cert_subdir: string
  root_ca_filename: string
  client_cert_filename: string
  client_key_filename: string
  router_connect_certificate: string
  router_connect_private_key: string
  router_root_ca: string
}

interface PublishDefaults {
  current_endpoint: string
  endpoints: string[]
  tls_profile: string
  verify_name_on_connect: boolean
  client_cn: string
  cert_dir: string
  profile: PublishProfile
  profiles: PublishProfile[]
  config_path: string
}

function emptyRow(): Row {
  return { topic: '', message: '', count: 1, interval_s: 1.0 }
}

const inputClass = "w-full px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#141416] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-50"

function PublishBuilderPage() {
  const [routerEndpoint, setRouterEndpoint] = useState('')
  const [clientCn, setClientCn] = useState('')
  const [certDir, setCertDir] = useState('')
  const [tlsProfile, setTlsProfile] = useState('efdi')
  const [verifyNameOnConnect, setVerifyNameOnConnect] = useState(false)
  const [defaults, setDefaults] = useState<PublishDefaults | null>(null)
  const [loadingDefaults, setLoadingDefaults] = useState(true)
  const [rows, setRows] = useState<Row[]>([emptyRow()])
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('')

  async function loadDefaults() {
    setLoadingDefaults(true)
    try {
      const data = await apiJson<PublishDefaults>('/api/publish-script/defaults')
      setDefaults(data)
      setRouterEndpoint(data.current_endpoint)
      setClientCn(data.client_cn)
      setCertDir(data.cert_dir)
      setTlsProfile(data.tls_profile)
      setVerifyNameOnConnect(data.verify_name_on_connect)
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setLoadingDefaults(false)
    }
  }

  useEffect(() => { void loadDefaults() }, [])

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

  const selectedProfile = defaults?.profiles.find(profile => profile.id === tlsProfile) ?? defaults?.profile

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
          tls_profile: tlsProfile,
          verify_name_on_connect: verifyNameOnConnect,
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
          Add rows, then generate a ready-to-run publish script. Nothing is published while you
          edit this page; the downloaded script connects and sends the messages when you run it.
          Message boxes accept multi-line content (JSON, XML,
          any structured payload).
        </p>

        <div className="hud-frame relative hud-enter mb-6">
          <HudCorners />
          <div className="rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#0c0c0e] p-5 space-y-4">
          <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="hud-label text-sm font-semibold text-zinc-800 dark:text-zinc-200">Connection and certificates</h2>
              <p className="mt-1 text-xs text-zinc-500">Loaded from the active Zenoh Config. Choose a different configured link or certificate profile before generating.</p>
            </div>
            <button type="button" onClick={() => void loadDefaults()} disabled={loadingDefaults}
              className="flex items-center gap-2 rounded-md px-3 py-2 text-xs text-zinc-600 transition-colors hover:bg-zinc-200/50 hover:text-zinc-900 disabled:opacity-50 dark:text-zinc-400 dark:hover:bg-white/[0.05] dark:hover:text-white">
              <Waypoints size={14} /> Reload router settings
            </button>
          </div>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="text-sm text-zinc-700 dark:text-zinc-300">Router endpoint</label>
              <input className={inputClass} value={routerEndpoint} onChange={e => setRouterEndpoint(e.target.value)} placeholder="tls/<current-router>:7447" />
              {defaults && defaults.endpoints.length > 0 && <select aria-label="Configured router endpoints" value="" onChange={e => e.target.value && setRouterEndpoint(e.target.value)} className={`${inputClass} font-sans`}>
                <option value="">Choose a configured endpoint…</option>
                {defaults.endpoints.map(endpoint => <option key={endpoint} value={endpoint}>{endpoint}</option>)}
              </select>}
              {defaults && <button type="button" onClick={() => setRouterEndpoint(defaults.current_endpoint)} disabled={!defaults.current_endpoint}
                className="text-xs text-accent-ring hover:underline disabled:opacity-40">Use current Zenoh endpoint{defaults.current_endpoint ? ` (${defaults.current_endpoint})` : ''}</button>}
            </div>
            <div className="space-y-1">
              <label className="text-sm text-zinc-700 dark:text-zinc-300">Certificate profile</label>
              <select className={inputClass} value={tlsProfile} onChange={e => { setTlsProfile(e.target.value); const profile = defaults?.profiles.find(p => p.id === e.target.value); if (profile?.id === 'efdi' && defaults?.client_cn) setClientCn(defaults.client_cn) }}>
                {(defaults?.profiles ?? []).map(profile => <option key={profile.id} value={profile.id}>{profile.label}</option>)}
                {!defaults && <option value="efdi">EFDI backbone (EFDI CA)</option>}
              </select>
              <p className="text-xs text-zinc-500">Current router profile: <span className="font-mono">{defaults?.tls_profile ?? 'loading…'}</span></p>
            </div>
            <div className="space-y-1">
              <label className="text-sm text-zinc-700 dark:text-zinc-300">Client name (cert CN)</label>
              <input className={inputClass} value={clientCn} onChange={e => setClientCn(e.target.value)} placeholder="acme" />
              <p className="text-xs text-zinc-500">Used by the EFDI profile; sandbox uses its fixed <span className="font-mono">cert.pem</span> identity.</p>
            </div>
            <div className="space-y-1">
              <label className="text-sm text-zinc-700 dark:text-zinc-300">Certificate bundle directory</label>
              <input className={inputClass} value={certDir} onChange={e => setCertDir(e.target.value)} placeholder="/home/acme/efdi-certs" />
            </div>
            <label className="flex items-start gap-3 rounded-md border border-zinc-200 p-3 text-sm dark:border-white/10">
              <input type="checkbox" checked={verifyNameOnConnect} onChange={e => setVerifyNameOnConnect(e.target.checked)} className="mt-0.5 accent-accent-fill" />
              <span><span className="block text-zinc-700 dark:text-zinc-300">Verify endpoint name on connect</span><span className="mt-1 block text-xs text-zinc-500">Mirrors the active Zenoh Config connection policy.</span></span>
            </label>
          </div>
          {defaults?.profile && selectedProfile && <div className="grid gap-3 border-t border-zinc-200 pt-4 text-xs dark:border-white/10 sm:grid-cols-2">
            <div className="flex items-start gap-2"><ShieldCheck size={15} className="mt-0.5 shrink-0 text-accent-ring" /><div><p className="text-zinc-600 dark:text-zinc-300">Current router TLS identity</p><p className="mt-1 font-mono text-zinc-500">{defaults.profile.router_connect_certificate}</p><p className="font-mono text-zinc-500">{defaults.profile.router_connect_private_key}</p><p className="font-mono text-zinc-500">{defaults.profile.router_root_ca}</p></div></div>
            <div className="flex items-start gap-2"><FileCode2 size={15} className="mt-0.5 shrink-0 text-zinc-500" /><div><p className="text-zinc-600 dark:text-zinc-300">Generated script certificate files</p><p className="mt-1 font-mono text-zinc-500">{certDir || 'bundle directory'} / {selectedProfile.cert_subdir} / {selectedProfile.client_cert_filename}</p><p className="font-mono text-zinc-500">{certDir || 'bundle directory'} / {selectedProfile.cert_subdir} / {selectedProfile.client_key_filename}</p><p className="font-mono text-zinc-500">{certDir || 'bundle directory'} / {selectedProfile.cert_subdir} / {selectedProfile.root_ca_filename}</p></div></div>
          </div>}
          </div>
        </div>

        <div className="space-y-3 mb-4">
          {rows.map((row, i) => (
            <div key={i} className="hud-frame relative hud-card rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#0c0c0e] p-4">
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
