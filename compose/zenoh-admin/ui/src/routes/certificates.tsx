import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useState} from 'react'
import {CheckCircle2, Copy, KeyRound, Lock, Network, Plus, ShieldAlert, ShieldCheck, UploadCloud, Wifi} from 'lucide-react'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {HudCorners} from '@/components/HudCorners'
import {StatusPill} from '@/components/StatusPill'
import {Skeleton} from '@/components/Skeleton'
import {apiFetch, apiJson, errorDetail, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'
import {cn} from '@/lib/utils'

export const Route = createFileRoute('/certificates')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: CertificatesPage,
})

interface BootstrapStatus { bootstrap: boolean }
interface BackboneStatus { identity_uploaded: boolean; configured: boolean }
interface CertInfo { name: string; expires_at: string; days_remaining: number }
interface PkiStatus {
  configured: boolean
  available: boolean
  issuer: string | null
  expires_at: string | null
  path_length: number | null
  step_ca_available: boolean
  managed_trust: {
    ready: boolean
    identity?: string
    namespace_scope?: string
    max_delegation_depth?: number
    expires_at?: string
    error?: string
  }
}
interface Invitation {
  id: string
  child_name: string
  namespace: string
  max_delegation_depth: number
  created_at: string
  expires_at: string
  used_at: string | null
  status: string
  issued_serials: string | null
  token?: string
}

function certState(days: number): 'ok' | 'warn' | 'critical' {
  if (days < 7) return 'critical'
  if (days < 30) return 'warn'
  return 'ok'
}

const inputClass = 'w-full rounded-md border border-zinc-300 bg-zinc-100 px-3 py-2 text-sm text-zinc-900 focus:outline-none focus:ring-2 focus:ring-accent-ring dark:border-white/10 dark:bg-[#141416] dark:text-white'

