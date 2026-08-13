import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useMemo, useState} from 'react'
import {CheckCircle2, Plus, RadioTower, RefreshCw, Trash2} from 'lucide-react'
import {HudCorners} from '@/components/HudCorners'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {apiFetch, apiJson, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'

export const Route = createFileRoute('/topics')({
  beforeLoad: () => {
    if (!useAuth.getState().token) throw redirect({to: '/login'})
  },
  component: TopicsPage,
})

interface TopicRegistration {
  id: string
  key_expr: string
  encoding: string
  direction: 'publish' | 'subscribe' | 'bidirectional'
  description: string
  registered_by: string
  created_at: string
}

interface ObservedTopic {
  key_expr: string
  encoding: string
  first_seen: string
  last_seen: string
  sample_count: number
}

interface TopicsData {
  catalog_topic: string
  topics: TopicRegistration[]
  observed: ObservedTopic[]
}

interface TopicForm {
  key_expr: string
  encoding: string
  direction: TopicRegistration['direction']
  description: string
}

const emptyForm: TopicForm = {
  key_expr: '',
  encoding: 'application/json',
  direction: 'publish',
  description: '',
}

const inputClass = 'w-full rounded-md border border-zinc-300 bg-zinc-100 px-3 py-2 text-sm text-zinc-900 focus:outline-none focus:ring-2 focus:ring-accent-ring dark:border-white/10 dark:bg-[#141416] dark:text-white'
const cardClass = 'hud-card hud-glass hud-frame relative border border-zinc-200 p-5 dark:border-white/10'

function formatSeen(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function TopicsPage() {
  const role = useAuth(state => state.role)
  const canRegister = role === 'superadmin'
  const [data, setData] = useState<TopicsData | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState<TopicForm>(emptyForm)

  const registeredKeys = useMemo(
    () => new Set(data?.topics.map(topic => topic.key_expr) ?? []),
    [data],
  )

  async function load() {
    setLoading(true)
    try {
      setData(await apiJson<TopicsData>('/api/topics'))
    } catch (error) {
      notify.error(errorMessage(error))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  function prepareObserved(topic: ObservedTopic) {
    setForm({
      key_expr: topic.key_expr,
      encoding: topic.encoding.includes('/') ? topic.encoding : 'application/octet-stream',
      direction: 'subscribe',
      description: 'Observed live on this router',
    })
    window.scrollTo({top: 0, behavior: 'smooth'})
  }

  async function register(event: React.FormEvent) {
    event.preventDefault()
    setSaving(true)
    try {
      await apiJson<TopicRegistration>('/api/topics', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(form),
      })
      setForm(emptyForm)
      notify.success('Topic registered and catalog published')
      await load()
    } catch (error) {
      notify.error(errorMessage(error))
    } finally {
      setSaving(false)
    }
  }

  async function unregister(topic: TopicRegistration) {
    if (!window.confirm(`Unregister ${topic.key_expr}?`)) return
    try {
      const response = await apiFetch(`/api/topics/${topic.id}`, {method: 'DELETE'})
      if (!response.ok) {
        const body = await response.json().catch(() => ({detail: response.statusText}))
        throw new Error(body.detail ?? response.statusText)
      }
      notify.success('Topic removed and catalog republished')
      await load()
    } catch (error) {
      notify.error(errorMessage(error))
    }
  }

  return (
    <Layout>
      <PageHeader
        title="Topic Registry"
        count={data?.topics.length ?? 0}
        countLabel="registered"
        actions={(
          <button
            onClick={() => void load()}
            disabled={loading}
            className="flex items-center gap-2 rounded-md border border-zinc-300 px-3 py-2 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-white/10 dark:hover:bg-white/5"
          >
            <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        )}
      />

      {canRegister && (
        <form onSubmit={register} className={`${cardClass} mb-6`}>
          <HudCorners />
          <div className="mb-4 flex items-center gap-3">
            <div className="rounded-md border border-zinc-200 bg-zinc-100 p-2 dark:border-white/10 dark:bg-white/5">
              <Plus size={18} />
            </div>
            <div>
              <h2 className="font-display text-lg font-semibold">Register a topic contract</h2>
              <p className="text-xs text-zinc-500">Registration publishes the updated catalog on the local Zenoh fabric.</p>
            </div>
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <label className="space-y-1">
              <span className="hud-label text-xs text-zinc-500">Zenoh key expression</span>
              <input
                required
                maxLength={512}
                value={form.key_expr}
                onChange={event => setForm(current => ({...current, key_expr: event.target.value}))}
                placeholder="organization/source/tracks/v2"
                className={`${inputClass} font-mono`}
              />
            </label>
            <label className="space-y-1">
              <span className="hud-label text-xs text-zinc-500">MIME encoding</span>
              <input
                required
                maxLength={128}
                value={form.encoding}
                onChange={event => setForm(current => ({...current, encoding: event.target.value}))}
                placeholder="application/protobuf"
                className={`${inputClass} font-mono`}
              />
            </label>
            <label className="space-y-1">
              <span className="hud-label text-xs text-zinc-500">Direction</span>
              <select
                value={form.direction}
                onChange={event => setForm(current => ({
                  ...current,
                  direction: event.target.value as TopicForm['direction'],
                }))}
                className={inputClass}
              >
                <option value="publish">Publish</option>
                <option value="subscribe">Subscribe</option>
                <option value="bidirectional">Bidirectional</option>
              </select>
            </label>
            <label className="space-y-1">
              <span className="hud-label text-xs text-zinc-500">Description</span>
              <input
                maxLength={512}
                value={form.description}
                onChange={event => setForm(current => ({...current, description: event.target.value}))}
                placeholder="Purpose and payload contract"
                className={inputClass}
              />
            </label>
          </div>
          <div className="mt-4 flex justify-end">
            <button
              type="submit"
              disabled={saving}
              className="flex items-center gap-2 rounded-md bg-accent-fill px-4 py-2 text-sm font-medium text-accent-text hover:bg-accent-fill-hover disabled:opacity-50"
            >
              <CheckCircle2 size={15} />
              {saving ? 'Registering…' : 'Register topic'}
            </button>
          </div>
        </form>
      )}

      <section className={`${cardClass} mb-6`}>
        <HudCorners />
        <div className="mb-4">
          <h2 className="font-display text-lg font-semibold">Registered contracts</h2>
          <p className="mt-1 break-all font-mono text-xs text-zinc-500">
            Catalog: {data?.catalog_topic ?? 'Loading…'}
          </p>
        </div>
        {!data?.topics.length ? (
          <p className="rounded-md border border-dashed border-zinc-300 p-5 text-sm text-zinc-500 dark:border-white/10">
            No topics are registered yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-sm">
              <thead className="hud-label border-b border-zinc-200 text-xs text-zinc-500 dark:border-white/10">
                <tr>
                  <th className="pb-2 pr-4 font-medium">Key expression</th>
                  <th className="pb-2 pr-4 font-medium">Encoding</th>
                  <th className="pb-2 pr-4 font-medium">Direction</th>
                  <th className="pb-2 pr-4 font-medium">Description</th>
                  {canRegister && <th className="pb-2 text-right font-medium">Action</th>}
                </tr>
              </thead>
              <tbody>
                {data.topics.map(topic => (
                  <tr key={topic.id} className="border-b border-zinc-100 last:border-0 dark:border-white/5">
                    <td className="py-3 pr-4 font-mono text-xs">{topic.key_expr}</td>
                    <td className="py-3 pr-4 font-mono text-xs text-zinc-500">{topic.encoding}</td>
                    <td className="py-3 pr-4 capitalize">{topic.direction}</td>
                    <td className="py-3 pr-4 text-zinc-500">{topic.description || '—'}</td>
                    {canRegister && (
                      <td className="py-3 text-right">
                        <button
                          onClick={() => void unregister(topic)}
                          aria-label={`Unregister ${topic.key_expr}`}
                          className="rounded-none p-2 text-zinc-500 hover:bg-red-500/10 hover:text-red-500"
                        >
                          <Trash2 size={15} />
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className={cardClass}>
        <HudCorners />
        <div className="mb-4 flex items-start gap-3">
          <RadioTower size={19} className="mt-0.5 text-accent-ring" />
          <div>
            <h2 className="font-display text-lg font-semibold">Observed live topics</h2>
            <p className="text-xs text-zinc-500">
              Metadata seen since this admin backend started. Payload bytes are never retained by this view.
            </p>
          </div>
        </div>
        {!data?.observed.length ? (
          <p className="rounded-md border border-dashed border-zinc-300 p-5 text-sm text-zinc-500 dark:border-white/10">
            No Zenoh traffic has been observed since startup.
          </p>
        ) : (
          <div className="space-y-2">
            {data.observed.map(topic => {
              const registered = registeredKeys.has(topic.key_expr)
              return (
                <div
                  key={topic.key_expr}
                  className="flex flex-col gap-3 rounded-md border border-zinc-200 bg-zinc-50 p-3 dark:border-white/10 dark:bg-white/[0.025] md:flex-row md:items-center"
                >
                  <div className="min-w-0 flex-1">
                    <div className="break-all font-mono text-xs">{topic.key_expr}</div>
                    <div className="mt-1 text-xs text-zinc-500">
                      {topic.encoding} · {topic.sample_count.toLocaleString()} samples · last {formatSeen(topic.last_seen)}
                    </div>
                  </div>
                  {registered ? (
                    <span className="flex shrink-0 items-center gap-1 text-xs text-green-600 dark:text-green-400">
                      <CheckCircle2 size={14} />
                      Registered
                    </span>
                  ) : canRegister ? (
                    <button
                      onClick={() => prepareObserved(topic)}
                      className="shrink-0 rounded-md border border-zinc-300 px-3 py-1.5 text-xs hover:bg-zinc-100 dark:border-white/10 dark:hover:bg-white/5"
                    >
                      Register
                    </button>
                  ) : (
                    <span className="shrink-0 text-xs text-yellow-600 dark:text-yellow-400">Unregistered</span>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </section>
    </Layout>
  )
}
