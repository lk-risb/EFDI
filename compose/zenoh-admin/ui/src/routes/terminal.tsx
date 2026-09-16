import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useMemo, useRef, useState} from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import {HudCorners} from '@/components/HudCorners'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {StreamsPanel} from '@/components/StreamsPanel'
import {apiJson, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'
import {cn} from '@/lib/utils'

export const Route = createFileRoute('/terminal')({
  beforeLoad: () => {
    if (!useAuth.getState().token) throw redirect({to: '/login'})
  },
  component: TerminalPage,
})

interface Entity {
  id: string
  kind: 'drone' | 'sensor'
  source: string
  callsign: string
  lat: number
  lon: number
  alt_m: number | null
  heading_deg: number | null
  speed_kts: number | null
  status: string
  updated_ts: number | null
  raw: Record<string, unknown>
}

const STATUS_COLOR: Record<string, string> = {
  emergency: '#ef4444',
  armed: '#f59e0b',
  on_ground: '#71717a',
  airborne: '#3b82f6',
  online: '#22c55e',
  offline: '#71717a',
}

function colorFor(entity: Entity): string {
  return STATUS_COLOR[entity.status] ?? (entity.kind === 'sensor' ? '#22c55e' : '#3b82f6')
}

function glowIcon(color: string, selected: boolean): L.DivIcon {
  const size = selected ? 15 : 11
  const ring = selected ? `0 0 0 3px ${color}55, ` : ''
  return L.divIcon({
    className: '',
    html: `<span style="display:block;width:${size}px;height:${size}px;border-radius:9999px;background:${color};box-shadow:${ring}0 0 6px 2px ${color}88;border:1.5px solid rgba(255,255,255,0.8)"></span>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  })
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function timeAgo(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  return `${Math.round(s / 3600)}h ago`
}

// Shared field list for both the map popup (HTML string) and the Details
// panel (JSX) — one source of truth for what a statcard shows.
function entityRows(entity: Entity): [string, string][] {
  const rows: [string, string][] = []
  const push = (label: string, value: string | number | null | undefined) => {
    if (value === null || value === undefined || value === '') return
    rows.push([label, String(value)])
  }
  push('Source', entity.source)
  push('Status', entity.status)
  push('Lat, Lon', `${entity.lat.toFixed(5)}°, ${entity.lon.toFixed(5)}°`)
  if (entity.alt_m !== null) push('Altitude', `${Math.round(entity.alt_m)} m`)
  if (entity.heading_deg !== null) push('Heading', `${Math.round(entity.heading_deg)}°`)
  if (entity.speed_kts !== null) push('Speed', `${Math.round(entity.speed_kts)} kts`)
  if (entity.updated_ts) push('Updated', timeAgo(entity.updated_ts))
  const raw = entity.raw
  if (typeof raw.battery_pct === 'number') push('Battery', `${Math.round(raw.battery_pct)}%`)
  if (typeof raw.mission_phase === 'string') push('Mission phase', raw.mission_phase)
  if (typeof raw.alerts === 'string') push('Alerts', raw.alerts)
  if (typeof raw.last_detection_ts === 'number') push('Last detection', timeAgo(raw.last_detection_ts))
  return rows
}

function buildPopupHtml(entity: Entity, color: string): string {
  const rows = entityRows(entity)
    .map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`)
    .join('')
  const audioUrl = entity.raw.last_detection_audio_url
  const audio = typeof audioUrl === 'string'
    ? `<audio class="terminal-statcard-audio" controls preload="none" src="${escapeHtml(audioUrl)}"></audio>`
    : ''
  return `
    <div style="--card-color:${color}" class="terminal-statcard-header">
      <strong>${escapeHtml(entity.callsign)}</strong>
      <span class="terminal-statcard-type">${escapeHtml(entity.kind.toUpperCase())}</span>
    </div>
    <dl class="hud-kv">${rows}</dl>
    ${audio}
  `
}

// Small panel chrome matching TERMINAL's bordered, titled panels (Fleet /
// Map / Video / Telemetry each sit in their own framed box with a header
// row) — not a pixel copy, but the same "cockpit of panels" structure
// instead of one full-bleed page.
function Panel({title, badge, className, bodyClassName, children}: {
  title: string; badge?: string; className?: string; bodyClassName?: string; children: React.ReactNode
}) {
  return (
    <div className={cn('hud-card hud-glass hud-frame relative flex flex-col overflow-hidden rounded-lg border border-zinc-200 dark:border-white/10', className)}>
      <HudCorners />
      <div className="flex shrink-0 items-center justify-between border-b border-zinc-200 px-3 py-2 dark:border-white/10">
        <span className="hud-label text-xs text-zinc-600 dark:text-zinc-400">{title}</span>
        {badge && <span className="hud-label text-[11px] text-zinc-500">{badge}</span>}
      </div>
      <div className={cn('min-h-0 flex-1 overflow-auto', bodyClassName ?? 'p-3')}>{children}</div>
    </div>
  )
}

function batteryColor(pct: number): string {
  if (pct <= 20) return '#ef4444'
  if (pct <= 45) return '#f59e0b'
  return '#22c55e'
}

function FleetRow({entity, selected, onSelect}: {entity: Entity; selected: boolean; onSelect: () => void}) {
  const battery = typeof entity.raw.battery_pct === 'number' ? entity.raw.battery_pct : null
  return (
    <tr
      onClick={onSelect}
      className={cn(
        'cursor-pointer border-b border-zinc-100 text-[11px] last:border-0 dark:border-white/5',
        selected ? 'bg-accent-fill/10' : 'hover:bg-zinc-100 dark:hover:bg-zinc-900'
      )}
    >
      <td className="whitespace-nowrap px-2 py-1.5 font-mono text-zinc-800 dark:text-zinc-200">
        <span className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full align-middle" style={{background: colorFor(entity)}} />
        {entity.callsign}
      </td>
      <td className="hud-label px-2 py-1.5 text-[10px] text-zinc-500">{entity.status}</td>
      <td className="px-2 py-1.5 text-right font-mono" style={battery !== null ? {color: batteryColor(battery)} : undefined}>
        {battery !== null ? `${Math.round(battery)}%` : '—'}
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-zinc-500">
        {entity.alt_m !== null ? `${Math.round(entity.alt_m)}m` : '—'}
      </td>
    </tr>
  )
}

// Only the one-click commands mavlink_command_bridge.py handles without
// extra input (see its module docstring) — "goto" needs a lat/lon and has
// no map-click-to-target UI yet, so it's left out here.
const COMMAND_BUTTONS: {cmd: string; label: string}[] = [
  {cmd: 'arm', label: 'Arm'},
  {cmd: 'disarm', label: 'Disarm'},
  {cmd: 'takeoff', label: 'Takeoff'},
  {cmd: 'hold', label: 'Hold'},
  {cmd: 'rtl', label: 'RTL'},
  {cmd: 'land', label: 'Land'},
]

interface CommandAck {
  cmd?: string
  result?: string
  detail?: string
  _ts?: number
}

function CommandPanel({entityId, canCommand, ack, sending, onSend}: {
  entityId: string
  canCommand: boolean
  ack: CommandAck | null
  sending: string | null
  onSend: (cmd: string) => void
}) {
  if (!canCommand) return null
  return (
    <div className="mt-3 border-t border-zinc-200 pt-3 dark:border-white/10">
      <div className="hud-label mb-2 text-[10px] text-zinc-500">Commands</div>
      <div className="flex flex-wrap gap-1.5">
        {COMMAND_BUTTONS.map(({cmd, label}) => (
          <button
            key={cmd}
            type="button"
            disabled={sending !== null}
            onClick={() => onSend(cmd)}
            className="rounded border border-zinc-300 px-2 py-1 text-[11px] font-medium text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-white/15 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            {sending === cmd ? '…' : label}
          </button>
        ))}
      </div>
      {ack && (
        <p className="mt-2 text-[11px] text-zinc-500">
          {ack.cmd ?? '—'}: <span className="font-medium">{ack.result ?? '—'}</span>
          {ack.detail ? ` — ${ack.detail}` : ''}
          {ack._ts ? ` (${timeAgo(ack._ts)})` : ''}
        </p>
      )}
      <p className="mt-1 text-[10px] text-zinc-400">Entity: {entityId} — generic MAVLink airframes only.</p>
    </div>
  )
}

function DetailsPanel({entity, canCommand, ack, sending, onSend}: {
  entity: Entity | null
  canCommand: boolean
  ack: CommandAck | null
  sending: string | null
  onSend: (cmd: string) => void
}) {
  if (!entity) {
    return <p className="text-xs text-zinc-500">Select an entity on the map or in the Fleet list to see details.</p>
  }
  const rows = entityRows(entity)
  const audioUrl = entity.raw.last_detection_audio_url
  return (
    <div>
      <div
        style={{'--card-color': colorFor(entity)} as React.CSSProperties}
        className="terminal-statcard-header"
      >
        <strong className="text-sm text-zinc-900 dark:text-zinc-100">{entity.callsign}</strong>
        <span className="terminal-statcard-type">{entity.kind.toUpperCase()}</span>
      </div>
      <dl className="hud-kv">
        {rows.map(([label, value]) => (
          <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
        ))}
      </dl>
      {typeof audioUrl === 'string' && (
        <audio className="terminal-statcard-audio" controls preload="none" src={audioUrl} />
      )}
      {entity.kind === 'drone' && (
        <CommandPanel entityId={entity.id} canCommand={canCommand} ack={ack} sending={sending} onSend={onSend} />
      )}
    </div>
  )
}

function TerminalPage() {
  const mapRef = useRef<HTMLDivElement>(null)
  const mapInstance = useRef<L.Map | null>(null)
  const markersRef = useRef<Map<string, L.Marker>>(new Map())
  const [entities, setEntities] = useState<Entity[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [ack, setAck] = useState<CommandAck | null>(null)
  const [sending, setSending] = useState<string | null>(null)
  const role = useAuth(s => s.role)
  const canCommand = role === 'admin' || role === 'superadmin'

  const selected = useMemo(() => entities.find(e => e.id === selectedId) ?? null, [entities, selectedId])

  async function sendCommand(cmd: string) {
    if (!selectedId) return
    setSending(cmd)
    try {
      await apiJson(`/api/terminal/entities/${encodeURIComponent(selectedId)}/command`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cmd}),
      })
      notify.success(`${cmd} queued for ${selectedId}`)
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setSending(null)
    }
  }

  function selectAndFocus(id: string) {
    setSelectedId(id)
    const marker = markersRef.current.get(id)
    if (marker && mapInstance.current) {
      mapInstance.current.panTo(marker.getLatLng())
      marker.openPopup()
    }
  }

  useEffect(() => {
    if (!mapRef.current || mapInstance.current) return
    const map = L.map(mapRef.current, {zoomControl: false}).setView([55.17, 23.88], 7) // Lithuania
    L.control.zoom({position: 'topright'}).addTo(map)
    L.tileLayer('/api/terminal/tiles/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors',
      className: 'terminal-map-tiles',
      maxZoom: 19,
    }).addTo(map)
    mapInstance.current = map
    return () => {
      map.remove()
      mapInstance.current = null
    }
  }, [])

  // Rebuild/refresh markers whenever the entity list changes, and keep the
  // selected marker's icon a bit larger so it's findable on a busy map.
  useEffect(() => {
    if (!mapInstance.current) return
    const seen = new Set<string>()
    for (const entity of entities) {
      seen.add(entity.id)
      const color = colorFor(entity)
      const icon = glowIcon(color, entity.id === selectedId)
      const popup = buildPopupHtml(entity, color)
      const existing = markersRef.current.get(entity.id)
      if (existing) {
        existing.setLatLng([entity.lat, entity.lon])
        existing.setIcon(icon)
        existing.setPopupContent(popup)
      } else {
        const marker = L.marker([entity.lat, entity.lon], {icon}).addTo(mapInstance.current)
        marker.bindPopup(popup, {maxWidth: 280})
        marker.on('click', () => setSelectedId(entity.id))
        markersRef.current.set(entity.id, marker)
      }
    }
    for (const [id, marker] of markersRef.current) {
      if (!seen.has(id)) {
        marker.remove()
        markersRef.current.delete(id)
      }
    }
  }, [entities, selectedId])

  useEffect(() => {
    let cancelled = false

    async function poll() {
      try {
        const res = await apiJson<{entities: Entity[]}>('/api/terminal/entities')
        if (!cancelled) setEntities(res.entities)
      } catch (e) {
        if (!cancelled) notify.error(errorMessage(e))
      }
    }

    poll()
    const interval = setInterval(poll, 4000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  // Poll the selected drone's latest command ack — separate from the entity
  // poll above since it only matters while something is selected, and resets
  // to null on selection change so a stale ack from a previously selected
  // drone never appears to belong to the newly selected one.
  useEffect(() => {
    setAck(null)
    if (!selectedId || !canCommand) return
    let cancelled = false

    async function poll() {
      try {
        const res = await apiJson<{ack: CommandAck | null}>(`/api/terminal/entities/${encodeURIComponent(selectedId!)}/command/ack`)
        if (!cancelled) setAck(res.ack)
      } catch {
        // Ack polling failure isn't worth a toast — the command buttons
        // themselves already report their own send failures.
      }
    }

    poll()
    const interval = setInterval(poll, 3000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [selectedId, canCommand])

  return (
    <Layout>
      <PageHeader title="Terminal" eyebrow="LIVE MAP" count={entities.length} countLabel="tracked" />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[240px_1fr_300px]">
        <Panel title="Fleet" badge={`${entities.length}`} className="h-[60vh]" bodyClassName="overflow-y-auto p-0">
          {entities.length === 0 ? (
            <p className="p-3 text-xs text-zinc-500">No entities tracked yet.</p>
          ) : (
            <table className="w-full border-collapse">
              <thead className="sticky top-0 bg-zinc-50 dark:bg-zinc-950">
                <tr className="hud-label border-b border-zinc-200 text-[10px] text-zinc-500 dark:border-white/10">
                  <th className="px-2 py-1.5 text-left font-medium">CS</th>
                  <th className="px-2 py-1.5 text-left font-medium">STATE</th>
                  <th className="px-2 py-1.5 text-right font-medium">BATT</th>
                  <th className="px-2 py-1.5 text-right font-medium">ALT</th>
                </tr>
              </thead>
              <tbody>
                {entities
                  .slice()
                  .sort((a, b) => a.callsign.localeCompare(b.callsign))
                  .map(e => (
                    <FleetRow key={e.id} entity={e} selected={e.id === selectedId} onSelect={() => selectAndFocus(e.id)} />
                  ))}
              </tbody>
            </table>
          )}
        </Panel>
        <Panel title="Map" className="h-[60vh]" bodyClassName="p-0">
          <div ref={mapRef} className="terminal-map h-full w-full" />
        </Panel>
        <Panel title="Details" className="h-[60vh]">
          <DetailsPanel entity={selected} canCommand={canCommand} ack={ack} sending={sending} onSend={sendCommand} />
        </Panel>
      </div>
      <div className="mt-8">
        <StreamsPanel />
      </div>
    </Layout>
  )
}