function CertificatesPage() {
  const [certs, setCerts] = useState<CertInfo[] | null>(null)
  const [pki, setPki] = useState<PkiStatus | null>(null)
  const [invitations, setInvitations] = useState<Invitation[] | null>(null)
  const [childName, setChildName] = useState('')
  const [namespace, setNamespace] = useState('')
  const [depth, setDepth] = useState(0)
  const [hours, setHours] = useState(24)
  const [creating, setCreating] = useState(false)
  const [newInvitation, setNewInvitation] = useState<Invitation | null>(null)
  const maximumChildDepth = pki?.path_length == null ? -1 : pki.path_length - 1
  const canDelegate = Boolean(pki?.available && pki.managed_trust.ready && maximumChildDepth >= 0)

  const [bootstrapStatus, setBootstrapStatus] = useState<BootstrapStatus | null>(null)
  const [caFile, setCaFile] = useState<File | null>(null)
  const [certFile, setCertFile] = useState<File | null>(null)
  const [keyFile, setKeyFile] = useState<File | null>(null)
  const [bootPartnerNamespace, setBootPartnerNamespace] = useState('')
  const [bootNamespacePrefix, setBootNamespacePrefix] = useState('EFDI')
  const [uploading, setUploading] = useState(false)
  const [showRotate, setShowRotate] = useState(false)

  // Backbone identity — a SEPARATE mTLS identity (Desert Bread CA, not
  // EFDI's own) for the dedicated zenoh-router-backbone container. See
  // api/backbone_bootstrap.py: Zenoh 1.x applies one TLS identity per router
  // session, so this can never be the same upload flow as the pod's own
  // identity above.
  const [backboneStatus, setBackboneStatus] = useState<BackboneStatus | null>(null)
  const [backboneCaFile, setBackboneCaFile] = useState<File | null>(null)
  const [backboneCertFile, setBackboneCertFile] = useState<File | null>(null)
  const [backboneKeyFile, setBackboneKeyFile] = useState<File | null>(null)
  const [backboneEndpoint, setBackboneEndpoint] = useState('tls/zenoh.efdi.netbird.efdi-backbone.net:7447')
  const [backboneUploading, setBackboneUploading] = useState(false)
  const [showBackboneRotate, setShowBackboneRotate] = useState(false)

  // Backbone NetBird join — a second, independent netbird daemon on this
  // host (efdi.ltu and the backbone are separate NetBird accounts; one
  // daemon can only join one). See api/netbird_instances.py.
  const [netbirdManagementUrl, setNetbirdManagementUrl] = useState('')
  const [netbirdSetupKey, setNetbirdSetupKey] = useState('')
  const [netbirdConfiguring, setNetbirdConfiguring] = useState(false)
  const [netbirdStatus, setNetbirdStatus] = useState<{ connected: boolean } | null>(null)
  const [showNetbirdJoin, setShowNetbirdJoin] = useState(false)

  async function load() {
    try {
      const [health, status, invites, boot, config, backbone] = await Promise.all([
        apiJson<{ system: { certs: CertInfo[] } }>('/api/health'),
        apiJson<PkiStatus>('/api/pki/status'),
        apiJson<Invitation[]>('/api/pki/invitations'),
        apiJson<BootstrapStatus>('/api/certs/bootstrap/status'),
        apiJson<{ fields: { partner_namespace: string; namespace_prefix: string } | null }>('/api/config'),
        apiJson<BackboneStatus>('/api/certs/backbone/status'),
      ])
      setCerts(health.system.certs)
      setPki(status)
      setInvitations(invites)
      setBootstrapStatus(boot)
      setBackboneStatus(backbone)
      // Prefill so rotating an already-secured identity doesn't require
      // retyping values the pod already knows — only bootstrap mode (no
      // fields yet) needs the operator to type these from scratch.
      if (config.fields) {
        setBootPartnerNamespace(current => current || config.fields!.partner_namespace)
        setBootNamespacePrefix(current => (current && current !== 'EFDI') ? current : config.fields!.namespace_prefix)
      }
    } catch (error) {
      notify.error(errorMessage(error))
    }
    // Isolated from the Promise.all above: this hits the host control agent
    // (a subprocess call, not just a DB read), so a transient hiccup there
    // shouldn't take down the rest of the page's status. Never joined yet is
    // a normal, silent "not connected" result, not an error.
    try {
      setNetbirdStatus(await apiJson<{ connected: boolean }>('/api/netbird/backbone/status'))
    } catch {
      // leave netbirdStatus as-is — the join form stays expanded either way
    }
  }

  // Best-effort convenience: read the certificate's own CN so the operator
  // doesn't have to run `openssl x509 -noout -subject` and copy it in by
  // hand. Never overwrites a value already typed, and any failure (not a
  // real cert yet, network hiccup, no CN present) is silently ignored — this
  // is a prefill, not a validation step, and /bootstrap's own server-side
  // checks are what actually enforce correctness on submit.
  async function inspectCertFile(file: File) {
    try {
      const form = new FormData()
      form.append('certificate', file)
      const body = await apiJson<{ common_name: string | null }>('/api/certs/inspect', {
        method: 'POST', body: form,
      })
      if (body.common_name) {
        setBootPartnerNamespace(current => current || body.common_name!)
      }
    } catch {
      // best-effort — leave the field for the operator to fill in by hand
    }
  }

  async function uploadBootstrapIdentity(event: React.FormEvent) {
    event.preventDefault()
    if (!caFile || !certFile || !keyFile) {
      notify.error('CA root, certificate, and private key are all required')
      return
    }
    setUploading(true)
    try {
      const form = new FormData()
      form.append('ca_root', caFile)
      form.append('certificate', certFile)
      form.append('private_key', keyFile)
      form.append('partner_namespace', bootPartnerNamespace)
      form.append('namespace_prefix', bootNamespacePrefix)
      const response = await apiFetch('/api/certs/bootstrap', { method: 'POST', body: form })
      const body = await response.json().catch(() => ({ detail: response.statusText }))
      if (!response.ok) throw new Error(errorDetail(body, response))
      notify.success(bootstrapStatus?.bootstrap === false
        ? 'Certificates rotated and applied. This page may blip for a few seconds while the admin service restarts.'
        : 'Certificates applied — router switched to mTLS. This page may blip for a few seconds while the admin service restarts.')
      setCaFile(null)
      setCertFile(null)
      setKeyFile(null)
      setShowRotate(false)
    } catch (error) {
      notify.error(errorMessage(error))
    } finally {
      setUploading(false)
      // The admin container recreates itself to pick up the new namespace —
      // this request's own connection may die mid-flight either way, so
      // always re-check state after a delay rather than trusting the outcome
      // of this specific fetch.
      setTimeout(load, 6000)
    }
  }

  async function uploadBackboneIdentity(event: React.FormEvent) {
    event.preventDefault()
    if (!backboneCaFile || !backboneCertFile || !backboneKeyFile || !backboneEndpoint) {
      notify.error('CA root, certificate, private key, and the router endpoint are all required')
      return
    }
    setBackboneUploading(true)
    try {
      const form = new FormData()
      form.append('ca_root', backboneCaFile)
      form.append('certificate', backboneCertFile)
      form.append('private_key', backboneKeyFile)
      form.append('backbone_endpoint', backboneEndpoint)
      const response = await apiFetch('/api/certs/backbone/bootstrap', { method: 'POST', body: form })
      const body = await response.json().catch(() => ({ detail: response.statusText }))
      if (!response.ok) throw new Error(errorDetail(body, response))
      notify.success('Backbone identity applied — zenoh-router-backbone starting.')
      setBackboneCaFile(null)
      setBackboneCertFile(null)
      setBackboneKeyFile(null)
      setShowBackboneRotate(false)
    } catch (error) {
      notify.error(errorMessage(error))
    } finally {
      setBackboneUploading(false)
      setTimeout(load, 4000)
    }
  }

  async function configureBackboneNetbird(event: React.FormEvent) {
    event.preventDefault()
    if (!netbirdManagementUrl || !netbirdSetupKey) {
      notify.error('Management URL and setup key are both required')
      return
    }
    setNetbirdConfiguring(true)
    try {
      const response = await apiFetch('/api/netbird/backbone/configure', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ management_url: netbirdManagementUrl, setup_key: netbirdSetupKey }),
      })
      const body = await response.json().catch(() => ({ detail: response.statusText }))
      if (!response.ok) throw new Error(errorDetail(body, response))
      notify.success('Backbone NetBird instance joined. DNS/routing for the backbone endpoint should resolve shortly.')
      setNetbirdSetupKey('')
      setShowNetbirdJoin(false)
    } catch (error) {
      notify.error(errorMessage(error))
    } finally {
      setNetbirdConfiguring(false)
      setTimeout(load, 4000)
    }
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 30000)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    if (maximumChildDepth >= 0 && depth > maximumChildDepth) setDepth(maximumChildDepth)
  }, [depth, maximumChildDepth])

  async function createInvitation(event: React.FormEvent) {
    event.preventDefault()
    setCreating(true)
    try {
      const response = await apiFetch('/api/pki/invitations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ child_name: childName, namespace, max_delegation_depth: depth, expires_in_hours: hours }),
      })
      const body = await response.json().catch(() => ({ detail: response.statusText }))
      if (!response.ok) throw new Error(errorDetail(body, response))
      setNewInvitation(body)
      setChildName('')
      setNamespace('')
      notify.success('Single-use router enrollment invitation created')
      await load()
    } catch (error) {
      notify.error(errorMessage(error))
    } finally {
      setCreating(false)
    }
  }

  async function copyToken() {
    if (!newInvitation?.token) return
    await navigator.clipboard.writeText(newInvitation.token)
    notify.success('Enrollment token copied')
  }

  return (
    <Layout>
      <div className="p-6">
        <PageHeader eyebrow="SECURITY / CERTIFICATES" title="Certificate Authority" count={certs?.length} countLabel="local identities" />

        {bootstrapStatus?.bootstrap === false && (
          <div className="mb-5 flex flex-wrap items-center gap-2 text-xs">
            <span className="flex items-center gap-1"><Lock size={13} /> <StatusPill text="router secured (mTLS)" tone="ok" /></span>
            {!showRotate && (
              <button onClick={() => setShowRotate(true)}
                className="flex items-center gap-1 rounded-md border border-zinc-300 px-2.5 py-1 text-zinc-600 hover:border-accent-ring hover:text-zinc-900 dark:border-white/10 dark:text-zinc-400 dark:hover:text-white">
                <UploadCloud size={12} /> Rotate certificates…
              </button>
            )}
          </div>
        )}

        {(bootstrapStatus?.bootstrap === true || showRotate) && (
          <section className="hud-card hud-glass hud-frame relative mb-6 border border-amber-500/30 p-4">
            <HudCorners />
            <div className="mb-3 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <ShieldAlert size={16} className="text-amber-600 dark:text-amber-400" />
                <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                  {bootstrapStatus?.bootstrap === true ? 'Router not yet secured — plaintext bootstrap mode' : 'Rotate certificate identity'}
                </h2>
              </div>
              {bootstrapStatus?.bootstrap === false && (
                <button onClick={() => setShowRotate(false)} className="text-xs text-zinc-500 hover:text-zinc-900 dark:hover:text-white">Cancel</button>
              )}
            </div>
            <p className="mb-4 text-xs text-zinc-500">
              {bootstrapStatus?.bootstrap === true
                ? <>This pod is running on a throwaway self-signed identity with no mTLS. Upload your real CA root,
                    certificate, and private key below to switch the router to mTLS — from <code>scripts/gen-certs.sh</code>,
                    or from your own org's issuer. The admin service restarts briefly afterward.</>
                : <>Replaces this pod's current identity with a newly issued CA root, certificate, and private key —
                    from <code>scripts/gen-certs.sh</code>, or from your own org's issuer. The router and admin service
                    restart briefly afterward — make sure the new material is signed for the same PARTNER_NAMESPACE
                    unless you intend to move slots.</>}
            </p>
            <form onSubmit={uploadBootstrapIdentity} className="grid gap-3 sm:grid-cols-2">
              <label className="text-xs text-zinc-500">
                CA root (.crt or .pem)
                <input type="file" required accept=".pem,.crt,.cer"
                  onChange={event => setCaFile(event.target.files?.[0] ?? null)}
                  className="mt-1 block w-full text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 dark:file:bg-white/10 dark:file:text-white" />
              </label>
              <label className="text-xs text-zinc-500">
                Certificate (.crt or .pem)
                <input type="file" required accept=".pem,.crt,.cer"
                  onChange={event => {
                    const file = event.target.files?.[0] ?? null
                    setCertFile(file)
                    if (file) void inspectCertFile(file)
                  }}
                  className="mt-1 block w-full text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 dark:file:bg-white/10 dark:file:text-white" />
              </label>
              <label className="text-xs text-zinc-500">
                Private key (.key or .pem)
                <input type="file" required accept=".pem,.key"
                  onChange={event => setKeyFile(event.target.files?.[0] ?? null)}
                  className="mt-1 block w-full text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 dark:file:bg-white/10 dark:file:text-white" />
              </label>
              <label className="text-xs text-zinc-500">
                PARTNER_NAMESPACE
                <input className={`${inputClass} mt-1 font-mono`} value={bootPartnerNamespace}
                  onChange={event => setBootPartnerNamespace(event.target.value)}
                  placeholder="0123456789abcdef0123456789abcdef" required />
              </label>
              <label className="text-xs text-zinc-500 sm:col-span-2">
                NAMESPACE_PREFIX
                <input className={`${inputClass} mt-1 font-mono`} value={bootNamespacePrefix}
                  onChange={event => setBootNamespacePrefix(event.target.value)} required />
              </label>
              <button disabled={uploading} className="sm:col-span-2 flex items-center justify-center gap-2 rounded-md bg-accent-fill px-4 py-2 text-sm text-accent-text disabled:opacity-50">
                <UploadCloud size={14} /> {uploading ? 'Applying…' : bootstrapStatus?.bootstrap === true ? 'Upload and switch to mTLS' : 'Upload and rotate identity'}
              </button>
            </form>
          </section>
        )}

        {netbirdStatus?.connected && (
          <div className="mb-5 flex flex-wrap items-center gap-2 text-xs">
            <span className="flex items-center gap-1">
              <Wifi size={13} /> <StatusPill text="backbone netbird connected" tone="ok" />
            </span>
            {!showNetbirdJoin && (
              <button onClick={() => setShowNetbirdJoin(true)}
                className="flex items-center gap-1 rounded-md border border-zinc-300 px-2.5 py-1 text-zinc-600 hover:border-accent-ring hover:text-zinc-900 dark:border-white/10 dark:text-zinc-400 dark:hover:text-white">
                <Wifi size={12} /> Rejoin / change key…
              </button>
            )}
          </div>
        )}

        {(!netbirdStatus?.connected || showNetbirdJoin) && (
          <section className="hud-card hud-glass hud-frame relative mb-6 border border-zinc-200 p-4 dark:border-white/10">
            <HudCorners />
            <div className="mb-3 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Wifi size={16} className="text-zinc-500" />
                <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Backbone NetBird join</h2>
              </div>
              {netbirdStatus?.connected && (
                <button onClick={() => setShowNetbirdJoin(false)} className="text-xs text-zinc-500 hover:text-zinc-900 dark:hover:text-white">Cancel</button>
              )}
            </div>
            <p className="mb-4 text-xs text-zinc-500">
              This pod's own NetBird instance and the EFDI Backbone trial fabric are separate NetBird
              accounts — one daemon can only join one. This provisions a SECOND, independent netbird
              service on this host (its own systemd unit, config dir, and control socket) purely to reach
              the backbone, without touching the pod's existing NetBird connection. Required before the
              backbone identity below can resolve or route anywhere. Get the management URL and a fresh
              setup key from the backbone NetBird admin panel — each peer needs its own setup key.
            </p>
            <form onSubmit={configureBackboneNetbird} className="grid gap-3 sm:grid-cols-2">
              <label className="text-xs text-zinc-500">
                Management URL
                <input className={`${inputClass} mt-1 font-mono`} value={netbirdManagementUrl}
                  onChange={event => setNetbirdManagementUrl(event.target.value)}
                  placeholder="https://netbird.efdi-backbone.net:443" required />
              </label>
              <label className="text-xs text-zinc-500">
                Setup key
                <input type="password" className={`${inputClass} mt-1 font-mono`} value={netbirdSetupKey}
                  onChange={event => setNetbirdSetupKey(event.target.value)}
                  placeholder="one-time join key" required />
              </label>
              <button disabled={netbirdConfiguring} className="sm:col-span-2 flex items-center justify-center gap-2 rounded-md bg-accent-fill px-4 py-2 text-sm text-accent-text disabled:opacity-50">
                <Wifi size={14} /> {netbirdConfiguring ? 'Joining…' : 'Join backbone NetBird network'}
              </button>
            </form>
          </section>
        )}

        {backboneStatus?.identity_uploaded && (
          <div className="mb-5 flex flex-wrap items-center gap-2 text-xs">
            <span className="flex items-center gap-1">
              <Network size={13} /> <StatusPill text="backbone identity uploaded" tone="ok" />
            </span>
            {!showBackboneRotate && (
              <button onClick={() => setShowBackboneRotate(true)}
                className="flex items-center gap-1 rounded-md border border-zinc-300 px-2.5 py-1 text-zinc-600 hover:border-accent-ring hover:text-zinc-900 dark:border-white/10 dark:text-zinc-400 dark:hover:text-white">
                <UploadCloud size={12} /> Rotate backbone identity…
              </button>
            )}
          </div>
        )}

        {(!backboneStatus?.identity_uploaded || showBackboneRotate) && (
          <section className="hud-card hud-glass hud-frame relative mb-6 border border-zinc-200 p-4 dark:border-white/10">
            <HudCorners />
            <div className="mb-3 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Network size={16} className="text-zinc-500" />
                <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Backbone identity</h2>
              </div>
              {backboneStatus?.identity_uploaded && (
                <button onClick={() => setShowBackboneRotate(false)} className="text-xs text-zinc-500 hover:text-zinc-900 dark:hover:text-white">Cancel</button>
              )}
            </div>
            <p className="mb-4 text-xs text-zinc-500">
              Separate mTLS identity for the dedicated <code>zenoh-router-backbone</code> container that
              bridges this pod to the EFDI Backbone trial fabric. Distinct CA from this pod's own identity
              above — Zenoh applies one TLS identity per router session, so the backbone link runs as its
              own router process.
            </p>
            <form onSubmit={uploadBackboneIdentity} className="grid gap-3 sm:grid-cols-2">
              <label className="text-xs text-zinc-500 sm:col-span-2">
                Backbone router endpoint
                <input className={`${inputClass} mt-1 font-mono`} value={backboneEndpoint}
                  onChange={event => setBackboneEndpoint(event.target.value)}
                  placeholder="tls/zenoh.efdi.netbird.efdi-backbone.net:7447" required />
              </label>
              <label className="text-xs text-zinc-500">
                CA root (.crt or .pem)
                <input type="file" required accept=".pem,.crt,.cer"
                  onChange={event => setBackboneCaFile(event.target.files?.[0] ?? null)}
                  className="mt-1 block w-full text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 dark:file:bg-white/10 dark:file:text-white" />
              </label>
              <label className="text-xs text-zinc-500">
                Certificate (.crt or .pem)
                <input type="file" required accept=".pem,.crt,.cer"
                  onChange={event => setBackboneCertFile(event.target.files?.[0] ?? null)}
                  className="mt-1 block w-full text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 dark:file:bg-white/10 dark:file:text-white" />
              </label>
              <label className="text-xs text-zinc-500 sm:col-span-2">
                Private key (.key or .pem)
                <input type="file" required accept=".pem,.key"
                  onChange={event => setBackboneKeyFile(event.target.files?.[0] ?? null)}
                  className="mt-1 block w-full text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 dark:file:bg-white/10 dark:file:text-white" />
              </label>
              <button disabled={backboneUploading} className="sm:col-span-2 flex items-center justify-center gap-2 rounded-md bg-accent-fill px-4 py-2 text-sm text-accent-text disabled:opacity-50">
                <UploadCloud size={14} /> {backboneUploading ? 'Applying…' : backboneStatus?.identity_uploaded ? 'Upload and rotate backbone identity' : 'Upload and start backbone router'}
              </button>
            </form>
          </section>
        )}

        <div className="mb-5 flex flex-wrap items-center gap-2 text-xs">
          <span className="flex items-center gap-1">
            {pki?.available ? <CheckCircle2 size={13} /> : <ShieldAlert size={13} />}
            <StatusPill text={pki?.available ? 'router CA available' : 'router CA unavailable'} tone={pki?.available ? 'ok' : 'warn'} />
          </span>
          <span className="flex items-center gap-1">
            {pki?.managed_trust.ready ? <ShieldCheck size={13} /> : <ShieldAlert size={13} />}
            <StatusPill text={pki?.managed_trust.ready ? 'delegation chain verified' : 'managed trust unavailable'} tone={pki?.managed_trust.ready ? 'ok' : 'warn'} />
          </span>
          <span className="flex items-center gap-1">
            {pki?.step_ca_available ? <CheckCircle2 size={13} /> : <ShieldAlert size={13} />}
            <StatusPill text={pki?.step_ca_available ? 'step-ca leaf issuer ready' : 'step-ca leaf issuer unavailable'} tone={pki?.step_ca_available ? 'ok' : 'warn'} />
          </span>
          <span className="flex items-center gap-1"><ShieldCheck size={13} /> <StatusPill text="child-generated private keys" tone="neutral" /></span>
          <span className="flex items-center gap-1"><Network size={13} /> <StatusPill text="bounded delegation depth" tone="neutral" /></span>
        </div>

        <div className="mb-6 grid gap-4 lg:grid-cols-2">
          <section className="hud-card hud-glass hud-frame relative border border-zinc-200 dark:border-white/10 p-4">
            <HudCorners />
            <h2 className="mb-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">Issuer status</h2>
            <dl className="hud-kv">
              <dt>Mode</dt><dd>{pki?.configured ? 'managed router CA' : 'not configured'}</dd>
              <dt>CA availability</dt><dd className={pki?.available ? 'text-green-600 dark:text-green-400' : 'text-amber-600 dark:text-amber-400'}>{pki?.available ? 'certificate and key verified' : 'signing disabled'}</dd>
              <dt>Managed trust</dt><dd title={pki?.managed_trust.error}>{pki?.managed_trust.ready ? 'identity and delegation chain verified' : (pki?.managed_trust.error ?? 'unavailable')}</dd>
              <dt>Transport issuer</dt><dd>{pki?.step_ca_available ? 'step-ca short-lived leaf issuer' : 'router CA fallback'}</dd>
              <dt>Issuer</dt><dd className="break-all font-mono">{pki?.issuer ?? '—'}</dd>
              <dt>Router identity</dt><dd className="break-all font-mono">{pki?.managed_trust.identity ?? '—'}</dd>
              <dt>Namespace scope</dt><dd className="break-all font-mono">{pki?.managed_trust.namespace_scope ?? '—'}</dd>
              <dt>Expires</dt><dd>{pki?.expires_at ?? '—'}</dd>
              <dt>CA path length</dt><dd>{pki?.path_length ?? 'not delegated'}{maximumChildDepth >= 0 ? ` · child maximum ${maximumChildDepth}` : ''}</dd>
              <dt>Key boundary</dt><dd>Router CA remains in the host control agent; the policy signer is mounted read-only in the admin API</dd>
            </dl>
          </section>

          <section className="hud-card hud-glass hud-frame relative border border-zinc-200 dark:border-white/10 p-4">
            <HudCorners />
            <h2 className="mb-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">Create child enrollment</h2>
            <form onSubmit={createInvitation} className="grid gap-3 sm:grid-cols-2">
              <label className="text-xs text-zinc-500">Router name<input className={`${inputClass} mt-1`} value={childName} onChange={event => setChildName(event.target.value)} placeholder="Branch router" required /></label>
              <label className="text-xs text-zinc-500">Namespace<input className={`${inputClass} mt-1 font-mono`} value={namespace} onChange={event => setNamespace(event.target.value)} placeholder="region/branch-1" required /></label>
              <label className="text-xs text-zinc-500">Delegation depth<input type="number" min={0} max={Math.max(0, maximumChildDepth)} disabled={!canDelegate} className={`${inputClass} mt-1 disabled:opacity-50`} value={depth} onChange={event => setDepth(Number(event.target.value))} /></label>
              <label className="text-xs text-zinc-500">Invitation lifetime (hours)<input type="number" min={1} max={168} className={`${inputClass} mt-1`} value={hours} onChange={event => setHours(Number(event.target.value))} /></label>
              <button disabled={creating || !canDelegate} className="sm:col-span-2 flex items-center justify-center gap-2 rounded-md bg-accent-fill px-4 py-2 text-sm text-accent-text disabled:opacity-50">
                <Plus size={14} /> {creating ? 'Creating…' : 'Create invitation'}
              </button>
            </form>
          </section>
        </div>

        {newInvitation?.token && (
          <div className="mb-6 rounded-md border border-amber-500/30 bg-amber-500/10 p-4">
            <p className="hud-label text-xs text-amber-700 dark:text-amber-300">Shown once · enrollment token</p>
            <div className="mt-2 flex gap-2"><code className="min-w-0 flex-1 break-all rounded-none bg-black/5 p-2 text-xs dark:bg-black/30">{newInvitation.token}</code><button onClick={copyToken} className="rounded-md border border-amber-500/30 px-3 text-amber-700 dark:text-amber-300" aria-label="Copy token"><Copy size={15} /></button></div>
            <p className="mt-2 text-xs text-zinc-600 dark:text-zinc-400">The child generates all three private keys locally and submits only its router-CA, transport, and policy-signer CSRs with this token.</p>
          </div>
        )}

        <h2 className="mb-3 mt-6 text-sm font-semibold text-zinc-900 dark:text-zinc-100">Local trust material</h2>
        <div className="mb-6 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {certs === null ? Array.from({ length: 2 }).map((_, index) => (
            <div key={index} className="hud-card hud-glass border border-zinc-200 dark:border-white/10 p-4"><Skeleton className="mb-3 h-3 w-24" /><Skeleton className="mb-2 h-4 w-32" /><Skeleton className="h-3 w-20" /></div>
          )) : certs.length === 0 ? (
            <p className="col-span-full text-sm text-zinc-500">No local certificates found.</p>
          ) : certs.map(cert => {
            const state = certState(cert.days_remaining)
            const color = state === 'critical' ? 'text-red-600 dark:text-red-400' : state === 'warn' ? 'text-yellow-600 dark:text-yellow-400' : 'text-green-600 dark:text-green-400'
            return <div key={cert.name} className="hud-card hud-glass border border-zinc-200 dark:border-white/10 p-4"><p className="hud-label text-[10px] text-zinc-500">{cert.name}</p><p className="mt-2 font-mono text-sm">{cert.expires_at}</p><p className={cn('mt-2 text-xs', color)}>{cert.days_remaining} days remaining</p></div>
          })}
        </div>

        <h2 className="mb-3 mt-6 text-sm font-semibold text-zinc-900 dark:text-zinc-100">Enrollment activity</h2>
        <div className="hud-frame relative overflow-hidden rounded-md border border-zinc-200 hud-glass dark:border-white/10">
          <HudCorners />
          {invitations === null ? <p className="p-5 text-sm text-zinc-500">Loading invitations…</p> : invitations.length === 0 ? <p className="p-5 text-sm text-zinc-500">No router invitations created.</p> : invitations.map(invitation => (
            <div key={invitation.id} className="grid gap-3 border-b border-zinc-100 p-4 last:border-0 sm:grid-cols-[minmax(10rem,1fr)_8rem_8rem_9rem] dark:border-white/5">
              <div><p className="text-sm font-medium">{invitation.child_name}</p><p className="font-mono text-xs text-zinc-500">{invitation.namespace}</p></div>
              <span className="flex items-center gap-1"><KeyRound size={12} /><StatusPill text={invitation.status} tone={invitation.status === 'used' ? 'ok' : 'warn'} /></span>
              <span className="text-xs text-zinc-500">depth {invitation.max_delegation_depth}</span>
              <span className="text-xs text-zinc-500">expires {new Date(invitation.expires_at).toLocaleString()}</span>
            </div>
          ))}
        </div>
      </div>
    </Layout>
  )
}
