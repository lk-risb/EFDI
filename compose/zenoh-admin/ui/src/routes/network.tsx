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
import {StatusPill} from '@/components/StatusPill'
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

interface AclReadiness {
  ready: boolean
  reason: string | null
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
  const [aclReadiness, setAclReadiness] = useState<AclReadiness | null>(null)
  const [changing, setChanging] = useState<string | null>(null)

  async function fetchAclReadiness(): Promise<AclReadiness | null> {
    if (!canManage) return null
    const response = await apiFetch('/api/trust/acl/preview')
    const body = await response.json().catch(() => ({}))
    return response.ok
      ? { ready: true, reason: null }
      : { ready: false, reason: body.detail ?? response.statusText }
  }

  async function load() {
    try {
      const [topologyData, childData, trustData, aclState] = await Promise.all([
        fetchTopology(),
        canManage ? apiJson<FederatedChild[]>('/api/federation') : Promise.resolve([]),
        canManage ? apiJson<TrustInventory>('/api/trust') : Promise.resolve(null),
        fetchAclReadiness(),
      ])
      setChildren(childData)
      setTopology(topologyData.nodes)
      setTransportEdges(topologyData.transport_edges)
      setTrust(trustData)
      setAclReadiness(aclState)
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
              <button
                onClick={applyAcl}
                disabled={changing !== null || aclReadiness?.ready === false}
                title={aclReadiness?.reason ?? undefined}
                className="flex items-center gap-2 rounded-md border border-zinc-300 px-3 py-2 text-xs disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/10"
              >
                <ShieldCheck size={13} /> Apply trust ACL
              </button>
              <button onClick={() => navigate({ to: '/certificates' })} className="flex items-center gap-2 rounded-md bg-accent-fill px-3 py-2 text-xs text-accent-text hover:bg-accent-fill-hover">
                <KeyRound size={13} /> Enroll child router
              </button>
            </div>
          ) : undefined}
        />

