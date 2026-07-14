import { useEffect, useState } from 'react'
import { Link, useNavigate, useRouterState } from '@tanstack/react-router'
import { useAuth } from '@/store/auth'
import { useRoute } from '@/store/route'
import { cn } from '@/lib/utils'
import { apiFetch, errorMessage } from '@/lib/api'
import { notify } from '@/lib/notify'
import { useBranding } from '@/store/branding'
import { useTheme } from '@/store/theme'
import { useNotifications } from '@/store/notifications'
import {
  LayoutDashboard, FileCog, ShieldUser, LogOut, Users, Sun, Moon, Bell, ShieldCheck, Radio, Network, Waypoints
} from 'lucide-react'

// Three-bar icon that morphs into an X on open — plain CSS transitions on
// each bar's rotation/position/opacity, no icon-swap flash.
function HamburgerIcon({ open }: { open: boolean }) {
  return (
    <span className="relative block w-4 h-4">
      <span className={cn(
        'absolute left-0 top-0.5 w-4 h-0.5 rounded-full bg-current transition-all duration-300',
        open && 'top-[7px] rotate-45'
      )} />
      <span className={cn(
        'absolute left-0 top-[7px] w-4 h-0.5 rounded-full bg-current transition-all duration-300',
        open && 'opacity-0'
      )} />
      <span className={cn(
        'absolute left-0 bottom-0.5 w-4 h-0.5 rounded-full bg-current transition-all duration-300',
        open && 'bottom-[7px] -rotate-45'
      )} />
    </span>
  )
}

const navItems = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
]

const adminItems = [
  { to: '/config', label: 'Config', icon: FileCog },
  { to: '/topology', label: 'Topology', icon: Waypoints },
]

const superAdminItems = [
  { to: '/admin-users', label: 'Admin Users', icon: ShieldUser },
  { to: '/certificates', label: 'Certificates', icon: ShieldCheck },
  { to: '/publish-builder', label: 'Publish Script', icon: Radio },
  { to: '/federation', label: 'Federation', icon: Network },
]

function passwordStrength(p: string): { score: number; label: string; color: string } {
  let score = 0
  if (p.length >= 12) score++
  if (p.length >= 16) score++
  if (/[A-Z]/.test(p)) score++
  if (/[a-z]/.test(p)) score++
  if (/\d/.test(p)) score++
  if (/[^A-Za-z0-9]/.test(p)) score++
  if (score <= 2) return { score, label: 'Weak', color: 'bg-red-600 dark:bg-red-500' }
  if (score <= 4) return { score, label: 'Fair', color: 'bg-yellow-600 dark:bg-yellow-500' }
  if (score === 5) return { score, label: 'Good', color: 'bg-blue-500' }
  return { score, label: 'Strong', color: 'bg-green-600 dark:bg-green-500' }
}

function ChangePasswordFields({ onClose }: { onClose: () => void }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [ok, setOk] = useState(false)
  const strength = passwordStrength(next)

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
    } catch (err) {
      setError(errorMessage(err) || 'Failed')
    }
  }

  if (ok) {
    return (
      <div className="space-y-4">
        <p className="text-green-600 dark:text-green-400 text-sm">Password changed successfully.</p>
        <button onClick={onClose} className="w-full py-2 rounded bg-zinc-300 dark:bg-[#232326] hover:bg-zinc-400 dark:hover:bg-[#2b2b2f] text-sm">Close</button>
      </div>
    )
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-3">
      <input type="password" placeholder="Current password" value={current} onChange={e => setCurrent(e.target.value)}
        className="w-full bg-zinc-200 dark:bg-[#1a1a1d] border border-zinc-300 dark:border-white/10 rounded px-3 py-2 text-sm" required />
      <div className="space-y-1">
        <input type="password" placeholder="New password (min 12 chars)" value={next} onChange={e => setNext(e.target.value)}
          className="w-full bg-zinc-200 dark:bg-[#1a1a1d] border border-zinc-300 dark:border-white/10 rounded px-3 py-2 text-sm" required />
        {next.length > 0 && (
          <div className="space-y-1">
            <div className="flex gap-1 h-1">
              {[1,2,3,4,5,6].map(i => (
                <div key={i} className={`flex-1 rounded-full ${i <= strength.score ? strength.color : 'bg-zinc-300 dark:bg-[#232326]'}`} />
              ))}
            </div>
            <p className="text-xs text-zinc-600 dark:text-zinc-400">{strength.label} · must have uppercase, lowercase, digit, special char</p>
          </div>
        )}
      </div>
      <input type="password" placeholder="Confirm new password" value={confirm} onChange={e => setConfirm(e.target.value)}
        className="w-full bg-zinc-200 dark:bg-[#1a1a1d] border border-zinc-300 dark:border-white/10 rounded px-3 py-2 text-sm" required />
      {error && <p className="text-red-600 dark:text-red-400 text-xs">{error}</p>}
      <div className="flex gap-2 pt-1">
        <button type="button" onClick={onClose} className="flex-1 py-2 rounded bg-zinc-300 dark:bg-[#232326] hover:bg-zinc-400 dark:hover:bg-[#2b2b2f] text-sm">Cancel</button>
        <button type="submit" className="flex-1 py-2 rounded bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm">Change</button>
      </div>
    </form>
  )
}

