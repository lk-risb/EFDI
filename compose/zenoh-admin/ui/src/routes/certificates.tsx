import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useState} from 'react'
import {CheckCircle2, Copy, KeyRound, Lock, Network, Plus, ShieldAlert, ShieldCheck, UploadCloud} from 'lucide-react'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {HudCorners} from '@/components/HudCorners'
import {StatusPill} from '@/components/StatusPill'
import {Skeleton} from '@/components/Skeleton'
import {apiFetch, apiJson, errorMessage} from '@/lib/api'
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

  async function load() {
    try {
      const [health, status, invites, boot] = await Promise.all([
        apiJson<{ system: { certs: CertInfo[] } }>('/api/health'),
        apiJson<PkiStatus>('/api/pki/status'),
        apiJson<Invitation[]>('/api/pki/invitations'),
        apiJson<BootstrapStatus>('/api/certs/bootstrap/status'),
      ])
      setCerts(health.system.certs)
      setPki(status)
      setInvitations(invites)
      setBootstrapStatus(boot)
    } catch (error) {
      notify.error(errorMessage(error))
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
      if (!response.ok) throw new Error(body.detail ?? response.statusText)
      notify.success('Certificates applied — router switched to mTLS. This page may blip for a few seconds while the admin service restarts.')
      setCaFile(null)
      setCertFile(null)
      setKeyFile(null)
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
      if (!response.ok) throw new Error(body.detail ?? response.statusText)
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
      <div className="mx-auto max-w-7xl p-6">
        <PageHeader title="Certificate Authority" count={certs?.length} countLabel="local identities" />

        {bootstrapStatus?.bootstrap === true && (
          <section className="hud-card hud-glass hud-frame relative mb-6 border border-amber-500/30 p-4">
            <HudCorners />
            <div className="mb-3 flex items-center gap-2">
              <ShieldAlert size={16} className="text-amber-600 dark:text-amber-400" />
              <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Router not yet secured — plaintext bootstrap mode</h2>
            </div>
            <p className="mb-4 text-xs text-zinc-500">
              This pod is running on a throwaway self-signed identity with no mTLS. Upload your
              real CA root, certificate, and private key from <code>scripts/gen-certs.sh</code>{' '}
              below to switch the router to mTLS. The admin service restarts briefly afterward.
            </p>
            <form onSubmit={uploadBootstrapIdentity} className="grid gap-3 sm:grid-cols-2">
              <label className="text-xs text-zinc-500">
                CA root (efdi-ca-root.pem)
                <input type="file" required accept=".pem,.crt,.cer"
                  onChange={event => setCaFile(event.target.files?.[0] ?? null)}
                  className="mt-1 block w-full text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 dark:file:bg-white/10 dark:file:text-white" />
              </label>
              <label className="text-xs text-zinc-500">
                Certificate (&lt;namespace&gt;-cert.pem)
                <input type="file" required accept=".pem,.crt,.cer"
                  onChange={event => setCertFile(event.target.files?.[0] ?? null)}
                  className="mt-1 block w-full text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 dark:file:bg-white/10 dark:file:text-white" />
              </label>
              <label className="text-xs text-zinc-500">
                Private key (&lt;namespace&gt;-key.pem)
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
                <UploadCloud size={14} /> {uploading ? 'Applying…' : 'Upload and switch to mTLS'}
              </button>
            </form>
          </section>
        )}
        {bootstrapStatus?.bootstrap === false && (
          <div className="mb-5 flex flex-wrap gap-2 text-xs">
            <span className="flex items-center gap-1"><Lock size={13} /> <StatusPill text="router secured (mTLS)" tone="ok" /></span>
          </div>
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
