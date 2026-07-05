import { useEffect, useState } from 'react'
import { Link, useNavigate, useRouterState } from '@tanstack/react-router'
import { useAuth } from '@/store/auth'
import { useRoute } from '@/store/route'
import { cn } from '@/lib/utils'
import { apiFetch } from '@/lib/api'
import { brand } from '@/brand'
import {
  LayoutDashboard, FileCog, ShieldUser, LogOut, KeyRound, Menu
} from 'lucide-react'

const navItems = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
]

const adminItems = [
  { to: '/config', label: 'Config', icon: FileCog },
]

const superAdminItems = [
  { to: '/admin-users', label: 'Admin Users', icon: ShieldUser },
]

function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [ok, setOk] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    if (next !== confirm) { setError('Passwords do not match'); return }
    if (next.length < 12) { setError('New password must be at least 12 characters'); return }
    try {
      const res = await apiFetch('/api/admin-users/me/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: current, new_password: next }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(err.detail ?? res.statusText)
      }
      setOk(true)
    } catch (err: any) {
      setError(err.message ?? 'Failed')
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-[60]">
      <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-6 w-full max-w-sm">
        <h2 className="text-lg font-semibold mb-4">Change Password</h2>
        {ok ? (
          <div className="space-y-4">
            <p className="text-green-400 text-sm">Password changed successfully.</p>
            <button onClick={onClose} className="w-full py-2 rounded bg-zinc-700 hover:bg-zinc-600 text-sm">Close</button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-3">
            <input type="password" placeholder="Current password" value={current} onChange={e => setCurrent(e.target.value)}
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm" required />
            <input type="password" placeholder="New password (min 12 chars)" value={next} onChange={e => setNext(e.target.value)}
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm" required />
            <input type="password" placeholder="Confirm new password" value={confirm} onChange={e => setConfirm(e.target.value)}
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm" required />
            {error && <p className="text-red-400 text-xs">{error}</p>}
            <div className="flex gap-2 pt-1">
              <button type="button" onClick={onClose} className="flex-1 py-2 rounded bg-zinc-700 hover:bg-zinc-600 text-sm">Cancel</button>
              <button type="submit" className="flex-1 py-2 rounded bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm">Change</button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}

export function Layout({ children }: { children: React.ReactNode }) {
  const { role, username, clear } = useAuth()
  const navigate = useNavigate()
  const routerState = useRouterState()
  const current = routerState.location.pathname
  const setPathname = useRoute((s) => s.setPathname)
  const [showChangePw, setShowChangePw] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  useEffect(() => {
    setPathname(current)
  }, [current, setPathname])

  async function handleLogout() {
    await apiFetch('/auth/logout', { method: 'POST' })
    clear()
    navigate({ to: '/login' })
  }

  const items = [
    ...navItems,
    ...(role === 'admin' || role === 'superadmin' ? adminItems : []),
  ]
  const extraItems = role === 'superadmin' ? superAdminItems : []

  return (
    <div className="flex h-screen bg-zinc-950 text-zinc-100">
      <button
        onClick={() => setSidebarOpen(v => !v)}
        className="md:hidden fixed top-3 left-3 z-50 p-2 rounded-md bg-zinc-900 border border-zinc-800 text-zinc-300"
        aria-label="Toggle menu"
      >
        <Menu size={18} />
      </button>
      {sidebarOpen && (
        <div className="md:hidden fixed inset-0 bg-black/50 z-30" onClick={() => setSidebarOpen(false)} />
      )}
      <aside className={cn(
        'w-56 flex-shrink-0 border-r border-zinc-800 flex flex-col',
        'fixed inset-y-0 left-0 z-40 bg-zinc-950 transition-transform md:relative md:translate-x-0',
        sidebarOpen ? 'translate-x-0' : '-translate-x-full'
      )}>
        <div className="p-4 border-b border-zinc-800">
          <span className="font-bold text-lg tracking-tight">{brand.orgName}</span>
        </div>
        <nav className="flex-1 p-2 space-y-1">
          {items.map(({ to, label, icon: Icon }) => (
            <Link
              key={to}
              to={to}
              onClick={() => setSidebarOpen(false)}
              className={cn(
                'flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors',
                current === to
                  ? 'bg-zinc-800 text-white'
                  : 'text-zinc-400 hover:text-white hover:bg-zinc-800/50'
              )}
            >
              <Icon size={16} />
              {label}
            </Link>
          ))}
          {extraItems.length > 0 && (
            <>
              <div className="pt-3 pb-1 px-3 text-[10px] font-semibold tracking-wider text-zinc-600 uppercase">Admin</div>
              {extraItems.map(({ to, label, icon: Icon }) => (
                <Link
                  key={to}
                  to={to}
                  onClick={() => setSidebarOpen(false)}
                  className={cn(
                    'flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors',
                    current === to
                      ? 'bg-zinc-800 text-white'
                      : 'text-zinc-400 hover:text-white hover:bg-zinc-800/50'
                  )}
                >
                  <Icon size={16} />
                  {label}
                </Link>
              ))}
            </>
          )}
        </nav>
        <div className="px-3 py-3 border-t border-zinc-800 flex items-center gap-2">
          <div className="w-7 h-7 rounded-full bg-accent-fill text-accent-text flex items-center justify-center text-xs font-semibold shrink-0">
            {(username ?? '?').slice(0, 2).toUpperCase()}
          </div>
          <div className="min-w-0">
            <p className="text-sm text-zinc-200 truncate">{username}</p>
            <p className="text-xs text-zinc-500">{role}</p>
          </div>
        </div>
        <div className="p-2 border-t border-zinc-800 space-y-1">
          <button
            onClick={() => setShowChangePw(true)}
            className="flex items-center gap-3 w-full px-3 py-2 rounded-md text-sm text-zinc-400 hover:text-white hover:bg-zinc-800/50 transition-colors"
          >
            <KeyRound size={16} />
            Change Password
          </button>
          <button
            onClick={handleLogout}
            className="flex items-center gap-3 w-full px-3 py-2 rounded-md text-sm text-zinc-400 hover:text-white hover:bg-zinc-800/50 transition-colors"
          >
            <LogOut size={16} />
            Sign out
          </button>
        </div>
      </aside>
      <main className="flex-1 overflow-auto pt-14 md:pt-0">{children}</main>
      {showChangePw && <ChangePasswordModal onClose={() => setShowChangePw(false)} />}
    </div>
  )
}
