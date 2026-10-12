import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { timeAgo, topicLabel } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { Globe, Search, Shield } from '../components/Icons'

const COUNTRY = { IN: 'India', US: 'USA', UK: 'UK', FR: 'France', DE: 'Germany', JP: 'Japan', SG: 'Singapore', IL: 'Israel', UA: 'Ukraine', QA: 'Qatar',
  AU: 'Australia', CA: 'Canada', CN: 'China', HK: 'Hong Kong', KR: 'Korea', ES: 'Spain', PK: 'Pakistan', EU: 'Europe', ASIA: 'Asia', AFRICA: 'Africa', GLOBAL: 'Global', INT: 'Global' }
const TYPE = { newspaper: 'Newspaper', broadcaster: 'Broadcaster', wire: 'Wire service', magazine: 'Magazine', digital: 'Digital', 'fact-checker': 'Fact-checker',
  explainer: 'Explainer', official: 'Official', financial: 'Business', tech: 'Technology', scientific: 'Science', health: 'Health', legal: 'Legal', sports: 'Sports', culture: 'Culture',
  research: 'Research', academic: 'Academic' }

function Pips({ rank }) {
  const n = Math.max(1, 4 - rank)
  return <span className="pips" title={`Trust tier ${rank}`} aria-label={`Trust tier ${rank}`}>{[1, 2, 3].map((i) => <i key={i} className={i <= n ? 'on' : ''} />)}</span>
}

function Outlet({ o, onOpen }) {
  return (
    <button className="outlet" onClick={() => onOpen(o)} title={`See the stories ${o.name} contributed to`}>
      <span className="src-top"><b>{o.name}</b><Pips rank={o.trust_rank} /></span>
      <span className="src-meta">{COUNTRY[o.country] || o.country || '—'} · {TYPE[o.type] || o.type}</span>
      <span className="src-foot">
        {o.articles > 0 ? <span><b>{o.articles}</b> stories · {timeAgo(o.last_at)}</span> : <span className="muted">quiet in the last 3 days</span>}
        {o.feeds > 1 && <span className="muted">{o.feeds} feeds</span>}
      </span>
      {o.topics.length > 0 && <span className="src-topics">{o.topics.slice(0, 4).map((t) => <i key={t}>{topicLabel(t)}</i>)}</span>}
    </button>
  )
}

export default function Sources() {
  const nav = useNavigate()
  const { data, loading, error } = useAsync(() => api.sourcesOverview(), [])
  const [q, setQ] = useState('')
  const open = (o) => nav(`/all?source=${encodeURIComponent(o.name)}`)
  const groups = useMemo(() => {
    const s = q.trim().toLowerCase()
    return (data?.groups || []).map((g) => ({ ...g, outlets: g.outlets.filter((o) => !s || `${o.name} ${o.country} ${o.type} ${o.topics.join(' ')}`.toLowerCase().includes(s)) })).filter((g) => g.outlets.length)
  }, [data, q])
  const t = data?.totals

  return (
    <div className="container wide">
      <div className="eyebrow">Sources</div>
      <h1 className="h-page" style={{ margin: '6px 0 8px' }}>Where your news <em style={{ fontStyle: 'italic', color: 'var(--accent)' }}>comes from</em></h1>
      <p className="lede" style={{ maxWidth: 760, marginBottom: 18 }}>
        ContextLens reads many independent newsrooms at once and only trusts a fact when several of them agree. Indian and global papers, wire-style broadcasters, specialist magazines,
        and fact-checkers who test viral claims — all of it, in one place.
      </p>
      {t && (
        <div className="src-stats">
          <div><b>{t.outlets}</b><span>outlets</span></div>
          <div><b>{t.feeds}</b><span>live feeds</span></div>
          <div><b>{t.countries}</b><span>countries & regions</span></div>
          <div><b>{t.articles_72h.toLocaleString()}</b><span>reports read in 3 days</span></div>
        </div>
      )}
      <div className="src-bar">
        <label className="src-search"><Search size={16} /><input className="field" placeholder="Find an outlet, country or topic…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Find an outlet" /></label>
        <div className="src-legend">
          <span><Pips rank={1} /> Top tier — wires, papers of record, official bodies</span>
          <span><Pips rank={2} /> Established</span>
          <span><Pips rank={3} /> Specialist</span>
        </div>
      </div>
      {loading && <div className="sk" style={{ height: 320, borderRadius: 18 }} />}
      {error && <div className="empty"><h3>Couldn’t load the sources</h3><p>{error.message}</p></div>}
      {groups.map((g) => (
        <section key={g.key} className="src-group">
          <div className="sec-head"><h2>{g.label}</h2><span className="sub">{g.outlets.length} · {g.desc}</span></div>
          <div className="src-grid">{g.outlets.map((o) => <Outlet key={o.name} o={o} onOpen={open} />)}</div>
        </section>
      ))}
      {data && !groups.length && <div className="empty"><h3>No outlet matches “{q}”</h3></div>}
      <div className="src-how">
        <div><Shield size={18} /><b>How trust works here</b><p>Trust tiers come from a curated list, not from popularity. A story is only treated as important when independent outlets report it — one outlet repeated ten times counts once.</p></div>
        <div><Globe size={18} /><b>Fact-checkers</b><p>Alt News, BOOM, Factly, Newschecker and Vishvas News are read like any newsroom, so a debunk sits next to the claim it debunks.</p></div>
        <div><Search size={18} /><b>Missing something?</b><p>Add a feed in <code>backend/feeds_extra.json</code> (url, outlet, topic). Dead feeds are skipped automatically.</p></div>
      </div>
    </div>
  )
}
