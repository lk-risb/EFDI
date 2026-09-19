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
  kind: 'drone' | 'sensor' | 'unit'
  source: string
  callsign: string
  lat: number
  lon: number
  alt_m: number | null
  heading_deg: number | null
  speed_kts: number | null
  status: string
  updated_ts: number | null
  mgrs: string | null
  raw: Record<string, unknown>
}

interface TerminalAsset {
  id: string
  name: string
  category: string
  lat_deg: number
  lon_deg: number
  description: string
}

interface TerminalZone {
  id: string
  name: string
  tier: 'info' | 'warn' | 'crit'
  center_lat_deg: number
  center_lon_deg: number
  radius_m: number
  description: string
}

const STATUS_COLOR: Record<string, string> = {
  emergency: '#ef4444',
  armed: '#f59e0b',
  on_ground: '#71717a',
  airborne: '#3b82f6',
  online: '#22c55e',
  offline: '#71717a',
}

// Ground/operator-position tracks (AARTOS WiFi/direction-finding, once wired)
// — kept a distinct purple so they never read as another drone on the map,
// same way mainline.inc TERMINAL differentiates its phone/operator markers.
const UNIT_COLOR = '#a855f7'

function colorFor(entity: Entity): string {
  // Units keep their own purple identity regardless of status text — only
  // emergency overrides it — so an "online" unit never reads as a green
  // sensor and an "unknown"-status one never falls back to a generic color.
  if (entity.kind === 'unit') return entity.status === 'emergency' ? STATUS_COLOR.emergency : UNIT_COLOR
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

// Rotated square (diamond) instead of a dot — a shape-level distinction from
// drone/sensor circle markers, matching real TERMINAL's operator/phone icons
// looking nothing like its drone dots.
function unitIcon(color: string, selected: boolean): L.DivIcon {
  const size = selected ? 13 : 10
  return L.divIcon({
    className: '',
    html: `<span style="display:block;width:${size}px;height:${size}px;background:${color};box-shadow:0 0 5px 1px ${color}88;border:1.5px solid rgba(255,255,255,0.85);transform:rotate(45deg)"></span>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  })
}

function iconFor(entity: Entity, selected: boolean): L.DivIcon {
  const color = colorFor(entity)
  return entity.kind === 'unit' ? unitIcon(color, selected) : glowIcon(color, selected)
}

const ASSET_CATEGORY_COLOR: Record<string, string> = {
  generic: '#94a3b8',
  gsm_tower: '#f59e0b',
  building: '#64748b',
  industrial: '#ea580c',
  rally_point: '#22c55e',
}

// Rounded square with a category-initial label — visually distinct from both
// the entity dots and the unit diamonds, since an asset is operator-placed
// static data, not a live track.
function assetIcon(category: string): L.DivIcon {
  const color = ASSET_CATEGORY_COLOR[category] ?? ASSET_CATEGORY_COLOR.generic
  const label = category.slice(0, 1).toUpperCase()
  return L.divIcon({
    className: '',
    html: `<span style="display:flex;align-items:center;justify-content:center;width:16px;height:16px;background:${color};border:1.5px solid rgba(255,255,255,0.9);border-radius:3px;color:#fff;font-size:9px;font-weight:600;box-shadow:0 0 4px 1px ${color}77">${label}</span>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  })
}

const ZONE_TIER_COLOR: Record<TerminalZone['tier'], string> = {
  info: '#3b82f6',
  warn: '#f59e0b',
  crit: '#ef4444',
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
  push('Kind', entity.kind[0].toUpperCase() + entity.kind.slice(1))
  push('Source', entity.source)
  push('Status', entity.status)
  push('Lat, Lon', `${entity.lat.toFixed(5)}°, ${entity.lon.toFixed(5)}°`)
  if (entity.mgrs) push('MGRS', entity.mgrs)
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
  title: string; badge?: React.ReactNode; className?: string; bodyClassName?: string; children: React.ReactNode
}) {
  return (
    <div className={cn('hud-card hud-glass hud-frame relative flex flex-col overflow-hidden rounded-lg border border-zinc-200 dark:border-white/10', className)}>
      <HudCorners />
      {title && (
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-zinc-200 px-3 py-2 dark:border-white/10">
          <span className="hud-label shrink-0 text-xs text-zinc-600 dark:text-zinc-400">{title}</span>
          {badge && <span className="hud-label min-w-0 text-[11px] text-zinc-500">{badge}</span>}
        </div>
      )}
      <div className={cn('min-h-0 flex-1 overflow-auto', bodyClassName ?? 'p-3')}>{children}</div>
    </div>
  )
}

// Visual-only, matching real TERMINAL's top nav — Panels/Layout/Fleet/Plan/
// Modules are panel-management/mission-planning menus with no EFDI feature
// behind them yet. Rendered non-interactive (no onClick, cursor-default)
// rather than wired to fake behavior, so nobody mistakes a click here for a
// working control. Wiring any of these to a real feature is a separate task.
const NAV_TABS = ['Panels', 'Layout', 'Fleet', 'Plan', 'Modules']

