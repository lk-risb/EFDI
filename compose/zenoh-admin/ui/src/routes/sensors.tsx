import {createFileRoute, redirect} from '@tanstack/react-router'
import {useEffect, useState} from 'react'
import {HudCorners} from '@/components/HudCorners'
import {Layout} from '@/components/Layout'
import {PageHeader} from '@/components/PageHeader'
import {apiJson, errorMessage} from '@/lib/api'
import {notify} from '@/lib/notify'
import {useAuth} from '@/store/auth'

export const Route = createFileRoute('/sensors')({
  beforeLoad: () => {
    if (!useAuth.getState().token) throw redirect({to: '/login'})
  },
  component: SensorsPage,
})

interface DronuradarasSensor {
  sensor_id: string
  sensor_name: string
  lat_deg: number
  lon_deg: number
  is_online: boolean
  last_detection_ts?: number
  last_detection_audio_url?: string
}

// Matches tak_layer.py's _SENSOR_ALERT_HOT_S / _SENSOR_ALERT_WARM_S — a
// detection older than this has fully reverted to idle on the TAK map too,
// so there's no reason to keep highlighting it here either.
const DETECTION_WARM_S = 300

function timeAgo(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 60) return `${Math.round(s)}s ago`
  return `${Math.round(s / 60)}m ago`
}

function SensorCard({sensor}: {sensor: DronuradarasSensor}) {
  const age = sensor.last_detection_ts ? Date.now() / 1000 - sensor.last_detection_ts : null
  const isRecent = age !== null && age <= DETECTION_WARM_S

  return (
    <div className="hud-card hud-glass hud-frame relative rounded-md border border-zinc-200 p-4 dark:border-white/10">
      <HudCorners />
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="truncate font-mono text-sm text-zinc-900 dark:text-zinc-100">{sensor.sensor_name}</span>
        <span className={`h-2 w-2 shrink-0 rounded-full ${sensor.is_online ? 'bg-green-500' : 'bg-zinc-400 dark:bg-zinc-600'}`} />
      </div>
      <p className="font-mono text-xs text-zinc-500">
        {sensor.lat_deg.toFixed(5)}°, {sensor.lon_deg.toFixed(5)}°
      </p>
      {sensor.last_detection_ts ? (
        <div className="mt-3 border-t border-zinc-200 pt-3 dark:border-white/10">
          <p className={`text-xs ${isRecent ? 'text-yellow-600 dark:text-yellow-400' : 'text-zinc-500'}`}>
            Last detection: {timeAgo(sensor.last_detection_ts)}
          </p>
          {sensor.last_detection_audio_url ? (
            <audio controls preload="none" src={sensor.last_detection_audio_url} className="mt-2 h-8 w-full" />
          ) : (
            <p className="mt-2 text-xs text-zinc-500">No audio for this detection.</p>
          )}
        </div>
      ) : (
        <p className="mt-3 border-t border-zinc-200 pt-3 text-xs text-zinc-500 dark:border-white/10">No detections recorded.</p>
      )}
    </div>
  )
}

function SensorsPage() {
  const [sensors, setSensors] = useState<DronuradarasSensor[]>([])

  useEffect(() => {
    async function load() {
      try {
        const res = await apiJson<{sensors: DronuradarasSensor[]}>('/api/sensors/dronuradaras')
        setSensors(res.sensors)
      } catch (e) {
        notify.error(errorMessage(e))
      }
    }
    load()
    // Matches streams.tsx's poll cadence — cheap endpoint, no reason to lag.
    const interval = setInterval(load, 5000)
    return () => clearInterval(interval)
  }, [])

  return (
    <Layout>
      <PageHeader title="Sensors" eyebrow="DRONURADARAS" count={sensors.length} countLabel="known" />
      {sensors.length === 0 ? (
        <p className="text-sm text-zinc-500">
          No dronuradaras.lt sensors seen yet — the dronuradaras bridge needs to be running and have polled at least once.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {sensors.map(s => (
            <SensorCard key={s.sensor_id} sensor={s} />
          ))}
        </div>
      )}
    </Layout>
  )
}
