import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useState} from 'react'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {HudCorners} from '@/components/HudCorners'
import {PasswordInput} from '@/components/PasswordInput'
import {apiJson, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'
import {KeyRound, Pencil, Plus, RefreshCw, Satellite, Trash2, X} from 'lucide-react'

export const Route = createFileRoute('/sitaware-targets')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'admin' && role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: SitawareTargetsPage,
})

type Target = {
  id: string
  name: string
  url: string
  url_fallback: string | null
  url_tailscale: string | null
  enabled: boolean
  created_by: string
  created_at: string
  has_credentials: boolean
  running: boolean | null
}

type TargetForm = {
  name: string
  url: string
  url_fallback: string
  url_tailscale: string
  username: string
  password: string
  enabled: boolean
}

const EMPTY_FORM: TargetForm = {
  name: '', url: '', url_fallback: '', url_tailscale: '', username: '', password: '', enabled: true,
}

function Card({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <section className={`hud-frame hud-glass relative rounded-md border border-zinc-200 p-5 dark:border-white/10 ${className}`}><HudCorners />{children}</section>
}

function TargetFormFields({ form, setForm, isEdit }: {
  form: TargetForm
  setForm: (updater: (f: TargetForm) => TargetForm) => void
  isEdit: boolean
}) {
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <label className="text-xs text-zinc-500">
        Name
        <input
          value={form.name}
          disabled={isEdit}
          onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
          placeholder="e.g. hq-north"
          className="mt-1 w-full rounded-md border border-zinc-300 bg-zinc-100 px-3 py-2 text-sm disabled:opacity-50 dark:border-white/10 dark:bg-[#141416]"
        />
        {!isEdit && <span className="mt-1 block text-[10px] text-zinc-500">Lowercase, digits, hyphens — becomes the Zenoh topic segment and process name.</span>}
      </label>
      <label className="text-xs text-zinc-500">
        Primary URL
        <input
          value={form.url}
          onChange={e => setForm(f => ({ ...f, url: e.target.value }))}
          placeholder="https://10.0.0.1"
          className="mt-1 w-full rounded-md border border-zinc-300 bg-zinc-100 px-3 py-2 text-sm dark:border-white/10 dark:bg-[#141416]"
        />
      </label>
      <label className="text-xs text-zinc-500">
        Fallback URL <span className="text-zinc-400">(optional)</span>
        <input
          value={form.url_fallback}
          onChange={e => setForm(f => ({ ...f, url_fallback: e.target.value }))}
          placeholder="https://100.x.x.x"
          className="mt-1 w-full rounded-md border border-zinc-300 bg-zinc-100 px-3 py-2 text-sm dark:border-white/10 dark:bg-[#141416]"
        />
      </label>
      <label className="text-xs text-zinc-500">
        Tailscale/mesh URL <span className="text-zinc-400">(optional)</span>
        <input
          value={form.url_tailscale}
          onChange={e => setForm(f => ({ ...f, url_tailscale: e.target.value }))}
          placeholder="https://100.x.x.x"
          className="mt-1 w-full rounded-md border border-zinc-300 bg-zinc-100 px-3 py-2 text-sm dark:border-white/10 dark:bg-[#141416]"
        />
      </label>
      <label className="text-xs text-zinc-500">
        Username {isEdit && <span className="text-zinc-400">(leave blank to keep current)</span>}
        <input
          value={form.username}
          onChange={e => setForm(f => ({ ...f, username: e.target.value }))}
          className="mt-1 w-full rounded-md border border-zinc-300 bg-zinc-100 px-3 py-2 text-sm dark:border-white/10 dark:bg-[#141416]"
        />
      </label>
      <label className="text-xs text-zinc-500">
        Password {isEdit && <span className="text-zinc-400">(leave blank to keep current)</span>}
        <PasswordInput
          value={form.password}
          onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
          className="mt-1 w-full rounded-md border border-zinc-300 bg-zinc-100 px-3 py-2 text-sm dark:border-white/10 dark:bg-[#141416]"
        />
      </label>
    </div>
  )
}

