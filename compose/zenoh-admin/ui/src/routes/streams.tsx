import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useRef, useState} from 'react'
import {Settings2, X} from 'lucide-react'
import {HudCorners} from '@/components/HudCorners'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {apiJson, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'
import {cn} from '@/lib/utils'

export const Route = createFileRoute('/streams')({
  beforeLoad: () => {
    if (!useAuth.getState().token) throw redirect({to: '/login'})
  },
  component: StreamsPage,
})

interface StreamInfo {
  name: string
  ready: boolean
  tracks: string[]
  readers: number
  bytes_received: number | null
}

interface RecordingSegment {
  file: string
  modified: number
}

interface MediamtxSettings {
  log_level: string
  rtmp_enabled: boolean
  rtsp_enabled: boolean
  rtsp_tcp_only: boolean
  webrtc_enabled: boolean
  webrtc_encryption: boolean
  srt_enabled: boolean
  record_segment_minutes: number
  record_retention_minutes: number
}

const DEFAULT_SETTINGS: MediamtxSettings = {
  log_level: 'info',
  rtmp_enabled: true,
  rtsp_enabled: true,
  rtsp_tcp_only: true,
  webrtc_enabled: true,
  webrtc_encryption: false,
  srt_enabled: false,
  record_segment_minutes: 1,
  record_retention_minutes: 10,
}

// WHEP (WebRTC-HTTP Egress Protocol): POST an SDP offer, get an SDP answer
// back. MediaMTX exposes one WHEP endpoint per path on its own WebRTC port
// (mediamtx.yml's webrtcAddress) — same host as this WebUI (network_mode:
// host), different port — so this dials MediaMTX directly rather than
// through this app's own API.
async function negotiateWhep(video: HTMLVideoElement, whepUrl: string, signal: AbortSignal): Promise<RTCPeerConnection> {
  const pc = new RTCPeerConnection()
  pc.addTransceiver('video', {direction: 'recvonly'})
  pc.addTransceiver('audio', {direction: 'recvonly'})
  pc.ontrack = (event) => {
    if (video.srcObject !== event.streams[0]) video.srcObject = event.streams[0]
  }
  const offer = await pc.createOffer()
  await pc.setLocalDescription(offer)
  const response = await fetch(whepUrl, {
    method: 'POST',
    headers: {'Content-Type': 'application/sdp'},
    body: offer.sdp,
    signal,
  })
  if (!response.ok) {
    pc.close()
    throw new Error(`WHEP negotiation failed (HTTP ${response.status})`)
  }
  const answer = await response.text()
  await pc.setRemoteDescription({type: 'answer', sdp: answer})
  return pc
}

function whepUrlFor(name: string): string {
  return `${window.location.protocol}//${window.location.hostname}:8889/${name}/whep`
}

function StreamTile({stream, onClick}: {stream: StreamInfo; onClick: () => void}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const pcRef = useRef<RTCPeerConnection | null>(null)

  useEffect(() => {
    const video = videoRef.current
    if (!stream.ready || !video) return
    const controller = new AbortController()
    negotiateWhep(video, whepUrlFor(stream.name), controller.signal)
      .then(pc => { pcRef.current = pc })
      .catch(() => { /* tile stays blank; next 5s poll re-triggers this effect if still ready */ })
    return () => {
      controller.abort()
      pcRef.current?.close()
      pcRef.current = null
    }
  }, [stream.name, stream.ready])

  return (
    <div
      onClick={onClick}
      className="hud-card hud-glass hud-frame relative aspect-video cursor-pointer overflow-hidden border border-zinc-200 bg-black dark:border-white/10"
    >
      <HudCorners />
      {stream.ready ? (
        <video ref={videoRef} autoPlay muted playsInline className="h-full w-full object-contain" />
      ) : (
        <div className="flex h-full items-center justify-center text-xs text-zinc-500">no signal</div>
      )}
      <div className="absolute inset-x-0 bottom-0 flex items-center justify-between bg-black/60 px-2 py-1 font-mono text-[11px] text-white">
        <span className="truncate">{stream.name}</span>
        <span>{stream.readers} viewing</span>
      </div>
    </div>
  )
}

function EnlargedStream({name, retentionMinutes, onClose}: {
  name: string; retentionMinutes: number; onClose: () => void
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const pcRef = useRef<RTCPeerConnection | null>(null)
  const [segments, setSegments] = useState<RecordingSegment[]>([])
  const [scrubSeconds, setScrubSeconds] = useState(0)
  const [live, setLive] = useState(true)
  const windowSeconds = retentionMinutes * 60

  useEffect(() => {
    async function loadSegments() {
      try {
        setSegments(await apiJson<RecordingSegment[]>(`/api/streams/${encodeURI(name)}/recordings`))
      } catch {
        // Recording is best-effort context for the scrub bar, not required for live viewing.
      }
    }
    loadSegments()
    const interval = setInterval(loadSegments, 15000)
    return () => clearInterval(interval)
  }, [name])

  useEffect(() => {
    const video = videoRef.current
    if (!video || !live) return
    const controller = new AbortController()
    negotiateWhep(video, whepUrlFor(name), controller.signal)
      .then(pc => { pcRef.current = pc })
      .catch(e => notify.error(errorMessage(e)))
    return () => {
      controller.abort()
      pcRef.current?.close()
      pcRef.current = null
    }
  }, [name, live])

  function handleScrub(seconds: number) {
    setScrubSeconds(seconds)
    if (seconds === 0) {
      setLive(true)
      return
    }
    setLive(false)
    pcRef.current?.close()
    pcRef.current = null
    const video = videoRef.current
    if (!video || segments.length === 0) return
    // Each segment file's mtime is roughly its END time (mediamtx names/
    // closes it once the configured recordSegmentDuration elapses) — the
    // first segment ending at or after the target instant is the one
    // covering it.
    const targetTime = Date.now() / 1000 - seconds
    const target = segments.find(s => s.modified >= targetTime) ?? segments[segments.length - 1]
    const offsetIntoSegment = Math.max(0, target.modified - targetTime)
    video.srcObject = null
    video.src = `/api/streams/${encodeURI(name)}/recordings/${target.file}#t=${offsetIntoSegment.toFixed(1)}`
  }

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black/95 p-6">
      <div className="mb-3 flex items-center justify-between text-white">
        <span className="font-mono text-sm">{name}</span>
        <button onClick={onClose} className="rounded-md p-1 hover:bg-white/10" aria-label="Close">
          <X size={18} />
        </button>
      </div>
      <video
        ref={videoRef}
        autoPlay
        muted={live}
        controls={!live}
        playsInline
        className="min-h-0 flex-1 object-contain"
      />
      <div className="mt-4 flex items-center gap-3 text-white">
        <span className="w-14 font-mono text-xs">{live ? 'LIVE' : `-${Math.round(scrubSeconds / 60)}m`}</span>
        <input
          type="range"
          min={0}
          max={windowSeconds}
          step={5}
          value={scrubSeconds}
          onChange={e => handleScrub(Number(e.target.value))}
          className="flex-1"
          disabled={segments.length === 0 && scrubSeconds === 0}
        />
        <span className="w-14 text-right font-mono text-xs">-{retentionMinutes}m</span>
      </div>
    </div>
  )
}

function Toggle({label, help, checked, disabled, onChange}: {
  label: string; help?: string; checked: boolean; disabled: boolean; onChange: (v: boolean) => void
}) {
  return (
    <div className="flex max-w-sm items-start justify-between gap-4">
      <div>
        <p className="text-sm text-zinc-700 dark:text-zinc-300">{label}</p>
        {help && <p className="mt-0.5 text-xs text-zinc-500">{help}</p>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`relative h-6 w-10 shrink-0 rounded-full transition-colors disabled:opacity-50 ${checked ? 'bg-accent-fill' : 'bg-zinc-300 dark:bg-zinc-700'}`}
      >
        <span className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${checked ? 'translate-x-4' : ''}`} />
      </button>
    </div>
  )
}

const inputClass = 'w-24 rounded-md border border-zinc-300 bg-zinc-100 px-2 py-1 text-sm text-zinc-900 focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-50 dark:border-white/10 dark:bg-[#141416] dark:text-white'

function MediamtxSettingsPanel({settings, canWrite, onSaved}: {
  settings: MediamtxSettings; canWrite: boolean; onSaved: (s: MediamtxSettings) => void
}) {
  const [draft, setDraft] = useState(settings)
  const [saving, setSaving] = useState(false)

  useEffect(() => setDraft(settings), [settings])

  const dirty = JSON.stringify(draft) !== JSON.stringify(settings)

  async function save() {
    setSaving(true)
    try {
      const result = await apiJson<MediamtxSettings & {restarted: boolean}>('/api/streams/config', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(draft),
      })
      onSaved(result)
      notify.success(result.restarted
        ? 'mediamtx settings saved and restarted'
        : 'mediamtx settings saved — restart it from Runtime Control to apply')
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="hud-frame hud-glass relative mb-6 rounded-md border border-zinc-200 p-5 dark:border-white/10">
      <HudCorners />
      <div className="mb-5 flex items-start justify-between gap-3 border-b border-zinc-200 pb-4 dark:border-white/10">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 rounded-md border border-accent-ring/30 bg-accent-ring/10 p-2 text-accent-ring">
            <Settings2 size={16} />
          </div>
          <div>
            <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">mediamtx settings</h2>
            <p className="mt-1 text-xs text-zinc-500">
              {canWrite
                ? 'Saving writes compose/bridges/mediamtx/mediamtx.yml and restarts mediamtx to apply it.'
                : 'Your role can view mediamtx settings but only a superadmin can change them.'}
            </p>
          </div>
        </div>
        {canWrite && (
          <button
            onClick={save}
            disabled={!dirty || saving}
            className="rounded-md bg-accent-fill px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Save & restart'}
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 gap-x-8 gap-y-4 md:grid-cols-2">
        <Toggle
          label="RTMP ingest"
          help="Drone/FreeFlight push, port 1935"
          checked={draft.rtmp_enabled}
          disabled={!canWrite}
          onChange={v => setDraft({...draft, rtmp_enabled: v})}
        />
        <Toggle
          label="RTSP egress"
          help="TAK / SitaWare pull, port 8554"
          checked={draft.rtsp_enabled}
          disabled={!canWrite}
          onChange={v => setDraft({...draft, rtsp_enabled: v})}
        />
        <Toggle
          label="RTSP: TCP only"
          help="Keep on — RTSP-over-UDP fails through most NAT/relay paths (see Troubleshooting)"
          checked={draft.rtsp_tcp_only}
          disabled={!canWrite}
          onChange={v => setDraft({...draft, rtsp_tcp_only: v})}
        />
        <Toggle
          label="WebRTC (live tiles)"
          help="This page's own grid + enlarged view, port 8889"
          checked={draft.webrtc_enabled}
          disabled={!canWrite}
          onChange={v => setDraft({...draft, webrtc_enabled: v})}
        />
        <Toggle
          label="WebRTC encryption"
          help="HTTPS handshake — leave off unless this host is reachable outside the NetBird overlay"
          checked={draft.webrtc_encryption}
          disabled={!canWrite}
          onChange={v => setDraft({...draft, webrtc_encryption: v})}
        />
        <Toggle
          label="SRT ingest"
          help="Alternate to RTMP with built-in packet-loss recovery, port 8890 — enable if a drone/GCS offers it"
          checked={draft.srt_enabled}
          disabled={!canWrite}
          onChange={v => setDraft({...draft, srt_enabled: v})}
        />
      </div>

      <div className="mt-6 grid grid-cols-1 gap-x-8 gap-y-4 border-t border-zinc-200 pt-4 dark:border-white/10 md:grid-cols-3">
        <label className="block">
          <span className="text-sm text-zinc-700 dark:text-zinc-300">Log level</span>
          <select
            value={draft.log_level}
            disabled={!canWrite}
            onChange={e => setDraft({...draft, log_level: e.target.value})}
            className={cn(inputClass, 'mt-1 block w-full')}
          >
            {['error', 'warn', 'info', 'debug'].map(level => (
              <option key={level} value={level}>{level}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-sm text-zinc-700 dark:text-zinc-300">Recording segment (minutes)</span>
          <input
            type="number" min={1} max={60}
            value={draft.record_segment_minutes}
            disabled={!canWrite}
            onChange={e => setDraft({...draft, record_segment_minutes: Number(e.target.value)})}
            className={cn(inputClass, 'mt-1 block w-full')}
          />
        </label>
        <label className="block">
          <span className="text-sm text-zinc-700 dark:text-zinc-300">Recording retention (minutes)</span>
          <input
            type="number" min={1} max={1440}
            value={draft.record_retention_minutes}
            disabled={!canWrite}
            onChange={e => setDraft({...draft, record_retention_minutes: Number(e.target.value)})}
            className={cn(inputClass, 'mt-1 block w-full')}
          />
          <p className="mt-1 text-xs text-zinc-500">Also sets the enlarged tile's scrub-back range.</p>
        </label>
      </div>
    </section>
  )
}

function StreamsPage() {
  const {role} = useAuth()
  const [streams, setStreams] = useState<StreamInfo[]>([])
  const [enlarged, setEnlarged] = useState<string | null>(null)
  const [settings, setSettings] = useState<MediamtxSettings>(DEFAULT_SETTINGS)

  useEffect(() => {
    async function load() {
      try {
        setStreams(await apiJson<StreamInfo[]>('/api/streams'))
      } catch (e) {
        notify.error(errorMessage(e))
      }
    }
    load()
    // Matches this codebase's existing dashboard poll cadence (network.tsx,
    // index.tsx's /api/health) — MediaMTX's own path list is cheap to poll.
    const interval = setInterval(load, 5000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    apiJson<MediamtxSettings>('/api/streams/config')
      .then(setSettings)
      .catch(e => notify.error(errorMessage(e)))
  }, [])

  return (
    <Layout>
      <PageHeader title="Streams" eyebrow="VIDEO" count={streams.length} countLabel="active" />
      <MediamtxSettingsPanel
        settings={settings}
        canWrite={role === 'superadmin'}
        onSaved={setSettings}
      />
      {streams.length === 0 ? (
        <p className="text-sm text-zinc-500">
          No drone feeds connected. Point FreeFlight's RTMP URL at this router's mediamtx service to see it here.
        </p>
      ) : (
        <div className={cn('grid grid-cols-2 gap-4', 'lg:grid-cols-3 xl:grid-cols-4')}>
          {streams.map(s => (
            <StreamTile key={s.name} stream={s} onClick={() => setEnlarged(s.name)} />
          ))}
        </div>
      )}
      {enlarged && (
        <EnlargedStream
          name={enlarged}
          retentionMinutes={settings.record_retention_minutes}
          onClose={() => setEnlarged(null)}
        />
      )}
    </Layout>
  )
}
