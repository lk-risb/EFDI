import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Layout } from '@/components/Layout'
import { apiJson } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { CheckCircle, XCircle } from 'lucide-react'
import { cn } from '@/lib/utils'

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
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">{label}</span>
      <p className="text-sm font-medium font-mono text-zinc-200 mt-2">{value}</p>
    </div>
  )
}

function KeyExprList({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">{label}</span>
      <div className="mt-3 space-y-1 max-h-64 overflow-auto">
        {items.map(k => (
          <p key={k} className="text-xs font-mono text-zinc-400 truncate">{k}</p>
        ))}
      </div>
    </div>
  )
}

function SystemStatCard({ label, value, state = 'ok' }: { label: string; value: string; state?: 'ok' | 'warn' | 'critical' }) {
  const badgeClass = state === 'critical' ? 'border-red-800 bg-red-500/10' : state === 'warn' ? 'border-yellow-800 bg-yellow-500/10' : 'border-zinc-700 bg-zinc-800/50'
  const textClass = state === 'critical' ? 'text-red-400' : state === 'warn' ? 'text-yellow-400' : 'text-zinc-200'
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      <div className="flex items-start justify-between mb-3">
        <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">{label}</span>
        <div className={cn('w-7 h-7 rounded-md border shrink-0', badgeClass)} />
      </div>
      <p className={cn('text-sm font-medium font-mono', textClass)}>{value}</p>
    </div>
  )
}

function CertList({ certs }: { certs: CertInfo[] }) {
  if (certs.length === 0) return null
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4 mb-6">
      <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">Certificates</span>
      <div className="mt-3 space-y-2">
        {certs.map(c => {
          const state = c.days_remaining < 7 ? 'critical' : c.days_remaining < 30 ? 'warn' : 'ok'
          const textClass = state === 'critical' ? 'text-red-400' : state === 'warn' ? 'text-yellow-400' : 'text-green-400'
          return (
            <div key={c.name} className="flex items-center justify-between text-sm">
              <span className="font-mono text-zinc-300">{c.name}</span>
              <span className="text-zinc-500">{c.expires_at}</span>
              <span className={cn('font-mono text-xs', textClass)}>{c.days_remaining}d remaining</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ServiceCard({ service }: { service: ServiceState }) {
  const running = service.status === 'running'
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      <div className="flex items-start justify-between mb-3">
        <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">Service</span>
        <div className={cn(
          'w-7 h-7 rounded-md border flex items-center justify-center shrink-0',
          running ? 'border-green-800 bg-green-500/10' : 'border-red-800 bg-red-500/10'
        )}>
          {running
            ? <CheckCircle size={14} className="text-green-500" />
            : <XCircle size={14} className="text-red-500" />
          }
        </div>
      </div>
      <p className="text-sm font-medium text-zinc-200 font-mono mb-2">{service.name}</p>
      <div className="border-t border-zinc-800 pt-2">
        <span className={cn('text-xs font-medium', running ? 'text-green-400' : 'text-red-400')}>
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
        <h1 className="text-xl font-semibold mb-6">Dashboard</h1>

        {system && (
          <>
            <div className="grid grid-cols-2 lg:grid-cols-6 gap-4 mb-6">
              <SystemStatCard label="CPU" value={system.cpu_percent !== null ? `${system.cpu_percent}%` : '—'} />
              <SystemStatCard label="RAM" value={`${(system.mem_used_mb / 1024).toFixed(1)} / ${(system.mem_total_mb / 1024).toFixed(1)} GB`} />
              <SystemStatCard label="Disk" value={`${system.disk_used_gb.toFixed(1)} / ${system.disk_total_gb.toFixed(1)} GB`} state={diskState} />
              <SystemStatCard label="Uptime" value={formatUptime(system.uptime_seconds)} />
              <SystemStatCard label="Load avg" value={system.load_avg.map(n => n.toFixed(2)).join(' / ')} />
              <SystemStatCard label="Network" value={`↓${formatBytesPerSec(system.net_rx_bytes_per_sec)} ↑${formatBytesPerSec(system.net_tx_bytes_per_sec)}`} />
            </div>
            <CertList certs={system.certs} />
          </>
        )}

        {services.length > 0 && (
          <>
            <h2 className="text-sm font-semibold text-zinc-400 uppercase tracking-wider mb-3">Services</h2>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
              {services.map(s => (
                <ServiceCard key={s.name} service={s} />
              ))}
            </div>
          </>
        )}

        {status && (
          <>
            <h2 className="text-sm font-semibold text-zinc-400 uppercase tracking-wider mb-3">Zenoh</h2>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
              <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
                <div className="flex items-start justify-between mb-3">
                  <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">Router</span>
                  <div className={cn(
                    'w-7 h-7 rounded-md border flex items-center justify-center shrink-0',
                    status.connected ? 'border-green-800 bg-green-500/10' : 'border-red-800 bg-red-500/10'
                  )}>
                    {status.connected
                      ? <CheckCircle size={14} className="text-green-500" />
                      : <XCircle size={14} className="text-red-500" />}
                  </div>
                </div>
                <p className={cn('text-sm font-medium', status.connected ? 'text-green-400' : 'text-red-400')}>
                  {status.connected ? 'connected' : 'disconnected'}
                </p>
              </div>
              <StatCard label="Router ZID" value={status.router_zid ?? '—'} />
              <StatCard label="Endpoint" value={status.endpoint} />
              <StatCard label="Admin space" value={status.admin_space_reachable ? 'reachable' : 'unreachable'} />
            </div>
            <div className="grid grid-cols-2 lg:grid-cols-3 gap-4 mb-6">
              <StatCard label="Subscribers" value={String(status.subscriber_count)} />
              <StatCard label="Queryables" value={String(status.queryable_count)} />
              <StatCard label="Storages" value={String(status.storages.length)} />
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <KeyExprList label="Subscribers" items={status.subscribers} />
              <KeyExprList label="Queryables" items={status.queryables} />
            </div>
          </>
        )}
      </div>
    </Layout>
  )
}
