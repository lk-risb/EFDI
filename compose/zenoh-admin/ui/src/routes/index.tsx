import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Layout } from '@/components/Layout'
import { PageHeader } from '@/components/PageHeader'
import { HudCorners } from '@/components/HudCorners'
import { apiJson } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { CheckCircle, XCircle, ChevronRight, Radio, Cpu, MemoryStick, HardDrive, Clock, Activity, Network, type LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Skeleton } from '@/components/Skeleton'

export const Route = createFileRoute('/')({
  beforeLoad: () => {
    const { token } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
  },
  component: DashboardPage,
})

interface StatusData {
  connected: boolean
  router_zid: string | null
  endpoint: string
  admin_space_reachable: boolean
  subscriber_count: number
  subscribers: string[]
  queryable_count: number
  queryables: string[]
  storages: string[]
  peer_count: number
  peers: PeerInfo[]
}

interface PeerInfo {
  zid: string
  whatami: string
  link_count: number | null
}

interface ServiceState {
  name: string
  status: string
  health: string
}

interface CertInfo {
  name: string
  expires_at: string
  days_remaining: number
}

interface SystemStats {
  cpu_percent: number | null
  mem_used_mb: number
  mem_total_mb: number
  disk_used_gb: number
  disk_total_gb: number
  uptime_seconds: number
  load_avg: [number, number, number]
  net_rx_bytes_per_sec: number | null
  net_tx_bytes_per_sec: number | null
  certs: CertInfo[]
}

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${mins}m`
  return `${mins}m`
}

function formatBytesPerSec(bytes: number | null): string {
  if (bytes === null) return '—'
  if (bytes < 1024) return `${bytes.toFixed(0)} B/s`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB/s`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB/s`
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-4 hud-card">
      <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">{label}</span>
      <p className="text-sm font-medium font-mono text-zinc-800 dark:text-zinc-200 mt-2">{value}</p>
    </div>
  )
}

const WHATAMI_LABEL: Record<string, string> = {
  router: 'Router', peer: 'Peer', client: 'Client',
}

function PeerList({ peers }: { peers: PeerInfo[] }) {
  return (
    <div className="rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-4 hud-card">
      <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">Connected routers</span>
      {peers.length === 0 ? (
        <p className="text-xs text-zinc-400 dark:text-zinc-600 mt-3">no peer links reported</p>
      ) : (
        <div className="mt-3 space-y-2 max-h-80 overflow-auto">
          {peers.map(p => (
            <div key={p.zid} className="flex items-center gap-2 text-sm">
              <Radio size={12} className="text-sky-500 hud-live-dot shrink-0" />
              <span className="font-mono text-xs text-zinc-700 dark:text-zinc-300 truncate flex-1">{p.zid}</span>
              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded border text-zinc-600 dark:text-zinc-400 bg-zinc-200 dark:bg-zinc-400/10 border-zinc-300 dark:border-zinc-700 shrink-0">
                {WHATAMI_LABEL[p.whatami] ?? p.whatami}
              </span>
              {p.link_count !== null && (
                <span className="text-xs text-zinc-500 shrink-0">{p.link_count} link{p.link_count === 1 ? '' : 's'}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Groups a raw key_expr like "LTU/CISB/<namespace>/air/**/civ/aircraft/**" into
// a domain bucket ("AIR") + the remainder path, so the long, repetitive
// namespace prefix doesn't have to be read over and over to see what's what.
function groupTopic(keyExpr: string): { group: string; rest: string } {
  if (keyExpr.startsWith('@/')) return { group: 'ADMIN', rest: keyExpr }
  const parts = keyExpr.split('/')
  if (parts[0] === 'LTU' && parts[1] === 'CISB' && parts.length > 3) {
    return { group: (parts[3] || 'other').toUpperCase(), rest: parts.slice(3).join('/') }
  }
  return { group: 'OTHER', rest: keyExpr }
}

const GROUP_COLORS: Record<string, string> = {
  AIR: 'text-sky-600 dark:text-sky-400 bg-sky-100 dark:bg-sky-400/10 border-sky-300 dark:border-sky-800',
  LAND: 'text-green-600 dark:text-green-400 bg-green-100 dark:bg-green-400/10 border-green-300 dark:border-green-800',
  SEA: 'text-cyan-600 dark:text-cyan-400 bg-cyan-100 dark:bg-cyan-400/10 border-cyan-300 dark:border-cyan-800',
  SPACE: 'text-purple-600 dark:text-purple-400 bg-purple-100 dark:bg-purple-400/10 border-purple-300 dark:border-purple-800',
  ENV: 'text-amber-600 dark:text-amber-400 bg-amber-100 dark:bg-amber-400/10 border-amber-300 dark:border-amber-800',
}

function groupTopics(items: string[]): { group: string; items: string[] }[] {
  const map = new Map<string, string[]>()
  for (const item of items) {
    const { group, rest } = groupTopic(item)
    if (!map.has(group)) map.set(group, [])
    map.get(group)!.push(rest)
  }
  return Array.from(map.entries())
    .map(([group, items]) => ({ group, items: items.sort() }))
    .sort((a, b) => b.items.length - a.items.length)
}

function TopicTree({ label, items }: { label: string; items: string[] }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  function toggle(group: string) {
    setExpanded(s => {
      const next = new Set(s)
      if (next.has(group)) next.delete(group); else next.add(group)
      return next
    })
  }

  return (
    <div className="rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-4 hud-card">
      <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">{label}</span>
      {items.length === 0 ? (
        <p className="text-xs text-zinc-400 dark:text-zinc-600 mt-3">none</p>
      ) : (
        <div className="mt-3 space-y-1 max-h-80 overflow-auto">
          {groupTopics(items).map(({ group, items: groupItems }) => {
            const isOpen = expanded.has(group)
            const colorClass = GROUP_COLORS[group] ?? 'text-zinc-600 dark:text-zinc-400 bg-zinc-200 dark:bg-zinc-400/10 border-zinc-300 dark:border-zinc-700'
            return (
              <div key={group}>
                <button onClick={() => toggle(group)}
                  className="flex items-center gap-2 w-full text-left px-2 py-1.5 rounded-md hover:bg-zinc-200/50 dark:hover:bg-white/[0.05] transition-colors">
                  <ChevronRight size={12} className={cn('text-zinc-500 transition-transform shrink-0', isOpen && 'rotate-90')} />
                  <span className={cn('text-xs font-semibold px-1.5 py-0.5 rounded border shrink-0', colorClass)}>{group}</span>
                  <span className="text-xs text-zinc-500">{groupItems.length}</span>
                </button>
                {isOpen && (
                  <div className="ml-6 space-y-0.5 mt-1 mb-1">
                    {groupItems.map(item => (
                      <p key={item} className="text-xs font-mono text-zinc-500 truncate">{item || '**'}</p>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function SystemStatCard({ icon: Icon, label, value, state = 'ok' }: { icon: LucideIcon; label: string; value: string; state?: 'ok' | 'warn' | 'critical' }) {
  const badgeClass = state === 'critical' ? 'border-red-300 dark:border-red-800 bg-red-100 dark:bg-red-500/10' : state === 'warn' ? 'border-yellow-300 dark:border-yellow-800 bg-yellow-100 dark:bg-yellow-500/10' : 'border-zinc-300 dark:border-white/10 bg-zinc-200/50 dark:bg-white/[0.04]'
  const textClass = state === 'critical' ? 'text-red-600 dark:text-red-400' : state === 'warn' ? 'text-yellow-600 dark:text-yellow-400' : 'text-zinc-800 dark:text-zinc-200'
  return (
    <div className="hud-frame rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-4 hud-card">
      <HudCorners />
      <div className="flex items-start justify-between mb-3">
        <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">{label}</span>
        <div className={cn('w-7 h-7 rounded-md border flex items-center justify-center shrink-0', badgeClass)}>
          <Icon size={14} className={textClass} />
        </div>
      </div>
      <p className={cn('text-sm font-medium font-mono', textClass)}>{value}</p>
    </div>
  )
}

function ServiceCard({ service }: { service: ServiceState }) {
  const running = service.status === 'running'
  return (
    <div className="rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-4 hud-card">
      <div className="flex items-start justify-between mb-3">
        <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">Service</span>
        <div className={cn(
          'w-7 h-7 rounded-md border flex items-center justify-center shrink-0',
          running ? 'border-green-300 dark:border-green-800 bg-green-100 dark:bg-green-500/10' : 'border-red-300 dark:border-red-800 bg-red-100 dark:bg-red-500/10'
        )}>
          {running
            ? <CheckCircle size={14} className="text-green-600 dark:text-green-500" />
            : <XCircle size={14} className="text-red-600 dark:text-red-500" />
          }
        </div>
      </div>
      <p className="text-sm font-medium text-zinc-800 dark:text-zinc-200 font-mono mb-2">{service.name}</p>
      <div className="border-t border-zinc-200 dark:border-white/10 pt-2">
        <span className={cn('text-xs font-medium', running ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400')}>
          {service.status}
        </span>
      </div>
    </div>
  )
}

function DashboardPage() {
  const [status, setStatus] = useState<StatusData | null>(null)
  const [services, setServices] = useState<ServiceState[]>([])
  const [system, setSystem] = useState<SystemStats | null>(null)

  async function load() {
    try {
      const data = await apiJson<StatusData>('/api/status')
      setStatus(data)
    } catch {}
    try {
      const health = await apiJson<{ services: ServiceState[]; system: SystemStats }>('/api/health')
      setServices(health.services)
      setSystem(health.system)
    } catch {}
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [])

  const diskPercent = system ? (system.disk_used_gb / system.disk_total_gb) * 100 : 0
  const diskState = diskPercent >= 95 ? 'critical' : diskPercent >= 85 ? 'warn' : 'ok'

  return (
    <Layout>
      <div className="p-6">
        <PageHeader title="Dashboard" />

        <div className="grid grid-cols-2 lg:grid-cols-6 gap-4 mb-6 hud-enter">
          {system ? (
            <>
              <SystemStatCard icon={Cpu} label="CPU" value={system.cpu_percent !== null ? `${system.cpu_percent}%` : '—'} />
              <SystemStatCard icon={MemoryStick} label="RAM" value={`${(system.mem_used_mb / 1024).toFixed(1)} / ${(system.mem_total_mb / 1024).toFixed(1)} GB`} />
              <SystemStatCard icon={HardDrive} label="Disk" value={`${system.disk_used_gb.toFixed(1)} / ${system.disk_total_gb.toFixed(1)} GB`} state={diskState} />
              <SystemStatCard icon={Clock} label="Uptime" value={formatUptime(system.uptime_seconds)} />
              <SystemStatCard icon={Activity} label="Load avg" value={system.load_avg.map(n => n.toFixed(2)).join(' / ')} />
              <SystemStatCard icon={Network} label="Network" value={`↓${formatBytesPerSec(system.net_rx_bytes_per_sec)} ↑${formatBytesPerSec(system.net_tx_bytes_per_sec)}`} />
            </>
          ) : (
            Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-4 hud-card">
                <div className="flex items-start justify-between mb-3">
                  <Skeleton className="h-2.5 w-10" />
                  <Skeleton className="w-7 h-7 rounded-md" />
                </div>
                <Skeleton className="h-4 w-24" />
              </div>
            ))
          )}
        </div>
        {services.length > 0 && (
          <>
            <h2 className="hud-label text-sm font-semibold text-zinc-600 dark:text-zinc-400 mb-3">Services</h2>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6 hud-enter hud-enter-delay-1">
              {services.map(s => (
                <ServiceCard key={s.name} service={s} />
              ))}
            </div>
          </>
        )}

        {status && (
          <>
            <h2 className="hud-label text-sm font-semibold text-zinc-600 dark:text-zinc-400 mb-3">Zenoh</h2>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6 hud-enter hud-enter-delay-2">
              <div className="rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] p-4 hud-card">
                <div className="flex items-start justify-between mb-3">
                  <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">Router</span>
                  <div className={cn(
                    'w-7 h-7 rounded-md border flex items-center justify-center shrink-0',
                    status.connected ? 'border-green-300 dark:border-green-800 bg-green-100 dark:bg-green-500/10' : 'border-red-300 dark:border-red-800 bg-red-100 dark:bg-red-500/10'
                  )}>
                    {status.connected
                      ? <CheckCircle size={14} className="text-green-600 dark:text-green-500 hud-live-dot" />
                      : <XCircle size={14} className="text-red-600 dark:text-red-500" />}
                  </div>
                </div>
                <p className={cn('text-sm font-medium', status.connected ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400')}>
                  {status.connected ? 'connected' : 'disconnected'}
                </p>
              </div>
              <StatCard label="Router ZID" value={status.router_zid ?? '—'} />
              <StatCard label="Endpoint" value={status.endpoint} />
              <StatCard label="Admin space" value={status.admin_space_reachable ? 'reachable' : 'unreachable'} />
            </div>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
              <StatCard label="Subscribers" value={String(status.subscriber_count)} />
              <StatCard label="Queryables" value={String(status.queryable_count)} />
              <StatCard label="Storages" value={String(status.storages.length)} />
              <StatCard label="Connected routers" value={String(status.peer_count)} />
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
              <TopicTree label="Subscribing to" items={status.subscribers} />
              <TopicTree label="Answering queries for" items={status.queryables} />
            </div>
            <div className="grid grid-cols-1 gap-4">
              <PeerList peers={status.peers} />
            </div>
          </>
        )}
      </div>
    </Layout>
  )
}
