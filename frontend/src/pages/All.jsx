import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import { timeAgo, topicLabel } from '../lib/format'
import { Check, Close, Search } from '../components/Icons'

const PAGE = 40

function Row({ s }) {
  const nav = useNavigate()
  return (
    <button className="arow" data-topic={s.topic} onClick={() => nav(`/story/${s.id}`)}>
      <span className="kicker">{topicLabel(s.topic)}</span>
      <b>{s.headline}</b>
      {(s.gist || s.dek) && <span className="gist">{s.gist || s.dek}</span>}
      <span className="arow-meta">
        <span>{s.outlet_count} {s.outlet_count === 1 ? 'outlet' : 'outlets'}</span>
        <span>{timeAgo(s.published_at)}</span>
        {s.factcheck && <span className="layer fc">Fact-check</span>}
        {s.angles > 0 && <span className="layer th" title="Several reports of the same story were merged into this one">{s.angles + 1} angles merged</span>}
        {s.built ? <span className="layer why"><Check size={11} /> explained</span> : <span className="muted">quick read · tap to explain</span>}
      </span>
    </button>
  )
}

export default function All() {
  const [params, setParams] = useSearchParams()
  const topic = params.get('topic') || 'all'
  const source = params.get('source') || ''
  const fact = params.get('factcheck') === '1'
  const q = params.get('q') || ''
  const [text, setText] = useState(q)
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [counts, setCounts] = useState({})
  const [loading, setLoading] = useState(true)
  const [more, setMore] = useState(false)
  const seq = useRef(0)

  const set = useCallback((patch) => {
    const next = new URLSearchParams(params)
    Object.entries(patch).forEach(([k, v]) => (v && v !== 'all' ? next.set(k, v) : next.delete(k)))
    setParams(next, { replace: true })
  }, [params, setParams])

  useEffect(() => { const t = setTimeout(() => text !== q && set({ q: text.trim() }), 320); return () => clearTimeout(t) }, [text])   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const my = ++seq.current
    setLoading(true)
    api.feedAll({ topic, q, source, factcheck: fact, offset: 0, limit: PAGE }).then((r) => {
      if (my !== seq.current) return
      setItems(r.items); setTotal(r.total); setCounts(r.topic_counts || {}); setLoading(false)
    }).catch(() => my === seq.current && setLoading(false))
  }, [topic, q, source, fact])

  const loadMore = async () => {
    setMore(true)
    try {
      const r = await api.feedAll({ topic, q, source, factcheck: fact, offset: items.length, limit: PAGE })
      setItems((cur) => [...cur, ...r.items.filter((x) => !cur.some((y) => y.id === x.id))])
    } finally { setMore(false) }
  }
  const all = Object.values(counts).reduce((a, b) => a + b, 0)

  return (
    <div className="container">
      <div className="eyebrow">Everything</div>
      <h1 className="h-page" style={{ margin: '6px 0 8px' }}>All the news, not just what ranks</h1>
      <p className="lede" style={{ marginBottom: 16, maxWidth: 680 }}>Today shows what matters most to you. This is the full list of stories from the last three days, so nothing important hides behind a ranking.</p>

      <div className="all-bar">
        <label className="src-search"><Search size={16} /><input className="field" placeholder="Search headlines…" value={text} onChange={(e) => setText(e.target.value)} aria-label="Search all stories" /></label>
        <button className={'chip' + (fact ? ' accent' : ' outline')} style={{ cursor: 'pointer' }} aria-pressed={fact} onClick={() => set({ factcheck: fact ? '' : '1' })} title="Only stories a fact-checking outlet covered">Fact-checks only</button>
        {source && <button className="chip accent" onClick={() => set({ source: '' })}>Only {source} <Close size={12} /></button>}
      </div>
      <div className="pills" style={{ marginBottom: 14 }}>
        <button className={'pill' + (topic === 'all' ? ' on' : '')} onClick={() => set({ topic: 'all' })}>All · {all}</button>
        {Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([t, n]) => (
          <button key={t} data-topic={t} className={'pill' + (topic === t ? ' on' : '')} onClick={() => set({ topic: t })}><i />{topicLabel(t)} · {n}</button>
        ))}
      </div>

      {loading && [0, 1, 2, 3, 4, 5].map((i) => <div key={i} className="sk" style={{ height: 84, borderRadius: 14, marginBottom: 10 }} />)}
      {!loading && items.length === 0 && <div className="empty"><h3>Nothing matches</h3><p>Try a different topic or search.</p></div>}
      <div className="arows">{!loading && items.map((s) => <Row key={s.id} s={s} />)}</div>
      {!loading && items.length < total && (
        <div style={{ textAlign: 'center', margin: '18px 0 30px' }}>
          <button className="btn" onClick={loadMore} disabled={more}>{more ? 'Loading…' : `Show more · ${total - items.length} left`}</button>
        </div>
      )}
    </div>
  )
}
