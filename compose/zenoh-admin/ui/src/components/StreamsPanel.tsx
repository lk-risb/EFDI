import {useEffect, useRef, useState} from 'react'
import {Settings2, X} from 'lucide-react'
import {HudCorners} from '@/components/HudCorners'
import {apiJson, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'
import {cn} from '@/lib/utils'

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

// Proxied through THIS already-HTTPS-trusted origin (Caddyfile's
// /mediamtx-whep/* handle_path -> 127.0.0.1:8889) rather than dialed
// directly against MediaMTX's own port. MediaMTX's WebRTC listener is
// plain HTTP by default (mediamtx.yml's webrtcEncryption) — a direct fetch
// from this HTTPS page against a plain-HTTP port gets blocked outright by
// the browser's own mixed-content policy (a bare "NetworkError when
// attempting to fetch resource", no further detail), independent of
// whether that port could actually serve the request. Proxying is a
// server-to-server hop from Caddy's point of view, so mixed-content rules
// don't apply, and it works regardless of MediaMTX's own encryption
// setting — no second self-signed cert for operators to trust, and no
// breakage every time that setting's checked-in default gets pulled from
// git. WebRTC's actual media (ICE/UDP on webrtcLocalUDPAddress :8189)
// still flows directly between the browser and MediaMTX either way — only
// the HTTP signaling request is proxied.
function whepUrlFor(name: string): string {
  return `${window.location.protocol}//${window.location.host}/mediamtx-whep/${name}/whep`
}

// One WHEP session per stream name, shared by whichever <video> element(s)
// currently want to show it (the grid tile, the enlarged view, or both at
// once while the enlarge transition is in flight). Previously each of
// StreamTile/EnlargedStream owned its own RTCPeerConnection and the tile
// closed its connection the instant you opened the enlarged view — that
// fixed mediamtx double-counting readers, but traded it for a visible
// restart (new WHEP negotiation, new ICE handshake, wait for the next
// keyframe) every time you maximized a tile. A MediaStream can be assigned
// as srcObject on more than one <video> element simultaneously with no
// extra network/WHEP cost — mediamtx still only sees one reader — so
// sharing the connection here fixes both problems: no doubled reader count,
// and maximizing just re-points an existing live MediaStream at a bigger
// <video> instead of reconnecting.
interface SharedConnection {
  pc: RTCPeerConnection
  stream: MediaStream | null
  listeners: Set<(stream: MediaStream) => void>
}
const sharedConnections = new Map<string, SharedConnection>()

function subscribeStream(name: string, onStream: (stream: MediaStream) => void): () => void {
  let entry = sharedConnections.get(name)
  if (!entry) {
    const pc = new RTCPeerConnection()
    entry = {pc, stream: null, listeners: new Set()}
    sharedConnections.set(name, entry)
    pc.addTransceiver('video', {direction: 'recvonly'})
    pc.addTransceiver('audio', {direction: 'recvonly'})
    pc.ontrack = (event) => {
      const current = sharedConnections.get(name)
      if (!current) return
      current.stream = event.streams[0]
      current.listeners.forEach(fn => fn(event.streams[0]))
    }
    ;(async () => {
      try {
        const offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        const response = await fetch(whepUrlFor(name), {
          method: 'POST',
          headers: {'Content-Type': 'application/sdp'},
          body: offer.sdp,
        })
        if (!response.ok) throw new Error(`WHEP negotiation failed (HTTP ${response.status})`)
        const answer = await response.text()
        await pc.setRemoteDescription({type: 'answer', sdp: answer})
      } catch {
        closeStream(name)  // negotiation failed — drop it so the next subscriber retries fresh
      }
    })()
  }
  entry.listeners.add(onStream)
  if (entry.stream) onStream(entry.stream)
  return () => {
    const current = sharedConnections.get(name)
    if (!current) return
    current.listeners.delete(onStream)
    // Nothing left watching this stream (tile polled it away, enlarged view
    // closed) — tear down the real connection instead of leaking it.
    if (current.listeners.size === 0) closeStream(name)
  }
}

function closeStream(name: string) {
  const entry = sharedConnections.get(name)
  if (!entry) return
  entry.pc.close()
  sharedConnections.delete(name)
}

function StreamTile({stream, onClick}: {
  stream: StreamInfo; onClick: () => void
}) {
  const videoRef = useRef<HTMLVideoElement>(null)

  useEffect(() => {
    const video = videoRef.current
    if (!stream.ready || !video) return
    return subscribeStream(stream.name, s => {
      if (video.srcObject !== s) video.srcObject = s
    })
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
  const [segments, setSegments] = useState<RecordingSegment[]>([])
  const [scrubSeconds, setScrubSeconds] = useState(0)
  const [live, setLive] = useState(true)
  const windowSeconds = retentionMinutes * 60

  // Escape as a guaranteed way out — the X button alone was the only exit,
  // and nothing here reflects into the URL/history, so a missed click left
  // no way back short of a hard page reload.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

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
    // Joins the SAME shared connection the grid tile is already using (or
    // starts one, if this path somehow isn't tiled anywhere) — no fresh
    // negotiation, no reconnect flash, no wait for the next keyframe.
    if (!video || !live) return
    return subscribeStream(name, s => {
      if (video.srcObject !== s) video.srcObject = s
    })
  }, [name, live])

  function handleScrub(seconds: number) {
    setScrubSeconds(seconds)
    if (seconds === 0) {
      setLive(true)
      return
    }
    setLive(false)
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
        {/* Always reads "LIVE" (not the current scrub offset — the slider
            thumb already shows that) so there's a fixed, always-visible
            target to click back to live from anywhere in the scrub range;
            it used to swap to "-Xm" the moment you scrubbed, which made the
            one button that could get you back to live disappear exactly
            when you needed it. */}
        <button
          type="button"
          onClick={() => handleScrub(0)}
          disabled={live}
          className={cn(
            'w-14 font-mono text-xs',
            live ? 'cursor-default text-white' : 'text-zinc-400 hover:text-white'
          )}
        >
          LIVE
        </button>
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

const inputClass = 'w-24 rounded-md border border-zinc-300 bg-zinc-100 px-2 py-1 text-sm text-zinc-900 focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-50 dark:border-white/10 dark:bg-zinc-950 dark:text-white'

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

// Ported from the deleted routes/streams.tsx — the Terminal tab hosts this
// below the live map instead of a separate Streams tab. No data link
// between a map entity and a stream tile: a drone's RTMP path is whatever
// an operator typed into its GCS app, not something EFDI can associate
// with a specific tracked uid (see docs/11-troubleshooting.md's TAK UAS
// Tool video note for the same limitation on the TAK side).
export function StreamsPanel() {
  const {role} = useAuth()
  const [streams, setStreams] = useState<StreamInfo[]>([])
  const [enlarged, setEnlarged] = useState<string | null>(null)
  const [settings, setSettings] = useState<MediamtxSettings>(DEFAULT_SETTINGS)
  const [showSettings, setShowSettings] = useState(false)

  useEffect(() => {
    async function load() {
      try {
        setStreams(await apiJson<StreamInfo[]>('/api/streams'))
      } catch (e) {
        notify.error(errorMessage(e))
      }
    }
    load()
    const interval = setInterval(load, 5000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    apiJson<MediamtxSettings>('/api/streams/config')
      .then(setSettings)
      .catch(e => notify.error(errorMessage(e)))
  }, [])

  return (
    <div>
      <div className="mb-4 flex items-center justify-between gap-3">
        <div className="flex items-baseline gap-3">
          <h2 className="hud-label text-sm text-zinc-700 dark:text-zinc-300">Video streams</h2>
          <span className="hud-label text-xs text-zinc-500">{streams.length} active</span>
        </div>
        <button
          type="button"
          onClick={() => setShowSettings(v => !v)}
          className="flex items-center gap-1.5 rounded-md border border-zinc-300 px-2.5 py-1 text-xs text-zinc-600 hover:bg-zinc-100 dark:border-white/15 dark:text-zinc-300 dark:hover:bg-zinc-800"
        >
          <Settings2 size={13} /> {showSettings ? 'Hide settings' : 'mediamtx settings'}
        </button>
      </div>
      {showSettings && (
        <MediamtxSettingsPanel
          settings={settings}
          canWrite={role === 'superadmin'}
          onSaved={setSettings}
        />
      )}
      {streams.length === 0 ? (
        <p className="text-sm text-zinc-500">
          No drone feeds connected. Point FreeFlight's RTMP URL at this router's mediamtx service to see it here.
        </p>
      ) : (
        <div className={cn('grid grid-cols-2 gap-4', 'lg:grid-cols-3 xl:grid-cols-4')}>
          {streams.map(s => (
            <StreamTile
              key={s.name}
              stream={s}
              onClick={() => setEnlarged(s.name)}
            />
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
    </div>
  )
}
