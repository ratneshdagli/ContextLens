import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { formatDate, timeAgo, topicLabel } from '../lib/format'
import { markRead, useAsync } from '../lib/hooks'
import { TermText } from '../components/Explain'
import { Close } from '../components/Icons'

/** Flow: a finite, swipeable deck. Left/right = next story. Down = go one layer deeper (gist → why → history → what next). */
function Layer({ s, layer }) {
  const ex = s.explain
  const terms = ex?.terms || []
  if (layer === 0) {
    return (
      <div className="flow-layer" key="gist">
        <span className="lab">The gist</span>
        <TermText text={ex?.gist || s.dek || s.what_happened} terms={terms} />
        {!ex && s.what_happened && <p style={{ marginTop: 12, color: 'var(--ink-2)' }}>{s.what_happened}</p>}
      </div>
    )
  }
  if (layer === 1) {
    return (
      <div className="flow-layer" key="why">
        <span className="lab">Why it happened</span>
        <ol className="flow-chain">{ex.why.map((w, i) => <li key={i}><b><TermText text={w.cause} terms={terms} /></b><TermText text={w.link} terms={terms} /></li>)}</ol>
      </div>
    )
  }
  if (layer === 2) {
    return (
      <div className="flow-layer" key="hist">
        <span className="lab">How it unfolded</span>
        <ol className="flow-chain">{s.timeline.slice(-4).map((e, i) => <li key={i}><b>{formatDate(e.date, e.precision)}</b>{e.text}</li>)}</ol>
      </div>
    )
  }
  return (
    <div className="flow-layer" key="next">
      <span className="lab">What it means</span>
      <TermText text={ex?.so_what || s.why_it_matters} terms={terms} />
      {ex?.next?.length > 0 && <>
        <span className="lab" style={{ marginTop: 18 }}>What to watch</span>
        <ol className="flow-chain">{ex.next.map((n, i) => <li key={i}>{n}</li>)}</ol>
      </>}
    </div>
  )
}

const layersOf = (s) => {
  const L = [0]
  if (s?.explain?.why?.length) L.push(1)
  if (s?.timeline?.length >= 2) L.push(2)
  if (s?.explain?.so_what || s?.why_it_matters) L.push(3)
  return L
}

