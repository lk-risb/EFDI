import { useEffect, useRef, useState } from 'react'
import { useAuth } from '@/store/auth'
import { useRoute } from '@/store/route'
import { apiJson, errorMessage } from '@/lib/api'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { notify } from '@/lib/notify'
import { ShieldAlert } from 'lucide-react'
import { cn } from '@/lib/utils'
import { PasswordInput } from '@/components/PasswordInput'

export function ShellSession() {
  const { token, role, authProvider } = useAuth()
  const pathname = useRoute((state) => state.pathname)
  const onShellPage = pathname === '/shell'
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [ticket, setTicket] = useState<string | null>(null)
  const termRef = useRef<HTMLDivElement>(null)
  const fitRef = useRef<FitAddon | null>(null)

  async function elevate(event: React.FormEvent) {
    event.preventDefault()
    setLoading(true)
    try {
      const data = await apiJson<{ ticket: string }>('/auth/shell-elevate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }),
      })
      setTicket(data.ticket)
      setPassword('')
    } catch (error) { notify.error(errorMessage(error)) } finally { setLoading(false) }
  }

  useEffect(() => {
    if (!ticket || !termRef.current) return
    const term = new Terminal({ theme: { background: '#09090b' }, cursorBlink: true, convertEol: true })
    const fit = new FitAddon()
    fitRef.current = fit
    term.loadAddon(fit)
    term.open(termRef.current)
    fit.fit()
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${window.location.host}/api/shell/ws?t=${ticket}`)
    ws.binaryType = 'arraybuffer'
    ws.onopen = () => term.writeln('Connected to the Zenoh router container\r\n')
    ws.onmessage = (event) => term.write(event.data instanceof ArrayBuffer ? new Uint8Array(event.data) : event.data)
    ws.onclose = (event) => { term.writeln(event.code === 4001 ? '\r\n[session refused — invalid ticket]' : '\r\n[session ended]'); setTicket(null) }
    ws.onerror = () => term.writeln('\r\n[connection error]')
    term.onData((data) => { if (ws.readyState === WebSocket.OPEN) ws.send(data) })
    return () => { ws.close(); term.dispose() }
  }, [ticket])

  useEffect(() => { if (!token || role !== 'superadmin' || authProvider !== 'local') setTicket(null) }, [token, role, authProvider])
  useEffect(() => { if (onShellPage) fitRef.current?.fit() }, [onShellPage])

  if (!token || role !== 'superadmin' || authProvider !== 'local') return null
  return <div className={cn('fixed inset-y-0 right-0 z-10 top-14 md:top-0 md:left-56 left-0 bg-zinc-50 dark:bg-[#000000] p-6 flex flex-col', onShellPage ? 'block' : 'hidden')}>
    <h1 className="text-xl font-semibold mb-4">Shell — Zenoh router</h1>
    {!ticket && <div className="max-w-sm space-y-4"><div className="flex items-start gap-3 p-4 rounded-md border border-yellow-300 dark:border-yellow-700/50 bg-yellow-50 dark:bg-yellow-900/20"><ShieldAlert size={16} className="text-yellow-600 dark:text-yellow-400 shrink-0 mt-0.5" /><p className="text-sm text-yellow-800 dark:text-yellow-200">Re-enter your password to unlock shell access. Session expires in 5 minutes.</p></div><form onSubmit={elevate} className="space-y-3"><PasswordInput value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Your admin password" required className="w-full px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#141416] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm focus:outline-none focus:ring-2 focus:ring-accent-ring" /><button type="submit" disabled={loading} className="w-full py-2 rounded-md bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm font-medium disabled:opacity-50 transition-colors">{loading ? 'Verifying…' : 'Unlock shell'}</button></form></div>}
    {ticket && <div ref={termRef} className="flex-1 rounded-md overflow-hidden border border-zinc-200 dark:border-white/10" style={{ minHeight: '500px' }} />}
  </div>
}
