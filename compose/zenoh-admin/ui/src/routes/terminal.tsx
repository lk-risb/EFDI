import {createFileRoute, redirect} from '@tanstack/react-router'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {StreamsPanel} from '@/components/StreamsPanel'
import {useAuth} from '@/store/auth'

export const Route = createFileRoute('/terminal')({
  beforeLoad: () => {
    if (!useAuth.getState().token) throw redirect({to: '/login'})
  },
  component: TerminalPage,
})

// Video-wall-only for now — the mainline.inc-TERMINAL-style cockpit
// (Fleet/Map/Telemetry/Events/Alerts) was pulled out to EFDI-Random for
// proper reverse-engineering later. This route keeps the "Terminal" nav
// tab and just shows the drone video wall (mediamtx/WHEP tiles).
function TerminalPage() {
  return (
    <Layout>
      <div className="mx-auto max-w-7xl px-6 py-4">
        <PageHeader title="Terminal" eyebrow="VIDEO WALL" />
        <div className="mt-4">
          <StreamsPanel />
        </div>
      </div>
    </Layout>
  )
}
