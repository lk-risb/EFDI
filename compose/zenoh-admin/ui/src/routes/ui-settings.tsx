import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useRef, useState} from 'react'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {useAuth} from '@/store/auth'
import {useTheme} from '@/store/theme'
import {useUiSettings} from '@/store/ui'
import {useBranding} from '@/store/branding'
import {notify} from '@/lib/notify'
import {errorMessage} from '@/lib/api'
import {Building2, ImagePlus, MonitorCog, Palette, Settings2, Sparkles, TimerReset, Type, Upload} from 'lucide-react'

export const Route = createFileRoute('/ui-settings')({
  beforeLoad: () => {
    const { token, role } = useAuth.getState()
    if (!token) throw redirect({ to: '/login' })
    if (role !== 'admin' && role !== 'superadmin') throw redirect({ to: '/' })
  },
  component: UiSettingsPage,
})

function Card({ title, description, icon, children }: { title: string; description: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="hud-frame hud-glass relative rounded-md border border-zinc-200 p-5 dark:border-white/10">
      <div className="mb-4 flex items-start gap-3 border-b border-zinc-200 pb-4 dark:border-white/10">
        <div className="rounded-md border border-accent-ring/30 bg-accent-ring/10 p-2 text-accent-ring">
          {icon}
        </div>
        <div>
          <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">{title}</h2>
          <p className="mt-1 text-xs text-zinc-500">{description}</p>
        </div>
      </div>
      {children}
    </section>
  )
}

function Toggle({ label, help, checked, onChange }: { label: string; help?: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <p className="text-sm text-zinc-700 dark:text-zinc-300">{label}</p>
        {help && <p className="mt-0.5 text-xs text-zinc-500">{help}</p>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative h-6 w-10 shrink-0 rounded-full transition-colors ${checked ? 'bg-accent-fill' : 'bg-zinc-300 dark:bg-zinc-700'}`}
      >
        <span className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${checked ? 'translate-x-4' : ''}`} />
      </button>
    </div>
  )
}

function ColorField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="flex items-center justify-between gap-3">
      <span className="text-sm text-zinc-700 dark:text-zinc-300">{label}</span>
      <span className="flex items-center gap-2">
        <span className="font-mono text-xs text-zinc-500">{value}</span>
        <input type="color" value={value} onChange={e => onChange(e.target.value)}
          className="h-8 w-10 cursor-pointer rounded-md border border-zinc-300 bg-transparent dark:border-white/10" />
      </span>
    </label>
  )
}

function BrandingCard() {
  const role = useAuth((s) => s.role)
  const isSuper = role === 'superadmin'
  const { orgName, accentFill, accentText, logoUrl, updateBranding, uploadLogo } = useBranding()
  const [name, setName] = useState(orgName)
  const [fill, setFill] = useState(accentFill)
  const [text, setText] = useState(accentText)
  const [saving, setSaving] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  // Keep the form in sync when branding loads/changes from the server.
  useEffect(() => { setName(orgName); setFill(accentFill); setText(accentText) }, [orgName, accentFill, accentText])

  async function save() {
    setSaving(true)
    try {
      // One picker drives fill, ring and hover so the operator sets a single
      // brand colour; accent-text is a separate control for button legibility.
      await updateBranding({ org_name: name, accent_fill: fill, accent_fill_hover: fill, accent_ring: fill, accent_text: text })
      notify.success('Branding saved')
    } catch (e) { notify.error(errorMessage(e)) } finally { setSaving(false) }
  }

  async function onLogo(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    try { await uploadLogo(file); notify.success('Logo updated') }
    catch (err) { notify.error(errorMessage(err)) }
    finally { if (fileRef.current) fileRef.current.value = '' }
  }

  return (
    <Card title="Branding" description="Organisation name, accent colour, and logo shown across the panel." icon={<Building2 size={16} />}>
      <div className="space-y-4">
        <label className="space-y-1 block">
          <span className="text-sm text-zinc-700 dark:text-zinc-300">Organisation name</span>
          <input value={name} onChange={e => setName(e.target.value)} disabled={!isSuper} maxLength={64}
            className="w-full rounded-md border border-zinc-300 bg-zinc-100 px-3 py-2 text-sm disabled:opacity-50 dark:border-white/10 dark:bg-[#141416]" />
        </label>

        <ColorField label="Accent colour" value={fill} onChange={setFill} />
        <ColorField label="Accent text colour" value={text} onChange={setText} />

        <div className="flex items-center justify-between gap-3 rounded-md border border-zinc-200 bg-zinc-50 p-3 dark:border-white/10 dark:bg-[#141416]">
          <div className="flex items-center gap-3">
            {logoUrl
              ? <img src={logoUrl} alt="logo" className="h-9 w-9 rounded object-contain" />
              : <div className="flex h-9 w-9 items-center justify-center rounded bg-zinc-200 text-zinc-500 dark:bg-white/5"><ImagePlus size={16} /></div>}
            <div>
              <p className="text-sm text-zinc-700 dark:text-zinc-300">Logo</p>
              <p className="text-xs text-zinc-500">PNG or JPG, up to 2&nbsp;MB.</p>
            </div>
          </div>
          <input ref={fileRef} type="file" accept="image/png,image/jpeg" onChange={onLogo} className="hidden" />
          <button type="button" disabled={!isSuper} onClick={() => fileRef.current?.click()}
            className="flex items-center gap-2 rounded-md border border-zinc-300 px-3 py-2 text-xs disabled:opacity-50 dark:border-white/10">
            <Upload size={13} /> Upload
          </button>
        </div>

        <div className="flex items-center justify-between pt-1">
          <p className="text-xs text-zinc-500">{isSuper ? 'Applies to every user of this panel.' : 'A superadmin is required to change branding.'}</p>
          <button disabled={!isSuper || saving} onClick={save}
            className="rounded-md bg-accent-fill px-4 py-2 text-sm text-accent-text disabled:opacity-50">
            {saving ? 'Saving…' : 'Save branding'}
          </button>
        </div>
      </div>
    </Card>
  )
}

