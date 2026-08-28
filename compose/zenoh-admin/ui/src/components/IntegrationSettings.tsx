import {useEffect, useMemo, useState} from 'react'
import {apiFetch, apiJson, errorDetail, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'
import {Card, CardHeader} from '@/components/ui/card'
import {
  ChevronDown, ChevronRight, Download, KeyRound, MapPinned, Radar, Router, Save,
  Settings2, ShieldCheck, Terminal, Upload, UploadCloud, Video, Waypoints, Wifi, Wrench,
} from 'lucide-react'

// Service `.env` editor. Lives on the Config tab alongside the router config:
// this edits native-process environment (endpoints, ports, credentials), while
// Zenoh Config edits the router itself. Deliberately fetches on mount and after
// a save only — no polling — so a background refresh never clobbers a field the
// operator is mid-edit.
//
// Groups render as collapsed-by-default accordion cards (one per integration)
// instead of one long always-open form — with 40+ fields across 7 systems,
// showing everything at once made it unclear which field belonged to which
// system. `help` gives per-field guidance for values that aren't obvious from
// the label alone (where to find it, what format is expected).

type ConfigValue = string | { configured: boolean }

type RuntimeConfig = {
  config: Record<string, ConfigValue>
  editable_keys: string[]
}

type Field = { key: string; label: string; secret?: boolean; placeholder?: string; help?: string }
type FieldGroup = { title: string; icon: typeof Router; description: string; fields: Field[] }

const FIELD_GROUPS: FieldGroup[] = [
  { title: 'Router and namespace', icon: Router, description: 'Native-process endpoint and namespace defaults. Use Zenoh Config for live router listener changes.', fields: [
    { key: 'ZENOH_LOCAL_ENDPOINT', label: 'Local Zenoh endpoint', placeholder: 'tcp/127.0.0.1:7448', help: 'Where native processes on this box (bridges, layers) reach the local router — normally the plaintext loopback listener, not the mTLS one.' },
    { key: 'PARTNER_NAMESPACE', label: 'Partner namespace', placeholder: 'partner-a', help: 'This pod’s slot name under the shared namespace prefix. Set once during bootstrap; changing it live requires a router restart.' },
    { key: 'ZENOH_LOG', label: 'Router log level (restart required)', placeholder: 'info', help: 'One of: error, warn, info, debug, trace. Takes effect on the router’s next restart.' },
  ] },
  { title: 'Fabric presets', icon: Waypoints, description: 'One-click "Switch fabric" options shown on the Zenoh Config page. Changing these changes what appears as a pill there, not the currently active connection.', fields: [
    { key: 'EFDI_LOCAL_FABRIC_LABEL', label: 'Local fabric — preset label', placeholder: 'Local fabric', help: 'Display name for the pill button on the Zenoh Config page.' },
    { key: 'EFDI_LOCAL_FABRIC_ENDPOINTS', label: 'Local fabric — endpoints (JSON array)', placeholder: '["tls/zenoh1.efdi.ltu:7447"]', help: 'A JSON array of strings, even for one endpoint — e.g. ["tls/zenoh1.efdi.ltu:7447"]. Not a bare hostname.' },
    { key: 'EFDI_LOCAL_FABRIC_PROFILE', label: 'Local fabric — TLS profile', placeholder: 'ltu-local', help: 'Must match one of the named TLS profiles on the Zenoh Config page (e.g. efdi, backbone, ltu-local) — not a file path.' },
    { key: 'EFDI_BACKBONE_FABRIC_LABEL', label: 'Backbone fabric — preset label', placeholder: 'Backbone' },
    { key: 'EFDI_BACKBONE_FABRIC_ENDPOINTS', label: 'Backbone fabric — endpoints (JSON array)', placeholder: '["tls/backbone.efdi.ltu:7447"]', help: 'Same JSON-array format as the local fabric endpoints above.' },
    { key: 'EFDI_BACKBONE_FABRIC_PROFILE', label: 'Backbone fabric — TLS profile', placeholder: 'backbone' },
  ] },
  { title: 'TAK and CoT', icon: MapPinned, description: 'Authenticated CoT feed to the TAK Server over mTLS.', fields: [
    { key: 'TAK_HOST', label: 'TAK Server hostname / IP', placeholder: 'tak.efdi.ltu' },
    { key: 'TAK_HOST_FALLBACK', label: 'TAK fallback hostname / IP', help: 'Tried if the primary host is unreachable — leave blank if there is only one path to the TAK Server.' },
    { key: 'TAK_HOST_TAILSCALE', label: 'TAK Tailscale fallback IP', placeholder: '100.x.x.x', help: 'Tried last, over the Tailscale/NetBird mesh — only relevant if this deployment has a mesh VPN configured.' },
    { key: 'TAK_PORT', label: 'TAK Server TLS port', placeholder: '8089' },
    { key: 'TAK_TLS', label: 'TAK TLS enabled (1/0)', placeholder: '1' },
    { key: 'TAK_TLS_SERVER_NAME', label: 'TAK certificate DNS name', placeholder: 'takserver', help: 'The name on the TAK Server’s certificate (SAN), for hostname verification — not necessarily the same as TAK_HOST.' },
  ] },
  { title: 'SitaWare HQ', icon: ShieldCheck, description: 'REST import (sitaware_bridge) and NVG 2.0.2 export feed (sitaware_layer). Passwords are write-only.', fields: [
    { key: 'SITAWARE_URL', label: 'SitaWare HQ base URL', placeholder: 'https://swhq.efdi.ltu:10006', help: 'Inbound: EFDI polls this URL for friendly-force units over REST.' },
    { key: 'SITAWARE_URL_FALLBACK', label: 'SitaWare HQ fallback URL', placeholder: 'https://swhq.efdi.ltu:10006' },
    { key: 'SITAWARE_URL_TAILSCALE', label: 'SitaWare HQ Tailscale fallback URL', placeholder: 'https://100.x.x.x:10006' },
    { key: 'SITAWARE_USER', label: 'SitaWare HQ username' }, { key: 'SITAWARE_PASS', label: 'SitaWare HQ password', secret: true },
    { key: 'SITAWARE_TLS_VERIFY', label: 'Verify SitaWare HQ TLS certificate (1/0)', placeholder: '1' },
    { key: 'SITAWARE_DISCOVER', label: 'Auto-discover SitaWare HQ endpoint (1/0)', placeholder: '0' },
    { key: 'SITAWARE_API_PATH', label: 'REST friendly-force path (sitaware_bridge)', placeholder: '/deployment-specific/path', help: 'The exact resource path this HQ installation exposes — varies per deployment, confirm with the HQ administrator before guessing.' },
    { key: 'SITAWARE_POLL_S', label: 'REST poll seconds', placeholder: '10' },
    { key: 'SITAWARE_HQ_NVG_ENABLE', label: 'NVG feed enabled — Zenoh → HQ polls (sitaware_layer) (1/0)', placeholder: '1', help: 'Outbound direction: turns on the read-only HTTP(S) feed that SitaWare HQ itself polls. There is no separate inbound NVG-XML path — inbound always goes through the REST fields above.' },
    { key: 'SITAWARE_HQ_NVG_BIND', label: 'NVG feed bind address', placeholder: '0.0.0.0' },
    { key: 'SITAWARE_HQ_NVG_PORT', label: 'NVG feed port', placeholder: '8088' },
    { key: 'SITAWARE_HQ_NVG_PATH', label: 'NVG feed path', placeholder: '/nvg' },
    { key: 'SITAWARE_HQ_NVG_USER', label: 'NVG feed username', help: 'A dedicated feed-only account — do not reuse a Keycloak login here.' }, { key: 'SITAWARE_HQ_NVG_PASS', label: 'NVG feed password', secret: true },
    { key: 'SITAWARE_HQ_NVG_TLS_CERT', label: 'NVG feed TLS certificate path', placeholder: '/path/to/server-cert.pem' },
    { key: 'SITAWARE_HQ_NVG_TLS_KEY', label: 'NVG feed TLS key path', placeholder: '/path/to/server-key.pem', secret: true },
    { key: 'SITAWARE_HQ_NVG_STALE_S', label: 'NVG feed staleness threshold (seconds)', placeholder: '120' },
    { key: 'SITAWARE_HQ_NVG_MAX_TRACKS', label: 'NVG feed max tracked entities', placeholder: '10000' },
    { key: 'SITAWARE_HQ_NVG_ALLOW_ANONYMOUS', label: 'NVG feed allow anonymous access (1/0)', placeholder: '0' },
    { key: 'SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP', label: 'NVG feed allow plain HTTP — isolated lab only (1/0)', placeholder: '0' },
  ] },
  { title: 'Video and metadata ingest', icon: Video, description: 'SRT/KLV sources and source naming for the STANAG 4609 bridge.', fields: [
    { key: 'STANAG4609_SRT_URL', label: 'STANAG 4609 SRT URL', placeholder: 'srt://host:port?mode=listener' },
    { key: 'STANAG4609_SOURCE', label: 'STANAG 4609 source label', placeholder: 'stanag_4609' },
  ] },
  { title: 'MQTT sensor feeds', icon: Wifi, description: 'MQTT broker ingress. The MQTT bridge forwards payloads verbatim; the mqtt translator reads JSON only.', fields: [
    { key: 'MQTT_HOST', label: 'MQTT broker host', placeholder: 'broker.example' },
    { key: 'MQTT_PORT', label: 'MQTT broker port', placeholder: '1883' },
    { key: 'MQTT_TOPIC', label: 'MQTT subscription filters', placeholder: 'sensors/#' },
    { key: 'MQTT_USER', label: 'MQTT username' },
    { key: 'MQTT_PASS', label: 'MQTT password', secret: true },
    { key: 'MQTT_TLS', label: 'MQTT TLS enabled (1/0)', placeholder: '0' },
  ] },
  { title: 'Sensors and data sources', icon: Radar, description: 'Common partner endpoints. Protocol-specific CAT and raw-port settings are available under Advanced.', fields: [
    { key: 'ASTERIX_ZENOH_UPSTREAM_ENDPOINT', label: 'ASTERIX upstream Zenoh endpoint', placeholder: 'tcp/zenoh2.example:7448' },
    { key: 'ASTERIX_ZENOH_UPSTREAM_ROOT', label: 'ASTERIX upstream topic root', placeholder: 'partner/org' },
    { key: 'UDP_INGRESS_PORT', label: 'Generic UDP ingress port', placeholder: '50000', help: 'One shared port for all ASTERIX categories — each datagram is self-framed (category + length) and demuxed automatically. Pair with the categories field to its right.' },
    { key: 'ASTERIX_CATEGORIES', label: 'ASTERIX auto-dispatch categories', placeholder: '34,48', help: 'Comma-separated category numbers to decode off the shared ingress port above — e.g. 10,20,21,34,48,62.' },
    { key: 'CAT10_PORT', label: 'CAT-010 port', placeholder: '50010', help: 'Only needed if this category has its own dedicated port instead of using the shared ingress port above.' }, { key: 'CAT21_PORT', label: 'CAT-021 port', placeholder: '50021' },
    { key: 'CAT34_PORT', label: 'CAT-034 port', placeholder: '50034' }, { key: 'CAT48_PORT', label: 'CAT-048 port', placeholder: '50048' },
    { key: 'CAT62_PORT', label: 'CAT-062 port', placeholder: '50062' },
  ] },
]

const KNOWN_KEYS = new Set(FIELD_GROUPS.flatMap(group => group.fields.map(field => field.key)))

function groupConfiguredCount(group: FieldGroup, values: Record<string, string>, secrets: Record<string, boolean>): number {
  return group.fields.filter(f => (values[f.key]?.trim() ? true : false) || secrets[f.key]).length
}

export function IntegrationSettings() {
  const { role } = useAuth()
  const canWrite = role === 'superadmin'
  const [values, setValues] = useState<Record<string, string>>({})
  const [secrets, setSecrets] = useState<Record<string, boolean>>({})
  const [editableKeys, setEditableKeys] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [advancedKey, setAdvancedKey] = useState('')
  const [advancedValue, setAdvancedValue] = useState('')
  const [takCaFile, setTakCaFile] = useState<File | null>(null)
  const [takCertFile, setTakCertFile] = useState<File | null>(null)
  const [takKeyFile, setTakKeyFile] = useState<File | null>(null)
  const [takZipFile, setTakZipFile] = useState<File | null>(null)
  const [takUploading, setTakUploading] = useState(false)
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set())

  function toggleGroup(title: string) {
    setOpenGroups(current => {
      const next = new Set(current)
      if (next.has(title)) next.delete(title); else next.add(title)
      return next
    })
  }

  async function load() {
    try {
      const data = await apiJson<RuntimeConfig>('/api/runtime')
      setEditableKeys(data.editable_keys ?? [])
      const nextValues: Record<string, string> = {}
      const nextSecrets: Record<string, boolean> = {}
      for (const [key, value] of Object.entries(data.config)) {
        if (typeof value === 'string') nextValues[key] = value
        else nextSecrets[key] = value.configured
      }
      setValues(nextValues)
      setSecrets(nextSecrets)
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  useEffect(() => { void load() }, [])

  function setValue(key: string, value: string) { setValues(current => ({ ...current, [key]: value })) }

  async function saveConfig() {
    if (!canWrite) return
    setSaving(true)
    try {
      const payload: Record<string, string> = { ...values }
      for (const [key, configured] of Object.entries(secrets)) if (configured && !values[key]) delete payload[key]
      await apiJson('/api/runtime/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values: payload }) })
      notify.success('Runtime settings saved. Restart affected services to apply them.')
      await load()
    } catch (e) { notify.error(errorMessage(e)) } finally { setSaving(false) }
  }

  function exportProfile() {
    const configuredSecrets = Object.keys(secrets).filter(key => secrets[key])
    const profile = {
      exported_at: new Date().toISOString(),
      values,
      // Secret values are write-only and never leave the server, so a
      // reimport of this file still needs these re-entered by hand.
      secrets_not_included: configuredSecrets,
    }
    const blob = new Blob([JSON.stringify(profile, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'efdi-integration-settings.json'
    a.click()
    URL.revokeObjectURL(url)
  }

  async function importProfile(file: File) {
    if (!canWrite) return
    try {
      const text = await file.text()
      const parsed = JSON.parse(text)
      const incoming: Record<string, unknown> = (parsed && typeof parsed === 'object' && parsed.values && typeof parsed.values === 'object')
        ? parsed.values
        : parsed
      const allowedKeys = new Set([...KNOWN_KEYS, ...editableKeys])
      let applied = 0
      let skipped = 0
      setValues(current => {
        const next = { ...current }
        for (const [key, value] of Object.entries(incoming ?? {})) {
          if (typeof value !== 'string' || !allowedKeys.has(key)) { skipped += 1; continue }
          next[key] = value
          applied += 1
        }
        return next
      })
      if (applied === 0) throw new Error('No recognized settings found in that file')
      notify.success(`Loaded ${applied} setting${applied === 1 ? '' : 's'} from file${skipped ? ` (${skipped} skipped)` : ''}. Review secrets, then Save settings.`)
    } catch (e) {
      notify.error(errorMessage(e))
    }
  }

  async function uploadTakPackage() {
    if (!canWrite || (!takCaFile && !takCertFile && !takKeyFile && !takZipFile)) return
    setTakUploading(true)
    try {
      const form = new FormData()
      if (takCaFile) form.append('ca_root', takCaFile)
      if (takCertFile) form.append('certificate', takCertFile)
      if (takKeyFile) form.append('private_key', takKeyFile)
      if (takZipFile) form.append('service_package', takZipFile)
      const response = await apiFetch('/api/integrations/tak', { method: 'POST', body: form })
      const body = await response.json().catch(() => ({ detail: response.statusText }))
      if (!response.ok) throw new Error(errorDetail(body, response))
      notify.success('TAK client credentials uploaded. Restart the TAK bridge to apply them.')
      setTakCaFile(null); setTakCertFile(null); setTakKeyFile(null); setTakZipFile(null)
      await load()
    } catch (e) {
      notify.error(errorMessage(e))
    } finally {
      setTakUploading(false)
    }
  }

  function addAdvanced() {
    const key = advancedKey.trim().toUpperCase()
    if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(key) || (!key.startsWith('CAT') && !key.startsWith('NFFI_') && !KNOWN_KEYS.has(key) && !editableKeys.includes(key))) {
      notify.error('Use an allowed protocol setting such as CAT48_TCP or NFFI_INPUT_TOPIC')
      return
    }
    setValue(key, advancedValue); setAdvancedKey(''); setAdvancedValue('')
  }

  const configuredKeys = useMemo(() => {
    return Array.from(new Set([...Object.keys(values), ...Object.keys(secrets)]))
      .filter(key => !KNOWN_KEYS.has(key))
      .sort()
  }, [values, secrets])

  function renderSetting(key: string) {
    const secret = secrets[key] !== undefined
    return <label key={key} className="space-y-1">
      <span className="flex items-center gap-1 text-xs text-zinc-600 dark:text-zinc-400">
        <span className="font-mono">{key}</span>
        {secret && <KeyRound size={11} className="text-amber-500" />}
        {secret && secrets[key] && <span className="ml-auto text-[10px] text-emerald-500">configured</span>}
      </span>
      <input
        type={secret ? 'password' : 'text'}
        disabled={!canWrite}
        value={values[key] ?? ''}
        onChange={e => setValue(key, e.target.value)}
        placeholder={secret && secrets[key] ? 'Leave blank to keep current value' : ''}
        className="w-full rounded-md border border-zinc-300 bg-zinc-100 px-2.5 py-2 font-mono text-xs text-zinc-900 focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-50 dark:border-white/10 dark:bg-[#141416] dark:text-white"
      />
    </label>
  }

  const allOpen = openGroups.size === FIELD_GROUPS.length

  return (
    <Card className="p-5">
      <CardHeader
        label={null}
        title={<><Settings2 size={16} className="text-accent-ring" /> Integration settings</>}
        action={
          <div className="flex shrink-0 items-center gap-2">
            <button onClick={exportProfile} title="Download the current non-secret settings as a JSON file" className="flex items-center gap-2 rounded-md border border-zinc-300 px-3 py-2 text-xs text-zinc-600 dark:border-white/10 dark:text-zinc-300"><Download size={13} /> Export</button>
            <label title="Load settings from a previously exported JSON file" className={`flex items-center gap-2 rounded-md border border-zinc-300 px-3 py-2 text-xs text-zinc-600 dark:border-white/10 dark:text-zinc-300 ${canWrite ? 'cursor-pointer' : 'cursor-not-allowed opacity-50'}`}>
              <Upload size={13} /> Import
              <input type="file" accept=".json" disabled={!canWrite} className="hidden"
                onChange={e => { const f = e.target.files?.[0]; if (f) void importProfile(f); e.target.value = '' }} />
            </label>
            <button disabled={!canWrite || saving} onClick={saveConfig} className="flex items-center gap-2 rounded-md bg-accent-fill px-3 py-2 text-xs text-accent-text disabled:opacity-50"><Save size={13} /> {saving ? 'Saving…' : 'Save settings'}</button>
          </div>
        }
      >
        <p className="text-xs text-zinc-500">
          Each card below is one integration — expand only the one you're setting up. Edit endpoints, ports, topics,
          and credentials without opening `.env` over SSH. Export/import a profile to reuse a known-good setup on
          another install.
        </p>
        <button
          onClick={() => setOpenGroups(allOpen ? new Set() : new Set(FIELD_GROUPS.map(g => g.title)))}
          className="mt-2 self-start text-[11px] text-accent-ring underline-offset-2 hover:underline"
        >
          {allOpen ? 'Collapse all' : 'Expand all'}
        </button>
      </CardHeader>

      <div className="space-y-3">
        {FIELD_GROUPS.map(groupDef => {
          const Icon = groupDef.icon
          const isOpen = openGroups.has(groupDef.title)
          const configuredCount = groupConfiguredCount(groupDef, values, secrets)
          return (
            <div key={groupDef.title} className="rounded-md border border-zinc-200 dark:border-white/10">
              <button
                onClick={() => toggleGroup(groupDef.title)}
                className="flex w-full items-center gap-3 px-3 py-2.5 text-left"
                aria-expanded={isOpen}
              >
                {isOpen ? <ChevronDown size={15} className="shrink-0 text-zinc-400" /> : <ChevronRight size={15} className="shrink-0 text-zinc-400" />}
                <Icon size={15} className="shrink-0 text-accent-ring" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="hud-label text-[11px] font-semibold text-zinc-700 dark:text-zinc-200">{groupDef.title}</span>
                    {configuredCount > 0 && (
                      <span className="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-600 dark:text-emerald-400">
                        {configuredCount} set
                      </span>
                    )}
                  </div>
                  {!isOpen && <p className="mt-0.5 truncate text-[11px] text-zinc-500">{groupDef.description}</p>}
                </div>
              </button>
              {isOpen && (
                <div className="border-t border-zinc-200 px-3 pb-3 pt-3 dark:border-white/10">
                  <p className="mb-3 text-[11px] text-zinc-500">{groupDef.description}</p>
                  <div className="grid gap-3 sm:grid-cols-2">
                    {groupDef.fields.map(field => {
                      const secret = field.secret || secrets[field.key] !== undefined
                      return (
                        <label key={field.key} className="space-y-1">
                          <span className="flex items-center gap-1 text-xs text-zinc-600 dark:text-zinc-400">
                            {field.label}
                            {secret && <KeyRound size={11} className="text-amber-500" />}
                            {secret && secrets[field.key] && <span className="ml-auto text-[10px] text-emerald-500">configured</span>}
                          </span>
                          <input
                            type={secret ? 'password' : 'text'}
                            disabled={!canWrite}
                            value={values[field.key] ?? ''}
                            onChange={e => setValue(field.key, e.target.value)}
                            placeholder={secret && secrets[field.key] ? 'Leave blank to keep current value' : field.placeholder}
                            className="w-full rounded-md border border-zinc-300 bg-zinc-100 px-2.5 py-2 font-mono text-xs text-zinc-900 focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-50 dark:border-white/10 dark:bg-[#141416] dark:text-white"
                          />
                          {field.help && <span className="block text-[10.5px] leading-snug text-zinc-500">{field.help}</span>}
                        </label>
                      )
                    })}
                  </div>
                  {groupDef.title === 'TAK and CoT' && (
                    <div className="mt-3 rounded-md border border-zinc-200 p-3 dark:border-white/10">
                      <p className="mb-2 text-[11px] text-zinc-500">
                        TAK Server client credentials (mTLS), generated via <code>make add-service NAME=efdi-pod</code>
                        in the TAK repo (writes <code>certs/efdi-pod/{'{ca,cert,key}'}.pem</code>). Two ways to upload —
                        pick either one, not both.
                      </p>
                      <p className="mb-1.5 text-[10.5px] font-medium text-zinc-500">Option A — one file at a time</p>
                      <div className="grid gap-3 sm:grid-cols-3">
                        <label className="text-xs text-zinc-500">
                          CA root
                          <input type="file" disabled={!canWrite} accept=".pem,.crt,.cer"
                            onChange={e => setTakCaFile(e.target.files?.[0] ?? null)}
                            className="mt-1 block w-full text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 disabled:opacity-50 dark:file:bg-white/10 dark:file:text-white" />
                        </label>
                        <label className="text-xs text-zinc-500">
                          Certificate
                          <input type="file" disabled={!canWrite} accept=".pem,.crt,.cer"
                            onChange={e => setTakCertFile(e.target.files?.[0] ?? null)}
                            className="mt-1 block w-full text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 disabled:opacity-50 dark:file:bg-white/10 dark:file:text-white" />
                        </label>
                        <label className="text-xs text-zinc-500">
                          Private key
                          <input type="file" disabled={!canWrite} accept=".pem,.key"
                            onChange={e => setTakKeyFile(e.target.files?.[0] ?? null)}
                            className="mt-1 block w-full text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 disabled:opacity-50 dark:file:bg-white/10 dark:file:text-white" />
                        </label>
                      </div>
                      <p className="mb-1.5 mt-3 text-[10.5px] font-medium text-zinc-500">
                        Option B — zip the whole <code>certs/efdi-pod/</code> folder and upload it as one file
                      </p>
                      <label className="text-xs text-zinc-500">
                        Service package (.zip)
                        <input type="file" disabled={!canWrite} accept=".zip"
                          onChange={e => setTakZipFile(e.target.files?.[0] ?? null)}
                          className="mt-1 block w-full max-w-xs text-xs text-zinc-500 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-200 file:px-3 file:py-1.5 file:text-xs file:text-zinc-900 disabled:opacity-50 dark:file:bg-white/10 dark:file:text-white" />
                      </label>
                      <button
                        disabled={!canWrite || takUploading || (!takCaFile && !takCertFile && !takKeyFile && !takZipFile)}
                        onClick={uploadTakPackage}
                        className="mt-3 flex items-center gap-2 rounded-md border border-accent-ring/50 px-3 py-1.5 text-xs text-accent-ring disabled:opacity-40"
                      >
                        <UploadCloud size={13} /> {takUploading ? 'Uploading…' : 'Upload TAK package'}
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <div className="mt-5 border-t border-zinc-200 pt-4 dark:border-white/10"><h3 className="hud-label flex items-center gap-2 text-[11px] font-semibold text-zinc-600 dark:text-zinc-300"><Wrench size={13} /> Advanced protocol setting</h3><p className="mb-2 mt-1 text-[11px] text-zinc-500">Use this for CATxx_TCP, raw ports, input topics, and other documented `.env` fields.</p><div className="flex gap-2"><input value={advancedKey} onChange={e => setAdvancedKey(e.target.value)} placeholder="CAT48_TCP" className="w-32 rounded-md border border-zinc-300 bg-zinc-100 px-2.5 py-2 font-mono text-xs dark:border-white/10 dark:bg-[#141416]" /><input value={advancedValue} onChange={e => setAdvancedValue(e.target.value)} placeholder="value" className="min-w-0 flex-1 rounded-md border border-zinc-300 bg-zinc-100 px-2.5 py-2 font-mono text-xs dark:border-white/10 dark:bg-[#141416]" /><button disabled={!canWrite} onClick={addAdvanced} className="rounded-md border border-accent-ring/50 px-3 text-xs text-accent-ring disabled:opacity-40">Add</button></div></div>
      {configuredKeys.length > 0 && <div className="mt-5 border-t border-zinc-200 pt-4 dark:border-white/10"><h3 className="hud-label text-[11px] font-semibold text-zinc-600 dark:text-zinc-300">Additional deployment settings</h3><p className="mb-3 mt-1 text-[11px] text-zinc-500">These fields are present in the deployment environment but are not tied to a fixed partner form.</p><div className="grid gap-3 sm:grid-cols-2">{configuredKeys.map(renderSetting)}</div></div>}
      <div className="mt-5 rounded-md border border-amber-500/20 bg-amber-500/10 p-3 text-[11px] text-amber-700 dark:text-amber-300"><Terminal size={13} className="mb-1" />Saving changes updates the deployment environment. Restart only the affected service after checking its log; secrets are write-only and never displayed.</div>
    </Card>
  )
}
