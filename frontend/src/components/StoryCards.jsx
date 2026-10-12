import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { timeAgo, topicLabel } from '../lib/format'
import { Check, Chevron, Clock, Why } from './Icons'

/** What extra depth a story has — shown as small glyphs so readers know before opening. */
function Layers({ s }) {
  const out = []
  if (s.built && s.peek?.length) out.push(<span key="w" className="layer why" title="Explains why this happened"><Why size={12} /> Why</span>)
  if (s.has_timeline) out.push(<span key="t" className="layer tl" title="Has a timeline of how it unfolded"><Clock size={12} /> Timeline</span>)
  if (s.thread_size > 1) out.push(<span key="h" className="layer th" title="Part of a running story">{s.thread_size} in thread</span>)
  if (s.factcheck) out.push(<span key="f" className="layer fc" title="A fact-checking outlet covered this">Fact-check</span>)
  if (s.angles > 0) out.push(<span key="a" className="layer mg" title="Several reports of the same story were merged into this one">{s.angles + 1} angles</span>)
  return out.length ? <span className="layers">{out}</span> : null
}

function Kicker({ s }) {
  return <span className="kicker" data-topic={s.topic}>{s.channel_name || topicLabel(s.topic)}</span>
}

/** Why this story is on your page: the channel or interest that pulled it in. */
function WhyLine({ s }) {
  if (!s.why_for_you || !(s.channel || s.interest >= 0.5)) return null
  return <p className="why-line"><Check size={12} /> {s.why_for_you[0].toUpperCase() + s.why_for_you.slice(1)}</p>
}

function Foot({ s, extra }) {
  return (
    <div className="scard-foot">
      <span>{s.outlet_count} {s.outlet_count === 1 ? 'outlet' : 'outlets'}</span>
      {s.built && <span>· {s.reading_minutes} min</span>}
      {s.interest >= 0.5 && s.why_for_you && <span className="foryou"><Check size={13} />For you</span>}
      <Layers s={s} />
      {extra}
    </div>
  )
}

/** Big lead card. */
export function Hero({ s, active, building }) {
  const go = useNavigate()
  return (
    <article className={'hero fade-in' + (s.image_url ? '' : ' noimg') + (active ? ' kb' : '')} onClick={() => go(`/story/${s.id}`)}>
      <div style={{ position: 'relative', zIndex: 1 }}>
        <div className="scard-top"><Kicker s={s} /><span className="t">{timeAgo(s.published_at)}</span></div>
        <h2>{s.headline}</h2>
        <p className="gist">{s.gist || s.dek}</p>
        <WhyLine s={s} />
        {s.built && s.peek?.length > 0 && (
          <ol className="peek" style={{ marginTop: 14 }}>{s.peek.map((c, i) => <li key={i}>{c}</li>)}</ol>
        )}
        {building && <div className="buildbar" style={{ marginTop: 14 }}><i /></div>}
        <Foot s={s} extra={!s.built && !building && <span className="explain-tag">tap to explain</span>} />
      </div>
      {s.image_url && <div className="hero-img" role="img" aria-label="" style={{ backgroundImage: `url(${s.image_url})` }} />}
    </article>
  )
}

/** Standard story card with an inline "peek" at the causal chain. */
export function Card({ s, active, read, building, onReact }) {
  const go = useNavigate()
  const [open, setOpen] = useState(false)
  const canPeek = s.built && (s.gist || s.peek?.length)
  return (
    <article className={'scard fade-in' + (s.built ? '' : ' wire') + (read ? ' read' : '') + (active ? ' kb' : '')} onClick={() => go(`/story/${s.id}`)}>
      <div className="scard-top"><Kicker s={s} /><span className="t">{timeAgo(s.published_at)}</span></div>
      <h3>{s.headline}</h3>
      {(s.gist || s.dek) && <p className="gist">{s.gist || s.dek}</p>}
      <WhyLine s={s} />
      {open && (
        <div className="peek" onClick={(e) => e.stopPropagation()}>
          {s.peek?.length > 0 && <><b style={{ fontSize: 12, letterSpacing: '.06em', textTransform: 'uppercase', color: 'var(--accent)' }}>Why it happened</b>
            <ol>{s.peek.map((c, i) => <li key={i}>{c}</li>)}</ol></>}
          <button className="btn sm" style={{ justifySelf: 'start' }} onClick={() => go(`/story/${s.id}`)}>Open the full story</button>
        </div>
      )}
      {building && <div className="buildbar"><i /></div>}
      <Foot s={s} extra={
        <>
          {!s.built && !building && <span className="explain-tag">tap to explain</span>}
          {building && <span className="explain-tag">explaining…</span>}
          {canPeek && (
            <button className="peek-btn" onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }} aria-expanded={open}>
              {open ? 'Close' : 'Peek'} <Chevron size={13} style={{ transform: open ? 'rotate(180deg)' : 'none' }} />
            </button>
          )}
          {onReact && (
            <span style={{ display: 'inline-flex', gap: 2 }} onClick={(e) => e.stopPropagation()}>
              <button className={'peek-btn' + (s.reaction === 'like' ? ' on' : '')} style={{ marginLeft: canPeek ? 0 : 'auto' }} title="More like this" onClick={() => onReact(s, 'like')}>＋</button>
              <button className="peek-btn" style={{ marginLeft: 0 }} title="Less like this" onClick={() => onReact(s, 'dislike')}>－</button>
            </span>
          )}
        </>
      } />
    </article>
  )
}

/** Compact row (Google-News density) for the long tail. */
export function WireRow({ s, building }) {
  const go = useNavigate()
  return (
    <div className="wire-row" data-topic={s.topic} onClick={() => go(`/story/${s.id}`)}>
      <div style={{ minWidth: 0 }}>
        <div className="t">{s.headline}</div>
        {s.built && (s.gist || s.dek) && <div className="small muted" style={{ marginTop: 2, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{s.gist || s.dek}</div>}
      </div>
      <span className="r">{building ? 'explaining…' : `${s.outlet_count} outlets · ${timeAgo(s.published_at)}`}</span>
    </div>
  )
}

export const Item = (p) => <div style={{ marginBottom: 12 }}><Card {...p} /></div>

export function MoreRow({ s }) {
  return <WireRow s={s} />
}

export function FeedSkeleton() {
  return (
    <div aria-busy="true">
      <div className="hero noimg" style={{ display: 'block' }}>
        <div className="sk" style={{ height: 12, width: 120, marginBottom: 16 }} />
        <div className="sk" style={{ height: 38, width: '85%', marginBottom: 10 }} />
        <div className="sk" style={{ height: 38, width: '60%', marginBottom: 18 }} />
        <div className="sk" style={{ height: 16, width: '70%' }} />
      </div>
      <div className="grid2">
        {[...Array(6)].map((_, i) => (
          <div key={i} className="scard" style={{ cursor: 'default' }}>
            <div className="sk" style={{ height: 12, width: 80 }} />
            <div className="sk" style={{ height: 22, width: '90%' }} />
            <div className="sk" style={{ height: 14, width: '100%' }} />
            <div className="sk" style={{ height: 14, width: '55%' }} />
          </div>
        ))}
      </div>
    </div>
  )
}
