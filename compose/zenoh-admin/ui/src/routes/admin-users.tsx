import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Layout } from '@/components/Layout'
import { apiJson, apiFetch } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { toast } from 'sonner'
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
  superadmin: 'text-purple-400 bg-purple-400/10',
  admin: 'text-accent-ring bg-accent-ring/10',
  readonly: 'text-zinc-400 bg-zinc-400/10',
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
      toast.success(`${username} created`)
      onCreated()
      onClose()
    } catch (e: any) {
      toast.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-6 w-full max-w-sm">
        <h2 className="text-lg font-semibold mb-4">New Admin User</h2>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="space-y-1">
            <label className="text-sm text-zinc-300">Username</label>
            <input type="text" value={username} onChange={e => setUsername(e.target.value)} required
              className="w-full px-3 py-2 rounded-md bg-zinc-800 border border-zinc-700 text-white text-sm focus:outline-none focus:ring-2 focus:ring-accent-ring" />
          </div>
          <div className="space-y-1">
            <label className="text-sm text-zinc-300">Password (min 12 chars)</label>
            <input type="password" value={password} onChange={e => setPassword(e.target.value)} required
              className="w-full px-3 py-2 rounded-md bg-zinc-800 border border-zinc-700 text-white text-sm focus:outline-none focus:ring-2 focus:ring-accent-ring" />
          </div>
          <div className="space-y-1">
            <label className="text-sm text-zinc-300">Role</label>
            <select value={role} onChange={e => setRole(e.target.value as 'admin' | 'superadmin' | 'readonly')}
              className="w-full px-3 py-2 rounded-md bg-zinc-800 border border-zinc-700 text-white text-sm focus:outline-none">
              <option value="readonly">readonly</option>
              <option value="admin">admin</option>
              <option value="superadmin">superadmin</option>
            </select>
          </div>
          <div className="flex gap-2 pt-1">
            <button type="button" onClick={onClose} className="flex-1 py-2 rounded bg-zinc-700 hover:bg-zinc-600 text-sm">Cancel</button>
            <button type="submit" disabled={loading}
              className="flex-1 py-2 rounded bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm disabled:opacity-50">
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
    } catch (e: any) { toast.error(e.message) }
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
      toast.success(`${username} deleted`)
      load()
    } catch (e: any) { toast.error(e.message) }
  }

  async function toggleActive(id: string, username: string, is_active: boolean) {
    try {
      await apiJson(`/api/admin-users/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: !is_active }),
      })
      toast.success(`${username} ${is_active ? 'deactivated' : 'activated'}`)
      load()
    } catch (e: any) { toast.error(e.message) }
  }

  return (
    <Layout>
      <div className="p-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-xl font-semibold">Admin Users</h1>
          <div className="flex items-center gap-4">
            <button onClick={() => setShowNew(true)}
              className="flex items-center gap-2 px-4 py-2 bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm rounded-md transition-colors">
              <UserPlus size={14} /> New Admin
            </button>
          </div>
        </div>
        <div className="rounded-lg border border-zinc-800 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900 text-zinc-400">
              <tr>
                <th className="px-4 py-3 text-left font-medium">Username</th>
                <th className="px-4 py-3 text-left font-medium">Role</th>
                <th className="px-4 py-3 text-left font-medium">Status</th>
                <th className="px-4 py-3 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800">
              {users.length === 0 && (
                <tr><td colSpan={4} className="px-4 py-8 text-center text-zinc-500">No admin users</td></tr>
              )}
              {users.map(u => (
                <tr key={u.id} className="bg-zinc-950 hover:bg-zinc-900/50">
                  <td className="px-4 py-3 font-mono">{u.username}</td>
                  <td className="px-4 py-3">
                    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${ROLE_COLORS[u.role] ?? 'text-zinc-400 bg-zinc-400/10'}`}>{u.role}</span>
                  </td>
                  <td className="px-4 py-3">
                    <button onClick={() => toggleActive(u.id, u.username, u.is_active)}
                      className={`text-xs font-medium hover:underline ${u.is_active ? 'text-green-400' : 'text-zinc-500'}`}>
                      {u.is_active ? 'active' : 'inactive'}
                    </button>
                  </td>
                  <td className="px-4 py-3 flex justify-end">
                    <button onClick={() => deleteUser(u.id, u.username)} title="Delete" className="p-1.5 rounded hover:bg-zinc-800 text-red-400"><Trash2 size={14} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      {showNew && <NewAdminModal onClose={() => setShowNew(false)} onCreated={load} />}
    </Layout>
  )
}