function SitawareTargetsPage() {
  const { role } = useAuth()
  const canWrite = role === 'superadmin'
  const [targets, setTargets] = useState<Target[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [addForm, setAddForm] = useState<TargetForm>(EMPTY_FORM)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editForm, setEditForm] = useState<TargetForm>(EMPTY_FORM)

  async function load() {
    setLoading(true)
    try {
      const data = await apiJson<Target[]>('/api/sitaware-targets')
      setTargets(data)
    } catch (e) {
      notify.error(errorMessage(e))
    } finally { setLoading(false) }
  }

  useEffect(() => {
    load()
    const timer = window.setInterval(() => { void load() }, 5000)
    return () => window.clearInterval(timer)
  }, [])

  async function createTarget() {
    if (!canWrite) return
    setBusy('create')
    try {
      await apiJson('/api/sitaware-targets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(addForm),
      })
      notify.success(`SitaWare target "${addForm.name}" created`)
      setAddForm(EMPTY_FORM)
      setAdding(false)
      await load()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally { setBusy(null) }
  }

  function startEdit(target: Target) {
    setEditingId(target.id)
    setEditForm({
      name: target.name, url: target.url,
      url_fallback: target.url_fallback ?? '', url_tailscale: target.url_tailscale ?? '',
      username: '', password: '', enabled: target.enabled,
    })
  }

  async function saveEdit(id: string) {
    if (!canWrite) return
    setBusy(`edit:${id}`)
    try {
      const body: Record<string, unknown> = {
        url: editForm.url, url_fallback: editForm.url_fallback || null,
        url_tailscale: editForm.url_tailscale || null, enabled: editForm.enabled,
      }
      if (editForm.username && editForm.password) {
        body.username = editForm.username
        body.password = editForm.password
      }
      await apiJson(`/api/sitaware-targets/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      notify.success('SitaWare target updated')
      setEditingId(null)
      await load()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally { setBusy(null) }
  }

  async function toggleEnabled(target: Target) {
    if (!canWrite) return
    setBusy(`toggle:${target.id}`)
    try {
      await apiJson(`/api/sitaware-targets/${target.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !target.enabled }),
      })
      notify.success(`${target.name} ${target.enabled ? 'disabled' : 'enabled'}`)
      await load()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally { setBusy(null) }
  }

  async function removeTarget(target: Target) {
    if (!canWrite) return
    if (!window.confirm(`Delete SitaWare target "${target.name}"? Its process will be stopped and credentials removed.`)) return
    setBusy(`delete:${target.id}`)
    try {
      await apiJson(`/api/sitaware-targets/${target.id}`, { method: 'DELETE' })
      notify.success(`${target.name} deleted`)
      await load()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally { setBusy(null) }
  }

  return (
    <Layout>
      <div className="mx-auto max-w-6xl p-6">
        <PageHeader
          title="SitaWare Targets"
          count={targets.length}
          countLabel="configured"
          actions={<button onClick={load} disabled={loading} className="flex items-center gap-2 rounded-md px-3 py-2 text-sm text-zinc-600 hover:bg-zinc-200/50 disabled:opacity-50 dark:text-zinc-400 dark:hover:bg-white/[0.05]"><RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Reload</button>}
        />
        {!canWrite && <div className="mb-5 rounded-md border border-yellow-500/30 bg-yellow-500/10 px-4 py-3 text-xs text-yellow-700 dark:text-yellow-300">Read-only view. A superadmin is required to add, edit, or remove targets.</div>}

        <p className="mb-5 text-sm text-zinc-500">
          Each enabled target runs its own independent SitaWare HQ ingress process, so multiple HQ
          endpoints can feed the fabric at once. Adding, editing, enabling, or deleting a target here
          takes effect live — no .env editing or restart required.
        </p>

        {canWrite && (
          <Card className="mb-5">
            {!adding ? (
              <button onClick={() => setAdding(true)} className="flex items-center gap-2 rounded-md bg-accent-fill px-3 py-2 text-xs text-accent-text hover:bg-accent-fill-hover">
                <Plus size={13} /> Add SitaWare target
              </button>
            ) : (
              <>
                <div className="mb-4 flex items-center justify-between border-b border-zinc-200 pb-3 dark:border-white/10">
                  <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">New target</h2>
                  <button onClick={() => { setAdding(false); setAddForm(EMPTY_FORM) }} className="rounded-none p-1 text-zinc-500 hover:bg-zinc-200 dark:hover:bg-white/10"><X size={14} /></button>
                </div>
                <TargetFormFields form={addForm} setForm={setAddForm} isEdit={false} />
                <div className="mt-4 flex justify-end gap-2">
                  <button onClick={() => { setAdding(false); setAddForm(EMPTY_FORM) }} className="rounded-md border border-zinc-300 px-3 py-2 text-xs dark:border-white/10">Cancel</button>
                  <button
                    onClick={createTarget}
                    disabled={busy === 'create' || !addForm.name || !addForm.url}
                    className="flex items-center gap-2 rounded-md bg-accent-fill px-3 py-2 text-xs text-accent-text hover:bg-accent-fill-hover disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <Plus size={13} /> Create
                  </button>
                </div>
              </>
            )}
          </Card>
        )}

        <div className="grid gap-3">
          {targets.length === 0 ? (
            <Card><p className="text-sm text-zinc-500">No SitaWare targets configured yet.</p></Card>
          ) : targets.map(target => (
            <Card key={target.id}>
              {editingId === target.id ? (
                <>
                  <div className="mb-4 flex items-center justify-between border-b border-zinc-200 pb-3 dark:border-white/10">
                    <h2 className="font-mono text-sm font-semibold text-zinc-900 dark:text-zinc-100">{target.name}</h2>
                    <button onClick={() => setEditingId(null)} className="rounded-none p-1 text-zinc-500 hover:bg-zinc-200 dark:hover:bg-white/10"><X size={14} /></button>
                  </div>
                  <TargetFormFields form={editForm} setForm={setEditForm} isEdit={true} />
                  <div className="mt-4 flex items-center justify-between">
                    <label className="flex items-center gap-2 text-xs text-zinc-500">
                      <input type="checkbox" checked={editForm.enabled} onChange={e => setEditForm(f => ({ ...f, enabled: e.target.checked }))} className="h-4 w-4 rounded-none border-zinc-400 bg-transparent accent-accent-fill" />
                      Enabled
                    </label>
                    <div className="flex gap-2">
                      <button onClick={() => setEditingId(null)} className="rounded-md border border-zinc-300 px-3 py-2 text-xs dark:border-white/10">Cancel</button>
                      <button onClick={() => saveEdit(target.id)} disabled={busy === `edit:${target.id}`} className="rounded-md bg-accent-fill px-3 py-2 text-xs text-accent-text hover:bg-accent-fill-hover disabled:opacity-50">Save</button>
                    </div>
                  </div>
                </>
              ) : (
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-3">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${target.running ? 'bg-emerald-400 hud-live-dot' : target.enabled ? 'bg-amber-400' : 'bg-zinc-400 dark:bg-zinc-700'}`} />
                    <div>
                      <div className="flex items-center gap-1.5">
                        <Satellite size={14} className="text-accent-ring" />
                        <p className="font-mono text-sm font-semibold text-zinc-900 dark:text-zinc-100">{target.name}</p>
                        {target.has_credentials && <KeyRound size={11} className="text-zinc-400" titleAccess="Credentials stored" />}
                      </div>
                      <p className="mt-0.5 truncate text-xs text-zinc-500 dark:text-zinc-400">{target.url}</p>
                      <p className="mt-0.5 text-[10px] uppercase tracking-[0.18em] text-zinc-500">
                        {!target.enabled ? 'disabled' : target.running == null ? 'status unknown' : target.running ? 'running' : 'not running'}
                      </p>
                    </div>
                  </div>
                  {canWrite && (
                    <div className="flex gap-1">
                      <button title={target.enabled ? 'Disable' : 'Enable'} disabled={busy === `toggle:${target.id}`} onClick={() => toggleEnabled(target)} className="rounded-none px-2 py-1.5 text-[10px] uppercase tracking-wide text-zinc-500 hover:bg-zinc-200 disabled:opacity-30 dark:hover:bg-white/10">
                        {target.enabled ? 'Disable' : 'Enable'}
                      </button>
                      <button title="Edit" onClick={() => startEdit(target)} className="rounded-none p-1.5 text-zinc-500 hover:bg-accent-ring/10 hover:text-accent-ring"><Pencil size={13} /></button>
                      <button title="Delete" disabled={busy === `delete:${target.id}`} onClick={() => removeTarget(target)} className="rounded-none p-1.5 text-zinc-500 hover:bg-red-500/10 hover:text-red-500 disabled:opacity-30"><Trash2 size={13} /></button>
                    </div>
                  )}
                </div>
              )}
            </Card>
          ))}
        </div>
      </div>
    </Layout>
  )
}
