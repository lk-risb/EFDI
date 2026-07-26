import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useMemo, useState} from 'react'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {HudCorners} from '@/components/HudCorners'
import {apiJson, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'
import {useUiSettings} from '@/store/ui'
import {Activity, FileText, Play, RefreshCw, Search, Square,} from 'lucide-react'

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
  reason?: string
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
  // Name the missing setting instead of leaving a service that cannot start
  // looking like one that merely happens to be down.
  if (state?.status === 'needs-config' && details.reason) parts.push(details.reason)
  if (details.tracks !== undefined) parts.push(`${details.tracks} tracks`)
  if (state?.status === 'waiting-for-client') parts.push('no successful HQ pulls')
  if (state?.status === 'auth-failed') parts.push(`${details.unauthorized_requests ?? 0} rejected pulls`)
  if ((state?.status === 'client-connected' || state?.status === 'client-stale') && details.seconds_since_last_success != null) {
    parts.push(`last HQ pull ${Math.round(details.seconds_since_last_success)}s ago`)
  }
  return parts.length ? ` · ${parts.join(' · ')}` : ''
}


function Card({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <section className={`hud-frame hud-glass relative rounded-md border border-zinc-200 p-5 dark:border-white/10 ${className}`}><HudCorners />{children}</section>
}

function RuntimePage() {
  const { role } = useAuth()
  const canWrite = role === 'superadmin'
  const [runtime, setRuntime] = useState<RuntimeData | null>(null)
  const [catalog, setCatalog] = useState<CatalogItem[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  const [group, setGroup] = useState('All')
  const [kind, setKind] = useState('All')
  const [openLogs, setOpenLogs] = useState<string | null>(null)
  const [logLines, setLogLines] = useState<string[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [selectionBusy, setSelectionBusy] = useState<string | null>(null)
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




  return (
    <Layout>
      <div className="p-6 max-w-7xl">
        <PageHeader title="Runtime Control" actions={<button onClick={load} disabled={loading} className="flex items-center gap-2 rounded-md px-3 py-2 text-sm text-zinc-600 hover:bg-zinc-200/50 disabled:opacity-50 dark:text-zinc-400 dark:hover:bg-white/[0.05]"><RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Reload</button>} />
        {!canWrite && <div className="mb-5 rounded-md border border-yellow-500/30 bg-yellow-500/10 px-4 py-3 text-xs text-yellow-700 dark:text-yellow-300">Read-only view. A superadmin is required to change settings or control processes.</div>}

        <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-5">
          {[['CONTROL AGENT', runtime ? `127.0.0.1:${runtime.control_port}` : 'offline'], ['RUNNING SERVICES', `${runningCount}/${runtime?.services.length ?? 0}`], ['SAVED SETTINGS', `${configuredCount}`], ['SAVED SELECTION', `${selectedList.length}`], ['CONFIG FILE', runtime?.env_file ?? '—']].map(([label, value]) => <Card key={label} className="p-5"><span className="hud-label text-[10px] text-zinc-500">{label}</span><p className="mt-2 truncate text-xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100" title={value}>{value}</p></Card>)}
        </div>
        {selectedList.length > 0 && <Card className="mb-5 p-4"><div className="mb-2 flex items-center justify-between"><span className="hud-label text-[11px] font-semibold text-zinc-600 dark:text-zinc-300">Saved selection</span><span className="text-[10px] text-zinc-500">Restored automatically on the next launcher run</span></div><div className="flex flex-wrap gap-2">{selectedList.map(name => <span key={name} className="rounded-full border border-zinc-300 bg-zinc-100 px-2.5 py-1 font-mono text-[10px] text-zinc-700 dark:border-white/10 dark:bg-[#141416] dark:text-zinc-300">{name}</span>)}</div></Card>}

        <Card>
            <div className="mb-4 flex items-start justify-between gap-3 border-b border-zinc-200 pb-4 dark:border-white/10"><div><h2 className="flex items-center gap-2 text-base font-semibold text-zinc-900 dark:text-zinc-100"><Activity size={16} className="text-accent-ring" /> Bridge and layer control</h2><p className="mt-1 text-xs text-zinc-500">Start, stop, restart, and inspect every host-managed integration.</p></div><div className="relative"><Search size={14} className="absolute left-2.5 top-2.5 text-zinc-500" /><input value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter" className="w-32 rounded-md border border-zinc-300 bg-zinc-100 py-2 pl-8 pr-2 text-xs dark:border-white/10 dark:bg-[#141416]" /></div></div>
            <div className="mb-4 flex gap-1 overflow-x-auto pb-1">{groups.map(item => <button key={item} onClick={() => setGroup(item)} className={`whitespace-nowrap rounded-full border px-2.5 py-1 text-[10px] ${group === item ? 'border-accent-ring bg-accent-ring/10 text-accent-ring' : 'border-zinc-300 text-zinc-500 dark:border-zinc-700'}`}>{item}</button>)}</div>
            <div className="mb-4 flex items-center gap-1 overflow-x-auto pb-1"><span className="shrink-0 text-[10px] uppercase tracking-wider text-zinc-500">Role</span>{kinds.map(item => <button key={item} onClick={() => setKind(item)} className={`whitespace-nowrap rounded-full border px-2.5 py-1 text-[10px] ${kind === item ? 'border-accent-ring bg-accent-ring/10 text-accent-ring' : 'border-zinc-300 text-zinc-500 dark:border-zinc-700'}`}>{item}</button>)}</div>
            <div className={`-mx-2 divide-y divide-zinc-200/60 dark:divide-white/[0.05] ${denseRows ? 'text-[11px]' : ''}`}>
              {filtered.map((item, index) => {
                const state = runtime?.services.find(service => service.name === item.name)
                const action = busy?.startsWith(`${item.name}:`) ? busy.split(':')[1] : null
                const selected = selectedServices.has(item.name)
                const presentation = servicePresentation(state, selected)
                // Set only while the service genuinely cannot start. A running
                // service is never blocked, whatever its configuration says.
                const blockedReason = !state?.running && state?.status === 'needs-config'
                  ? (state.details?.reason || 'required configuration is missing')
                  : ''
                return (
                  <div
                    key={item.name}
                    className={`group relative px-3 ${denseRows ? 'py-2.5' : 'py-3'} transition-colors hover:bg-black/[0.02] dark:hover:bg-white/[0.03] ${rowAnimations ? 'hud-enter' : ''}`}
                    style={{ animationDelay: rowAnimations ? `${Math.min(index, 16) * 18}ms` : undefined }}
                  >
                    <div className="flex items-center gap-3">
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
                          <p className="truncate text-sm font-semibold text-zinc-900 dark:text-zinc-100">{item.name}</p>
                          {item.kind && (
                            <span className={`shrink-0 rounded px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide ${KIND_STYLE[item.kind] ?? KIND_STYLE.infrastructure}`}>
                              {item.kind}
                            </span>
                          )}
                        </div>
                        <p className="mt-0.5 truncate text-xs text-zinc-500 dark:text-zinc-400">
                          {item.description}{state?.pid ? ` · PID ${state.pid}` : ''}{serviceTelemetry(state)}
                        </p>
                        {item.source && (
                          <p className="truncate font-mono text-[10px] text-zinc-400 dark:text-zinc-600" title={item.source}>
                            {item.source}
                          </p>
                        )}
                      </div>
                      <span className={`hidden text-[10px] uppercase tracking-[0.18em] sm:inline ${presentation.text}`}>{presentation.label}</span>
                      <button title={blockedReason ? `Cannot start — ${blockedReason}` : 'Start'} disabled={!canWrite || !!action || state?.running || !!blockedReason} onClick={() => serviceAction(item.name, 'start')} className="rounded p-1.5 text-zinc-500 transition hover:bg-emerald-500/10 hover:text-emerald-500 disabled:opacity-30"><Play size={13} /></button>
                      <button title={item.kind === 'infrastructure' ? 'Infrastructure cannot be stopped from here' : 'Stop'} disabled={!canWrite || !!action || !state?.running || item.kind === 'infrastructure'} onClick={() => serviceAction(item.name, 'stop')} className="rounded p-1.5 text-zinc-500 transition hover:bg-red-500/10 hover:text-red-500 disabled:opacity-30"><Square size={12} /></button>
                      <button title={blockedReason ? `Cannot restart — ${blockedReason}` : 'Restart'} disabled={!canWrite || !!action || item.name === 'admin-control' || !!blockedReason} onClick={() => serviceAction(item.name, 'restart')} className="rounded p-1.5 text-zinc-500 transition hover:bg-accent-ring/10 hover:text-accent-ring disabled:opacity-30"><RefreshCw size={13} className={action === 'restart' ? 'animate-spin' : ''} /></button>
                      <button title="Show logs" onClick={() => showLogs(item.name)} className="rounded p-1.5 text-zinc-500 transition hover:bg-zinc-200 dark:hover:bg-white/10"><FileText size={13} /></button>
                    </div>
                    {openLogs === item.name && <pre className="relative z-10 mt-2 max-h-40 overflow-auto rounded bg-zinc-950 p-2 font-mono text-[10px] leading-4 text-zinc-300">{logLines.join('\n') || 'No log output yet.'}</pre>}
                  </div>
                )
              })}
            </div>
          </Card>


      </div>
    </Layout>
  )
}
