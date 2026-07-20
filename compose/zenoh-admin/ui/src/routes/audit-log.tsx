import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Layout } from '@/components/Layout'
import { PageHeader } from '@/components/PageHeader'
import { apiJson, errorMessage } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { notify } from '@/lib/notify'
import { TableSkeletonRows } from '@/components/Skeleton'
import { HudCorners } from '@/components/HudCorners'

export const Route = createFileRoute('/audit-log')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: AuditLogPage,
})

interface AuditEntry { id: string; username: string; action: string; detail: string | null; timestamp: string }

function AuditLogPage() {
  const [entries, setEntries] = useState<AuditEntry[]>([])
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    apiJson<{ entries: AuditEntry[] }>('/api/audit-log')
      .then((data) => setEntries(data.entries))
      .catch((error) => notify.error(errorMessage(error)))
      .finally(() => setLoading(false))
  }, [])
  return (
    <Layout>
      <div className="p-6">
        <PageHeader title="Audit Logs" count={entries.length} countLabel="entries" />
        <div className="hud-frame relative rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#0c0c0e]"><HudCorners /><div className="overflow-x-auto"><table className="w-full text-sm min-w-[640px]">
          <thead className="bg-zinc-100 dark:bg-[#141416] text-zinc-600 dark:text-zinc-400"><tr><th className="px-4 py-3 text-left font-medium hud-label text-xs">Time</th><th className="px-4 py-3 text-left font-medium hud-label text-xs">User</th><th className="px-4 py-3 text-left font-medium hud-label text-xs">Action</th><th className="px-4 py-3 text-left font-medium hud-label text-xs">Detail</th></tr></thead>
          <tbody className="divide-y divide-zinc-200 dark:divide-white/10">{loading ? <TableSkeletonRows columns={4} /> : entries.length === 0 ? <tr className="bg-zinc-50 dark:bg-[#0c0c0e]"><td colSpan={4} className="px-4 py-8 text-center text-zinc-500">No activity recorded yet</td></tr> : entries.map((entry) => <tr key={entry.id} className="bg-zinc-50 dark:bg-[#000000] hover:bg-zinc-100/50 dark:hover:bg-white/[0.03]"><td className="px-4 py-3 text-zinc-600 dark:text-zinc-400 whitespace-nowrap">{new Date(entry.timestamp).toLocaleString()}</td><td className="px-4 py-3 font-mono">{entry.username}</td><td className="px-4 py-3 text-zinc-700 dark:text-zinc-300">{entry.action}</td><td className="px-4 py-3 text-zinc-500 font-mono">{entry.detail ?? '—'}</td></tr>)}</tbody>
        </table></div></div>
      </div>
    </Layout>
  )
}
