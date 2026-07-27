import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useState} from 'react'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {apiFetch, apiJson, errorMessage} from '@/lib/api'
import {useAuth} from '@/store/auth'
import {notify} from '@/lib/notify'
import {CheckCircle2, FileCode2, Network, Plus, RotateCw, Save, ShieldCheck, Trash2, Waypoints} from 'lucide-react'
import {HudCorners} from '@/components/HudCorners'
import {IntegrationSettings} from '@/components/IntegrationSettings'
import {fetchTopology} from '@/lib/topology'

export const Route = createFileRoute('/config')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'admin' && role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: ConfigPage,
})

interface ConfigFields {
  mtls_port: number
  local_tcp_port: number
  fabric_endpoint: string
  partner_namespace: string
  inbound_namespace: string
  namespace_prefix: string
  publish_prefix: string
  verify_name_on_connect: boolean
  plugins_loading_enabled: boolean
  fabric_endpoints: string[]
  fabric_tls_profile: string
}

const EMPTY_FIELDS: ConfigFields = {
  mtls_port: 7447,
  local_tcp_port: 7448,
  fabric_endpoint: '',
  partner_namespace: '',
  inbound_namespace: '',
  namespace_prefix: '',
  publish_prefix: '',
  verify_name_on_connect: false,
  plugins_loading_enabled: true,
  fabric_endpoints: [],
  fabric_tls_profile: 'efdi',
}

interface FederatedChild {
  id: string
  name: string
  namespace: string
}

interface ManagedTarget {
  namespace: string
  label: string
  direct: boolean
  fields: ConfigFields | null
}

function Field({ label, help, children }: { label: string; help?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <label className="text-sm text-zinc-700 dark:text-zinc-300">{label}</label>
      {children}
      {help && <p className="text-xs text-zinc-500">{help}</p>}
    </div>
  )
}

function ConfigSection({
  icon,
  title,
  description,
  children,
  className = '',
}: {
  icon: React.ReactNode
  title: string
  description: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <section className={`hud-frame relative hud-enter rounded-md border border-zinc-200 hud-glass p-5 dark:border-white/10 ${className}`}>
      <HudCorners />
      <div className="mb-5 flex items-start gap-3 border-b border-zinc-200 pb-4 dark:border-white/10">
        <div className="mt-0.5 rounded-md border border-accent-ring/30 bg-accent-ring/10 p-2 text-accent-ring">
          {icon}
        </div>
        <div>
          <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">{title}</h2>
          <p className="mt-1 text-xs text-zinc-500">{description}</p>
        </div>
      </div>
      <div className="space-y-4">{children}</div>
    </section>
  )
}

// Known-good fabric endpoints seen in this pod's history — one-click fill,
// still just host/port under the hood (scheme is always tls, never exposed).
const FABRIC_PRESETS = [
  { label: 'Local mesh · EFDI mTLS', host: 'zenoh.efdi.netbird.efdi-backbone.net', port: 7447, profile: 'efdi' },
  { label: 'Backbone · Desert Bread mTLS', host: 'zenoh.efdi.netbird.efdi-backbone.net', port: 7447, profile: 'backbone' },
  { label: 'Legacy sandbox (nbio.fairytail.eu)', host: 'nbio.fairytail.eu', port: 7447, profile: 'backbone' },
]

function parseFabricEndpoint(v: string): { host: string; port: number } {
  const m = v.match(/^tls\/(.+):(\d+)$/)
  return m ? { host: m[1], port: Number(m[2]) } : { host: v, port: 7447 }
}