function TerminalNav({rightSlot}: {rightSlot?: React.ReactNode}) {
  return (
    <div className="mb-3 flex items-center justify-between gap-1 border-b border-zinc-200 pb-2 dark:border-white/10">
      <div className="flex items-center gap-1">
        {NAV_TABS.map(tab => (
          <span
            key={tab}
            title="Not wired to a feature yet"
            className="hud-label cursor-default rounded px-2 py-1 text-[10px] text-zinc-400 dark:text-zinc-600"
          >
            {tab}
          </span>
        ))}
      </div>
      {rightSlot}
    </div>
  )
}

// Compact top-right status readout, matching real TERMINAL's own
// alerts-bell + live-clock cluster next to its nav strip — the bottom status
// bar already covers this in more detail, this is just the same real data
// (active alert count, connection state) surfaced where the reference also
// shows it. No fabricated fields (no GPS-sat/ground-station counts — this
// app has no source for those).
function TopStatusCluster({alertCount, connected}: {alertCount: number; connected: boolean}) {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const interval = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(interval)
  }, [])
  const clock = now.toISOString().slice(11, 19)
  return (
    <div className="hud-label flex shrink-0 items-center gap-3 text-[10px] text-zinc-500">
      <span className={cn('flex items-center gap-1', alertCount > 0 && 'text-red-500')}>
        🔔 {alertCount}
      </span>
      <span className="flex items-center gap-1">
        <span className="h-1.5 w-1.5 rounded-full" style={{background: connected ? '#22c55e' : '#71717a'}} />
        {connected ? 'LIVE' : 'DOWN'} {clock} Z
      </span>
    </div>
  )
}

// Assets/Markings now back real CRUD data (TerminalAsset/TerminalZone) — an
// operator-placed POI or geofence, same as mainline.inc TERMINAL's own
// Assets tab. Selection stays a static readout, the one honest piece of
// state this strip always had.
const FLEET_SUB_TABS = ['Assets', 'Markings', 'Selection'] as const
type FleetSubTab = typeof FLEET_SUB_TABS[number]

