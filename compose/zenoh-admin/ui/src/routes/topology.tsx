import { createFileRoute, redirect } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Layout } from '@/components/Layout'
import { errorMessage } from '@/lib/api'
import { useAuth } from '@/store/auth'
import { notify } from '@/lib/notify'
import { RotateCw } from 'lucide-react'
import { HudCorners } from '@/components/HudCorners'
import { TopologyMap } from '@/components/TopologyMap'
import { fetchTopology, type TopologyResponse } from '@/lib/topology'

export const Route = createFileRoute('/topology')({
  beforeLoad: () => {
    const { token } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
  },
  component: TopologyPage,
})

function TopologyPage() {
  const [data, setData] = useState<TopologyResponse | null>(null)

  async function load() {
    try {
      setData(await fetchTopology())
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  useEffect(() => {
    load()
    const interval = setInterval(load, 5000)
    return () => clearInterval(interval)
  }, [])

  const nodes = data?.nodes ?? []
  return (
    <Layout>
      <div className="p-6 max-w-2xl">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-xl font-semibold">Federation Topology</h1>
            <p className="text-xs text-zinc-500 mt-1">
              Live tree from each pod's self-published fact.
              {data && ` Nodes go stale after ${data.stale_after_s}s without a beat (published every ${data.publish_interval_s}s).`}
            </p>
          </div>
          <button onClick={load}
            className="flex items-center gap-2 px-3 py-2 rounded-md text-sm text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-white hover:bg-zinc-200/50 dark:hover:bg-white/[0.05] transition-colors">
            <RotateCw size={14} /> Reload
          </button>
        </div>

        <div className="hud-frame relative rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#0c0c0e] p-4">
          <HudCorners />
          {data === null ? (
            <p className="text-sm text-zinc-500">Loading…</p>
          ) : nodes.length === 0 ? (
            <p className="text-sm text-zinc-500">No topology facts received yet. Pods publish every {data.publish_interval_s}s.</p>
          ) : <TopologyMap nodes={nodes} transportEdges={data.transport_edges} />}
        </div>
      </div>
    </Layout>
  )
}
