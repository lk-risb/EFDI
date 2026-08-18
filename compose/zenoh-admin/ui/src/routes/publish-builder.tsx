import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useMemo, useRef, useState} from 'react'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {HudCorners} from '@/components/HudCorners'
import {apiFetch, apiJson, errorDetail, errorMessage} from '@/lib/api'
import {fetchTopology, type TopologyNode} from '@/lib/topology'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'
import {Download, FileCode2, Play, Plus, RefreshCw, Save, ShieldCheck, Square, Upload, X,} from 'lucide-react'

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
  requires_client_cn: boolean
}

interface PublishDefaults {
  current_endpoint: string
  endpoints: string[]
  publish_root: string
  tls_profile: string
  verify_name_on_connect: boolean
  client_cn: string
  cert_dir: string
  profile: PublishProfile
  profiles: PublishProfile[]
  config_path: string
}

type ConfigValue = string | { configured: boolean }

interface RuntimeService {
  name: string
  running: boolean
  status: string
  pid: number | null
}

interface RuntimeData {
  services: RuntimeService[]
  selected_services: string[]
  config: Record<string, ConfigValue>
  editable_keys: string[]
  env_file: string
  control_port: number
}

interface RouterTarget {
  namespace: string
  label: string
  local: boolean
  fields: TopologyNode['config_fields']
}

function emptyRow(): Row {
  return { topic: '', message: '', count: 1, interval_s: 1.0 }
}

const inputClass = 'w-full rounded-md border border-zinc-300 bg-zinc-200 px-3 py-2 text-sm font-mono text-zinc-900 focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-50 dark:border-white/10 dark:bg-[#141416] dark:text-white'
const textareaClass = `${inputClass} min-h-24`
const SAFE_ENDPOINT_RE = /^[A-Za-z0-9._/:-]+$/

function cardClass(extra = '') {
  return `hud-card hud-glass hud-frame relative border border-zinc-200 p-5 dark:border-white/10 ${extra}`
}

