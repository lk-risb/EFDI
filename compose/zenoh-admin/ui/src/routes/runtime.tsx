import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useMemo, useState} from 'react'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {HudCorners} from '@/components/HudCorners'
import {apiJson, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'
import {useUiSettings} from '@/store/ui'
import {
    Activity,
    FileText,
    KeyRound,
    Play,
    RefreshCw,
    Save,
    Search,
    Settings2,
    Square,
    Terminal,
    Wrench,
} from 'lucide-react'

export const Route = createFileRoute('/runtime')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'admin' && role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: RuntimePage,
})

type ConfigValue = string | { configured: boolean }
type RuntimeData = {
  services: ServiceState[]
  selected_services: string[]
  config: Record<string, ConfigValue>
  editable_keys: string[]
  env_file: string
  control_port: number
}
type ServiceDetails = {
  tracks?: number
  successful_requests?: number
  unauthorized_requests?: number
  last_successful_request?: string | null
  last_unauthorized_request?: string | null
  seconds_since_last_success?: number | null
}
type ServiceState = {
  name: string
  running: boolean
  status: string
  pid: number | null
  details?: ServiceDetails
}
type CatalogItem = {
  name: string
  group: string
  description: string
  // Which file backs the service, and its role derived from that path. A name
  // alone cannot say whether an entry owns an external connection (bridge),
  // decodes an already-published format (protocol), or writes out to a C2
  // system (layer) — and some names share one script with different arguments.
  source?: string
  kind?: 'bridge' | 'protocol' | 'layer' | 'infrastructure'
}

const KIND_STYLE: Record<string, string> = {
  bridge: 'text-sky-600 dark:text-sky-400 border-sky-500/40',
  protocol: 'text-violet-600 dark:text-violet-400 border-violet-500/40',
  layer: 'text-amber-600 dark:text-amber-400 border-amber-500/40',
  infrastructure: 'text-zinc-500 border-zinc-500/40',
}

// Runtime list reads outward along the data path: layers (what reaches C2)
// first, then the protocols that decode, then the bridges that ingest, and the
// router/control agent last — they are the substrate, not integrations you toggle.
const KIND_RANK: Record<string, number> = { layer: 0, protocol: 1, bridge: 2, infrastructure: 3 }

function servicePresentation(state: ServiceState | undefined, selected: boolean) {
  if (state?.status === 'auth-failed') return { label: 'HQ auth failed', text: 'text-red-500', dot: 'bg-red-500' }
  if (state?.status === 'client-stale') return { label: 'HQ pull stale', text: 'text-red-500', dot: 'bg-red-500' }
  if (state?.status === 'waiting-for-client') return { label: 'no HQ pulls', text: 'text-amber-500', dot: 'bg-amber-400' }
  if (state?.status === 'health-unavailable') return { label: 'health unavailable', text: 'text-amber-500', dot: 'bg-amber-400' }
  if (state?.status === 'client-connected') return { label: 'HQ connected', text: 'text-emerald-500', dot: 'bg-emerald-400 hud-live-dot' }
  if (state?.running) return { label: 'running', text: 'text-emerald-500', dot: 'bg-emerald-400 hud-live-dot' }
  if (state?.status === 'needs-config') return { label: 'needs config', text: 'text-amber-500', dot: 'bg-amber-400' }
  if (state?.status === 'crashed') return { label: 'crashed', text: 'text-red-500', dot: 'bg-red-500' }
  if (selected) return { label: 'stopped', text: 'text-amber-500', dot: 'bg-amber-400' }
  return { label: 'not selected', text: 'text-zinc-500', dot: 'bg-zinc-400 dark:bg-zinc-700' }
}

function serviceTelemetry(state: ServiceState | undefined) {
  const details = state?.details
  if (!details) return ''
  const parts: string[] = []
  if (details.tracks !== undefined) parts.push(`${details.tracks} tracks`)
  if (state?.status === 'waiting-for-client') parts.push('no successful HQ pulls')
  if (state?.status === 'auth-failed') parts.push(`${details.unauthorized_requests ?? 0} rejected pulls`)
  if ((state?.status === 'client-connected' || state?.status === 'client-stale') && details.seconds_since_last_success != null) {
    parts.push(`last HQ pull ${Math.round(details.seconds_since_last_success)}s ago`)
  }
  return parts.length ? ` · ${parts.join(' · ')}` : ''
}

