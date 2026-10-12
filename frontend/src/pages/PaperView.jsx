import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { timeAgo, topicLabel } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { Back } from '../components/Icons'
import { useApp } from '../components/Shell'

function Row({ a, onOpen }) {
  return (
    <div className="pa-row" data-topic={a.topic} onClick={() => onOpen(a)}>
      <div style={{ minWidth: 0 }}>
        <span className="kicker">{topicLabel(a.topic)} · page {a.page}</span>
        <h3 style={{ marginTop: 4 }}>{a.headline}</h3>
        <p>{a.gist || a.lead}</p>
        <div className="scard-foot" style={{ paddingTop: 8 }}>
          <span>{a.words} words</span>
          {a.built ? <span className="layer why">explained</span> : a.building ? <span className="explain-tag">explaining…</span> : <span className="explain-tag">tap to explain</span>}
        </div>
      </div>
      <div className="small muted" style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
        <span className="meter" title={`Match to your interests: ${Math.round(a.interest * 100)}%`}><i style={{ width: `${Math.round(a.interest * 100)}%` }} /></span>
        <div style={{ marginTop: 4 }}>{Math.round(a.interest * 100)}% match</div>
      </div>
    </div>
  )
}

export default function PaperView() {
  const { id } = useParams()
  const nav = useNavigate()
  const { toast } = useApp()
  const { data, loading, error, reload } = useAsync(() => api.paper(id), [id])
  const [showOther, setShowOther] = useState(false)
  const [showSkipped, setShowSkipped] = useState(false)

  const open = async (a) => {
    try {
      const r = await api.openArticle(id, a.id)
      nav(`/story/${r.story_id}`)
    } catch (e) { toast(e.message) }
  }
  const remove = async () => {
    if (!confirm('Delete this newspaper and its explained stories?')) return
    await api.deletePaper(id)
    nav('/papers')
  }

  if (loading && !data) return <div className="container narrow"><div className="sk" style={{ height: 60, marginTop: 40 }} /></div>
  if (error && !data) return <div className="container narrow"><div className="empty"><h3>Not found</h3><Link className="btn" to="/papers">Back</Link></div></div>
  const p = data
  const explained = p.matched.filter((a) => a.built).length

  return (
    <div className="container narrow">
      <button className="btn ghost sm" onClick={() => nav('/papers')} style={{ marginLeft: -10 }}><Back size={16} /> Newspapers</button>
      <div className="eyebrow" style={{ marginTop: 14 }}>{p.stats?.date ? new Date(p.stats.date).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' }) : `Uploaded ${timeAgo(p.uploaded_at)}`}</div>
      <h1 className="h-page" style={{ margin: '6px 0 8px' }}>{p.name}</h1>
      <p className="lede">
        {p.status === 'failed'
          ? <span style={{ color: 'var(--bad)' }}>{p.error || 'Could not read this file.'}</span>
          : <>{p.pages} {p.pages === 1 ? 'page' : 'pages'} → <b>{p.article_count}</b> articles → <b>{p.matched_count}</b> match your interests · {explained} explained so far.</>}
      </p>
      <div style={{ display: 'flex', gap: 8, margin: '14px 0 6px' }}>
        <button className="btn sm" onClick={() => reload(true)}>Refresh</button>
        <button className="btn sm ghost" onClick={remove}>Delete</button>
      </div>

      <div className="sec-head"><h2>Worth your time</h2><span className="sub">ranked by fit to your interests</span></div>
      {p.matched.length === 0 && <p className="muted">Nothing in this paper matched your interests strongly. Open the “other articles” below to explain any of them.</p>}
      {p.matched.map((a) => <Row key={a.id} a={a} onOpen={open} />)}

      {p.other.length > 0 && (
        <section style={{ marginTop: 26 }}>
          <button className="btn" onClick={() => setShowOther((v) => !v)}>{showOther ? 'Hide' : 'Show'} {p.other.length} other articles</button>
          <p className="help">Below your interest threshold — but you can still explain any of them.</p>
          {showOther && p.other.map((a) => <Row key={a.id} a={a} onOpen={open} />)}
        </section>
      )}
      {p.skipped.length > 0 && (
        <section style={{ marginTop: 20 }}>
          <button className="btn ghost sm" onClick={() => setShowSkipped((v) => !v)}>{showSkipped ? 'Hide' : 'Show'} {p.skipped.length} skipped items</button>
          {showSkipped && (
            <div className="wire-list" style={{ marginTop: 8 }}>
              {p.skipped.map((a) => <div key={a.id} className="wire-row"><div className="t">{a.headline}</div><span className="r">{a.skip_reason}</span></div>)}
            </div>
          )}
        </section>
      )}
    </div>
  )
}
