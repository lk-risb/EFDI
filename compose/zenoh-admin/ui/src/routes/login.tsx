import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { useAuth } from '@/store/auth'
import { errorMessage } from '@/lib/api'
import { useBranding } from '@/store/branding'
import { notify } from '@/lib/notify'
import { HudCorners } from '@/components/HudCorners'

export const Route = createFileRoute('/login')({
  component: LoginPage,
})

function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const { setToken } = useAuth()
  const navigate = useNavigate()
  const orgName = useBranding((s) => s.orgName)
  const logoUrl = useBranding((s) => s.logoUrl)
  const [oidc, setOidc] = useState<{ enabled: boolean; provider_name: string } | null>(null)

  useEffect(() => {
    fetch('/auth/oidc/config', { credentials: 'include' })
      .then((r) => (r.ok ? r.json() : null))
      .then(setOidc)
      .catch(() => {})
  }, [])

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    try {
      const res = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ username, password }),
      })
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail ?? 'Login failed')
      }
      const { access_token } = await res.json()
      const payload = JSON.parse(atob(access_token.split('.')[1]))
      setToken(access_token, payload.role, payload.username, payload.auth_provider)
      navigate({ to: '/' })
    } catch (err) {
      notify.error(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-zinc-50 dark:bg-[#000000] hud-grid-bg">
      <div className="hud-frame w-full max-w-sm space-y-6 p-8 rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#0c0c0e] hud-glass">
        <HudCorners />
        <div>
          {logoUrl && <img src={logoUrl} alt="" className="w-10 h-10 rounded object-contain mb-3" />}
          <h1 className="text-3xl font-bold tracking-tight text-zinc-900 dark:text-white">{orgName}</h1>
          <p className="hud-label text-xs text-zinc-500 dark:text-zinc-500 mt-2">Sign in to manage the Zenoh router</p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1">
            <label className="hud-label text-xs text-zinc-500 dark:text-zinc-500">Username</label>
            <input
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              required
              className="w-full px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#141416] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm focus:outline-none focus:ring-2 focus:ring-accent-ring"
            />
          </div>
          <div className="space-y-1">
            <label className="hud-label text-xs text-zinc-500 dark:text-zinc-500">Password</label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
              className="w-full px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#141416] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm focus:outline-none focus:ring-2 focus:ring-accent-ring"
            />
          </div>
          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 rounded-md bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm font-semibold uppercase tracking-wider disabled:opacity-50 transition-colors"
          >
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        {oidc?.enabled && (
          <>
            <div className="flex items-center gap-3">
              <div className="flex-1 h-px bg-zinc-200 dark:bg-white/10" />
              <span className="hud-label text-[10px] text-zinc-400 dark:text-zinc-600">or</span>
              <div className="flex-1 h-px bg-zinc-200 dark:bg-white/10" />
            </div>
            <a
              href="/auth/oidc/login"
              className="block w-full py-2.5 rounded-md border border-zinc-300 dark:border-white/10 bg-zinc-100 dark:bg-[#141416] hover:bg-zinc-200 dark:hover:bg-[#232326] text-zinc-900 dark:text-white text-sm font-semibold text-center transition-colors"
            >
              Sign in with {oidc.provider_name}
            </a>
          </>
        )}
      </div>
    </div>
  )
}