function UserSettingsModal({ onClose }: { onClose: () => void }) {
  const { username: currentUsername, setToken, role } = useAuth()
  const [newUsername, setNewUsername] = useState(currentUsername ?? '')
  const [usernamePassword, setUsernamePassword] = useState('')
  const [usernameError, setUsernameError] = useState('')
  const [usernameOk, setUsernameOk] = useState(false)

  async function handleUsernameSubmit(e: React.FormEvent) {
    e.preventDefault()
    setUsernameError('')
    try {
      const res = await apiFetch('/api/admin-users/me/change-username', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: usernamePassword, new_username: newUsername }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(err.detail ?? res.statusText)
      }
      const data: { access_token: string } = await res.json()
      setToken(data.access_token, role ?? '', newUsername)
      setUsernameOk(true)
      notify.success('Username updated')
    } catch (err) {
      setUsernameError(errorMessage(err) || 'Failed')
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-[60]">
      <div className="bg-zinc-100 dark:bg-[#111113] border border-zinc-300 dark:border-white/10 rounded-md p-6 w-full max-w-sm space-y-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">User Settings</h2>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-900 dark:hover:text-white text-sm">Close</button>
        </div>

        <div>
          <h3 className="hud-label text-sm font-semibold text-zinc-600 dark:text-zinc-400 mb-2">Change Username</h3>
          {usernameOk ? (
            <p className="text-green-600 dark:text-green-400 text-sm">Username changed successfully.</p>
          ) : (
            <form onSubmit={handleUsernameSubmit} className="space-y-3">
              <input type="text" placeholder="New username" value={newUsername} onChange={e => setNewUsername(e.target.value)}
                className="w-full bg-zinc-200 dark:bg-[#1a1a1d] border border-zinc-300 dark:border-white/10 rounded px-3 py-2 text-sm" required />
              <input type="password" placeholder="Current password" value={usernamePassword} onChange={e => setUsernamePassword(e.target.value)}
                className="w-full bg-zinc-200 dark:bg-[#1a1a1d] border border-zinc-300 dark:border-white/10 rounded px-3 py-2 text-sm" required />
              {usernameError && <p className="text-red-600 dark:text-red-400 text-xs">{usernameError}</p>}
              <button type="submit" className="w-full py-2 rounded bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm">Change Username</button>
            </form>
          )}
        </div>

        <div className="border-t border-zinc-300 dark:border-white/10 pt-6">
          <h3 className="hud-label text-sm font-semibold text-zinc-600 dark:text-zinc-400 mb-2">Change Password</h3>
          <ChangePasswordFields onClose={onClose} />
        </div>
      </div>
    </div>
  )
}

