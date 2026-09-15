import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useRef, useState} from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {StreamsPanel} from '@/components/StreamsPanel'
import {apiJson, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'

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

// Matches tak_layer.py's _SENSOR_ALERT_HOT_S / _SENSOR_ALERT_WARM_S — a
// detection older than this has fully reverted to idle on the TAK map too
// (same threshold sensors.tsx used before this tab replaced it).
const DETECTION_WARM_S = 300

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

function glowIcon(color: string): L.DivIcon {
  return L.divIcon({
    className: '',
    html: `<span style="display:block;width:11px;height:11px;border-radius:9999px;background:${color};box-shadow:0 0 6px 2px ${color}88;border:1.5px solid rgba(255,255,255,0.8)"></span>`,
    iconSize: [11, 11],
    iconAnchor: [5.5, 5.5],
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
  return `${Math.round(s / 60)}m ago`
}

function buildStatCard(entity: Entity, color: string): string {
  const rows: string[] = []
  const row = (label: string, value: string | number | null | undefined) => {
    if (value === null || value === undefined || value === '') return
    rows.push(`<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd></div>`)
  }
  row('Source', entity.source)
  row('Status', entity.status)
  row('Lat, Lon', `${entity.lat.toFixed(5)}°, ${entity.lon.toFixed(5)}°`)
  if (entity.alt_m !== null) row('Altitude', `${Math.round(entity.alt_m)} m`)
  if (entity.heading_deg !== null) row('Heading', `${Math.round(entity.heading_deg)}°`)
  if (entity.speed_kts !== null) row('Speed', `${Math.round(entity.speed_kts)} kts`)
  if (entity.updated_ts) row('Updated', timeAgo(entity.updated_ts))
  const raw = entity.raw
  if (typeof raw.battery_pct === 'number') row('Battery', `${Math.round(raw.battery_pct)}%`)
  if (typeof raw.mission_phase === 'string') row('Mission phase', raw.mission_phase)
  if (typeof raw.alerts === 'string') row('Alerts', raw.alerts)
  if (typeof raw.last_detection_ts === 'number') row('Last detection', timeAgo(raw.last_detection_ts))

  const audio = typeof raw.last_detection_audio_url === 'string'
    ? `<audio class="terminal-statcard-audio" controls preload="none" src="${escapeHtml(raw.last_detection_audio_url)}"></audio>`
    : ''

  return `
    <div style="--card-color:${color}" class="terminal-statcard-header">
      <strong>${escapeHtml(entity.callsign)}</strong>
      <span class="terminal-statcard-type">${escapeHtml(entity.kind.toUpperCase())}</span>
    </div>
    <dl class="hud-kv">${rows.join('')}</dl>
    ${audio}
  `
}

function TerminalPage() {
  const mapRef = useRef<HTMLDivElement>(null)
  const mapInstance = useRef<L.Map | null>(null)
  const markersRef = useRef<Map<string, L.Marker>>(new Map())
  const [entityCount, setEntityCount] = useState(0)

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

  useEffect(() => {
    let cancelled = false

    async function poll() {
      try {
        const res = await apiJson<{entities: Entity[]}>('/api/terminal/entities')
        if (cancelled || !mapInstance.current) return
        const seen = new Set<string>()
        for (const entity of res.entities) {
          seen.add(entity.id)
          const color = colorFor(entity)
          const icon = glowIcon(color)
          const popup = buildStatCard(entity, color)
          const existing = markersRef.current.get(entity.id)
          if (existing) {
            existing.setLatLng([entity.lat, entity.lon])
            existing.setIcon(icon)
            existing.setPopupContent(popup)
          } else {
            const marker = L.marker([entity.lat, entity.lon], {icon}).addTo(mapInstance.current)
            marker.bindPopup(popup, {maxWidth: 280})
            markersRef.current.set(entity.id, marker)
          }
        }
        for (const [id, marker] of markersRef.current) {
          if (!seen.has(id)) {
            marker.remove()
            markersRef.current.delete(id)
          }
        }
        setEntityCount(markersRef.current.size)
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

  return (
    <Layout>
      <PageHeader title="Terminal" eyebrow="LIVE MAP" count={entityCount} countLabel="tracked" />
      <div
        ref={mapRef}
        className="terminal-map h-[70vh] w-full overflow-hidden rounded-lg border border-zinc-200 dark:border-white/10"
      />
      <div className="mt-8">
        <StreamsPanel />
      </div>
    </Layout>
  )
}