        <div className="mb-5 flex flex-wrap items-center gap-2 text-xs">
          <span className="flex items-center gap-1"><CheckCircle2 size={13} /> <StatusPill text={`${topology.filter(node => node.online && node.healthy).length} healthy`} tone="ok" /></span>
          {canManage && <span className="flex items-center gap-1"><GitBranch size={13} /> <StatusPill text={`${children.length} direct children`} tone="neutral" /></span>}
          {canManage && <span className="flex items-center gap-1"><ShieldCheck size={13} /> <StatusPill text={`ACL ${trust?.acl ? `v${trust.acl.sequence} ${trust.acl.state}` : 'not generated'}`} tone={trust?.acl?.state === 'applied' ? 'ok' : 'warn'} /></span>}
          <span className="flex items-center gap-1"><ShieldCheck size={13} /> <StatusPill text="immediate-parent authority" tone="neutral" /></span>
          <span className="flex items-center gap-1"><WifiOff size={13} /> <StatusPill text="child autonomy on disconnect" tone="neutral" /></span>
        </div>
        {aclReadiness?.ready === false && (
          <div className="mb-5 rounded-md border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-800 dark:text-amber-300">
            <p className="font-medium">Managed trust ACL is not ready to apply.</p>
            <p className="mt-1 text-xs opacity-90">{aclReadiness.reason}</p>
            <p className="mt-1 text-xs opacity-90">
              Observed LTU fabric routers are transport peers, not enrolled management children.
              Keep the current transport policy until that relationship is explicitly migrated.
            </p>
          </div>
        )}

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
          const displayName = node.role === 'hq' && node.reported !== false
            ? 'Local HQ'
            : node.namespace.replace(/^router:/, 'Router ')
          return (
            <section className="hud-frame relative mb-6 rounded-md border border-accent-ring/40 bg-white p-5 dark:bg-[#0c0c0e]">
              <HudCorners />
              <div className="mb-5 flex flex-wrap items-start justify-between gap-3 border-b border-zinc-200 pb-4 dark:border-white/10">
                <div>
                  <p className="hud-label text-[10px] text-zinc-500">Network / managed router</p>
                  <h2 className="mt-1 flex items-center gap-2 font-display text-2xl font-semibold text-zinc-900 dark:text-white">
                    <Router size={21} className="text-accent-ring" /> {displayName}
                  </h2>
                  {displayName !== node.namespace && (
                    <p className="mt-1 break-all font-mono text-xs text-zinc-500">
                      Organization namespace: {node.namespace}
                    </p>
                  )}
                  <div className="mt-2 flex flex-wrap gap-2">
                    <StatusPill
                      text={node.online ? (node.healthy ? 'healthy' : 'degraded') : 'offline'}
                      tone={node.online && node.healthy ? 'ok' : 'warn'}
                    />
                    <StatusPill text={child ? 'direct child' : node.role === 'hq' ? 'local root' : 'delegated descendant'} tone="neutral" />
                    <StatusPill text={node.verified ? 'signed identity verified' : 'unverified observation'} tone={node.verified ? 'ok' : 'warn'} />
                    <StatusPill text={node.config_status ?? 'no config result'} tone="neutral" />
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
                <div className="hud-card hud-glass border border-zinc-200 dark:border-white/10 p-4">
                  <h3 className="mb-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">Summary</h3>
                  <dl className="grid grid-cols-[minmax(7.5rem,0.65fr)_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
                    <dt className="text-zinc-500">Stable Zenoh ID</dt><dd className="font-mono break-all text-zinc-900 dark:text-zinc-100">{node.router_zid ?? 'Not reported'}</dd>
                    <dt className="text-zinc-500">Process</dt><dd className="text-zinc-900 dark:text-zinc-100">{node.online ? 'running' : 'unreachable'}</dd>
                    <dt className="text-zinc-500">Role</dt><dd className="text-zinc-900 dark:text-zinc-100">{node.role}</dd>
                    <dt className="text-zinc-500">Parent</dt><dd className="font-mono break-all text-zinc-900 dark:text-zinc-100">{node.parent_namespace ?? 'Local root'}</dd>
                    <dt className="text-zinc-500">Last presence</dt><dd className="text-zinc-900 dark:text-zinc-100">{node.last_seen_seconds.toFixed(1)}s ago</dd>
                    <dt className="text-zinc-500">Config result</dt><dd className="text-zinc-900 dark:text-zinc-100">{node.config_status ? `${node.config_status} · v${node.config_status_version}` : 'No result reported'}</dd>
                  </dl>
                </div>

                <div className="hud-card hud-glass border border-zinc-200 dark:border-white/10 p-4">
                  <h3 className="mb-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">Observed links</h3>
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

                <div className="hud-card hud-glass border border-zinc-200 dark:border-white/10 p-4 lg:col-span-2">
                  <h3 className="mb-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">Authority and offline behavior</h3>
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
            <h2 className="mb-3 mt-6 text-sm font-semibold text-zinc-900 dark:text-zinc-100">Direct management relationships</h2>
            <div className="grid gap-3 md:grid-cols-2">
              {children.length === 0 ? (
                <div className="hud-card hud-glass border border-zinc-200 dark:border-white/10 p-4 md:col-span-2">
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
                  <button title="Rotate link credential" disabled={changing !== null || authority.state !== 'active'} onClick={() => rotateLink(authority)} className="rounded-none p-2 text-zinc-500 hover:bg-zinc-100 disabled:opacity-30 dark:hover:bg-white/5"><RefreshCw size={14} /></button>
                  {authority.state === 'quarantined' ?
                    <button title="Restore authority" disabled={changing !== null} onClick={() => lifecycle(authority, 'restore')} className="rounded-none p-2 text-green-600 hover:bg-green-500/10"><ShieldCheck size={14} /></button> :
                    <button title="Quarantine authority" disabled={changing !== null || authority.state !== 'active'} onClick={() => lifecycle(authority, 'quarantine')} className="rounded-none p-2 text-amber-600 hover:bg-amber-500/10 disabled:opacity-30"><Ban size={14} /></button>}
                  <button title="Irreversibly decommission" disabled={changing !== null || authority.state === 'decommissioned'} onClick={() => lifecycle(authority, 'decommission')} className="rounded-none p-2 text-red-600 hover:bg-red-500/10 disabled:opacity-30"><ShieldOff size={14} /></button>
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