function FleetSubTabs({
  selected, assets, zones, canCommand, placing, onStartPlacing, onCancelPlacing, onDelete, tab, onTabChange,
}: {
  selected: Entity | null
  assets: TerminalAsset[]
  zones: TerminalZone[]
  canCommand: boolean
  placing: 'asset' | 'zone' | null
  onStartPlacing: (kind: 'asset' | 'zone') => void
  onCancelPlacing: () => void
  onDelete: (kind: 'asset' | 'zone', id: string) => void
  tab: FleetSubTab
  onTabChange: (tab: FleetSubTab) => void
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-1 border-b border-zinc-200 px-2 py-1 dark:border-white/10">
        {FLEET_SUB_TABS.map(t => (
          <button
            key={t}
            type="button"
            onClick={() => onTabChange(t)}
            className={cn(
              'hud-label rounded px-1.5 py-0.5 text-[9px]',
              tab === t ? 'bg-zinc-200 text-zinc-600 dark:bg-white/10 dark:text-zinc-300' : 'text-zinc-400 hover:text-zinc-600 dark:text-zinc-600'
            )}
          >
            {t}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-2 text-[11px]">
        {tab === 'Assets' && (
          <div>
            {canCommand && (
              <button
                type="button"
                onClick={() => (placing === 'asset' ? onCancelPlacing() : onStartPlacing('asset'))}
                className={cn(
                  'mb-1.5 w-full rounded border px-1.5 py-1 text-[10px]',
                  placing === 'asset'
                    ? 'border-accent-fill bg-accent-fill/10 text-accent-fill'
                    : 'border-zinc-300 text-zinc-600 hover:bg-zinc-100 dark:border-white/15 dark:text-zinc-300 dark:hover:bg-zinc-800'
                )}
              >
                {placing === 'asset' ? 'Click the map…' : '+ Place asset'}
              </button>
            )}
            {assets.length === 0 ? (
              <p className="text-zinc-500">No assets placed.</p>
            ) : assets.map(a => (
              <div key={a.id} className="flex items-center justify-between border-b border-zinc-100 py-1 last:border-0 dark:border-white/5">
                <span className="truncate" title={a.description || a.name}>
                  <span className="mr-1 inline-block h-2 w-2 rounded-sm align-middle" style={{background: ASSET_CATEGORY_COLOR[a.category] ?? ASSET_CATEGORY_COLOR.generic}} />
                  {a.name}
                </span>
                {canCommand && (
                  <button type="button" onClick={() => onDelete('asset', a.id)} className="ml-2 shrink-0 text-zinc-400 hover:text-red-500">✕</button>
                )}
              </div>
            ))}
          </div>
        )}
        {tab === 'Markings' && (
          <div>
            {canCommand && (
              <button
                type="button"
                onClick={() => (placing === 'zone' ? onCancelPlacing() : onStartPlacing('zone'))}
                className={cn(
                  'mb-1.5 w-full rounded border px-1.5 py-1 text-[10px]',
                  placing === 'zone'
                    ? 'border-accent-fill bg-accent-fill/10 text-accent-fill'
                    : 'border-zinc-300 text-zinc-600 hover:bg-zinc-100 dark:border-white/15 dark:text-zinc-300 dark:hover:bg-zinc-800'
                )}
              >
                {placing === 'zone' ? 'Click the map for center…' : '+ Place zone'}
              </button>
            )}
            {zones.length === 0 ? (
              <p className="text-zinc-500">No zones drawn.</p>
            ) : zones.map(z => (
              <div key={z.id} className="flex items-center justify-between border-b border-zinc-100 py-1 last:border-0 dark:border-white/5">
                <span className="truncate" title={z.description || z.name}>
                  <span className="mr-1 inline-block h-2 w-2 rounded-full align-middle" style={{background: ZONE_TIER_COLOR[z.tier]}} />
                  {z.name} <span className="text-zinc-400">({Math.round(z.radius_m)}m)</span>
                </span>
                {canCommand && (
                  <button type="button" onClick={() => onDelete('zone', z.id)} className="ml-2 shrink-0 text-zinc-400 hover:text-red-500">✕</button>
                )}
              </div>
            ))}
          </div>
        )}
        {tab === 'Selection' && (
          selected ? (
            <div>
              <p className="mb-1 font-medium text-zinc-700 dark:text-zinc-300">{selected.callsign}</p>
              <dl className="hud-kv text-[10px]">
                {entityRows(selected).map(([label, value]) => (
                  <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
                ))}
              </dl>
            </div>
          ) : (
            <p className="text-zinc-500">Nothing selected.</p>
          )
        )}
      </div>
    </div>
  )
}

// Visual-only, matching real TERMINAL's own empty state exactly: a plain
// "+" in the header (same non-interactive placeholder chrome as TerminalNav
// and the Fleet/SAT columns — no per-panel "add" feature exists here) and a
// small "no feed" pill in the body, not a centered message. A real feed
// picker was tried here first (a manual dropdown over StreamsPanel's own
// WHEP playback) but the reference's default state has no picker at all
// until a feed exists — reverted rather than show UI real TERMINAL doesn't.
function VideoHeaderPlus() {
  return (
    <span title="Not wired to a feature yet" className="hud-label cursor-default text-sm text-zinc-500 dark:text-zinc-500">
      +
    </span>
  )
}

function VideoPanel() {
  return (
    <div className="relative h-full bg-black p-2">
      <span className="hud-label rounded bg-zinc-800 px-2 py-1 text-[11px] text-zinc-400">no feed</span>
    </div>
  )
}

const ASSET_CATEGORIES = ['generic', 'gsm_tower', 'building', 'industrial', 'rally_point'] as const

// Shown once an admin has clicked the map while in "place asset/zone" mode —
// the point is already fixed, this only collects the remaining fields
// before the actual POST.
function NewMarkerForm({kind, point, onCancel, onSubmitAsset, onSubmitZone}: {
  kind: 'asset' | 'zone'
  point: {lat: number; lon: number}
  onCancel: () => void
  onSubmitAsset: (payload: {name: string; category: string; lat_deg: number; lon_deg: number; description: string}) => void
  onSubmitZone: (payload: {name: string; tier: string; center_lat_deg: number; center_lon_deg: number; radius_m: number; description: string}) => void
}) {
  const [name, setName] = useState('')
  const [category, setCategory] = useState<typeof ASSET_CATEGORIES[number]>('generic')
  const [tier, setTier] = useState<TerminalZone['tier']>('info')
  const [radiusM, setRadiusM] = useState('250')
  const [description, setDescription] = useState('')

  function submit() {
    if (!name.trim()) return
    if (kind === 'asset') {
      onSubmitAsset({name: name.trim(), category, lat_deg: point.lat, lon_deg: point.lon, description})
    } else {
      const radius = Number(radiusM)
      if (!Number.isFinite(radius) || radius <= 0) return
      onSubmitZone({name: name.trim(), tier, center_lat_deg: point.lat, center_lon_deg: point.lon, radius_m: radius, description})
    }
  }

  return (
    <Panel title={kind === 'asset' ? 'New Asset' : 'New Zone'} className="shrink-0">
      <div className="flex flex-col gap-1.5 text-[11px]">
        <p className="text-zinc-400">{point.lat.toFixed(5)}°, {point.lon.toFixed(5)}°</p>
        <input
          value={name}
          onChange={e => setName(e.target.value)}
          placeholder="Name"
          className="rounded border border-zinc-300 bg-transparent px-1.5 py-1 dark:border-white/15"
        />
        {kind === 'asset' ? (
          <select value={category} onChange={e => setCategory(e.target.value as typeof category)} className="rounded border border-zinc-300 bg-transparent px-1.5 py-1 dark:border-white/15">
            {ASSET_CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        ) : (
          <div className="flex gap-1.5">
            <select value={tier} onChange={e => setTier(e.target.value as TerminalZone['tier'])} className="flex-1 rounded border border-zinc-300 bg-transparent px-1.5 py-1 dark:border-white/15">
              <option value="info">info</option>
              <option value="warn">warn</option>
              <option value="crit">crit</option>
            </select>
            <input
              value={radiusM}
              onChange={e => setRadiusM(e.target.value)}
              placeholder="Radius (m)"
              inputMode="numeric"
              className="w-24 rounded border border-zinc-300 bg-transparent px-1.5 py-1 dark:border-white/15"
            />
          </div>
        )}
        <input
          value={description}
          onChange={e => setDescription(e.target.value)}
          placeholder="Description (optional)"
          className="rounded border border-zinc-300 bg-transparent px-1.5 py-1 dark:border-white/15"
        />
        <div className="flex gap-1.5">
          <button type="button" onClick={submit} className="flex-1 rounded border border-accent-fill bg-accent-fill/10 px-1.5 py-1 text-accent-fill">Save</button>
          <button type="button" onClick={onCancel} className="flex-1 rounded border border-zinc-300 px-1.5 py-1 text-zinc-500 dark:border-white/15">Cancel</button>
        </div>
      </div>
    </Panel>
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
        {entity.source && entity.source !== 'unknown' && (
          <span className="ml-1.5 text-[9px] font-normal text-zinc-400">{entity.source}</span>
        )}
      </td>
      <td className="hud-label px-2 py-1.5 text-[10px] text-zinc-500">{entity.status}</td>
      <td className="px-2 py-1.5 text-right font-mono" style={battery !== null ? {color: batteryColor(battery)} : undefined}>
        {battery !== null ? `${Math.round(battery)}%` : '—'}
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-zinc-500">
        {entity.alt_m !== null ? `${Math.round(entity.alt_m)}m` : '—'}
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-zinc-500">
        {entity.speed_kts !== null ? `${Math.round(entity.speed_kts)}kt` : '—'}
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-zinc-600" title="Link quality — not wired to a data source yet">—</td>
      <td className="px-2 py-1.5 text-right font-mono text-zinc-600" title="Satellite count — not wired to a data source yet">—</td>
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

interface TerminalEvent {
  ts: number
  entity_id: string
  kind: 'cmd' | 'status' | 'alert' | 'system'
  severity: 'info' | 'warn' | 'crit'
  message: string
}

const SEVERITY_COLOR: Record<TerminalEvent['severity'], string> = {
  info: '#3b82f6',
  warn: '#f59e0b',
  crit: '#ef4444',
}

const SEVERITY_TABS: TerminalEvent['severity'][] = ['info', 'warn', 'crit']

// Real events only — see terminal.py's _push_event: command sends/acks,
// drone status transitions, health.alerts changes, and — for "system"
// events — a native bridge/layer's own status transition (running/crashed/
// degraded), reused from the same admin_control.py runtime data the
// Runtime Control page shows. No fabricated mission/geofence event types
// mainline.inc TERMINAL has that this backend cannot actually observe.
function EventRow({event}: {event: TerminalEvent}) {
  return (
    <div className="flex items-start gap-2 border-b border-zinc-100 px-3 py-1.5 text-[11px] last:border-0 dark:border-white/5">
      <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full" style={{background: SEVERITY_COLOR[event.severity]}} />
      <span className="w-12 shrink-0 font-mono text-zinc-400">{timeAgo(event.ts)}</span>
      <span className="w-20 shrink-0 truncate font-mono text-zinc-500" title={event.entity_id}>{event.entity_id}</span>
      <span className="hud-label shrink-0 text-[9px] text-zinc-400">{event.kind}</span>
      <span className="flex-1 text-zinc-700 dark:text-zinc-300">{event.message}</span>
    </div>
  )
}

// mainline.inc TERMINAL's Events panel has INFO/WARN/CRIT filter toggles
// plus a text filter — both purely client-side over the same event list,
// no backend change needed since severity/message are already returned.
function EventsToolbar({activeSeverities, onToggleSeverity, filterText, onFilterText}: {
  activeSeverities: Set<TerminalEvent['severity']>
  onToggleSeverity: (s: TerminalEvent['severity']) => void
  filterText: string
  onFilterText: (v: string) => void
}) {
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-zinc-200 px-3 py-1.5 dark:border-white/10">
      {SEVERITY_TABS.map(sev => (
        <button
          key={sev}
          type="button"
          onClick={() => onToggleSeverity(sev)}
          className={cn(
            'hud-label rounded border px-1.5 py-0.5 text-[9px]',
            activeSeverities.has(sev)
              ? 'border-transparent text-white'
              : 'border-zinc-300 text-zinc-400 dark:border-white/15'
          )}
          style={activeSeverities.has(sev) ? {background: SEVERITY_COLOR[sev]} : undefined}
        >
          {sev}
        </button>
      ))}
      <input
        value={filterText}
        onChange={e => onFilterText(e.target.value)}
        placeholder="Filter"
        className="ml-auto min-w-0 max-w-[10rem] rounded border border-zinc-300 bg-transparent px-1.5 py-0.5 text-[11px] dark:border-white/15"
      />
    </div>
  )
}

// mainline.inc TERMINAL's Alerts panel tracks acked/unacked state server-side.
// This backend has no such workflow (nothing anywhere persists an ack), so
// "Dismiss" here only hides an alert in this browser tab — it does not write
// anything back. Labeled as such rather than pretending to be the real thing.
function AlertsPanel({alerts, dismissed, onDismiss}: {
  alerts: TerminalEvent[]
  dismissed: Set<string>
  onDismiss: (key: string) => void
}) {
  const visible = alerts.filter(a => !dismissed.has(`${a.entity_id}-${a.ts}`))
  if (visible.length === 0) {
    return <p className="p-3 text-xs text-zinc-500">No active alerts.</p>
  }
  return (
    <div>
      {visible.map(alert => {
        const key = `${alert.entity_id}-${alert.ts}`
        return (
          <div key={key} className="flex items-start gap-2 border-b border-zinc-100 px-3 py-2 text-[11px] last:border-0 dark:border-white/5">
            <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full" style={{background: SEVERITY_COLOR[alert.severity]}} />
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium text-zinc-800 dark:text-zinc-200">{alert.message}</p>
              <p className="hud-label text-[10px] text-zinc-400">
                <span title={alert.entity_id}>{alert.entity_id.toUpperCase()}</span> · {timeAgo(alert.ts)}
              </p>
            </div>
            <button
              type="button"
              onClick={() => onDismiss(key)}
              className="shrink-0 rounded border border-zinc-300 px-1.5 py-0.5 text-[10px] text-zinc-500 hover:bg-zinc-100 dark:border-white/15 dark:hover:bg-zinc-800"
              title="Hide in this browser tab only — does not acknowledge anything server-side."
            >
              Dismiss
            </button>
          </div>
        )
      })}
    </div>
  )
}

const TIMELINE_RANGES: {label: string; ms: number}[] = [
  {label: '1H', ms: 60 * 60 * 1000},
  {label: '6H', ms: 6 * 60 * 60 * 1000},
  {label: '24H', ms: 24 * 60 * 60 * 1000},
  {label: '7D', ms: 7 * 24 * 60 * 60 * 1000},
]

// A visualization of the same event log on a time axis, not an interactive
// scrubber — this backend keeps no historical entity/map state to "scrub"
// back to, unlike mainline.inc TERMINAL's own timeline. Ticks are purely
// informational (hover for what happened, when). Range buttons only change
// the window drawn over the same already-fetched event list — no separate
// backend query per range, since the ring buffer this reads from caps at
// 300 entries anyway.
function TimelineStrip({events, windowMs, onWindowChange}: {
  events: TerminalEvent[]
  windowMs: number
  onWindowChange: (ms: number) => void
}) {
  const now = Date.now()
  const windowStart = now - windowMs
  const inWindow = events.filter(e => e.ts * 1000 >= windowStart)
  const activeLabel = TIMELINE_RANGES.find(r => r.ms === windowMs)?.label ?? '1H'
  return (
    <div className="flex items-center gap-2">
      <div className="flex shrink-0 gap-1">
        {TIMELINE_RANGES.map(({label, ms}) => (
          <button
            key={label}
            type="button"
            onClick={() => onWindowChange(ms)}
            className={cn(
              'hud-label rounded border px-1.5 py-0.5 text-[9px]',
              ms === windowMs
                ? 'border-accent-fill bg-accent-fill/10 text-accent-fill'
                : 'border-zinc-300 text-zinc-400 dark:border-white/15'
            )}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="relative h-8 w-full overflow-hidden rounded border border-zinc-200 bg-zinc-50 dark:border-white/10 dark:bg-zinc-950">
        {inWindow.map(event => {
          const pct = Math.min(100, Math.max(0, ((event.ts * 1000 - windowStart) / windowMs) * 100))
          return (
            <span
              key={`${event.entity_id}-${event.ts}`}
              className="absolute top-1/2 h-3 w-0.5 -translate-y-1/2 rounded-full"
              style={{left: `${pct}%`, background: SEVERITY_COLOR[event.severity]}}
              title={`${event.entity_id}: ${event.message} (${timeAgo(event.ts)})`}
            />
          )
        })}
        <span className="hud-label pointer-events-none absolute left-1.5 top-1/2 -translate-y-1/2 text-[9px] text-zinc-400">{activeLabel}</span>
      </div>
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
      {entity.mgrs && (
        <button
          type="button"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(entity.mgrs!)
              notify.success('MGRS copied')
            } catch (e) {
              notify.error(errorMessage(e))
            }
          }}
          className="mt-2 rounded border border-zinc-300 px-2 py-1 text-[11px] text-zinc-600 hover:bg-zinc-100 dark:border-white/15 dark:text-zinc-300 dark:hover:bg-zinc-800"
        >
          Copy MGRS
        </button>
      )}
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
  const tileLayerRef = useRef<L.TileLayer | null>(null)
  const assetMarkersRef = useRef<Map<string, L.Marker>>(new Map())
  const zoneLayersRef = useRef<Map<string, L.Circle>>(new Map())
  const [entities, setEntities] = useState<Entity[]>([])
  const [mapboxEnabled, setMapboxEnabled] = useState(false)
  const [assets, setAssets] = useState<TerminalAsset[]>([])
  const [zones, setZones] = useState<TerminalZone[]>([])
  const [placing, setPlacing] = useState<'asset' | 'zone' | null>(null)
  const [fleetSubTab, setFleetSubTab] = useState<FleetSubTab>('Assets')
  const placingRef = useRef(placing)
  const [pendingPoint, setPendingPoint] = useState<{lat: number; lon: number} | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [ack, setAck] = useState<CommandAck | null>(null)
  const [sending, setSending] = useState<string | null>(null)
  const [events, setEvents] = useState<TerminalEvent[]>([])
  const [dismissedAlerts, setDismissedAlerts] = useState<Set<string>>(new Set())
  const [activeSeverities, setActiveSeverities] = useState<Set<TerminalEvent['severity']>>(
    () => new Set(SEVERITY_TABS)
  )
  const [eventFilterText, setEventFilterText] = useState('')
  const [timelineWindowMs, setTimelineWindowMs] = useState(TIMELINE_RANGES[0].ms)
  const [routerStatus, setRouterStatus] = useState<{connected: boolean; endpoint: string} | null>(null)
  const role = useAuth(s => s.role)
  const username = useAuth(s => s.username)
  const canCommand = role === 'admin' || role === 'superadmin'
  // Alerts = drone health-string alerts, plus any non-info "system" event
  // (a bridge/layer crashing or degrading) — mirrors mainline.inc TERMINAL's
  // own Alerts panel, which is infra-health-sourced ("world-sim heartbeat is
  // unhealthy"), not just entity-derived.
  const alerts = useMemo(
    () => events.filter(e => e.kind === 'alert' || (e.kind === 'system' && e.severity !== 'info')),
    [events]
  )
  const filteredEvents = useMemo(() => {
    const needle = eventFilterText.trim().toLowerCase()
    return events.filter(e => {
      if (!activeSeverities.has(e.severity)) return false
      if (!needle) return true
      return e.message.toLowerCase().includes(needle) || e.entity_id.toLowerCase().includes(needle)
    })
  }, [events, activeSeverities, eventFilterText])

  function toggleSeverity(sev: TerminalEvent['severity']) {
    setActiveSeverities(prev => {
      const next = new Set(prev)
      if (next.has(sev)) next.delete(sev); else next.add(sev)
      return next
    })
  }

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
    placingRef.current = placing
  }, [placing])

  useEffect(() => {
    if (!mapRef.current || mapInstance.current) return
    const map = L.map(mapRef.current, {zoomControl: false}).setView([55.17, 23.88], 7) // Lithuania
    L.control.zoom({position: 'topright'}).addTo(map)
    // Only fires while an admin is in "place asset/zone" mode (see
    // FleetSubTabs) — otherwise a map click just does Leaflet's own pan/zoom.
    map.on('click', e => {
      if (!placingRef.current) return
      setPendingPoint({lat: e.latlng.lat, lon: e.latlng.lng})
    })
    mapInstance.current = map
    return () => {
      map.remove()
      mapInstance.current = null
      tileLayerRef.current = null
    }
  }, [])

  // Tiles proxy through /api/terminal/tiles (caches + sets a real
  // User-Agent for OSM's policy). When MAPBOX_ACCESS_TOKEN is configured
  // server-side, the same proxy serves Mapbox's satellite-streets style
  // instead — matching the real mainline.inc TERMINAL reference this tab is
  // styled after. Real satellite imagery shouldn't get the OSM-only
  // dark-invert filter (terminal-map-tiles in index.css), so that class and
  // the lower maxZoom only apply in OSM mode; rebuilds whenever the
  // entities poll reports a change (e.g. the operator adds the token later
  // without restarting the browser tab).
  useEffect(() => {
    if (!mapInstance.current) return
    tileLayerRef.current?.remove()
    const layer = L.tileLayer('/api/terminal/tiles/{z}/{x}/{y}.png', {
      attribution: mapboxEnabled
        ? '&copy; Mapbox &copy; OpenStreetMap <a href="https://www.mapbox.com/about/maps/" target="_blank" rel="noopener">Improve this map</a>'
        : '&copy; OpenStreetMap contributors',
      className: mapboxEnabled ? undefined : 'terminal-map-tiles',
      maxZoom: mapboxEnabled ? 22 : 19,
    }).addTo(mapInstance.current)
    layer.bringToBack()
    tileLayerRef.current = layer
  }, [mapboxEnabled])

  // Rebuild/refresh markers whenever the entity list changes, and keep the
  // selected marker's icon a bit larger so it's findable on a busy map.
  useEffect(() => {
    if (!mapInstance.current) return
    const seen = new Set<string>()
    for (const entity of entities) {
      seen.add(entity.id)
      const color = colorFor(entity)
      const icon = iconFor(entity, entity.id === selectedId)
      const popup = buildPopupHtml(entity, color)
      const existing = markersRef.current.get(entity.id)
      if (existing) {
        existing.setLatLng([entity.lat, entity.lon])
        existing.setIcon(icon)
        existing.setPopupContent(popup)
        existing.setTooltipContent(entity.callsign)
      } else {
        const marker = L.marker([entity.lat, entity.lon], {icon}).addTo(mapInstance.current)
        marker.bindPopup(popup, {maxWidth: 280})
        // Permanent label, not just a click popup — matches real TERMINAL,
        // where every marker's name is always visible on the map.
        marker.bindTooltip(entity.callsign, {permanent: true, direction: 'right', offset: [8, 0], className: 'terminal-marker-label'})
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
        const res = await apiJson<{entities: Entity[]; mapbox_enabled: boolean}>('/api/terminal/entities')
        if (!cancelled) {
          setEntities(res.entities)
          setMapboxEnabled(res.mapbox_enabled)
        }
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

  // Assets/zones are operator-authored, not live telemetry — a slower poll
  // than the entity feed is enough to pick up another operator's changes.
  async function refetchAssets() {
    try {
      const res = await apiJson<{assets: TerminalAsset[]}>('/api/terminal/assets')
      setAssets(res.assets)
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  async function refetchZones() {
    try {
      const res = await apiJson<{zones: TerminalZone[]}>('/api/terminal/zones')
      setZones(res.zones)
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  useEffect(() => {
    let cancelled = false
    async function poll() {
      if (cancelled) return
      await refetchAssets()
      if (!cancelled) await refetchZones()
    }
    poll()
    const interval = setInterval(poll, 15000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  async function createAsset(payload: {name: string; category: string; lat_deg: number; lon_deg: number; description: string}) {
    try {
      await apiJson('/api/terminal/assets', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      })
      notify.success(`Asset "${payload.name}" placed`)
      setPlacing(null)
      setPendingPoint(null)
      await refetchAssets()
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  async function createZone(payload: {name: string; tier: string; center_lat_deg: number; center_lon_deg: number; radius_m: number; description: string}) {
    try {
      await apiJson('/api/terminal/zones', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      })
      notify.success(`Zone "${payload.name}" drawn`)
      setPlacing(null)
      setPendingPoint(null)
      await refetchZones()
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  async function deleteMarker(kind: 'asset' | 'zone', id: string) {
    try {
      await apiJson(`/api/terminal/${kind === 'asset' ? 'assets' : 'zones'}/${encodeURIComponent(id)}`, {method: 'DELETE'})
      if (kind === 'asset') await refetchAssets(); else await refetchZones()
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  // Asset markers — same add/update/remove-by-id pattern as the entity
  // markers above, just a static (non-glowing) icon since these never move.
  useEffect(() => {
    if (!mapInstance.current) return
    const seen = new Set<string>()
    for (const asset of assets) {
      seen.add(asset.id)
      const existing = assetMarkersRef.current.get(asset.id)
      if (existing) {
        existing.setLatLng([asset.lat_deg, asset.lon_deg])
        existing.setTooltipContent(asset.name)
      } else {
        const marker = L.marker([asset.lat_deg, asset.lon_deg], {icon: assetIcon(asset.category)}).addTo(mapInstance.current)
        marker.bindPopup(`<strong>${escapeHtml(asset.name)}</strong><br/><span style="opacity:.7">${escapeHtml(asset.category)}</span>${asset.description ? `<br/>${escapeHtml(asset.description)}` : ''}`)
        marker.bindTooltip(asset.name, {permanent: true, direction: 'right', offset: [10, 0], className: 'terminal-marker-label'})
        assetMarkersRef.current.set(asset.id, marker)
      }
    }
    for (const [id, marker] of assetMarkersRef.current) {
      if (!seen.has(id)) {
        marker.remove()
        assetMarkersRef.current.delete(id)
      }
    }
  }, [assets])

  // Zone circles — dashed, tier-colored geofences. Radius is drawn in
  // meters via L.circle (a true geographic circle, not a fixed pixel size).
  useEffect(() => {
    if (!mapInstance.current) return
    const seen = new Set<string>()
    for (const zone of zones) {
      seen.add(zone.id)
      const color = ZONE_TIER_COLOR[zone.tier]
      const existing = zoneLayersRef.current.get(zone.id)
      if (existing) {
        existing.setLatLng([zone.center_lat_deg, zone.center_lon_deg])
        existing.setRadius(zone.radius_m)
        existing.setStyle({color})
        existing.setTooltipContent(zone.name)
      } else {
        const circle = L.circle([zone.center_lat_deg, zone.center_lon_deg], {
          radius: zone.radius_m,
          color,
          weight: 2,
          dashArray: '6 4',
          fillColor: color,
          fillOpacity: 0.08,
        }).addTo(mapInstance.current)
        circle.bindPopup(`<strong>${escapeHtml(zone.name)}</strong><br/><span style="opacity:.7">${zone.tier.toUpperCase()} · ${Math.round(zone.radius_m)}m</span>${zone.description ? `<br/>${escapeHtml(zone.description)}` : ''}`)
        // Permanent label on the boundary, same treatment as entity/asset
        // markers — matches real TERMINAL's "Alpha Zona" label on its own
        // dashed geofence circle.
        circle.bindTooltip(zone.name, {permanent: true, direction: 'top', className: 'terminal-marker-label'})
        zoneLayersRef.current.set(zone.id, circle)
      }
    }
    for (const [id, circle] of zoneLayersRef.current) {
      if (!seen.has(id)) {
        circle.remove()
        zoneLayersRef.current.delete(id)
      }
    }
  }, [zones])

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

  useEffect(() => {
    let cancelled = false

    async function poll() {
      try {
        const res = await apiJson<{events: TerminalEvent[]}>('/api/terminal/events')
        if (!cancelled) setEvents(res.events)
      } catch (e) {
        if (!cancelled) notify.error(errorMessage(e))
      }
    }

    poll()
    const interval = setInterval(poll, 5000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  // Router connection state for the bottom status bar — same /api/status the
  // Dashboard page already polls, just the connected/endpoint fields.
  useEffect(() => {
    let cancelled = false

    async function poll() {
      try {
        const res = await apiJson<{connected: boolean; endpoint: string}>('/api/status')
        if (!cancelled) setRouterStatus({connected: res.connected, endpoint: res.endpoint})
      } catch {
        // Non-critical for this page — the Dashboard already surfaces a
        // connection-failure toast; no need to duplicate it here.
      }
    }

    poll()
    const interval = setInterval(poll, 10000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  const droneEntities = useMemo(() => entities.filter(e => e.kind === 'drone'), [entities])
  const airborneCount = useMemo(
    () => droneEntities.filter(e => e.status !== 'on_ground' && e.status !== 'offline').length,
    [droneEntities]
  )
  const onlineCount = useMemo(() => entities.filter(e => e.status !== 'offline').length, [entities])
  const activeAlertCount = alerts.filter(a => !dismissedAlerts.has(`${a.entity_id}-${a.ts}`)).length

  return (
    <Layout>
      <PageHeader title="Terminal" eyebrow="LIVE MAP" count={entities.length} countLabel="tracked" />
      <TerminalNav rightSlot={<TopStatusCluster alertCount={activeAlertCount} connected={routerStatus?.connected ?? false} />} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[240px_1fr_300px]">
        <div className="flex h-[60vh] flex-col gap-4">
          <Panel title="Fleet" badge={`${entities.length}`} className="flex-1" bodyClassName="flex flex-col overflow-hidden p-0">
            <p className="hud-label flex shrink-0 items-center justify-between border-b border-zinc-200 px-2 py-1.5 text-[10px] text-zinc-500 dark:border-white/10">
              <span>{airborneCount} airborne · {onlineCount}/{entities.length} online</span>
              <button type="button" onClick={() => setFleetSubTab('Assets')} className="text-accent-fill hover:underline">Manage assets</button>
            </p>
            <div className="min-h-0 flex-1 overflow-y-auto">
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
                    <th className="px-2 py-1.5 text-right font-medium">GS</th>
                    <th className="px-2 py-1.5 text-right font-medium" title="Not wired to a data source yet">LNK</th>
                    <th className="px-2 py-1.5 text-right font-medium" title="Not wired to a data source yet">SAT</th>
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
            </div>
          </Panel>
          <Panel title="" className="h-32" bodyClassName="p-0">
            <FleetSubTabs
              selected={selected}
              assets={assets}
              zones={zones}
              canCommand={canCommand}
              placing={placing}
              onStartPlacing={k => {
                setPlacing(k)
                setPendingPoint(null)
              }}
              onCancelPlacing={() => {
                setPlacing(null)
                setPendingPoint(null)
              }}
              onDelete={deleteMarker}
              tab={fleetSubTab}
              onTabChange={setFleetSubTab}
            />
          </Panel>
          {pendingPoint && placing && (
            <NewMarkerForm
              kind={placing}
              point={pendingPoint}
              onCancel={() => setPendingPoint(null)}
              onSubmitAsset={createAsset}
              onSubmitZone={createZone}
            />
          )}
        </div>
        <Panel title="Map" className="h-[60vh]" bodyClassName="p-0">
          <div ref={mapRef} className="terminal-map h-full w-full" />
        </Panel>
        <div className="flex h-[60vh] flex-col gap-4">
          <Panel title="Video" className="h-40" bodyClassName="p-0" badge={<VideoHeaderPlus />}>
            <VideoPanel />
          </Panel>
          <Panel title="Telemetry" className="flex-1">
            <DetailsPanel entity={selected} canCommand={canCommand} ack={ack} sending={sending} onSend={sendCommand} />
          </Panel>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[1fr_320px]">
        <Panel title="Events" badge={`${filteredEvents.length}/${events.length}`} className="h-64" bodyClassName="flex flex-col overflow-hidden p-0">
          <EventsToolbar
            activeSeverities={activeSeverities}
            onToggleSeverity={toggleSeverity}
            filterText={eventFilterText}
            onFilterText={setEventFilterText}
          />
          <div className="min-h-0 flex-1 overflow-y-auto">
            {events.length === 0 ? (
              <p className="p-3 text-xs text-zinc-500">No events yet.</p>
            ) : filteredEvents.length === 0 ? (
              <p className="p-3 text-xs text-zinc-500">No events match this filter.</p>
            ) : (
              filteredEvents.map(event => <EventRow key={`${event.entity_id}-${event.ts}-${event.kind}`} event={event} />)
            )}
          </div>
        </Panel>
        <Panel
          title="Alerts"
          badge={`${alerts.filter(a => !dismissedAlerts.has(`${a.entity_id}-${a.ts}`)).length} active`}
          className="h-64"
          bodyClassName="overflow-y-auto p-0"
        >
          <AlertsPanel
            alerts={alerts}
            dismissed={dismissedAlerts}
            onDismiss={key => setDismissedAlerts(prev => new Set(prev).add(key))}
          />
        </Panel>
      </div>

      <div className="mt-4">
        <TimelineStrip events={events} windowMs={timelineWindowMs} onWindowChange={setTimelineWindowMs} />
      </div>

      {/* Real TERMINAL's bottom status bar shows a render-loop FPS counter
          too — omitted here since this page has no such loop to measure
          (a REST-polled Leaflet map, not a continuously-rendered canvas). */}
      <div className="hud-label mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-zinc-200 px-1 py-2 text-[10px] text-zinc-500 dark:border-white/10">
        <span className="flex items-center gap-1.5">
          <span
            className="h-1.5 w-1.5 rounded-full"
            style={{background: routerStatus?.connected ? '#22c55e' : '#71717a'}}
          />
          {routerStatus?.connected ? `LIVE - ${routerStatus.endpoint}` : 'DISCONNECTED'}
        </span>
        <span>{selected ? `${selected.callsign} · ${selected.kind[0].toUpperCase()}${selected.kind.slice(1)}` : 'no selection'}</span>
        <span className="ml-auto">{role ?? 'guest'} · {username ?? '—'}</span>
        <span>AIR {airborneCount}/{droneEntities.length}</span>
        <span>TRK {entities.length}</span>
        <span>ALR {activeAlertCount}</span>
      </div>

      <div className="mt-8">
        <StreamsPanel />
      </div>
    </Layout>
  )
}
