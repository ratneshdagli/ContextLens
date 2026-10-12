import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { WireRow } from './StoryCards'

const safeUrl = (u) => (/^https?:\/\//i.test(u || '') ? u : null)

/** Local (Google News Local / NewsBreak / SmartNews): news from the reader's own place, stories first, then live local headlines. */
export function LocalShelf() {
  const { data, reload } = useAsync(() => api.local(), [])
  const [text, setText] = useState('')
  const [found, setFound] = useState(null)
  const [changing, setChanging] = useState(false)
  if (!data) return null
  const search = async (e) => {
    e.preventDefault()
    if (text.trim().length >= 2) setFound(await api.localSearch(text.trim()).catch(() => []))
  }
  const pick = async (p) => {
    await api.setHome({ name: p.name, country: p.country, lat: p.lat, lon: p.lon })
    setFound(null); setText(''); setChanging(false)
    reload()
  }
  const picker = (
    <form onSubmit={search} style={{ display: 'flex', gap: 8, flexWrap: 'wrap', margin: '8px 0' }}>
      <input className="input" value={text} onChange={(e) => setText(e.target.value)} placeholder="Your city or town" aria-label="Your city or town" style={{ maxWidth: 260 }} />
      <button className="btn sm" type="submit">Find</button>
      {found?.length === 0 && <span className="help">No match. Try a bigger nearby city.</span>}
      {found?.map((p) => <button key={`${p.name}${p.lat}`} type="button" className="btn sm ghost" onClick={() => pick(p)}>{p.name}, {p.country}</button>)}
    </form>
  )
  if (!data.place) {
    return (
      <section style={{ marginTop: 26 }}>
        <div className="sec-head"><h2>Local news</h2><span className="sub">from your own place</span></div>
        <p className="help" style={{ marginTop: 0 }}>Tell ContextLens where you are and this shelf fills with news from there, live.</p>
        {picker}
      </section>
    )
  }
  return (
    <section style={{ marginTop: 26 }}>
      <div className="sec-head"><h2>Near you · {data.place.name}</h2><span className="sub">{data.items.length} nearby · <Link to="/globe">open the globe</Link> · <button className="btn sm ghost" onClick={() => setChanging((v) => !v)}>change</button></span></div>
      {changing && picker}
      {data.items.length === 0 && <p className="help">Nothing reported near {data.place.name} yet. Local headlines are being collected now; check back in a minute.</p>}
      <div className="wire-list">
        {data.items.map((i) => {
          const url = i.kind === 'story' ? `/story/${i.id}` : safeUrl(i.url)
          const row = (
            <div className="wire-row" data-topic={i.topic}>
              <div style={{ minWidth: 0 }}><div className="t">{i.title}</div></div>
              <span className="r">{i.kind === 'story' ? `${i.outlets} outlets` : i.outlet || 'local'} · {i.place} · {timeAgo(i.t)}</span>
            </div>
          )
          if (!url) return <div key={i.id}>{row}</div>
          return i.kind === 'story'
            ? <Link key={i.id} to={url} style={{ color: 'inherit', textDecoration: 'none' }}>{row}</Link>
            : <a key={i.id} href={url} target="_blank" rel="noopener noreferrer" style={{ color: 'inherit', textDecoration: 'none' }}>{row}</a>
        })}
      </div>
    </section>
  )
}

/** Blind spots (Ground News): stories several outlets ran, but only one country's press did. */
export function BlindShelf() {
  const { data } = useAsync(() => api.blindspots(), [])
  if (!data?.length) return null
  return (
    <section style={{ marginTop: 26 }}>
      <div className="sec-head"><h2>Blind spots</h2><span className="sub">covered by only one country’s press</span></div>
      <div className="wire-list">
        {data.map((s) => (
          <div key={s.id}>
            <WireRow s={s} />
            <div className="help" style={{ margin: '-4px 0 6px 20px' }}>Only {s.only_country || 'one country'}’s outlets reported this. Open it to see how they framed it.</div>
          </div>
        ))}
      </div>
    </section>
  )
}