const FIELD_GROUPS: { title: string; description: string; fields: { key: string; label: string; secret?: boolean; placeholder?: string }[] }[] = [
  { title: 'Router and namespace', description: 'Native-process endpoint and namespace defaults. Use Zenoh Config for live router listener changes.', fields: [
    { key: 'ZENOH_LOCAL_ENDPOINT', label: 'Local Zenoh endpoint', placeholder: 'tcp/127.0.0.1:7448' },
    { key: 'PARTNER_NAMESPACE', label: 'Partner namespace', placeholder: 'partner-a' },
  ] },
  { title: 'TAK and CoT', description: 'Authenticated CoT feed to the TAK Server over mTLS.', fields: [
    { key: 'TAK_HOST', label: 'TAK Server host / IP', placeholder: '192.168.20.6' },
    { key: 'TAK_PORT', label: 'TAK Server TLS port', placeholder: '8087' },
    { key: 'TAK_TLS', label: 'TAK TLS enabled (1/0)', placeholder: '1' },
  ] },
  { title: 'SitaWare and UTM', description: 'HTTPS endpoints and polling settings. Passwords and tokens are write-only.', fields: [
    { key: 'SITAWARE_URL', label: 'SitaWare HQ URL', placeholder: 'https://host.example' },
    { key: 'SITAWARE_API_PATH', label: 'SitaWare API path', placeholder: '/deployment-specific/path' },
    { key: 'SITAWARE_USER', label: 'SitaWare username' }, { key: 'SITAWARE_PASS', label: 'SitaWare password', secret: true },
    { key: 'SITAWARE_POLL_S', label: 'SitaWare poll seconds', placeholder: '10' },
    { key: 'SITAWARE_NVG_URL', label: 'NVG push endpoint (nvg_layer)', placeholder: 'https://sitaware.example:8080' },
    { key: 'SITAWARE_NVG_USER', label: 'NVG push username' },
    { key: 'SITAWARE_NVG_PASS', label: 'NVG push password', secret: true },
    { key: 'SITAWARE_NVG_SOURCE', label: 'NVG source name', placeholder: 'efdi-live' },
    { key: 'UTM_ANS_API_URL', label: 'UTM JSON/GeoJSON URL', placeholder: 'https://authorized-feed.example' },
    { key: 'UTM_ANS_API_TOKEN', label: 'UTM API token', secret: true },
  ] },
  { title: 'Video and metadata ingest', description: 'SRT/KLV sources and source naming for the STANAG 4609 bridge.', fields: [
    { key: 'STANAG4609_SRT_URL', label: 'STANAG 4609 SRT URL', placeholder: 'srt://host:port?mode=listener' },
    { key: 'STANAG4609_SOURCE', label: 'STANAG 4609 source label', placeholder: 'stanag_4609' },
  ] },
  { title: 'MQTT and IoT sensor feeds', description: 'MQTT broker ingress and OGC SensorThings polling. The MQTT bridge forwards payloads verbatim; the mqtt translator reads JSON only.', fields: [
    { key: 'MQTT_HOST', label: 'MQTT broker host', placeholder: 'broker.example' },
    { key: 'MQTT_PORT', label: 'MQTT broker port', placeholder: '1883' },
    { key: 'MQTT_TOPIC', label: 'MQTT subscription filters', placeholder: 'sensors/#' },
    { key: 'MQTT_USER', label: 'MQTT username' },
    { key: 'MQTT_PASS', label: 'MQTT password', secret: true },
    { key: 'MQTT_TLS', label: 'MQTT TLS enabled (1/0)', placeholder: '0' },
    { key: 'SENSORTHINGS_URL', label: 'SensorThings service root', placeholder: 'https://host/FROST-Server/v1.1' },
    { key: 'SENSORTHINGS_POLL_S', label: 'SensorThings poll seconds', placeholder: '30' },
    { key: 'SENSORTHINGS_TOKEN', label: 'SensorThings bearer token', secret: true },
  ] },
  { title: 'Sensors and data sources', description: 'Common partner endpoints. Protocol-specific CAT and raw-port settings are available under Advanced.', fields: [
    { key: 'ASTERIX_PORT', label: 'Mixed ASTERIX UDP port', placeholder: '50000' }, { key: 'ASTERIX_CATEGORIES', label: 'ASTERIX categories', placeholder: '34,48' },
    { key: 'CAT10_PORT', label: 'CAT-010 port', placeholder: '50010' }, { key: 'CAT21_PORT', label: 'CAT-021 port', placeholder: '50021' },
    { key: 'CAT34_PORT', label: 'CAT-034 port', placeholder: '50034' }, { key: 'CAT48_PORT', label: 'CAT-048 port', placeholder: '50048' },
    { key: 'CAT62_PORT', label: 'CAT-062 port', placeholder: '50062' }, { key: 'MAVLINK_PORT', label: 'MAVLink port' },
    { key: 'DJI_MQTT_HOST', label: 'DJI Cloud MQTT host' }, { key: 'DJI_MQTT_PORT', label: 'DJI MQTT port', placeholder: '8883' },
    { key: 'DJI_MQTT_USERNAME', label: 'DJI MQTT username' }, { key: 'DJI_MQTT_PASSWORD', label: 'DJI MQTT password', secret: true },
  ] },
]

