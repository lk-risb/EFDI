import {useEffect, useMemo, useState} from 'react'
import {apiFetch, apiJson, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'
import {HudCorners} from '@/components/HudCorners'
import {KeyRound, Save, Settings2, Terminal, UploadCloud, Wrench} from 'lucide-react'

// Service `.env` editor. Lives on the Config tab alongside the router config:
// this edits native-process environment (endpoints, ports, credentials), while
// Zenoh Config edits the router itself. Deliberately fetches on mount and after
// a save only — no polling — so a background refresh never clobbers a field the
// operator is mid-edit.

type ConfigValue = string | { configured: boolean }

type RuntimeConfig = {
  config: Record<string, ConfigValue>
  editable_keys: string[]
}

const FIELD_GROUPS: { title: string; description: string; fields: { key: string; label: string; secret?: boolean; placeholder?: string }[] }[] = [
  { title: 'Router and namespace', description: 'Native-process endpoint and namespace defaults. Use Zenoh Config for live router listener changes.', fields: [
    { key: 'ZENOH_LOCAL_ENDPOINT', label: 'Local Zenoh endpoint', placeholder: 'tcp/127.0.0.1:7448' },
    { key: 'PARTNER_NAMESPACE', label: 'Partner namespace', placeholder: 'partner-a' },
  ] },
  { title: 'TAK and CoT', description: 'Authenticated CoT feed to the TAK Server over mTLS.', fields: [
    { key: 'TAK_HOST', label: 'TAK Server hostname / IP', placeholder: 'tak.efdi.ltu' },
    { key: 'TAK_HOST_FALLBACK', label: 'TAK fallback hostname / IP' },
    { key: 'TAK_HOST_TAILSCALE', label: 'TAK Tailscale fallback IP', placeholder: '100.x.x.x' },
    { key: 'TAK_PORT', label: 'TAK Server TLS port', placeholder: '8089' },
    { key: 'TAK_TLS', label: 'TAK TLS enabled (1/0)', placeholder: '1' },
    { key: 'TAK_TLS_SERVER_NAME', label: 'TAK certificate DNS name', placeholder: 'takserver' },
  ] },
  { title: 'SitaWare HQ', description: 'Bidirectional HQ integration over NVG 2.0.2 and REST. Passwords are write-only.', fields: [
    { key: 'SITAWARE_URL', label: 'SitaWare HQ base URL', placeholder: 'https://swhq.efdi.ltu:10006' },
    { key: 'SITAWARE_URL_FALLBACK', label: 'SitaWare HQ fallback URL', placeholder: 'https://swhq.efdi.ltu:10006' },
    { key: 'SITAWARE_URL_TAILSCALE', label: 'SitaWare HQ Tailscale fallback URL', placeholder: 'https://100.x.x.x:10006' },
    { key: 'SITAWARE_USER', label: 'SitaWare HQ username' }, { key: 'SITAWARE_PASS', label: 'SitaWare HQ password', secret: true },
    { key: 'SITAWARE_NVG_IMPORT_URL', label: 'NVG import URL — HQ export → Zenoh (nvg_bridge)', placeholder: 'https://host:10006/.../nvg' },
    { key: 'SITAWARE_NVG_IMPORT_CA', label: 'NVG import pinned CA path', placeholder: '/.../sitaware-hq-server.pem' },
    { key: 'SITAWARE_NVG_IMPORT_POLL_S', label: 'NVG import poll seconds', placeholder: '10' },
    { key: 'SITAWARE_HQ_NVG_PORT', label: 'NVG feed port — Zenoh → HQ polls (nvg_layer)', placeholder: '8088' },
    { key: 'SITAWARE_HQ_NVG_BIND', label: 'NVG feed bind address', placeholder: '0.0.0.0' },
    { key: 'SITAWARE_HQ_NVG_USER', label: 'NVG feed username' }, { key: 'SITAWARE_HQ_NVG_PASS', label: 'NVG feed password', secret: true },
    { key: 'SITAWARE_API_PATH', label: 'REST friendly-force path (sitaware_bridge)', placeholder: '/deployment-specific/path' },
    { key: 'SITAWARE_POLL_S', label: 'REST poll seconds', placeholder: '10' },
  ] },
  { title: 'Video and metadata ingest', description: 'SRT/KLV sources and source naming for the STANAG 4609 bridge.', fields: [
    { key: 'STANAG4609_SRT_URL', label: 'STANAG 4609 SRT URL', placeholder: 'srt://host:port?mode=listener' },
    { key: 'STANAG4609_SOURCE', label: 'STANAG 4609 source label', placeholder: 'stanag_4609' },
  ] },
  { title: 'MQTT and IoT sensor feeds', description: 'MQTT broker ingress and OGC SensorThings polling. The MQTT bridge forwards payloads verbatim; the mqtt translator reads JSON only.', fields: [
    { key: 'MQTT_HOST', label: 'MQTT broker host', placeholder: 'broker.example' },
    { key: 'MQTT_PORT', label: 'MQTT broker port', placeholder: '1883' },
    { key: 'MQTT_TOPIC', label: 'MQTT subscription filters', placeholder: 'sensors/#' },
    { key: 'MQTT_USER', label: 'MQTT username' },
    { key: 'MQTT_PASS', label: 'MQTT password', secret: true },
    { key: 'MQTT_TLS', label: 'MQTT TLS enabled (1/0)', placeholder: '0' },
    { key: 'SENSORTHINGS_URL', label: 'SensorThings service root', placeholder: 'https://host/FROST-Server/v1.1' },
    { key: 'SENSORTHINGS_POLL_S', label: 'SensorThings poll seconds', placeholder: '30' },
    { key: 'SENSORTHINGS_TOKEN', label: 'SensorThings bearer token', secret: true },
  ] },
  { title: 'Sensors and data sources', description: 'Common partner endpoints. Protocol-specific CAT and raw-port settings are available under Advanced.', fields: [
    { key: 'ASTERIX_ZENOH_UPSTREAM_ENDPOINT', label: 'ASTERIX upstream Zenoh endpoint', placeholder: 'tcp/zenoh2.example:7448' },
    { key: 'ASTERIX_ZENOH_UPSTREAM_ROOT', label: 'ASTERIX upstream topic root', placeholder: 'partner/org' },
    { key: 'UDP_INGRESS_PORT', label: 'Generic UDP ingress port', placeholder: '50000' }, { key: 'ASTERIX_CATEGORIES', label: 'ASTERIX auto-dispatch categories', placeholder: '34,48' },
    { key: 'CAT10_PORT', label: 'CAT-010 port', placeholder: '50010' }, { key: 'CAT21_PORT', label: 'CAT-021 port', placeholder: '50021' },
    { key: 'CAT34_PORT', label: 'CAT-034 port', placeholder: '50034' }, { key: 'CAT48_PORT', label: 'CAT-048 port', placeholder: '50048' },
    { key: 'CAT62_PORT', label: 'CAT-062 port', placeholder: '50062' },
  ] },
]

const KNOWN_KEYS = new Set(FIELD_GROUPS.flatMap(group => group.fields.map(field => field.key)))

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
  const [takUploading, setTakUploading] = useState(false)

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

  async function uploadTakPackage() {
    if (!canWrite || (!takCaFile && !takCertFile && !takKeyFile)) return
    setTakUploading(true)
    try {
      const form = new FormData()
      if (takCaFile) form.append('ca_root', takCaFile)
      if (takCertFile) form.append('certificate', takCertFile)
      if (takKeyFile) form.append('private_key', takKeyFile)
      const response = await apiFetch('/api/integrations/tak', { method: 'POST', body: form })
      const body = await response.json().catch(() => ({ detail: response.statusText }))
      if (!response.ok) throw new Error(body.detail ?? response.statusText)
      notify.success('TAK client credentials uploaded. Restart the TAK bridge to apply them.')
      setTakCaFile(null); setTakCertFile(null); setTakKeyFile(null)
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

  return (
    <section className="hud-frame hud-glass relative rounded-md border border-zinc-200 p-5 dark:border-white/10">
      <HudCorners />
      <div className="mb-4 flex items-start justify-between gap-3 border-b border-zinc-200 pb-4 dark:border-white/10"><div><h2 className="flex items-center gap-2 text-sm font-semibold"><Settings2 size={16} className="text-accent-ring" /> Integration settings</h2><p className="mt-1 text-xs text-zinc-500">Edit endpoints, ports, topics, and credentials without opening `.env` over SSH.</p></div><button disabled={!canWrite || saving} onClick={saveConfig} className="flex items-center gap-2 rounded-md bg-accent-fill px-3 py-2 text-xs text-accent-text disabled:opacity-50"><Save size={13} /> {saving ? 'Saving…' : 'Save settings'}</button></div>
      <div className="space-y-5">
        {FIELD_GROUPS.map(groupDef => (
          <div key={groupDef.title}>
            <h3 className="hud-label text-[11px] font-semibold text-zinc-600 dark:text-zinc-300">{groupDef.title}</h3>
            <p className="mb-3 mt-1 text-[11px] text-zinc-500">{groupDef.description}</p>
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
                  </label>
                )
              })}
            </div>
            {groupDef.title === 'TAK and CoT' && (
              <div className="mt-3 rounded-md border border-zinc-200 p-3 dark:border-white/10">
                <p className="mb-2 text-[11px] text-zinc-500">
                  TAK Server client credentials (mTLS). Upload the CA root, certificate, and private key
                  generated via <code>make add-service NAME=efdi-pod</code> in the TAK repo — each file is
                  optional and only the ones you select are replaced.
                </p>
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
                <button
                  disabled={!canWrite || takUploading || (!takCaFile && !takCertFile && !takKeyFile)}
                  onClick={uploadTakPackage}
                  className="mt-3 flex items-center gap-2 rounded-md border border-accent-ring/50 px-3 py-1.5 text-xs text-accent-ring disabled:opacity-40"
                >
                  <UploadCloud size={13} /> {takUploading ? 'Uploading…' : 'Upload TAK package'}
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="mt-5 border-t border-zinc-200 pt-4 dark:border-white/10"><h3 className="hud-label flex items-center gap-2 text-[11px] font-semibold text-zinc-600 dark:text-zinc-300"><Wrench size={13} /> Advanced protocol setting</h3><p className="mb-2 mt-1 text-[11px] text-zinc-500">Use this for CATxx_TCP, raw ports, input topics, and other documented `.env` fields.</p><div className="flex gap-2"><input value={advancedKey} onChange={e => setAdvancedKey(e.target.value)} placeholder="CAT48_TCP" className="w-32 rounded-md border border-zinc-300 bg-zinc-100 px-2.5 py-2 font-mono text-xs dark:border-white/10 dark:bg-[#141416]" /><input value={advancedValue} onChange={e => setAdvancedValue(e.target.value)} placeholder="value" className="min-w-0 flex-1 rounded-md border border-zinc-300 bg-zinc-100 px-2.5 py-2 font-mono text-xs dark:border-white/10 dark:bg-[#141416]" /><button disabled={!canWrite} onClick={addAdvanced} className="rounded-md border border-accent-ring/50 px-3 text-xs text-accent-ring disabled:opacity-40">Add</button></div></div>
      {configuredKeys.length > 0 && <div className="mt-5 border-t border-zinc-200 pt-4 dark:border-white/10"><h3 className="hud-label text-[11px] font-semibold text-zinc-600 dark:text-zinc-300">Additional deployment settings</h3><p className="mb-3 mt-1 text-[11px] text-zinc-500">These fields are present in the deployment environment but are not tied to a fixed partner form.</p><div className="grid gap-3 sm:grid-cols-2">{configuredKeys.map(renderSetting)}</div></div>}
      <div className="mt-5 rounded-md border border-amber-500/20 bg-amber-500/10 p-3 text-[11px] text-amber-700 dark:text-amber-300"><Terminal size={13} className="mb-1" />Saving changes updates the deployment environment. Restart only the affected service after checking its log; secrets are write-only and never displayed.</div>
    </section>
  )
}
