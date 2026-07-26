import {createFileRoute, redirect, useNavigate} from '@tanstack/react-router'
import {useEffect, useState} from 'react'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {apiFetch, apiJson, errorMessage} from '@/lib/api'
import {useAuth} from '@/store/auth'
import {notify} from '@/lib/notify'
import {
    ArrowRight,
    Ban,
    Cable,
    CheckCircle2,
    GitBranch,
    KeyRound,
    RefreshCw,
    Router,
    ShieldCheck,
    ShieldOff,
    WifiOff
} from 'lucide-react'
import {HudCorners} from '@/components/HudCorners'
import {TopologyMap} from '@/components/TopologyMap'
import {fetchTopology, type TopologyNode, type TopologyTransportEdge} from '@/lib/topology'

export const Route = createFileRoute('/network')({
  beforeLoad: () => {
    const { token } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
  },
  component: NetworkPage,
})

interface FederatedChild {
  id: string
  name: string
  namespace: string
  created_by: string
  created_at: string
  last_status: string | null
  last_status_version: number | null
  last_status_at: string | null
  last_status_error: string | null
}

interface TrustAuthority {
  id: string
  identity_uri: string
  namespace_scope: string
  relationship: 'local' | 'child' | 'ancestor'
  state: string
  not_after: string
  max_delegation_depth: number
}

interface TrustInventory {
  authorities: TrustAuthority[]
  acl: { sequence: number; sha256: string; state: string; applied_at: string | null } | null
}

function StatusBadge({ child }: { child: FederatedChild }) {
  if (!child.last_status) {
    return <span className="text-xs text-zinc-500">No push yet</span>
  }
  const color = child.last_status === 'ok'
    ? 'text-green-600 dark:text-green-400'
    : child.last_status === 'rejected' || child.last_status === 'rolled_back'
      ? 'text-red-600 dark:text-red-400'
      : 'text-zinc-500'
  return (
    <span className={`text-xs ${color}`} title={child.last_status_error ?? undefined}>
      {child.last_status} (v{child.last_status_version}){child.last_status_at ? ` — ${new Date(child.last_status_at).toLocaleString()}` : ''}
    </span>
  )
}