export function Layout({ children }: { children: React.ReactNode }) {
  const { role, username, clear } = useAuth()
  const orgName = useBranding((s) => s.orgName)
  const logoUrl = useBranding((s) => s.logoUrl)
  const theme = useTheme((s) => s.theme)
  const toggleTheme = useTheme((s) => s.toggleTheme)
  const navigate = useNavigate()
  const routerState = useRouterState()
  const current = routerState.location.pathname
  const setPathname = useRoute((s) => s.setPathname)
  const [showUserSettings, setShowUserSettings] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const notifications = useNotifications((s) => s.items)
  const clearNotifications = useNotifications((s) => s.clear)
  const [notifOpen, setNotifOpen] = useState(false)
  const [userMenuOpen, setUserMenuOpen] = useState(false)

  useEffect(() => {
    setPathname(current)
  }, [current, setPathname])

  async function handleLogout() {
    try {
      await apiFetch('/auth/logout', { method: 'POST' })
    } catch (err) {
      notify.error(`Sign out request failed: ${errorMessage(err)}`)
    } finally {
      clear()
      navigate({ to: '/login' })
    }
  }

  const items = [
    ...navItems,
    ...(role === 'admin' || role === 'superadmin' ? adminItems : []),
  ]
  const extraItems = role === 'superadmin' ? superAdminItems : []

  return (
    <div className="flex h-screen bg-zinc-50 dark:bg-[#000000] text-zinc-900 dark:text-zinc-100">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:px-3 focus:py-2 focus:rounded-md focus:bg-accent-fill focus:text-accent-text focus:text-sm"
      >
        Skip to content
      </a>
      <header className="fixed top-0 left-0 right-0 z-50 h-14 pl-16 pr-4 flex items-center gap-2 min-w-0 border-b border-zinc-200 dark:border-white/10 bg-white/97 dark:bg-[#111113]/97 backdrop-blur-xl">
        <button
          onClick={() => setSidebarOpen(v => !v)}
          className="fixed top-3 left-3 z-50 p-2.5 rounded-md bg-zinc-100 dark:bg-[#111113] border border-zinc-200 dark:border-white/10 text-zinc-700 dark:text-zinc-300 hover:text-zinc-900 dark:hover:text-white transition-colors"
          aria-label="Toggle menu"
          aria-expanded={sidebarOpen}
        >
          <HamburgerIcon open={sidebarOpen} />
        </button>
        {logoUrl && <img src={logoUrl} alt="" className="w-7 h-7 rounded object-contain shrink-0" />}
        <span className="font-display font-bold text-xl tracking-tight truncate">{orgName}</span>
        <div className="relative ml-auto">
          <button
            onClick={() => setNotifOpen((v) => !v)}
            aria-label="Notifications"
            className="p-1.5 rounded hover:bg-zinc-200 dark:hover:bg-[#1a1a1d] text-zinc-600 dark:text-zinc-400 relative focus:outline-none focus:ring-2 focus:ring-accent-ring"
          >
            <Bell size={16} />
            {notifications.length > 0 && (
              <span className="absolute top-0.5 right-0.5 w-2 h-2 rounded-full bg-red-500" />
            )}
          </button>
          {notifOpen && (
            <div className="fixed top-14 left-4 right-4 w-auto md:absolute md:top-auto md:left-0 md:right-auto md:mt-2 md:w-72 rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] shadow-lg z-50 max-h-80 overflow-y-auto">
              <div className="flex items-center justify-between px-3 py-2 border-b border-zinc-200 dark:border-white/10">
                <span className="hud-label text-xs font-semibold text-zinc-600 dark:text-zinc-400">Notifications</span>
                <button onClick={clearNotifications} className="text-xs text-accent-ring hover:underline">Clear all</button>
              </div>
              {notifications.length === 0 ? (
                <p className="px-3 py-4 text-sm text-zinc-500 text-center">No notifications yet</p>
              ) : (
                notifications.map((n) => (
                  <div key={n.id} className="flex items-start gap-2 px-3 py-2 border-b border-zinc-100 dark:border-white/5 last:border-0">
                    <span className={`w-1.5 h-1.5 rounded-full mt-1.5 ${n.type === 'success' ? 'bg-green-500' : 'bg-red-500'}`} />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-zinc-900 dark:text-zinc-200 break-words">{n.message}</p>
                      <p className="text-xs text-zinc-500">{new Date(n.timestamp).toLocaleTimeString()}</p>
                    </div>
                  </div>
                ))
              )}
            </div>
          )}
        </div>

        <div className="relative">
          <button
            onClick={() => setUserMenuOpen((v) => !v)}
            aria-label="User menu"
            aria-expanded={userMenuOpen}
            className="w-7 h-7 rounded-full bg-accent-fill text-accent-text flex items-center justify-center text-xs font-semibold shrink-0 focus:outline-none focus:ring-2 focus:ring-accent-ring"
          >
            {(username ?? '?').slice(0, 2).toUpperCase()}
          </button>
          {userMenuOpen && (
            <div className="fixed top-14 left-4 right-4 w-auto md:absolute md:top-full md:left-auto md:right-0 md:mt-2 md:w-56 rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#111113] shadow-lg z-50 overflow-hidden">
              <div className="px-3 py-2.5 border-b border-zinc-200 dark:border-white/10">
                <p className="text-sm text-zinc-800 dark:text-zinc-200 truncate">{username}</p>
                <p className="text-xs text-zinc-500">{role}</p>
              </div>
              <div className="p-1">
                <button
                  onClick={() => { toggleTheme(); setUserMenuOpen(false) }}
                  className="flex items-center gap-3 w-full px-3 py-2 rounded-md text-sm text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-white hover:bg-zinc-200/50 dark:hover:bg-white/[0.05] transition-colors"
                >
                  {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
                  {theme === 'dark' ? 'Light mode' : 'Dark mode'}
                </button>
                <button
                  onClick={() => { setUserMenuOpen(false); setShowUserSettings(true) }}
                  className="flex items-center gap-3 w-full px-3 py-2 rounded-md text-sm text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-white hover:bg-zinc-200/50 dark:hover:bg-white/[0.05] transition-colors"
                >
                  <Users size={16} />
                  Account settings
                </button>
                <button
                  onClick={() => { setUserMenuOpen(false); handleLogout() }}
                  className="flex items-center gap-3 w-full px-3 py-2 rounded-md text-sm text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-white hover:bg-zinc-200/50 dark:hover:bg-white/[0.05] transition-colors"
                >
                  <LogOut size={16} />
                  Sign out
                </button>
              </div>
            </div>
          )}
        </div>
      </header>
      {sidebarOpen && (
        <div className="fixed inset-0 top-14 bg-black/50 z-30" onClick={() => setSidebarOpen(false)} />
      )}
      <aside className={cn(
        'w-64 flex-shrink-0 border-r border-zinc-200 dark:border-white/10 flex flex-col',
        'fixed top-14 bottom-0 left-0 z-40 bg-white/97 dark:bg-[#111113]/97 shadow-xl backdrop-blur-xl transition-transform duration-300 ease-in-out',
        sidebarOpen ? 'translate-x-0' : '-translate-x-full'
      )}>
        <nav className="flex-1 p-2 space-y-1">
          {items.map(({ to, label, icon: Icon }) => (
            <Link
              key={to}
              to={to}
              onClick={() => setSidebarOpen(false)}
              className={cn(
                'flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors',
                current === to
                  ? 'bg-zinc-200 dark:bg-[#1a1a1d] text-zinc-900 dark:text-white'
                  : 'text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-white hover:bg-zinc-200/50 dark:hover:bg-white/[0.05]'
              )}
            >
              <Icon size={16} />
              {label}
            </Link>
          ))}
          {extraItems.length > 0 && (
            <>
              <div className="hud-label pt-3 pb-1 px-3 text-[10px] font-semibold text-zinc-400 dark:text-zinc-600">Admin</div>
              {extraItems.map(({ to, label, icon: Icon }) => (
                <Link
                  key={to}
                  to={to}
                  onClick={() => setSidebarOpen(false)}
                  className={cn(
                    'flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors',
                    current === to
                      ? 'bg-zinc-200 dark:bg-[#1a1a1d] text-zinc-900 dark:text-white'
                      : 'text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-white hover:bg-zinc-200/50 dark:hover:bg-white/[0.05]'
                  )}
                >
                  <Icon size={16} />
                  {label}
                </Link>
              ))}
            </>
          )}
        </nav>
      </aside>
      <main id="main-content" tabIndex={-1} className="flex-1 overflow-auto pt-14 isolate hud-grid-bg">{children}</main>
      {showUserSettings && <UserSettingsModal onClose={() => setShowUserSettings(false)} />}
    </div>
  )
}