const KNOWN_KEYS = new Set(FIELD_GROUPS.flatMap(group => group.fields.map(field => field.key)))

function Card({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <section className={`hud-frame relative rounded-md border border-zinc-200 bg-white p-5 dark:border-white/10 dark:bg-[#0c0c0e] ${className}`}><HudCorners />{children}</section>
}

function RuntimePage() {
  const { role } = useAuth()
  const canWrite = role === 'superadmin'
  const [runtime, setRuntime] = useState<RuntimeData | null>(null)
  const [catalog, setCatalog] = useState<CatalogItem[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [filter, setFilter] = useState('')
  const [group, setGroup] = useState('All')
  const [kind, setKind] = useState('All')
  const [values, setValues] = useState<Record<string, string>>({})
  const [secrets, setSecrets] = useState<Record<string, boolean>>({})
  const [editableKeys, setEditableKeys] = useState<string[]>([])
  const [advancedKey, setAdvancedKey] = useState('')
  const [advancedValue, setAdvancedValue] = useState('')
  const [openLogs, setOpenLogs] = useState<string | null>(null)
  const [logLines, setLogLines] = useState<string[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [selectionBusy, setSelectionBusy] = useState<string | null>(null)
  const showCorners = useUiSettings((s) => s.showCorners)
  const rowAnimations = useUiSettings((s) => s.rowAnimations)
  const denseRows = useUiSettings((s) => s.denseRows)
  const refreshIntervalMs = useUiSettings((s) => s.refreshIntervalMs)

  async function load() {
    setLoading(true)
    try {
      const [data, catalogData] = await Promise.all([
        apiJson<RuntimeData>('/api/runtime'),
        apiJson<{ services: CatalogItem[] }>('/api/runtime/catalog'),
      ])
      setRuntime(data)
      setCatalog(catalogData.services)
      setEditableKeys(data.editable_keys ?? [])
      const nextValues: Record<string, string> = {}
      const nextSecrets: Record<string, boolean> = {}
      for (const [key, value] of Object.entries(data.config)) {
        if (typeof value === 'string') nextValues[key] = value
        else nextSecrets[key] = value.configured
      }
      setValues(nextValues)
      setSecrets(nextSecrets)
    } catch (e) {
      notify.error(errorMessage(e))
    } finally { setLoading(false) }
  }

  useEffect(() => {
    load()
    const timer = window.setInterval(() => {
      void load()
    }, refreshIntervalMs)
    return () => window.clearInterval(timer)
  }, [refreshIntervalMs])

  // Runtime Control lists only integrations an operator manages. The router,
  // the control agent and the cert-renewer are the substrate the pod runs ON —
  // you never toggle them from here, and stopping one severs your own control —
  // so they are dropped entirely rather than shown as inert rows.
  const managed = useMemo(() => catalog.filter(item => item.kind !== 'infrastructure'), [catalog])
  const groups = useMemo(() => ['All', ...Array.from(new Set(managed.map(item => item.group)))], [managed])
  // Architectural role, independent of the business grouping above: a C2
  // integration cares whether a service ingests (bridge), decodes (protocol),
  // or writes out to a C2 system (layer).
  const kinds = useMemo(() => {
    const present = new Set(managed.map(item => item.kind).filter(Boolean) as string[])
    return ['All', 'bridge', 'protocol', 'layer'].filter(k => k === 'All' || present.has(k))
  }, [managed])
  const filtered = managed.filter(item =>
    (group === 'All' || item.group === group) &&
    (kind === 'All' || item.kind === kind) &&
    (!filter || `${item.name} ${item.description} ${item.source ?? ''}`.toLowerCase().includes(filter.toLowerCase()))
  ).sort((a, b) => {
    const ra = KIND_RANK[a.kind ?? 'bridge'] ?? 2
    const rb = KIND_RANK[b.kind ?? 'bridge'] ?? 2
    return ra - rb || a.name.localeCompare(b.name)
  })
  const runningCount = runtime?.services.filter(service => service.running).length ?? 0
  const configuredCount = Object.keys(runtime?.config ?? {}).length
  const selectedServices = useMemo(() => new Set(runtime?.selected_services ?? []), [runtime?.selected_services])
  const selectedList = runtime?.selected_services ?? []

  function setValue(key: string, value: string) { setValues(current => ({ ...current, [key]: value })) }

  async function saveConfig() {
    if (!canWrite) return
    setSaving(true)
    try {
      const payload: Record<string, string> = { ...values }
      for (const [key, configured] of Object.entries(secrets)) if (configured && !values[key]) delete payload[key]
      await apiJson('/api/runtime/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values: payload }) })
      notify.success('Runtime settings saved. Restart affected services to apply them.')
      await load()
    } catch (e) { notify.error(errorMessage(e)) } finally { setSaving(false) }
  }

  async function serviceAction(name: string, action: 'start' | 'stop' | 'restart') {
    if (!canWrite) return
    setBusy(`${name}:${action}`)
    try {
      await apiJson(`/api/runtime/services/${name}/${action}`, { method: 'POST' })
      const selected = new Set(runtime?.selected_services ?? [])
      if (action === 'stop') selected.delete(name)
      else selected.add(name)
      setRuntime(current => current ? ({ ...current, selected_services: Array.from(selected) }) : current)
      notify.success(`${name} ${action} requested`)
      await load()
    } catch (e) { notify.error(errorMessage(e)) } finally { setBusy(null) }
  }

  async function updateSelection(name: string, selected: boolean) {
    if (!canWrite) return
    setSelectionBusy(name)
    try {
      const current = new Set(runtime?.selected_services ?? [])
      if (selected) current.add(name)
      else current.delete(name)
      const data = await apiJson<{ selected_services: string[] }>('/api/runtime/selection', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ selected_services: Array.from(current) }),
      })
      setRuntime(prev => prev ? ({ ...prev, selected_services: data.selected_services }) : prev)
      notify.success('Saved launcher selection')
      await load()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setSelectionBusy(null)
    }
  }

  async function showLogs(name: string) {
    if (openLogs === name) { setOpenLogs(null); return }
    try {
      const data = await apiJson<{ lines: string[] }>(`/api/runtime/logs/${name}`)
      setLogLines(data.lines); setOpenLogs(name)
    } catch (e) { notify.error(errorMessage(e)) }
  }

  function addAdvanced() {
    const key = advancedKey.trim().toUpperCase()
    if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(key) || (!key.startsWith('CAT') && !key.startsWith('NFFI_') && !KNOWN_KEYS.has(key) && !editableKeys.includes(key))) {
      notify.error('Use an allowed protocol setting such as CAT48_TCP or NFFI_INPUT_TOPIC')
      return
    }
    setValue(key, advancedValue); setAdvancedKey(''); setAdvancedValue('')
  }

  const configuredKeys = useMemo(() => {
    const known = new Set(FIELD_GROUPS.flatMap(groupDef => groupDef.fields.map(field => field.key)))
    return Array.from(new Set([...Object.keys(values), ...Object.keys(secrets)]))
      .filter(key => !known.has(key))
      .sort()
  }, [values, secrets])

  function renderSetting(key: string) {
    const secret = secrets[key] !== undefined
    return <label key={key} className="space-y-1">
      <span className="flex items-center gap-1 text-xs text-zinc-600 dark:text-zinc-400">
        <span className="font-mono">{key}</span>
        {secret && <KeyRound size={11} className="text-amber-500" />}
        {secret && secrets[key] && <span className="ml-auto text-[10px] text-emerald-500">configured</span>}
      </span>
      <input
        type={secret ? 'password' : 'text'}
        disabled={!canWrite}
        value={values[key] ?? ''}
        onChange={e => setValue(key, e.target.value)}
        placeholder={secret && secrets[key] ? 'Leave blank to keep current value' : ''}
        className="w-full rounded-md border border-zinc-300 bg-zinc-100 px-2.5 py-2 font-mono text-xs text-zinc-900 focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-50 dark:border-white/10 dark:bg-[#141416] dark:text-white"
      />
    </label>
  }

  return (
    <Layout>
      <div className="p-6 max-w-7xl">
        <PageHeader title="Runtime Control" actions={<button onClick={load} disabled={loading} className="flex items-center gap-2 rounded-md px-3 py-2 text-sm text-zinc-600 hover:bg-zinc-200/50 disabled:opacity-50 dark:text-zinc-400 dark:hover:bg-white/[0.05]"><RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Reload</button>} />
        {!canWrite && <div className="mb-5 rounded-md border border-yellow-500/30 bg-yellow-500/10 px-4 py-3 text-xs text-yellow-700 dark:text-yellow-300">Read-only view. A superadmin is required to change settings or control processes.</div>}

        <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-5">
          {[['CONTROL AGENT', runtime ? `127.0.0.1:${runtime.control_port}` : 'offline'], ['RUNNING SERVICES', `${runningCount}/${runtime?.services.length ?? 0}`], ['SAVED SETTINGS', `${configuredCount}`], ['SAVED SELECTION', `${selectedList.length}`], ['CONFIG FILE', runtime?.env_file ?? '—']].map(([label, value]) => <Card key={label} className="p-4"><span className="hud-label text-[10px] text-zinc-500">{label}</span><p className="mt-2 truncate font-mono text-sm text-zinc-800 dark:text-zinc-200" title={value}>{value}</p></Card>)}
        </div>
        {selectedList.length > 0 && <Card className="mb-5 p-4"><div className="mb-2 flex items-center justify-between"><span className="hud-label text-[11px] font-semibold text-zinc-600 dark:text-zinc-300">Saved selection</span><span className="text-[10px] text-zinc-500">Restored automatically on the next launcher run</span></div><div className="flex flex-wrap gap-2">{selectedList.map(name => <span key={name} className="rounded-full border border-zinc-300 bg-zinc-100 px-2.5 py-1 font-mono text-[10px] text-zinc-700 dark:border-white/10 dark:bg-[#141416] dark:text-zinc-300">{name}</span>)}</div></Card>}

        <div className="grid items-start gap-5 xl:grid-cols-[1.1fr_1fr]">
          <Card>
            <div className="mb-4 flex items-start justify-between gap-3 border-b border-zinc-200 pb-4 dark:border-white/10"><div><h2 className="hud-label flex items-center gap-2 text-sm font-semibold"><Activity size={16} className="text-accent-ring" /> Bridge and layer control</h2><p className="mt-1 text-xs text-zinc-500">Start, stop, restart, and inspect every host-managed integration.</p></div><div className="relative"><Search size={14} className="absolute left-2.5 top-2.5 text-zinc-500" /><input value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter" className="w-32 rounded-md border border-zinc-300 bg-zinc-100 py-2 pl-8 pr-2 text-xs dark:border-white/10 dark:bg-[#141416]" /></div></div>
            <div className="mb-4 flex gap-1 overflow-x-auto pb-1">{groups.map(item => <button key={item} onClick={() => setGroup(item)} className={`whitespace-nowrap rounded-full border px-2.5 py-1 text-[10px] ${group === item ? 'border-accent-ring bg-accent-ring/10 text-accent-ring' : 'border-zinc-300 text-zinc-500 dark:border-zinc-700'}`}>{item}</button>)}</div>
            <div className="mb-4 flex items-center gap-1 overflow-x-auto pb-1"><span className="shrink-0 text-[10px] uppercase tracking-wider text-zinc-500">Role</span>{kinds.map(item => <button key={item} onClick={() => setKind(item)} className={`whitespace-nowrap rounded-full border px-2.5 py-1 text-[10px] ${kind === item ? 'border-accent-ring bg-accent-ring/10 text-accent-ring' : 'border-zinc-300 text-zinc-500 dark:border-zinc-700'}`}>{item}</button>)}</div>
            <div className={`max-h-[680px] space-y-1.5 overflow-auto pr-1 ${denseRows ? 'text-[11px]' : ''}`}>
              {filtered.map((item, index) => {
                const state = runtime?.services.find(service => service.name === item.name)
                const action = busy?.startsWith(`${item.name}:`) ? busy.split(':')[1] : null
                const selected = selectedServices.has(item.name)
                const presentation = servicePresentation(state, selected)
                return (
                  <div
                    key={item.name}
                    className={`hud-frame hud-card relative overflow-hidden rounded-md border border-zinc-200 bg-white px-3 ${denseRows ? 'py-2' : 'py-2.5'} transition duration-150 hover:-translate-y-px hover:border-accent-ring/30 dark:border-white/10 dark:bg-[#0c0c0e] ${rowAnimations ? 'hud-enter' : ''}`}
                    style={{ animationDelay: rowAnimations ? `${Math.min(index, 16) * 18}ms` : undefined }}
                  >
                    {showCorners && <HudCorners />}
                    <div className="relative z-10 flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={selected || item.kind === 'infrastructure'}
                        // Infrastructure is always in the launch set and cannot be
                        // deselected — dropping the router or control agent from the
                        // next launch is another way to lose control of the pod.
                        disabled={!canWrite || selectionBusy === item.name || item.kind === 'infrastructure'}
                        onChange={e => updateSelection(item.name, e.target.checked)}
                        className="h-4 w-4 rounded border-zinc-400 bg-transparent accent-accent-fill focus:ring-accent-ring disabled:opacity-30 dark:border-zinc-600"
                      />
                      <span className={`h-2 w-2 shrink-0 rounded-full ${presentation.dot}`} />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <p className="truncate font-mono text-xs text-zinc-800 dark:text-zinc-200">{item.name}</p>
                          {item.kind && (
                            <span className={`shrink-0 rounded border px-1 text-[9px] uppercase tracking-wider ${KIND_STYLE[item.kind] ?? KIND_STYLE.infrastructure}`}>
                              {item.kind}
                            </span>
                          )}
                        </div>
                        <p className="truncate text-[10px] text-zinc-500">
                          {item.description}{state?.pid ? ` · PID ${state.pid}` : ''}{serviceTelemetry(state)}
                        </p>
                        {item.source && (
                          <p className="truncate font-mono text-[10px] text-zinc-400 dark:text-zinc-600" title={item.source}>
                            {item.source}
                          </p>
                        )}
                      </div>
                      <span className={`hidden text-[10px] uppercase tracking-[0.18em] sm:inline ${presentation.text}`}>{presentation.label}</span>
                      <button title="Start" disabled={!canWrite || !!action || state?.running} onClick={() => serviceAction(item.name, 'start')} className="rounded p-1.5 text-zinc-500 transition hover:bg-emerald-500/10 hover:text-emerald-500 disabled:opacity-30"><Play size={13} /></button>
                      <button title={item.kind === 'infrastructure' ? 'Infrastructure cannot be stopped from here' : 'Stop'} disabled={!canWrite || !!action || !state?.running || item.kind === 'infrastructure'} onClick={() => serviceAction(item.name, 'stop')} className="rounded p-1.5 text-zinc-500 transition hover:bg-red-500/10 hover:text-red-500 disabled:opacity-30"><Square size={12} /></button>
                      <button title="Restart" disabled={!canWrite || !!action || item.name === 'admin-control'} onClick={() => serviceAction(item.name, 'restart')} className="rounded p-1.5 text-zinc-500 transition hover:bg-accent-ring/10 hover:text-accent-ring disabled:opacity-30"><RefreshCw size={13} className={action === 'restart' ? 'animate-spin' : ''} /></button>
                      <button title="Show logs" onClick={() => showLogs(item.name)} className="rounded p-1.5 text-zinc-500 transition hover:bg-zinc-200 dark:hover:bg-white/10"><FileText size={13} /></button>
                    </div>
                    {openLogs === item.name && <pre className="relative z-10 mt-2 max-h-40 overflow-auto rounded bg-zinc-950 p-2 font-mono text-[10px] leading-4 text-zinc-300">{logLines.join('\n') || 'No log output yet.'}</pre>}
                  </div>
                )
              })}
            </div>
          </Card>

          <Card>
            <div className="mb-4 flex items-start justify-between gap-3 border-b border-zinc-200 pb-4 dark:border-white/10"><div><h2 className="hud-label flex items-center gap-2 text-sm font-semibold"><Settings2 size={16} className="text-accent-ring" /> Integration settings</h2><p className="mt-1 text-xs text-zinc-500">Edit endpoints, ports, topics, and credentials without opening `.env` over SSH.</p></div><button disabled={!canWrite || saving} onClick={saveConfig} className="flex items-center gap-2 rounded-md bg-accent-fill px-3 py-2 text-xs text-accent-text disabled:opacity-50"><Save size={13} /> {saving ? 'Saving…' : 'Save settings'}</button></div>
            <div className="space-y-5">
              {FIELD_GROUPS.map(groupDef => (
                <div key={groupDef.title}>
                  <h3 className="hud-label text-[11px] font-semibold text-zinc-600 dark:text-zinc-300">{groupDef.title}</h3>
                  <p className="mb-3 mt-1 text-[11px] text-zinc-500">{groupDef.description}</p>
                  <div className="grid gap-3 sm:grid-cols-2">
                    {groupDef.fields.map(field => {
                      const secret = field.secret || secrets[field.key] !== undefined
                      return (
                        <label key={field.key} className="space-y-1">
                          <span className="flex items-center gap-1 text-xs text-zinc-600 dark:text-zinc-400">
                            {field.label}
                            {secret && <KeyRound size={11} className="text-amber-500" />}
                            {secret && secrets[field.key] && <span className="ml-auto text-[10px] text-emerald-500">configured</span>}
                          </span>
                          <input
                            type={secret ? 'password' : 'text'}
                            disabled={!canWrite}
                            value={values[field.key] ?? ''}
                            onChange={e => setValue(field.key, e.target.value)}
                            placeholder={secret && secrets[field.key] ? 'Leave blank to keep current value' : field.placeholder}
                            className="w-full rounded-md border border-zinc-300 bg-zinc-100 px-2.5 py-2 font-mono text-xs text-zinc-900 focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-50 dark:border-white/10 dark:bg-[#141416] dark:text-white"
                          />
                        </label>
                      )
                    })}
                  </div>
                </div>
              ))}
            </div>
            <div className="mt-5 border-t border-zinc-200 pt-4 dark:border-white/10"><h3 className="hud-label flex items-center gap-2 text-[11px] font-semibold text-zinc-600 dark:text-zinc-300"><Wrench size={13} /> Advanced protocol setting</h3><p className="mb-2 mt-1 text-[11px] text-zinc-500">Use this for CATxx_TCP, raw ports, input topics, and other documented `.env` fields.</p><div className="flex gap-2"><input value={advancedKey} onChange={e => setAdvancedKey(e.target.value)} placeholder="CAT48_TCP" className="w-32 rounded-md border border-zinc-300 bg-zinc-100 px-2.5 py-2 font-mono text-xs dark:border-white/10 dark:bg-[#141416]" /><input value={advancedValue} onChange={e => setAdvancedValue(e.target.value)} placeholder="value" className="min-w-0 flex-1 rounded-md border border-zinc-300 bg-zinc-100 px-2.5 py-2 font-mono text-xs dark:border-white/10 dark:bg-[#141416]" /><button disabled={!canWrite} onClick={addAdvanced} className="rounded-md border border-accent-ring/50 px-3 text-xs text-accent-ring disabled:opacity-40">Add</button></div></div>
            {configuredKeys.length > 0 && <div className="mt-5 border-t border-zinc-200 pt-4 dark:border-white/10"><h3 className="hud-label text-[11px] font-semibold text-zinc-600 dark:text-zinc-300">Additional deployment settings</h3><p className="mb-3 mt-1 text-[11px] text-zinc-500">These fields are present in the deployment environment but are not tied to a fixed partner form.</p><div className="grid gap-3 sm:grid-cols-2">{configuredKeys.map(renderSetting)}</div></div>}
            <div className="mt-5 rounded-md border border-amber-500/20 bg-amber-500/10 p-3 text-[11px] text-amber-700 dark:text-amber-300"><Terminal size={13} className="mb-1" />Saving changes updates the deployment environment. Restart only the affected service after checking its log; secrets are write-only and never displayed.</div>
          </Card>
        </div>
      </div>
    </Layout>
  )
}