function replaceConnectEndpoint(rendered: string, endpoint: string): string | null {
  const match = rendered.match(/(connect:\s*\{[\s\S]*?endpoints:\s*\[)([\s\S]*?)(\]\s*,)/m)
  if (!match) return null
  const [full, prefix, body, suffix] = match
  const parts = body
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
    .filter(item => item.replace(/^"|"$/g, '') !== endpoint)
  parts.push(JSON.stringify(endpoint))
  const replacement = `${prefix}${parts.join(', ')}${suffix}`
  return rendered.replace(full, replacement)
}

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

  const [runtime, setRuntime] = useState<RuntimeData | null>(null)
  const [routerTargets, setRouterTargets] = useState<RouterTarget[]>([])
  const [selectedRouter, setSelectedRouter] = useState('local')
  const [routerConfigText, setRouterConfigText] = useState('')
  const [routerConfigLoading, setRouterConfigLoading] = useState(true)
  const [routerConfigSaving, setRouterConfigSaving] = useState(false)
  const [routerConfigError, setRouterConfigError] = useState('')
  const [routerEndpointInput, setRouterEndpointInput] = useState('')
  const [routerBusy, setRouterBusy] = useState<string | null>(null)
  const routerConfigFileRef = useRef<HTMLInputElement>(null)

  function loadRouterConfigFromFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      setRouterConfigText(String(reader.result ?? ''))
      setRouterConfigError('')
    }
    reader.onerror = () => setRouterConfigError('Could not read the selected file')
    reader.readAsText(file)
  }

  const selectedProfile = defaults?.profiles.find(profile => profile.id === tlsProfile) ?? defaults?.profile

  async function loadPublishDefaults() {
    setLoadingDefaults(true)
    try {
      const data = await apiJson<PublishDefaults>('/api/publish-script/defaults')
      setDefaults(data)
      setRouterEndpoint(data.current_endpoint)
      setClientCn(data.client_cn)
      setCertDir(data.cert_dir)
      setTlsProfile(data.tls_profile)
      setVerifyNameOnConnect(data.verify_name_on_connect)
      setRows(current => {
        if (current.length !== 1 || current[0].topic.trim()) return current
        const topic = data.publish_root
          ? `${data.publish_root}/health/publish-test/v1`
          : 'health/publish-test/v1'
        return [{...current[0], topic}]
      })
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setLoadingDefaults(false)
    }
  }

  async function loadRuntime() {
    try {
      const data = await apiJson<RuntimeData>('/api/runtime')
      setRuntime(data)
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  async function loadRouterTargets() {
    try {
      const [topology, localConfig] = await Promise.all([
        fetchTopology(),
        apiJson<{ fields: { partner_namespace: string } | null }>('/api/config'),
      ])
      // fields is null during the plaintext bootstrap state (no namespace set yet).
      const localNamespace = localConfig.fields?.partner_namespace ?? ''
      const nextTargets = topology.nodes
        .filter(node => node.config_fields)
        .map(node => ({
          namespace: node.namespace,
          label: node.namespace + (node.namespace === localNamespace ? ' (this host)' : ''),
          local: node.namespace === localNamespace,
          fields: node.config_fields,
        }))
        .sort((a, b) => Number(b.local) - Number(a.local) || a.namespace.localeCompare(b.namespace))
      setRouterTargets(nextTargets)
      if (!nextTargets.some(target => target.namespace === selectedRouter)) {
        setSelectedRouter(nextTargets.find(target => target.local)?.namespace ?? nextTargets[0]?.namespace ?? 'local')
      }
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  async function refreshRouterConfig(targetNamespace = selectedRouter) {
    setRouterConfigLoading(true)
    setRouterConfigError('')
    try {
      if (targetNamespace === 'local') {
        const data = await apiJson<{ rendered: string }>('/api/config/rendered')
        setRouterConfigText(data.rendered)
        return
      }
      const target = routerTargets.find(item => item.namespace === targetNamespace)
      if (!target?.fields) {
        throw new Error('No config snapshot is available for the selected router')
      }
      const data = await apiJson<{ rendered: string }>('/api/config/render', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(target.fields),
      })
      setRouterConfigText(data.rendered)
    } catch (e) {
      setRouterConfigError(errorMessage(e))
    } finally {
      setRouterConfigLoading(false)
    }
  }

  useEffect(() => {
    void loadPublishDefaults()
    void loadRuntime()
    void loadRouterTargets()
  }, [])

  useEffect(() => {
    void refreshRouterConfig(selectedRouter)
  }, [selectedRouter, routerTargets])

  function updateRow(index: number, patch: Partial<Row>) {
    setRows(current => current.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  function addRow() {
    setRows(current => [...current, emptyRow()])
  }

  function removeRow(index: number) {
    setRows(current => current.filter((_, i) => i !== index))
  }

  function validateRows(): string | null {
    if (!routerEndpoint.trim()) return 'Router endpoint is required'
    if (selectedProfile?.requires_client_cn && !clientCn.trim()) {
      return 'Client name (cert CN) is required by the selected TLS profile'
    }
    if (!certDir.trim()) return 'Cert directory is required'
    if (rows.length === 0) return 'At least one row is required'
    for (const row of rows) {
      if (!row.topic.trim()) return 'Every row needs a topic'
      if (row.topic.includes('*')) return 'Publish topics must be concrete and cannot contain wildcards'
      if (row.count < 1) return 'Count must be at least 1'
      if (row.interval_s < 0) return 'Interval must be 0 or more'
    }
    return null
  }

  async function handleGenerate() {
    const validationError = validateRows()
    if (validationError) {
      setError(validationError)
      return
    }
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
        throw new Error(errorDetail(err, res))
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

  function addConfiguredEndpoint() {
    const endpoint = routerEndpointInput.trim()
    if (!endpoint) {
      notify.error('Enter a router endpoint first')
      return
    }
    if (!SAFE_ENDPOINT_RE.test(endpoint)) {
      notify.error('Router endpoint contains unsupported characters')
      return
    }
    const next = replaceConnectEndpoint(routerConfigText, endpoint)
    if (!next) {
      notify.error('Could not locate connect.endpoints in the current config')
      return
    }
    setRouterConfigText(next)
    notify.success('Endpoint added to connect.endpoints')
  }

  async function saveRouterConfig() {
    if (selectedRouter !== 'local') {
      notify.error('Only this host can be saved from this dashboard')
      return
    }
    setRouterConfigSaving(true)
    try {
      const res = await apiFetch('/api/config/rendered', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rendered: routerConfigText }),
      })
      const body = await res.json().catch(() => ({ detail: res.statusText }))
      if (!res.ok) throw new Error(errorDetail(body, res))
      const status = body.restarted ? 'Router saved, activated, and restarted' : `Config activation status: ${body.status}`
      notify.success(status)
      await Promise.all([loadRuntime(), loadRouterTargets()])
      await refreshRouterConfig('local')
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setRouterConfigSaving(false)
    }
  }

  async function routerAction(name: string, action: 'start' | 'stop' | 'restart') {
    setRouterBusy(`${name}:${action}`)
    try {
      const res = await apiJson(`/api/runtime/services/${name}/${action}`, { method: 'POST' })
      void res
      notify.success(`${name} ${action} requested`)
      await loadRuntime()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setRouterBusy(null)
    }
  }

  const routerServices = useMemo(() => {
    const services = runtime?.services ?? []
    const routers = services.filter(service => service.name.startsWith('zenoh'))
    return routers.length > 0 ? routers : services
  }, [runtime])

  return (
    <Layout>
      <div className="max-w-[1700px] p-6">
        <PageHeader title="Publish Script Builder" />
        <p className="mb-6 text-sm text-zinc-500">
          Add rows, then generate a ready-to-run publish script. Nothing is sent from here.
          The center panel edits the router&apos;s current `config.json5`; the right panel controls
          the runtime-managed router processes.
        </p>

        <div className="grid items-start gap-5 xl:grid-cols-[1.2fr_1fr_0.85fr]">
          <section className={cardClass('hud-enter')}>
            <HudCorners />
            <div className="mb-4 border-b border-zinc-200 pb-4 dark:border-white/10">
              <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Publish script builder</h2>
              <p className="mt-1 text-xs text-zinc-500">
                Add rows, then generate a ready-to-run publish script. Message boxes accept multi-line content (JSON, XML, any structured payload).
              </p>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <label className="space-y-1">
                <span className="text-sm text-zinc-700 dark:text-zinc-300">Router endpoint</span>
                <input
                  className={inputClass}
                  value={routerEndpoint}
                  onChange={e => setRouterEndpoint(e.target.value)}
                  placeholder="tls/zenohpeer.nb.efdi:7447"
                />
              </label>
              {selectedProfile?.requires_client_cn ? (
                <label className="space-y-1">
                  <span className="text-sm text-zinc-700 dark:text-zinc-300">Client name (cert CN)</span>
                  <input
                    className={inputClass}
                    value={clientCn}
                    onChange={e => setClientCn(e.target.value)}
                    placeholder="router-slot"
                  />
                </label>
              ) : (
                <div className="space-y-1">
                  <span className="text-sm text-zinc-700 dark:text-zinc-300">Client certificate</span>
                  <div className={`${inputClass} text-zinc-500`}>
                    {selectedProfile?.client_cert_filename ?? 'Fixed by TLS profile'}
                  </div>
                </div>
              )}
              <label className="space-y-1">
                <span className="text-sm text-zinc-700 dark:text-zinc-300">Certificate directory</span>
                <input
                  className={inputClass}
                  value={certDir}
                  onChange={e => setCertDir(e.target.value)}
                  placeholder="/path/to/your/certs"
                />
              </label>
              <label className="space-y-1">
                <span className="text-sm text-zinc-700 dark:text-zinc-300">TLS profile</span>
                <select
                  className={inputClass}
                  value={tlsProfile}
                  onChange={e => {
                    setTlsProfile(e.target.value)
                    const profile = defaults?.profiles.find(item => item.id === e.target.value)
                    if (profile?.id === 'efdi' && defaults?.client_cn) setClientCn(defaults.client_cn)
                  }}
                >
                  {(defaults?.profiles ?? []).map(profile => (
                    <option key={profile.id} value={profile.id}>{profile.label}</option>
                  ))}
                  {!defaults && <option value="efdi">EFDI backbone (EFDI CA)</option>}
                </select>
              </label>
              <label className="flex items-start gap-3 rounded-md border border-zinc-200 p-3 text-sm dark:border-white/10 sm:col-span-2">
                <input
                  type="checkbox"
                  checked={verifyNameOnConnect}
                  onChange={e => setVerifyNameOnConnect(e.target.checked)}
                  className="mt-0.5 accent-accent-fill"
                />
                <span>
                  <span className="block text-zinc-700 dark:text-zinc-300">Verify endpoint name on connect</span>
                  <span className="mt-1 block text-xs text-zinc-500">Mirrors the active Zenoh config connection policy.</span>
                </span>
              </label>
            </div>

            {defaults?.profile && selectedProfile && (
              <div className="mt-4 grid gap-3 border-t border-zinc-200 pt-4 text-xs dark:border-white/10 sm:grid-cols-2">
                <div className="flex items-start gap-2">
                  <ShieldCheck size={15} className="mt-0.5 shrink-0 text-accent-ring" />
                  <div>
                    <p className="text-zinc-600 dark:text-zinc-300">Current router TLS identity</p>
                    <p className="mt-1 font-mono text-zinc-500">{defaults.profile.router_connect_certificate}</p>
                    <p className="font-mono text-zinc-500">{defaults.profile.router_connect_private_key}</p>
                    <p className="font-mono text-zinc-500">{defaults.profile.router_root_ca}</p>
                  </div>
                </div>
                <div className="flex items-start gap-2">
                  <FileCode2 size={15} className="mt-0.5 shrink-0 text-zinc-500" />
                  <div>
                    <p className="text-zinc-600 dark:text-zinc-300">Generated script certificate files</p>
                    <p className="mt-1 font-mono text-zinc-500">
                      {certDir || 'bundle directory'} / {selectedProfile.cert_subdir} / {selectedProfile.client_cert_filename}
                    </p>
                    <p className="font-mono text-zinc-500">
                      {certDir || 'bundle directory'} / {selectedProfile.cert_subdir} / {selectedProfile.client_key_filename}
                    </p>
                    <p className="font-mono text-zinc-500">
                      {certDir || 'bundle directory'} / {selectedProfile.cert_subdir} / {selectedProfile.root_ca_filename}
                    </p>
                  </div>
                </div>
              </div>
            )}

            <div className="mt-4 space-y-3">
              {rows.map((row, index) => (
                <div key={index} className="hud-frame relative rounded-md border border-zinc-200 bg-white p-4 dark:border-white/10 dark:bg-[#0c0c0e]">
                  <HudCorners />
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_2fr_auto_auto_auto]">
                    <label className="space-y-1">
                      <span className="text-xs uppercase tracking-wide text-zinc-500">Topic</span>
                      <input
                        className={inputClass}
                        value={row.topic}
                        onChange={e => updateRow(index, { topic: e.target.value })}
                        placeholder={defaults?.publish_root
                          ? `${defaults.publish_root}/health/status/v1`
                          : 'router-slot/health/status/v1'}
                      />
                      <span className="block text-[11px] text-zinc-500">
                        Use a concrete key. `/**` is an ACL/subscription pattern and cannot be published.
                      </span>
                    </label>
                    <label className="space-y-1">
                      <span className="text-xs uppercase tracking-wide text-zinc-500">Message</span>
                      <textarea
                        className={textareaClass}
                        value={row.message}
                        onChange={e => updateRow(index, { message: e.target.value })}
                        placeholder="hello world"
                      />
                    </label>
                    <label className="space-y-1 w-20">
                      <span className="text-xs uppercase tracking-wide text-zinc-500">Count</span>
                      <input
                        type="number"
                        min={1}
                        className={inputClass}
                        value={row.count}
                        onChange={e => updateRow(index, { count: Number(e.target.value) })}
                      />
                    </label>
                    <label className="space-y-1 w-24">
                      <span className="text-xs uppercase tracking-wide text-zinc-500">Interval (s)</span>
                      <input
                        type="number"
                        min={0}
                        step={0.1}
                        className={inputClass}
                        value={row.interval_s}
                        onChange={e => updateRow(index, { interval_s: Number(e.target.value) })}
                      />
                    </label>
                    <button
                      type="button"
                      onClick={() => removeRow(index)}
                      disabled={rows.length === 1}
                      className="mt-6 rounded-md p-2 text-zinc-500 transition-colors hover:bg-zinc-200/50 hover:text-red-600 disabled:opacity-30 dark:hover:bg-white/[0.05] dark:hover:text-red-400"
                    >
                      <X size={16} />
                    </button>
                  </div>
                </div>
              ))}
            </div>

            <div className="mt-4 flex items-center gap-2">
              <button
                type="button"
                onClick={addRow}
                className="flex items-center gap-2 rounded-md px-3 py-2 text-sm text-zinc-600 transition-colors hover:bg-zinc-200/50 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-white/[0.05] dark:hover:text-white"
              >
                <Plus size={14} /> Add row
              </button>
            </div>

            {error && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{error}</p>}

            <div className="mt-4 flex items-center gap-2">
              <button
                type="button"
                onClick={handleGenerate}
                disabled={generating || loadingDefaults}
                className="flex items-center gap-2 rounded-md bg-accent-fill px-4 py-2 text-sm text-accent-text transition-colors hover:bg-accent-fill-hover disabled:opacity-50"
              >
                <Download size={14} /> {generating ? 'Generating…' : 'Generate Script'}
              </button>
            </div>
          </section>

          <section className={cardClass('hud-enter')}>
            <HudCorners />
            <div className="mb-4 flex items-start justify-between gap-3 border-b border-zinc-200 pb-4 dark:border-white/10">
              <div>
                <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Router configs</h2>
                <p className="mt-1 text-xs text-zinc-500">View/edit config.json5 across all known routers</p>
              </div>
              <div className="flex items-center gap-1">
                <input
                  ref={routerConfigFileRef}
                  type="file"
                  accept=".json5,.json,.txt"
                  className="hidden"
                  onChange={loadRouterConfigFromFile}
                />
                <button
                  type="button"
                  onClick={() => routerConfigFileRef.current?.click()}
                  disabled={selectedRouter !== 'local'}
                  title="Load a config.json5 file from disk — no SCP needed"
                  className="flex items-center gap-2 rounded-md px-3 py-2 text-xs text-zinc-600 transition-colors hover:bg-zinc-200/50 hover:text-zinc-900 disabled:opacity-50 dark:text-zinc-400 dark:hover:bg-white/[0.05] dark:hover:text-white"
                >
                  <Upload size={14} /> Load from file…
                </button>
                <button
                  type="button"
                  onClick={() => void refreshRouterConfig(selectedRouter)}
                  disabled={routerConfigLoading}
                  className="flex items-center gap-2 rounded-md px-3 py-2 text-xs text-zinc-600 transition-colors hover:bg-zinc-200/50 hover:text-zinc-900 disabled:opacity-50 dark:text-zinc-400 dark:hover:bg-white/[0.05] dark:hover:text-white"
                >
                  <RefreshCw size={14} className={routerConfigLoading ? 'animate-spin' : ''} /> Reload from disk
                </button>
              </div>
            </div>

            <div className="space-y-4">
              <div className="space-y-1">
                <label className="text-sm text-zinc-700 dark:text-zinc-300">Router</label>
                <select
                  className={inputClass}
                  value={selectedRouter}
                  onChange={e => setSelectedRouter(e.target.value)}
                >
                  <option value="local">local · this host</option>
                  {routerTargets.map(target => (
                    <option key={target.namespace} value={target.namespace}>
                      {target.label}
                    </option>
                  ))}
                </select>
              </div>

              <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
                <input
                  className={inputClass}
                  value={routerEndpointInput}
                  onChange={e => setRouterEndpointInput(e.target.value)}
                  placeholder="tls/zenoh4.efdi.ltu:7447"
                />
                <button
                  type="button"
                  onClick={addConfiguredEndpoint}
                  className="flex items-center justify-center gap-2 rounded-md border border-zinc-300 px-3 py-2 text-sm text-zinc-700 transition-colors hover:border-accent-ring hover:text-zinc-900 dark:border-white/10 dark:text-zinc-300 dark:hover:text-white"
                >
                  <Plus size={14} /> Add to connect.endpoints
                </button>
              </div>

              {routerConfigError && (
                <p className="text-sm text-red-600 dark:text-red-400">{routerConfigError}</p>
              )}

              <textarea
                className="min-h-[420px] w-full rounded-md border border-zinc-300 bg-zinc-100 p-3 font-mono text-xs leading-5 text-zinc-900 focus:outline-none focus:ring-2 focus:ring-accent-ring dark:border-white/10 dark:bg-[#141416] dark:text-white"
                value={routerConfigText}
                onChange={e => {
                  setRouterConfigText(e.target.value)
                }}
                spellCheck={false}
                readOnly={selectedRouter !== 'local'}
              />

              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => void saveRouterConfig()}
                  disabled={routerConfigSaving || routerConfigLoading || selectedRouter !== 'local'}
                  className="flex items-center gap-2 rounded-md bg-accent-fill px-3 py-2 text-sm text-accent-text transition-colors hover:bg-accent-fill-hover disabled:opacity-50"
                >
                  <Save size={14} /> {routerConfigSaving ? 'Saving…' : 'Save'}
                </button>
                <button
                  type="button"
                  onClick={() => void saveRouterConfig()}
                  disabled={routerConfigSaving || routerConfigLoading || selectedRouter !== 'local'}
                  className="flex items-center gap-2 rounded-md border border-red-500/60 px-3 py-2 text-sm text-red-500 transition-colors hover:bg-red-500/10 disabled:opacity-50"
                >
                  <Save size={14} /> Save & Restart Router
                </button>
                <button
                  type="button"
                  onClick={() => void refreshRouterConfig(selectedRouter)}
                  disabled={routerConfigLoading}
                  className="flex items-center gap-2 rounded-md px-3 py-2 text-sm text-zinc-600 transition-colors hover:bg-zinc-200/50 hover:text-zinc-900 disabled:opacity-50 dark:text-zinc-400 dark:hover:bg-white/[0.05] dark:hover:text-white"
                >
                  <RefreshCw size={14} className={routerConfigLoading ? 'animate-spin' : ''} /> Reload from disk
                </button>
              </div>

              <p className="text-xs text-zinc-500">
                {selectedRouter === 'local'
                  ? `Loaded from ${defaults?.config_path ?? 'config.json5'}`
                  : 'Selected router snapshot is read-only on this dashboard.'}
              </p>
            </div>
          </section>

          <section className={cardClass('hud-enter')}>
            <HudCorners />
            <div className="mb-4 border-b border-zinc-200 pb-4 dark:border-white/10">
              <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Router control</h2>
              <p className="mt-1 text-xs text-zinc-500">Start / stop / check status across all known routers</p>
            </div>

            <div className="space-y-3">
              <div className="grid grid-cols-[1.2fr_auto_auto_auto] gap-2 text-[11px] uppercase tracking-[0.18em] text-zinc-500">
                <span>Router</span>
                <span>State</span>
                <span />
                <span>Actions</span>
              </div>

              {routerServices.map(service => {
                const busy = routerBusy?.startsWith(`${service.name}:`) ? routerBusy.split(':')[1] : null
                return (
                  <div key={service.name} className="grid grid-cols-[1.2fr_auto_auto_auto] items-center gap-2 rounded-md border border-zinc-200 bg-white px-3 py-2 dark:border-white/10 dark:bg-[#0c0c0e]">
                    <div className="min-w-0">
                      <p className="truncate font-mono text-sm text-zinc-800 dark:text-zinc-200">
                        {service.name}{service.name === 'zenoh' ? ' (this host)' : ''}
                      </p>
                    </div>
                    <span className={`text-xs ${service.running ? 'text-emerald-500' : 'text-amber-500'}`}>
                      {service.running ? 'running' : 'stopped'}
                    </span>
                    <span />
                    <div className="flex items-center gap-1">
                      <button
                        type="button"
                        title="Start"
                        disabled={!!busy || service.running}
                        onClick={() => void routerAction(service.name, 'start')}
                        className="rounded-none p-2 text-zinc-500 transition-colors hover:bg-emerald-500/10 hover:text-emerald-500 disabled:opacity-30"
                      >
                        <Play size={13} />
                      </button>
                      <button
                        type="button"
                        title="Stop"
                        disabled={!!busy || !service.running}
                        onClick={() => void routerAction(service.name, 'stop')}
                        className="rounded-none p-2 text-zinc-500 transition-colors hover:bg-red-500/10 hover:text-red-500 disabled:opacity-30"
                      >
                        <Square size={12} />
                      </button>
                      <button
                        type="button"
                        title="Refresh"
                        disabled={!!busy}
                        onClick={() => void loadRuntime()}
                        className="rounded-none p-2 text-zinc-500 transition-colors hover:bg-zinc-200/50 hover:text-zinc-900 disabled:opacity-30 dark:hover:bg-white/[0.05] dark:hover:text-white"
                      >
                        <RefreshCw size={13} className={busy === 'restart' ? 'animate-spin' : ''} />
                      </button>
                    </div>
                  </div>
                )
              })}
              <button
                type="button"
                onClick={() => void loadRuntime()}
                className="mt-2 rounded-md border border-zinc-300 px-3 py-2 text-sm text-zinc-700 transition-colors hover:border-accent-ring hover:text-zinc-900 dark:border-white/10 dark:text-zinc-300 dark:hover:text-white"
              >
                Refresh all
              </button>
              <p className="text-xs text-zinc-500">
                Control agent: {runtime ? `127.0.0.1:${runtime.control_port}` : 'offline'}
              </p>
            </div>
          </section>
        </div>
      </div>
    </Layout>
  )
}
