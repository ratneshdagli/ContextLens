import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { formatDate, topicLabel } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { Back } from '../components/Icons'
import { useApp } from '../components/Shell'

const TYPE = { person: 'Person', organization: 'Organisation', place: 'Place', event: 'Event', other: 'Topic' }

export default function Entity() {
  const { id } = useParams()
  const nav = useNavigate()
  const { toast } = useApp()
  const { data: e, loading, error, setData } = useAsync(() => api.entity(id), [id])
  if (loading && !e) return <div className="container narrow"><div className="sk" style={{ height: 60, marginTop: 40 }} /></div>
  if (error && !e) return <div className="container narrow"><div className="empty"><h3>Not found</h3><Link className="btn" to="/atlas">Back to Atlas</Link></div></div>
  const toggle = async () => {
    try {
      if (e.following) await api.unfollowEntity(id); else await api.followEntity(id)
      setData({ ...e, following: !e.following })
      toast(e.following ? 'Unfollowed' : `Following ${e.name} — their stories rank higher`)
    } catch (err) { toast(err.message) }
  }
  return (
    <div className="container narrow">
      <button className="btn ghost sm" onClick={() => nav(-1)} style={{ marginLeft: -10 }}><Back size={16} /> Back</button>
      <div className="ent-head" style={{ marginTop: 14 }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <div className="eyebrow">{TYPE[e.type] || 'Topic'} · in {e.story_count} {e.story_count === 1 ? 'story' : 'stories'}</div>
          <h1 style={{ marginTop: 6 }}>{e.name}</h1>
        </div>
        <button className={'btn' + (e.following ? ' primary' : '')} onClick={toggle}>{e.following ? '✓ Following' : '+ Follow'}</button>
      </div>
      {e.summary && <p className="lede" style={{ marginTop: 14 }}>{e.summary} {e.wiki_url && <a href={e.wiki_url} target="_blank" rel="noreferrer" className="small" style={{ textDecoration: 'underline' }}>Wikipedia</a>}</p>}

      {e.related?.length > 0 && (
        <>
          <div className="sec-head"><h2>Often appears with</h2></div>
          <div className="tagbox">{e.related.map((r) => <Link key={r.id} to={`/entity/${r.id}`} className="chip outline">{r.name}<span className="muted small">{r.together}×</span></Link>)}</div>
        </>
      )}

      <div className="sec-head"><h2>In the news</h2><span className="sub">newest first</span></div>
      <div className="threadbox">
        {e.stories.map((s) => (
          <div key={s.id} className="tb" data-topic={s.topic} onClick={() => nav(`/story/${s.id}`)}>
            <span className="when">{formatDate(s.at.slice(0, 10))}</span>
            <span><b style={{ fontWeight: 600 }}>{s.headline}</b><br /><span className="small muted">{topicLabel(s.topic)}{s.tier === 'wire' ? ' · not explained yet' : ''}</span></span>
          </div>
        ))}
      </div>
    </div>
  )
}