export default function Flow() {
  const nav = useNavigate()
  const { data } = useAsync(() => api.feed('all'), [])
  const [i, setI] = useState(0)
  const [layer, setLayer] = useState(0)
  const [details, setDetails] = useState({})
  const [busy, setBusy] = useState(false)
  const touch = useRef(null)

  const list = useMemo(() => {
    if (!data?.lead) return []
    const all = [data.lead, ...data.stories]
    return [...all.filter((s) => s.built), ...all.filter((s) => !s.built)].slice(0, 12)
  }, [data])
  const cur = list[i]
  const d = cur && details[cur.id]

  const load = useCallback((s) => {
    if (!s || details[s.id]) return
    api.story(s.id).then((x) => setDetails((m) => ({ ...m, [s.id]: x }))).catch(() => {})
  }, [details])
  useEffect(() => { load(list[i]); load(list[i + 1]) }, [list, i, load])
  useEffect(() => { if (cur) markRead(cur.id) }, [cur])

  const layers = layersOf(d)
  const deeper = () => setLayer((l) => Math.min(l + 1, layers.length - 1))
  const shallower = () => setLayer((l) => Math.max(0, l - 1))
  const next = useCallback(() => { setLayer(0); setI((x) => Math.min(list.length, x + 1)) }, [list.length])
  const prev = useCallback(() => { setLayer(0); setI((x) => Math.max(0, x - 1)) }, [])

  useEffect(() => {
    const h = (e) => {
      if (/input|textarea/i.test(e.target.tagName)) return
      if (e.key === 'ArrowRight' || e.key === 'j') next()
      else if (e.key === 'ArrowLeft' || e.key === 'k') prev()
      else if (e.key === 'ArrowDown' || e.key === ' ' || e.key === 'Enter') { e.preventDefault(); deeper() }
      else if (e.key === 'ArrowUp') shallower()
      else if (e.key === 'Escape') nav('/')
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  })

  const explainNow = async () => {
    setBusy(true)
    try {
      const r = await api.build(cur.id)
      if (r.run_id) {
        const t = setInterval(async () => {
          const x = await api.story(cur.id).catch(() => null)
          if (x?.built) { clearInterval(t); setDetails((m) => ({ ...m, [cur.id]: x })); setBusy(false) }
        }, 5000)
      } else setBusy(false)
    } catch { setBusy(false) }
  }

  const onTouchStart = (e) => { touch.current = { x: e.touches[0].clientX, y: e.touches[0].clientY } }
  const onTouchEnd = (e) => {
    if (!touch.current) return
    const dx = e.changedTouches[0].clientX - touch.current.x
    const dy = e.changedTouches[0].clientY - touch.current.y
    touch.current = null
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) (dx < 0 ? next : prev)()
    else if (dy < -60) deeper()
    else if (dy > 60) shallower()
  }

  return (
    <div className="flow" onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
      <div className="flow-top">
        <div className="flow-bars" aria-label="Progress">{list.map((s, k) => <i key={s.id} className={k < i ? 'done' : k === i ? 'cur' : ''} />)}</div>
        <span className="small muted">{Math.min(i + 1, list.length)}/{list.length}</span>
        <button className="icon-btn" onClick={() => nav('/')} aria-label="Close"><Close /></button>
      </div>

      <div className="flow-stage">
        {!data && <div className="sk" style={{ height: 220, width: 'min(680px, 100%)' }} />}
        {data && list.length === 0 && (
          <div className="empty"><h3>Nothing to flow through yet</h3><p>Build your briefing first, then come back.</p><Link className="btn primary" to="/">Go to Today</Link></div>
        )}
        {cur && i < list.length && (
          <div className="flow-card" data-topic={cur.topic}>
            <div className="scard-top"><span className="kicker">{topicLabel(cur.topic)}</span><span className="t small muted">{timeAgo(cur.published_at)} · {cur.outlet_count} outlets</span></div>
            <h2>{d?.headline || cur.headline}</h2>
            {!d && <div className="sk" style={{ height: 90 }} />}
            {d && (d.built
              ? <Layer s={d} layer={layers[Math.min(layer, layers.length - 1)]} />
              : (
                <div className="flow-layer">
                  <span className="lab">Not explained yet</span>
                  <p style={{ color: 'var(--ink-2)' }}>{cur.dek || 'This story has only been picked up so far.'}</p>
                  <button className="btn primary" style={{ marginTop: 14 }} onClick={explainNow} disabled={busy}>{busy ? 'Explaining… (about 3 min)' : 'Explain this story'}</button>
                </div>
              ))}
          </div>
        )}
        {data && list.length > 0 && i >= list.length && (
          <div className="caughtup" style={{ border: 0 }}>
            <h3>You’re all caught up</h3>
            <p>That’s the edition. Anything you follow will be flagged when it develops.</p>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}><Link className="btn primary" to="/">Back to Today</Link><button className="btn" onClick={() => { setI(0); setLayer(0) }}>Start over</button></div>
          </div>
        )}
      </div>

      {cur && i < list.length && (
        <div className="flow-bottom">
          <div className="dots" aria-label="Layer">{layers.map((_, k) => <i key={k} className={k === Math.min(layer, layers.length - 1) ? 'on' : ''} />)}</div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            <Link className="btn sm ghost" to={`/story/${cur.id}`}>Full story</Link>
            {layer < layers.length - 1 && d?.built && <button className="btn sm" onClick={deeper}>{['Why did this happen?', 'How did we get here?', 'What does it mean?'][Math.min(layer, 2)]} ↓</button>}
            <button className="btn primary sm" onClick={next}>{i === list.length - 1 ? 'Finish' : 'Next story →'}</button>
          </div>
        </div>
      )}
    </div>
  )
}
