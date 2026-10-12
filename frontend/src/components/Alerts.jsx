import { useEffect, useState } from 'react'
import { api } from '../lib/api'

const KEY = 'cl_alerts'
const SINCE = 'cl_alerts_since'
const SENT = 'cl_alerts_sent'
const IDS = 'cl_alerts_ids'
const DEFAULTS = { on: false, minSignal: 8, perHour: 2, quiet: true }

export const readAlerts = () => {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || '{}') } } catch { return { ...DEFAULTS } }
}
const writeAlerts = (c) => { try { localStorage.setItem(KEY, JSON.stringify(c)) } catch { /* private mode: the toggle just won't persist */ } }
const quietNow = () => { const h = new Date().getHours(); return h >= 22 || h < 7 }
const supported = () => typeof Notification !== 'undefined'

/** Breaking alerts (Google News / Apple News / Inshorts): a desktop notification when a front-page story clears the reader's Signal bar.
 *  Runs while ContextLens is open; capped per hour, silent overnight, never repeats a story. */
export function useAlerts(open) {
  useEffect(() => {
    if (!supported()) return undefined
    let dead = false
    const tick = async () => {
      const c = readAlerts()
      if (!c.on || Notification.permission !== 'granted') return
      const since = localStorage.getItem(SINCE) || new Date(Date.now() - 3600e3).toISOString()
      try {
        const r = await api.alerts(since, c.minSignal)
        if (dead) return
        localStorage.setItem(SINCE, r.now)
        if (c.quiet && quietNow()) return
        const sent = JSON.parse(localStorage.getItem(SENT) || '[]').filter((x) => Date.now() - x.t < 3600e3)
        const seen = new Set(JSON.parse(localStorage.getItem(IDS) || '[]'))
        for (const it of r.items) {
          if (sent.length >= c.perHour) break
          if (seen.has(it.id)) continue
          const n = new Notification(it.headline, { body: it.dek || `${it.outlet_count} outlets · Signal ${it.signal}/10`, tag: it.id })
          n.onclick = () => { window.focus(); open(it.id); n.close() }
          sent.push({ id: it.id, t: Date.now() })
          seen.add(it.id)
        }
        localStorage.setItem(SENT, JSON.stringify(sent))
        localStorage.setItem(IDS, JSON.stringify([...seen].slice(-200)))
      } catch { /* offline or backend restarting: try again next tick */ }
    }
    const t0 = setTimeout(tick, 8000)
    const i = setInterval(tick, 120000)
    return () => { dead = true; clearTimeout(t0); clearInterval(i) }
  }, [open])
}

export function AlertsCard() {
  const [c, setC] = useState(readAlerts)
  const [perm, setPerm] = useState(() => (supported() ? Notification.permission : 'unsupported'))
  const set = (p) => { const n = { ...c, ...p }; setC(n); writeAlerts(n) }
  const toggle = async (on) => {
    if (on && supported() && Notification.permission !== 'granted') setPerm(await Notification.requestPermission())
    set({ on })
  }
  return (
    <div className="card panel">
      <h3>Breaking alerts</h3>
      <p className="help" style={{ marginTop: 0 }}>A desktop notification when a front-page story is big enough. Works while ContextLens is open; nothing is sent anywhere.</p>
      {perm === 'unsupported' && <p className="help">This browser has no notifications.</p>}
      {perm === 'denied' && <p className="help">Notifications are blocked for this site in your browser settings.</p>}
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>
        <input type="checkbox" checked={c.on && perm === 'granted'} disabled={perm === 'unsupported' || perm === 'denied'} onChange={(e) => toggle(e.target.checked)} /> Alert me to breaking stories
      </label>
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>Only Signal
        <select className="field" style={{ width: 90 }} value={c.minSignal} onChange={(e) => set({ minSignal: +e.target.value })}>{[6, 7, 8, 9, 10].map((n) => <option key={n} value={n}>{n}+</option>)}</select> out of 10
      </label>
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>At most
        <select className="field" style={{ width: 90 }} value={c.perHour} onChange={(e) => set({ perHour: +e.target.value })}>{[1, 2, 4, 6].map((n) => <option key={n} value={n}>{n}</option>)}</select> per hour
      </label>
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0' }}>
        <input type="checkbox" checked={c.quiet} onChange={(e) => set({ quiet: e.target.checked })} /> Quiet overnight (10 pm to 7 am)
      </label>
    </div>
  )
}
