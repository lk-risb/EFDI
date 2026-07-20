import { createFileRoute, redirect } from '@tanstack/react-router'
import { Layout } from '@/components/Layout'
import { PageHeader } from '@/components/PageHeader'
import { HudCorners } from '@/components/HudCorners'
import { useAuth } from '@/store/auth'
import { useTheme } from '@/store/theme'
import { useUiSettings } from '@/store/ui'
import { MonitorCog, Palette, Settings2, Sparkles, TimerReset, Type } from 'lucide-react'

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
    <section className="hud-frame relative rounded-md border border-zinc-200 bg-white p-5 dark:border-white/10 dark:bg-[#0c0c0e]">
      <HudCorners />
      <div className="mb-4 flex items-start gap-3 border-b border-zinc-200 pb-4 dark:border-white/10">
        <div className="rounded-md border border-accent-ring/30 bg-accent-ring/10 p-2 text-accent-ring">
          {icon}
        </div>
        <div>
          <h2 className="hud-label text-sm font-semibold text-zinc-800 dark:text-zinc-200">{title}</h2>
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

function UiSettingsPage() {
  const theme = useTheme((s) => s.theme)
  const toggleTheme = useTheme((s) => s.toggleTheme)
  const showCorners = useUiSettings((s) => s.showCorners)
  const rowAnimations = useUiSettings((s) => s.rowAnimations)
  const denseRows = useUiSettings((s) => s.denseRows)
  const refreshIntervalMs = useUiSettings((s) => s.refreshIntervalMs)
  const setShowCorners = useUiSettings((s) => s.setShowCorners)
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

        <div className="grid gap-5 lg:grid-cols-2">
          <Card
            title="Appearance"
            description="UI-only preferences for the admin panel."
            icon={<Palette size={16} />}
          >
            <div className="space-y-4">
              <Toggle label="Corner brackets" help="Show the TAK-style corner framing around panels and rows." checked={showCorners} onChange={setShowCorners} />
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
                  Theme is shared with the rest of the admin UI.
                </div>
              </div>
            </div>
          </Card>
        </div>
      </div>
    </Layout>
  )
}