function NetworkPage() {
  const { role } = useAuth()
  const canManage = role === 'superadmin'
  const navigate = useNavigate()
  const [children, setChildren] = useState<FederatedChild[]>([])
  const [topology, setTopology] = useState<TopologyNode[]>([])
  const [transportEdges, setTransportEdges] = useState<TopologyTransportEdge[]>([])
  const [selectedNamespace, setSelectedNamespace] = useState<string | null>(null)
  const [trust, setTrust] = useState<TrustInventory | null>(null)
  const [changing, setChanging] = useState<string | null>(null)

  async function load() {
    try {
      const [topologyData, childData, trustData] = await Promise.all([
        fetchTopology(),
        canManage ? apiJson<FederatedChild[]>('/api/federation') : Promise.resolve([]),
        canManage ? apiJson<TrustInventory>('/api/trust') : Promise.resolve(null),
      ])
      setChildren(childData)
      setTopology(topologyData.nodes)
      setTransportEdges(topologyData.transport_edges)
      setTrust(trustData)
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  useEffect(() => {
    load()
    // Poll for status updates — Task 7's subscriber writes last_status
    // asynchronously (up to ~30s after a push, per the child's health-check
    // window), so this page can't rely on a one-shot load to show the
    // outcome. 5s matches this codebase's existing dashboard poll cadence
    // (compose/zenoh-admin/ui/src/routes/index.tsx's /api/health poll).
    const interval = setInterval(load, 5000)
    return () => clearInterval(interval)
  }, [])

  function authorityFor(namespace: string) {
    return trust?.authorities.find(item => item.relationship === 'child' && item.namespace_scope.endsWith(`/${namespace}/**`))
  }

  async function lifecycle(authority: TrustAuthority, action: 'quarantine' | 'restore' | 'decommission') {
    const reason = window.prompt(`${action} ${authority.identity_uri}\n\nRecord the operational reason:`)
    if (!reason) return
    if (action === 'decommission' && !window.confirm('Decommissioning is irreversible. Continue?')) return
    setChanging(authority.id)
    try {
      const res = await apiFetch(`/api/trust/authorities/${authority.id}/${action}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(err.detail ?? res.statusText)
      }
      notify.success(`Authority ${action} recorded · apply the staged ACL`)
      await load()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setChanging(null)
    }
  }

  async function applyAcl() {
    setChanging('acl')
    try {
      const res = await apiFetch('/api/trust/acl/apply', { method: 'POST' })
      const body = await res.json().catch(() => ({ detail: res.statusText }))
      if (!res.ok) throw new Error(body.detail ?? res.statusText)
      notify.success(`Identity-bound ACL v${body.sequence} applied`)
      await load()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setChanging(null)
    }
  }

  async function rotateLink(authority: TrustAuthority) {
    if (!window.confirm(`Rotate the one-time link credential for ${authority.identity_uri}?`)) return
    setChanging(authority.id)
    try {
      const res = await apiFetch(`/api/trust/authorities/${authority.id}/rotate-link`, { method: 'POST' })
      const body = await res.json().catch(() => ({ detail: res.statusText }))
      if (!res.ok) throw new Error(body.detail ?? res.statusText)
      await navigator.clipboard.writeText(JSON.stringify(body.link_credential))
      notify.success('New child link credential copied once · install it on the child, then apply ACL')
      await load()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setChanging(null)
    }
  }

  function openManagedConfig(targetNamespace: string) {
    sessionStorage.setItem('efdi-config-target', targetNamespace)
    navigate({ to: '/config' })
  }

  return (
    <Layout>
      <div className="mx-auto max-w-7xl p-6">
        <PageHeader
          title="Managed Router Network"
          count={topology.length}
          countLabel="observed"
          actions={canManage ? (
            <div className="flex gap-2">
              <button onClick={applyAcl} disabled={changing !== null} className="flex items-center gap-2 rounded-md border border-zinc-300 px-3 py-2 text-xs disabled:opacity-50 dark:border-white/10">
                <ShieldCheck size={13} /> Apply trust ACL
              </button>
              <button onClick={() => navigate({ to: '/certificates' })} className="flex items-center gap-2 rounded-md bg-accent-fill px-3 py-2 text-xs text-accent-text hover:bg-accent-fill-hover">
                <KeyRound size={13} /> Enroll child router
              </button>
            </div>
          ) : undefined}
        />

        <div className="mb-5 flex flex-wrap gap-2 text-xs">
          <span className="enterprise-chip enterprise-chip-ok"><CheckCircle2 size={13} /> {topology.filter(node => node.online && node.healthy).length} healthy</span>
          {canManage && <span className="enterprise-chip"><GitBranch size={13} /> {children.length} direct children</span>}
          {canManage && <span className={`enterprise-chip ${trust?.acl?.state === 'applied' ? 'enterprise-chip-ok' : 'enterprise-chip-warn'}`}><ShieldCheck size={13} /> ACL {trust?.acl ? `v${trust.acl.sequence} ${trust.acl.state}` : 'not generated'}</span>}
          <span className="enterprise-chip"><ShieldCheck size={13} /> immediate-parent authority</span>
          <span className="enterprise-chip"><WifiOff size={13} /> child autonomy on disconnect</span>
        </div>

        <div className="hud-frame relative hud-enter mb-6">
          <HudCorners />
          <div className="rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#0c0c0e] p-4">
            <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100 mb-3">Topology</h2>
            <TopologyMap nodes={topology} transportEdges={transportEdges} selected={selectedNamespace} onSelect={setSelectedNamespace} />
          </div>
        </div>

        {selectedNamespace && (() => {
          const node = topology.find(item => item.namespace === selectedNamespace)
          const child = children.find(item => item.namespace === selectedNamespace)
          if (!node) return null
          const links = transportEdges.filter(edge => edge.source === node.namespace || edge.target === node.namespace)
          return (
            <section className="hud-frame relative mb-6 rounded-md border border-accent-ring/40 bg-white p-5 dark:bg-[#0c0c0e]">
              <HudCorners />
              <div className="mb-5 flex flex-wrap items-start justify-between gap-3 border-b border-zinc-200 pb-4 dark:border-white/10">
                <div>
                  <p className="hud-label text-[10px] text-zinc-500">Network / managed router</p>
                  <h2 className="mt-1 flex items-center gap-2 font-display text-2xl font-semibold text-zinc-900 dark:text-white">
                    <Router size={21} className="text-accent-ring" /> {node.namespace}
                  </h2>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <span className={`enterprise-chip ${node.online && node.healthy ? 'enterprise-chip-ok' : 'enterprise-chip-warn'}`}>
                      {node.online ? (node.healthy ? 'healthy' : 'degraded') : 'offline'}
                    </span>
                    <span className="enterprise-chip">{child ? 'direct child' : node.role === 'hq' ? 'local root' : 'delegated descendant'}</span>
                    <span className={`enterprise-chip ${node.verified ? 'enterprise-chip-ok' : 'enterprise-chip-warn'}`}>{node.verified ? 'signed identity verified' : 'unverified observation'}</span>
                    <span className="enterprise-chip">{node.config_status ?? 'no config result'}</span>
                  </div>
                </div>
                <div className="flex gap-2">
                  {canManage && node.role !== 'hq' && node.reported !== false && (
                    <button onClick={() => openManagedConfig(node.namespace)}
                      className="flex items-center gap-2 rounded-md bg-accent-fill px-3 py-2 text-xs text-accent-text hover:bg-accent-fill-hover">
                      Manage config <ArrowRight size={13} />
                    </button>
                  )}
                  <button onClick={() => setSelectedNamespace(null)} className="rounded-md border border-zinc-300 px-3 py-2 text-xs text-zinc-500 hover:text-zinc-800 dark:border-white/10 dark:hover:text-zinc-200">Close</button>
                </div>
              </div>

              <div className="grid gap-4 lg:grid-cols-2">
                <div className="enterprise-panel">
                  <h3 className="enterprise-panel-title">Summary</h3>
                  <dl className="enterprise-kv">
                    <dt>Stable Zenoh ID</dt><dd className="font-mono break-all">{node.router_zid ?? 'Not reported'}</dd>
                    <dt>Process</dt><dd>{node.online ? 'running' : 'unreachable'}</dd>
                    <dt>Role</dt><dd>{node.role}</dd>
                    <dt>Parent</dt><dd className="font-mono break-all">{node.parent_namespace ?? 'Local root'}</dd>
                    <dt>Last presence</dt><dd>{node.last_seen_seconds.toFixed(1)}s ago</dd>
                    <dt>Config result</dt><dd>{node.config_status ? `${node.config_status} · v${node.config_status_version}` : 'No result reported'}</dd>
                  </dl>
                </div>

                <div className="enterprise-panel">
                  <h3 className="enterprise-panel-title">Observed links</h3>
                  {links.length === 0 ? (
                    <p className="text-sm text-zinc-500">No established Zenoh transport link has been reported.</p>
                  ) : links.map(link => {
                    const peer = link.source === node.namespace ? link.target : link.source
                    return (
                      <div key={`${link.source}:${link.target}`} className="mb-2 rounded-md border border-zinc-200 p-3 last:mb-0 dark:border-white/10">
                        <p className="flex items-center gap-2 text-sm font-medium"><Cable size={14} className="text-accent-ring" /> management · connected</p>
                        <p className="mt-1 break-all font-mono text-xs text-zinc-500">{peer}</p>
                        <p className="mt-1 text-[11px] text-zinc-500">{link.protocols.join(', ') || 'transport'} · agent reported</p>
                      </div>
                    )
                  })}
                </div>

                <div className="enterprise-panel lg:col-span-2">
                  <h3 className="enterprise-panel-title">Authority and offline behavior</h3>
                  <div className="grid gap-4 text-sm md:grid-cols-3">
                    <div><p className="hud-label text-[10px] text-zinc-500">Authority</p><p className="mt-1">Only the immediate parent may sign a change. Deeper requests are verified and re-signed at every registered hop.</p></div>
                    <div><p className="hud-label text-[10px] text-zinc-500">Activation</p><p className="mt-1">Candidate configuration is preflighted by Zenoh, atomically activated, health checked, and rolled back on failure.</p></div>
                    <div><p className="hud-label text-[10px] text-zinc-500">Partition mode</p><p className="mt-1">The router and its local management database continue operating when the parent link is unavailable.</p></div>
                  </div>
                </div>
              </div>
            </section>
          )
        })()}

        {canManage && (
          <>
            <h2 className="enterprise-section-title">Direct management relationships</h2>
            <div className="grid gap-3 md:grid-cols-2">
              {children.length === 0 ? (
                <div className="enterprise-panel md:col-span-2">
                  <p className="text-sm text-zinc-500">No enrolled direct children. Create an invitation from Certificate Authority to establish one.</p>
                </div>
              ) : children.map(c => {
                const authority = authorityFor(c.namespace)
                return (
              <div key={c.id} className="hud-frame relative hud-card flex items-center justify-between rounded-md border border-zinc-200 dark:border-white/10 bg-white dark:bg-[#0c0c0e] p-4">
                <HudCorners />
                <div>
                  <p className="text-sm font-medium text-zinc-800 dark:text-zinc-200">{c.name}</p>
                  <p className="text-xs font-mono text-zinc-500">{c.namespace}</p>
                  <StatusBadge child={c} />
                  <p className="mt-1 text-[11px] text-zinc-500">trust: {authority?.state ?? 'not linked'}{authority ? ` · expires ${new Date(authority.not_after).toLocaleDateString()}` : ''}</p>
                </div>
                {authority && <div className="relative z-10 flex gap-1">
                  <button title="Rotate link credential" disabled={changing !== null || authority.state !== 'active'} onClick={() => rotateLink(authority)} className="rounded p-2 text-zinc-500 hover:bg-zinc-100 disabled:opacity-30 dark:hover:bg-white/5"><RefreshCw size={14} /></button>
                  {authority.state === 'quarantined' ?
                    <button title="Restore authority" disabled={changing !== null} onClick={() => lifecycle(authority, 'restore')} className="rounded p-2 text-green-600 hover:bg-green-500/10"><ShieldCheck size={14} /></button> :
                    <button title="Quarantine authority" disabled={changing !== null || authority.state !== 'active'} onClick={() => lifecycle(authority, 'quarantine')} className="rounded p-2 text-amber-600 hover:bg-amber-500/10 disabled:opacity-30"><Ban size={14} /></button>}
                  <button title="Irreversibly decommission" disabled={changing !== null || authority.state === 'decommissioned'} onClick={() => lifecycle(authority, 'decommission')} className="rounded p-2 text-red-600 hover:bg-red-500/10 disabled:opacity-30"><ShieldOff size={14} /></button>
                </div>}
              </div>
                )
              })}
            </div>
          </>
        )}
      </div>
    </Layout>
  )
}
