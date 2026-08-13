import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Layout } from '@/components/Layout'
import { PageHeader } from '@/components/PageHeader'
import { HudCorners } from '@/components/HudCorners'
import { PasswordInput } from '@/components/PasswordInput'
import { apiJson, apiFetch, errorMessage } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { notify } from '@/lib/notify'
import { UserPlus, Trash2 } from 'lucide-react'

export const Route = createFileRoute('/admin-users')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: AdminUsersPage,
})

interface AdminUser { id: string; username: string; role: string; is_active: boolean; created_at: string }

const ROLE_COLORS: Record<string, string> = {
  superadmin: 'text-purple-600 dark:text-purple-400 bg-purple-400/10',
  admin: 'text-accent-ring bg-accent-ring/10',
  readonly: 'text-zinc-600 dark:text-zinc-400 bg-zinc-400/10',
}

function NewAdminModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<'admin' | 'superadmin' | 'readonly'>('admin')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    try {
      await apiJson('/api/admin-users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, role }),
      })
      notify.success(`${username} created`)
      onCreated()
      onClose()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="bg-zinc-100 dark:bg-[#0c0c0e] border border-zinc-300 dark:border-white/10 rounded-md p-6 w-full max-w-sm">
        <h2 className="text-lg font-semibold mb-4">New Admin User</h2>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="space-y-1">
            <label className="text-sm text-zinc-700 dark:text-zinc-300">Username</label>
            <input type="text" value={username} onChange={e => setUsername(e.target.value)} required
              className="w-full px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#141416] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm focus:outline-none focus:ring-2 focus:ring-accent-ring" />
          </div>
          <div className="space-y-1">
            <label className="text-sm text-zinc-700 dark:text-zinc-300">Password (min 12 chars)</label>
            <PasswordInput value={password} onChange={e => setPassword(e.target.value)} required
              className="w-full px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#141416] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm focus:outline-none focus:ring-2 focus:ring-accent-ring" />
          </div>
          <div className="space-y-1">
            <label className="text-sm text-zinc-700 dark:text-zinc-300">Role</label>
            <select value={role} onChange={e => setRole(e.target.value as 'admin' | 'superadmin' | 'readonly')}
              className="w-full px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#141416] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm focus:outline-none">
              <option value="readonly">readonly</option>
              <option value="admin">admin</option>
              <option value="superadmin">superadmin</option>
            </select>
          </div>
          <div className="flex gap-2 pt-1">
            <button type="button" onClick={onClose} className="flex-1 py-2 rounded-none bg-zinc-300 dark:bg-zinc-700 hover:bg-zinc-400 dark:hover:bg-zinc-600 text-sm">Cancel</button>
            <button type="submit" disabled={loading}
              className="flex-1 py-2 rounded-none bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm disabled:opacity-50">
              {loading ? 'Creating…' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function AdminUsersPage() {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [showNew, setShowNew] = useState(false)

  async function load() {
    try {
      const data = await apiJson<{ users: AdminUser[] }>('/api/admin-users')
      setUsers(data.users)
    } catch (e) { notify.error(errorMessage(e)) }
  }

  useEffect(() => { load() }, [])

  async function deleteUser(id: string, username: string) {
    if (!confirm(`Delete ${username}? This cannot be undone.`)) return
    try {
      const res = await apiFetch(`/api/admin-users/${id}`, { method: 'DELETE' })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(err.detail ?? res.statusText)
      }
      notify.success(`${username} deleted`)
      load()
    } catch (e) { notify.error(errorMessage(e)) }
  }

  async function toggleActive(id: string, username: string, is_active: boolean) {
    try {
      await apiJson(`/api/admin-users/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: !is_active }),
      })
      notify.success(`${username} ${is_active ? 'deactivated' : 'activated'}`)
      load()
    } catch (e) { notify.error(errorMessage(e)) }
  }

  return (
    <Layout>
      <div className="p-6">
        <PageHeader eyebrow="ACCESS / ADMIN USERS" title="Admin Users" count={users.length} countLabel="users" actions={
          <button onClick={() => setShowNew(true)}
            className="flex items-center gap-2 px-4 py-2 bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm rounded-md transition-colors">
            <UserPlus size={14} /> New Admin
          </button>
        } />
        <div className="hud-frame relative hud-enter">
          <HudCorners />
          <div className="rounded-md border border-zinc-200 dark:border-white/10 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-zinc-100 dark:bg-[#0c0c0e] text-zinc-600 dark:text-zinc-400">
              <tr>
                <th className="px-4 py-3 text-left font-medium hud-label text-xs">Username</th>
                <th className="px-4 py-3 text-left font-medium hud-label text-xs">Role</th>
                <th className="px-4 py-3 text-left font-medium hud-label text-xs">Status</th>
                <th className="px-4 py-3 text-right font-medium hud-label text-xs">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-200 dark:divide-white/10">
              {users.length === 0 && (
                <tr><td colSpan={4} className="px-4 py-8 text-center text-zinc-500">No admin users</td></tr>
              )}
              {users.map(u => (
                <tr key={u.id} className="bg-zinc-50 dark:bg-zinc-950 hover:bg-zinc-100 dark:hover:bg-zinc-900/50">
                  <td className="px-4 py-3 font-mono">{u.username}</td>
                  <td className="px-4 py-3">
                    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${ROLE_COLORS[u.role] ?? 'text-zinc-600 dark:text-zinc-400 bg-zinc-400/10'}`}>{u.role}</span>
                  </td>
                  <td className="px-4 py-3">
                    <button onClick={() => toggleActive(u.id, u.username, u.is_active)}
                      className={`text-xs font-medium hover:underline ${u.is_active ? 'text-green-600 dark:text-green-400' : 'text-zinc-500'}`}>
                      {u.is_active ? 'active' : 'inactive'}
                    </button>
                  </td>
                  <td className="px-4 py-3 flex justify-end">
                    <button onClick={() => deleteUser(u.id, u.username)} title="Delete" className="p-1.5 rounded-none hover:bg-zinc-200 dark:hover:bg-zinc-800 text-red-600 dark:text-red-400"><Trash2 size={14} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
      </div>
      {showNew && <NewAdminModal onClose={() => setShowNew(false)} onCreated={load} />}
    </Layout>
  )
}