function Toggle({ label, help, checked, disabled, onChange }: {
  label: string; help?: string; checked: boolean; disabled: boolean; onChange: (v: boolean) => void
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <p className="text-sm text-zinc-700 dark:text-zinc-300">{label}</p>
        {help && <p className="text-xs text-zinc-500 mt-0.5">{help}</p>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`shrink-0 relative w-10 h-6 rounded-full transition-colors disabled:opacity-50 ${checked ? 'bg-accent-fill' : 'bg-zinc-300 dark:bg-zinc-700'}`}
      >
        <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform ${checked ? 'translate-x-4' : ''}`} />
      </button>
    </div>
  )
}

function ConfigPage() {
  const { role } = useAuth()
  const [fields, setFields] = useState<ConfigFields>(EMPTY_FIELDS)
  const [path, setPath] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [validating, setValidating] = useState(false)
  const canWrite = role === 'superadmin'
  const [targets, setTargets] = useState<ManagedTarget[]>([])
  const [target, setTarget] = useState<string>('local')
  const [localFields, setLocalFields] = useState<ConfigFields>(EMPTY_FIELDS)

  async function load() {
    setLoading(true)
    try {
      const data = await apiJson<{ fields: ConfigFields; path: string }>('/api/config')
      const normalized = {
        ...data.fields,
        // Keep the new UI compatible with an older admin API during a rolling
        // rebuild; older responses expose only the primary endpoint.
        fabric_endpoints: data.fields.fabric_endpoints ?? (data.fields.fabric_endpoint ? [data.fields.fabric_endpoint] : []),
        fabric_tls_profile: data.fields.fabric_tls_profile ?? 'efdi',
        publish_prefix: data.fields.publish_prefix ?? data.fields.namespace_prefix ?? '',
      }
      setLocalFields(normalized)
      if (target === 'local') setFields(normalized)
      setPath(data.path)
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  useEffect(() => {
    Promise.all([
      apiJson<FederatedChild[]>('/api/federation'),
      fetchTopology(),
    ]).then(([children, topology]) => {
      const direct = new Map(children.map(child => [child.namespace, child.name]))
      const nodes = new Map(topology.nodes.map(node => [node.namespace, node]))
      const parents = new Map(topology.nodes.map(node => [node.namespace, node.parent_namespace]))
      const isDescendant = (namespace: string) => {
        if (direct.has(namespace)) return true
        let current: string | null | undefined = namespace
        const seen = new Set<string>()
        while (current && !seen.has(current)) {
          seen.add(current)
          const parent = parents.get(current)
          if (parent === localFields.partner_namespace) return true
          current = parent
        }
        return false
      }
      const candidates = new Set([
        ...children.map(child => child.namespace),
        ...topology.nodes
          .filter(node => node.reported !== false && node.role !== 'peer' && isDescendant(node.namespace))
          .map(node => node.namespace),
      ])
      const discovered = [...candidates]
        .filter(namespace => namespace !== localFields.partner_namespace)
        .map(namespace => ({
          namespace,
          label: direct.has(namespace) ? `${direct.get(namespace)} (${namespace})` : namespace,
          direct: direct.has(namespace),
          fields: nodes.get(namespace)?.config_fields ?? null,
        }))
        .sort((a, b) => Number(b.direct) - Number(a.direct) || a.namespace.localeCompare(b.namespace))
      setTargets(discovered)
      const requested = sessionStorage.getItem('efdi-config-target')
      const requestedTarget = requested ? discovered.find(item => item.namespace === requested) : undefined
      if (requestedTarget?.fields) {
        setTarget(requested)
        setFields({ ...requestedTarget.fields, partner_namespace: requested })
        sessionStorage.removeItem('efdi-config-target')
      }
    }).catch(() => {})
  }, [localFields.partner_namespace])

  function selectTarget(next: string) {
    setTarget(next)
    if (next === 'local') {
      setFields(localFields)
    } else {
      const managed = targets.find(item => item.namespace === next)
      if (!managed?.fields) {
        notify.error('That router has not reported a current config snapshot yet')
        setTarget('local')
        setFields(localFields)
        return
      }
      setFields({ ...managed.fields, partner_namespace: next })
    }
  }

  function set<K extends keyof ConfigFields>(key: K, value: ConfigFields[K]) {
    setFields(f => ({ ...f, [key]: value }))
  }

  function endpointValues(): string[] {
    if (fields.fabric_endpoints.length > 0) return fields.fabric_endpoints
    return fields.fabric_endpoint ? [fields.fabric_endpoint] : ['']
  }

  function setEndpoint(index: number, value: string) {
    setFields(current => {
      const endpoints = current.fabric_endpoints.length > 0
        ? [...current.fabric_endpoints]
        : current.fabric_endpoint ? [current.fabric_endpoint] : []
      while (endpoints.length <= index) endpoints.push('')
      endpoints[index] = value
      return { ...current, fabric_endpoint: endpoints[0] ?? '', fabric_endpoints: endpoints }
    })
  }

  function removeEndpoint(index: number) {
    setFields(current => {
      const endpoints = current.fabric_endpoints.length > 0
        ? [...current.fabric_endpoints]
        : current.fabric_endpoint ? [current.fabric_endpoint] : []
      endpoints.splice(index, 1)
      return { ...current, fabric_endpoint: endpoints[0] ?? '', fabric_endpoints: endpoints }
    })
  }

  function addEndpoint() {
    setFields(current => {
      const endpoints = current.fabric_endpoints.length > 0
        ? [...current.fabric_endpoints]
        : current.fabric_endpoint ? [current.fabric_endpoint] : ['']
      endpoints.push('')
      return { ...current, fabric_endpoints: endpoints }
    })
  }

  async function handleSave() {
    if (!canWrite) return
    setSaving(true)
    try {
      const res = target === 'local'
        ? await apiFetch('/api/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(fields),
          })
        : await apiFetch('/api/federation/push-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_namespace: target, fields }),
          })
      const body = await res.json().catch(() => ({ detail: res.statusText }))
      if (!res.ok) throw new Error(body.detail ?? res.statusText)
      if (target === 'local') {
        const result = body.restarted ? 'Config validated, applied, and router restarted' : `Config activation status: ${body.status}`
        notify.success(body.native_process_restart_required
          ? `${result}. Restart native bridge scripts to apply the new namespace prefix.`
          : result)
      } else {
        notify.success(`Validated config sent via ${body.delivery} path (version ${body.version})`)
      }
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setSaving(false)
    }
  }

  async function handleValidate() {
    if (!canWrite) return
    setValidating(true)
    try {
      const res = await apiFetch('/api/config/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(fields),
      })
      const body = await res.json().catch(() => ({ detail: res.statusText }))
      if (!res.ok) throw new Error(body.detail ?? res.statusText)
      notify.success(body.detail ?? 'Zenoh accepted the candidate configuration')
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setValidating(false)
    }
  }

  const inputClass = "w-full px-3 py-2 rounded-md bg-zinc-200 dark:bg-[#141416] border border-zinc-300 dark:border-white/10 text-zinc-900 dark:text-white text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-50"

  return (
    <Layout>
      <div className="max-w-5xl p-6">
        <PageHeader
          title="Zenoh Config"
          actions={
            <>
            <button onClick={load} disabled={loading}
              className="flex items-center gap-2 px-3 py-2 rounded-md text-sm text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-white hover:bg-zinc-200/50 dark:hover:bg-white/[0.05] transition-colors disabled:opacity-50">
              <RotateCw size={14} /> Reload
            </button>
            {canWrite && (
              <button onClick={handleValidate} disabled={validating || loading}
                className="flex items-center gap-2 rounded-md border border-zinc-300 px-3 py-2 text-sm text-zinc-700 transition-colors hover:border-accent-ring hover:text-zinc-900 disabled:opacity-50 dark:border-white/10 dark:text-zinc-300 dark:hover:text-white">
                <CheckCircle2 size={14} /> {validating ? 'Validating…' : 'Validate'}
              </button>
            )}
            {canWrite && (
              <button onClick={handleSave} disabled={saving || loading}
                className="flex items-center gap-2 px-4 py-2 bg-accent-fill hover:bg-accent-fill-hover text-accent-text text-sm rounded-md transition-colors disabled:opacity-50">
                <Save size={14} /> {saving ? 'Saving…' : target === 'local' ? 'Save & Restart' : 'Push Config'}
              </button>
            )}
            </>
          }
        />

        <div className="mb-6 grid gap-3 sm:grid-cols-2">
          <div className="hud-frame relative flex min-h-20 items-center gap-3 rounded-md border border-zinc-200 bg-white px-4 py-3 dark:border-white/10 dark:bg-[#0c0c0e]">
            <HudCorners />
            <FileCode2 size={18} className="shrink-0 text-zinc-500" />
            <div className="min-w-0">
              <p className="hud-label text-[11px] text-zinc-500">Configuration source</p>
              <p className="mt-1 truncate font-mono text-xs text-zinc-700 dark:text-zinc-300" title={path || undefined}>
                {path || 'Loading configuration path…'}
              </p>
            </div>
          </div>

          <div className="hud-frame relative flex min-h-20 items-center gap-3 rounded-md border border-zinc-200 bg-white px-4 py-3 dark:border-white/10 dark:bg-[#0c0c0e]">
            <HudCorners />
            <Waypoints size={18} className="shrink-0 text-zinc-500" />
            <div className="min-w-0 flex-1">
              <label className="hud-label text-[11px] text-zinc-500" htmlFor="config-target">Apply target</label>
              {targets.length > 0 ? (
                <select id="config-target" value={target} onChange={e => selectTarget(e.target.value)}
                  className="mt-1 w-full bg-transparent text-sm text-zinc-800 focus:outline-none focus:ring-2 focus:ring-accent-ring dark:text-zinc-200">
                  <option value="local">This pod</option>
                  {targets.map(item => <option key={item.namespace} value={item.namespace} disabled={!item.fields}>{item.direct ? 'Direct · ' : 'Descendant · '}{item.label}{item.fields ? '' : ' · awaiting snapshot'}</option>)}
                </select>
              ) : (
                <p className="mt-1 text-sm text-zinc-700 dark:text-zinc-300">This pod</p>
              )}
            </div>
          </div>
        </div>

        {!canWrite && (
          <div className="mb-4 rounded-md border border-yellow-500/30 bg-yellow-500/10 px-4 py-3 text-xs text-yellow-700 dark:text-yellow-300">
            Your role can inspect this configuration but only a superadmin can change or push it.
          </div>
        )}

        {target !== 'local' && (
          <div className="mb-4 rounded-md border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs text-amber-700 dark:text-amber-300">
            Editing the last config snapshot reported by <span className="font-mono">{target}</span>. Identity, listener ports, CA profile, name-verification policy, and org control prefix are local-only. Endpoint replacement must retain an existing endpoint for a staged migration; the target rolls back unless both router health and a remote router link recover.
          </div>
        )}

        {loading ? (
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="h-80 animate-pulse rounded-md bg-zinc-200 dark:bg-zinc-800" />
            <div className="h-80 animate-pulse rounded-md bg-zinc-200 dark:bg-zinc-800" />
          </div>
        ) : (
          <div className="grid items-start gap-4 lg:grid-cols-2">
            <ConfigSection
              icon={<Network size={17} />}
              title="Transport"
              description="Local listeners and the encrypted parent-fabric connection."
              className="lg:col-span-2"
            >
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Local mTLS port" help="Mesh-facing listener for bridges and audit clients (default 7447).">
                  <input type="number" min={1} max={65535} disabled={!canWrite} className={inputClass}
                    value={fields.mtls_port} onChange={e => set('mtls_port', Number(e.target.value))} />
                </Field>
                <Field label="Local TCP port" help="Plaintext loopback listener for native processes and this GUI (default 7448).">
                  <input type="number" min={1} max={65535} disabled={!canWrite} className={inputClass}
                    value={fields.local_tcp_port} onChange={e => set('local_tcp_port', Number(e.target.value))} />
                </Field>
              </div>
              <Field label="Fabric endpoints" help="Explicit Zenoh peers this pod dials over mTLS. Use two or more for redundant uplinks or same-level links.">
                <div className="mb-2 flex flex-wrap gap-2">
                  <button type="button" disabled={!canWrite}
                    onClick={() => setFields(f => ({ ...f, fabric_endpoint: '', fabric_endpoints: [], fabric_tls_profile: 'efdi' }))}
                    className="rounded-full border border-zinc-300 px-2.5 py-1 text-xs text-zinc-600 transition-colors hover:border-zinc-500 hover:text-zinc-900 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:text-white">
                    Root / no upstream
                  </button>
                  {FABRIC_PRESETS.map(p => (
                    <button key={p.label} type="button" disabled={!canWrite}
                      onClick={() => setFields(f => ({ ...f, fabric_endpoint: `tls/${p.host}:${p.port}`, fabric_endpoints: [`tls/${p.host}:${p.port}`], fabric_tls_profile: p.profile }))}
                      className="rounded-full border border-zinc-300 px-2.5 py-1 text-xs text-zinc-600 transition-colors hover:border-zinc-500 hover:text-zinc-900 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:text-white">
                      {p.label}
                    </button>
                  ))}
                </div>
                <Field label="Fabric mTLS identity" help="The selected certificate, private key, and trust roots are used for every TLS link in this router. Choose the profile that belongs to the selected federation endpoint.">
                  <select disabled={!canWrite} className={inputClass} value={fields.fabric_tls_profile}
                    onChange={e => set('fabric_tls_profile', e.target.value)}>
                    <option value="efdi">Local mesh (EFDI CA)</option>
                    <option value="backbone">Backbone (Desert Bread CA)</option>
                  </select>
                </Field>
                <div className="space-y-2">
                  {endpointValues().map((endpoint, index) => {
                    const parsed = parseFabricEndpoint(endpoint)
                    return (
                      <div key={index} className="flex gap-2">
                        <div className="flex flex-1 items-center gap-2 rounded-md border border-zinc-300 bg-zinc-200 px-3 focus-within:ring-2 focus-within:ring-accent-ring dark:border-white/10 dark:bg-[#141416]">
                          <span className="shrink-0 text-sm text-zinc-500">tls://</span>
                          <input type="text" disabled={!canWrite} placeholder="host or NetBird name"
                            className="flex-1 bg-transparent py-2 font-mono text-sm text-zinc-900 focus:outline-none disabled:opacity-50 dark:text-white"
                            value={parsed.host}
                            onChange={e => setEndpoint(index, e.target.value ? `tls/${e.target.value}:${parsed.port}` : '')} />
                        </div>
                        <input type="number" min={1} max={65535} disabled={!canWrite}
                          className={`${inputClass} w-28`}
                          value={parsed.port}
                          onChange={e => setEndpoint(index, parsed.host ? `tls/${parsed.host}:${e.target.value}` : '')} />
                        {endpointValues().length > 1 && (
                          <button type="button" disabled={!canWrite} onClick={() => removeEndpoint(index)}
                            className="rounded-md px-2 text-zinc-500 hover:bg-zinc-200 hover:text-red-600 disabled:opacity-50 dark:hover:bg-white/[0.05] dark:hover:text-red-400" aria-label="Remove endpoint">
                            <Trash2 size={15} />
                          </button>
                        )}
                      </div>
                    )
                  })}
                  <button type="button" disabled={!canWrite} onClick={addEndpoint}
                    className="flex items-center gap-1.5 text-xs text-accent-ring hover:underline disabled:opacity-50">
                    <Plus size={13} /> Add direct link
                  </button>
                </div>
              </Field>
            </ConfigSection>

            <ConfigSection
              icon={<Waypoints size={17} />}
              title="Namespace"
              description="Ownership and bilateral routing boundaries for this pod."
            >
              <Field label="Data publish prefix" help="Prepended to the partner slot for bridge data. Leave empty for the EFDI sandbox contract: <slot-id>/**. Changes are applied live and restart native bridges.">
                <input type="text" disabled={!canWrite} className={inputClass}
                  value={fields.publish_prefix} onChange={e => set('publish_prefix', e.target.value)} placeholder="empty = slot root" />
              </Field>
              <Field label="Org namespace prefix" help="Organization path at any depth, for example LTU/CISB or LTU/CISB/LTK.">
                <input type="text" disabled={!canWrite} className={inputClass}
                  value={fields.namespace_prefix} onChange={e => set('namespace_prefix', e.target.value)} />
              </Field>
              <Field label="Partner namespace" help="This pod's first-party publish and subscribe slot.">
                <input type="text" disabled={!canWrite || target !== 'local'} className={inputClass}
                  value={fields.partner_namespace} onChange={e => set('partner_namespace', e.target.value)} />
              </Field>
              <Field label="Inbound namespace" help="Bilateral prefix that the fabric publishes toward this pod.">
                <input type="text" disabled={!canWrite} className={inputClass}
                  value={fields.inbound_namespace} onChange={e => set('inbound_namespace', e.target.value)} />
              </Field>
              <p className="rounded border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
                Namespace changes restart the router. Native bridge processes must also be restarted to publish under the new prefix.
              </p>
            </ConfigSection>

            <ConfigSection
              icon={<ShieldCheck size={17} />}
              title="Connection policy"
              description="Certificate verification and retained-value plugin behavior."
            >
            <Toggle label="Verify name on connect" disabled={!canWrite}
              help="Verify the fabric endpoint's cert SAN against the DNS name dialed. Off by default: the gateway cert SAN binds the mesh IP, not the DNS name — turning this on can break the fabric connection."
              checked={fields.verify_name_on_connect} onChange={v => set('verify_name_on_connect', v)} />
            <Toggle label="Storage plugin loading" disabled={!canWrite}
              help="Whether the storage_manager plugin loads at all. Off means new subscribers no longer get a last-known value via get() — publish/subscribe still works."
              checked={fields.plugins_loading_enabled} onChange={v => set('plugins_loading_enabled', v)} />
            </ConfigSection>
          </div>
        )}
        <div className="mt-5">
          <IntegrationSettings />
        </div>
      </div>
    </Layout>
  )
}
