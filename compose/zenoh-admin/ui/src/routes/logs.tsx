import { createFileRoute, redirect, useSearch } from '@tanstack/react-router'
import { useEffect, useRef, useState } from 'react'
import { Layout } from '@/components/Layout'
import { PageHeader } from '@/components/PageHeader'
import { useAuth } from '@/store/auth'
import { apiFetch, apiJson } from '@/lib/api'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'

export const Route = createFileRoute('/logs')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'admin' && role !== 'superadmin') throw redirect({ to: '/' })
  },
  validateSearch: (search: Record<string, unknown>): { service?: string } => ({
    service: typeof search.service === 'string' ? search.service : undefined,
  }),
  component: LogsPage,
})

type CatalogItem = { name: string; group: string; description: string }

function LogsPage() {
  const search = useSearch({ from: '/logs' })
  const [services, setServices] = useState<CatalogItem[]>([])
  const [service, setService] = useState(search.service ?? '')
  const termRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    apiJson<{ services: CatalogItem[] }>('/api/runtime/catalog')
      .then((data) => {
        setServices(data.services)
        setService((current) => data.services.some((item) => item.name === current)
          ? current
          : search.service && data.services.some((item) => item.name === search.service)
            ? search.service
            : data.services[0]?.name ?? '')
      })
      .catch(() => {})
  }, [search.service])

  useEffect(() => {
    if (!termRef.current || !service) return
    const term = new Terminal({ theme: { background: '#09090b' }, convertEol: true, scrollback: 2000 })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(termRef.current)
    fit.fit()

    let closedByUs = false
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    const connect = async () => {
      let ticket: string
      try {
        const response = await apiFetch('/auth/ws-ticket', { method: 'POST' })
        if (!response.ok) throw new Error('ticket request failed')
        ;({ ticket } = await response.json())
      } catch {
        term.writeln('\r\n[unable to authenticate — retrying in 2s]')
        reconnectTimer = setTimeout(connect, 2000)
        return
      }
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${proto}://${window.location.host}/api/logs?service=${encodeURIComponent(service)}&ticket=${ticket}`)
      wsRef.current = ws
      ws.onmessage = (event) => term.writeln(event.data)
      ws.onerror = () => term.writeln('\r\n[connection error]')
      ws.onclose = (event) => {
        if (closedByUs) return
        if (event.code === 4401) {
          term.writeln('\r\n[unauthorized — ticket invalid or expired]')
          return
        }
        term.writeln('\r\n[disconnected — reconnecting in 2s]')
        reconnectTimer = setTimeout(connect, 2000)
      }
    }
    void connect()
    return () => {
      closedByUs = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      wsRef.current?.close()
      term.dispose()
    }
  }, [service])

  return (
    <Layout>
      <div className="p-6 flex flex-col h-full">
        <PageHeader eyebrow="SYSTEM / LOGS" title="Logs" />
        <div className="flex items-center gap-3 mb-4">
          <select value={service} onChange={(e) => setService(e.target.value)} className="px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#141416] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm focus:outline-none">
            {services.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
          </select>
        </div>
        <div ref={termRef} className="flex-1 rounded-md overflow-hidden border border-zinc-200 dark:border-white/10" style={{ minHeight: '500px' }} />
      </div>
    </Layout>
  )
}