function UiSettingsPage() {
  const theme = useTheme((s) => s.theme)
  const toggleTheme = useTheme((s) => s.toggleTheme)
  const rowAnimations = useUiSettings((s) => s.rowAnimations)
  const denseRows = useUiSettings((s) => s.denseRows)
  const refreshIntervalMs = useUiSettings((s) => s.refreshIntervalMs)
  const setRowAnimations = useUiSettings((s) => s.setRowAnimations)
  const setDenseRows = useUiSettings((s) => s.setDenseRows)
  const setRefreshIntervalMs = useUiSettings((s) => s.setRefreshIntervalMs)

  return (
    <Layout>
      <div className="p-6 max-w-5xl">
        <PageHeader
          title="WebUI Settings"
          actions={(
            <button onClick={toggleTheme} className="rounded-md border border-zinc-300 px-3 py-2 text-sm dark:border-white/10">
              {theme === 'dark' ? 'Light theme' : 'Dark theme'}
            </button>
          )}
        />

        <div className="grid items-start gap-5 lg:grid-cols-2">
          <BrandingCard />

          <Card
            title="Appearance"
            description="UI-only preferences for the admin panel."
            icon={<Palette size={16} />}
          >
            <div className="space-y-4">
              <Toggle label="Row animations" help="Fade service rows in when data refreshes." checked={rowAnimations} onChange={setRowAnimations} />
              <Toggle label="Dense rows" help="Use tighter spacing in the runtime service list." checked={denseRows} onChange={setDenseRows} />
            </div>
          </Card>

          <Card
            title="Live behavior"
            description="How often the UI polls runtime state and how much motion it uses."
            icon={<TimerReset size={16} />}
          >
            <div className="space-y-4">
              <label className="space-y-1">
                <span className="flex items-center gap-2 text-sm text-zinc-700 dark:text-zinc-300">
                  <Settings2 size={14} className="text-accent-ring" />
                  Refresh interval
                </span>
                <select
                  value={refreshIntervalMs}
                  onChange={e => setRefreshIntervalMs(Number(e.target.value))}
                  className="w-full rounded-md border border-zinc-300 bg-zinc-100 px-3 py-2 text-sm dark:border-white/10 dark:bg-[#141416]"
                >
                  <option value={2000}>2 seconds</option>
                  <option value={5000}>5 seconds</option>
                  <option value={10000}>10 seconds</option>
                  <option value={30000}>30 seconds</option>
                </select>
              </label>

              <div className="rounded-md border border-zinc-200 bg-zinc-50 p-3 text-xs text-zinc-600 dark:border-white/10 dark:bg-[#141416] dark:text-zinc-400">
                <div className="flex items-center gap-2">
                  <MonitorCog size={14} className="text-accent-ring" />
                  Browser settings are saved locally on this device.
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <Sparkles size={14} className="text-accent-ring" />
                  Service selection and deployment config remain file-backed in the runtime page.
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <Type size={14} className="text-accent-ring" />
                  Branding and theme are shared with the rest of the admin UI.
                </div>
              </div>
            </div>
          </Card>
        </div>
      </div>
    </Layout>
  )
}
