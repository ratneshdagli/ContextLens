import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import { rememberChannels, timeAgo, todayLabel, topicLabel } from '../lib/format'
import { useAsync, useReadLog } from '../lib/hooks'
import { storyToSpeech } from '../lib/speech'
import { Card, FeedSkeleton, Hero, WireRow } from '../components/StoryCards'
import RunPanel from '../components/RunPanel'
import { BlindShelf, LocalShelf } from '../components/Shelves'
import { Flame, Flow, List, Mic, Play, Refresh, Upload } from '../components/Icons'
import { useApp } from '../components/Shell'

const BUDGETS = [3, 5, 10]

/** The topic tabs: a row that slides sideways (arrows, wheel, drag) so no tab is ever out of reach. */
function PillRow({ children }) {
  const ref = useRef(null)
  const [edge, setEdge] = useState({ l: false, r: false })
  const measure = useCallback(() => {
    const el = ref.current
    if (el) setEdge({ l: el.scrollLeft > 4, r: el.scrollLeft + el.clientWidth < el.scrollWidth - 4 })
  }, [])
  useEffect(() => { measure(); window.addEventListener('resize', measure); return () => window.removeEventListener('resize', measure) }, [measure, children])
  const go = (d) => ref.current?.scrollBy({ left: d * Math.max(200, ref.current.clientWidth * 0.6), behavior: 'smooth' })
  const onWheel = (e) => { if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && ref.current) { ref.current.scrollLeft += e.deltaY; measure() } }
  return (
    <div className="pillwrap">
      {edge.l && <button className="pill-arrow l" onClick={() => go(-1)} aria-label="Scroll topics left">‹</button>}
      <div className="pillrow" role="tablist" ref={ref} onScroll={measure} onWheel={onWheel}>{children}</div>
      {edge.r && <button className="pill-arrow r" onClick={() => go(1)} aria-label="Scroll topics right">›</button>}
    </div>
  )
}
const fmtSec = (s) => (s < 90 ? `${Math.round(s)}s` : `${Math.round(s / 60)} min`)
const hello = () => { const h = new Date().getHours(); return h < 5 ? 'Still up' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening' }

function Ring({ done, total }) {
  const r = 24
  const c = 2 * Math.PI * r
  const p = total ? Math.min(1, done / total) : 0
  return (
    <div className="ring" title={`${done} of ${total} stories read today`}>
      <svg width="58" height="58" viewBox="0 0 58 58">
        <circle cx="29" cy="29" r={r} fill="none" stroke="var(--line)" strokeWidth="5" />
        <circle cx="29" cy="29" r={r} fill="none" stroke="var(--accent)" strokeWidth="5" strokeLinecap="round" strokeDasharray={c} strokeDashoffset={c * (1 - p)} style={{ transition: 'stroke-dashoffset .6s ease' }} />
      </svg>
      <b>{done}/{total}</b>
    </div>
  )
}

function Following({ items, reload }) {
  const active = (items || []).filter((f) => f.new_count > 0)
  if (!active.length) return null
  return (
    <div className="widget">
      <h4>New on stories you follow</h4>
      {active.slice(0, 4).map((f) => (
        <div key={f.id} className="thread-item" onClick={() => { window.location.href = `/story/${f.story_id}` }}>
          <b>{f.title}</b>
          <small>{f.new_count} new {f.new_count === 1 ? 'report' : 'reports'} · <button className="evi-btn" style={{ marginLeft: 0 }} onClick={(e) => { e.stopPropagation(); api.seen(f.id).then(reload) }}>mark seen</button></small>
        </div>
      ))}
    </div>
  )
}

export default function Today() {
  const { running, run, refresh, refreshChannel, refreshTick, health, toast, listen } = useApp()
  const nav = useNavigate()
  const [params] = useSearchParams()
  const log = useReadLog()
  const [topic, setTopic] = useState(params.get('c') ? `ch:${params.get('c')}` : 'all')
  const [budget, setBudget] = useState(0)
  const [showMore, setShowMore] = useState(false)
  const [showLog, setShowLog] = useState(false)
  const [calm, setCalmState] = useState(() => { try { return localStorage.getItem('cl_calm') === '1' } catch { return false } })
  const setCalm = (v) => { setCalmState(v); try { localStorage.setItem('cl_calm', v ? '1' : '0') } catch { /* private mode */ } }
  const [sel, setSel] = useState(-1)
  const { data, loading, error, reload, setData } = useAsync(() => api.feed(topic, calm), [topic, calm])
  const follows = useAsync(() => api.following(), [])

  useEffect(() => { api.onboardingStatus().then((r) => !r.complete && nav('/welcome', { replace: true })).catch(() => {}) }, [nav])
  useEffect(() => { if (refreshTick) reload(true) }, [refreshTick]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {                                            // keep upgrading cards while writers are working
    const busy = running || (data?.building?.length > 0)
    const t = setInterval(() => reload(true), busy ? 25000 : 120000)
    return () => clearInterval(t)
  }, [running, data?.building?.length, reload])

  const flat = useMemo(() => (data?.lead ? [data.lead, ...data.stories] : []), [data])
  const building = useMemo(() => new Set(data?.building || []), [data])
  const plan = useMemo(() => {
    if (!budget) return null
    const sorted = [...flat].sort((a, b) => Number(b.built) - Number(a.built) || b.signal - a.signal || b.rank - a.rank)
    let t = 0
    const items = []
    for (const s of sorted) {
      const sec = Math.max(35, s.brief_seconds)
      if (items.length && t + sec > budget * 60) continue
      items.push(s); t += sec
    }
    return { items, seconds: t }
  }, [flat, budget])

  const react = useCallback(async (s, action) => {
    try {
      await api.feedback(s.id, action)
      if (action === 'dislike') {
        toast('Got it — we’ll show less like this')
        setData({ ...data, stories: data.stories.filter((x) => x.id !== s.id) })
      } else {
        toast('Thanks — we’ll show more like this')
        setData({ ...data, stories: data.stories.map((x) => (x.id === s.id ? { ...x, reaction: 'like' } : x)) })
      }
    } catch (e) { toast(e.message) }
  }, [data, setData, toast])

  const listenBrief = async () => {
    if (listen.state !== 'idle') { listen.stop(); return }
    try {
      const list = (plan ? plan.items : flat.filter((s) => s.built)).slice(0, 5)
      const full = await Promise.all(list.map((s) => api.story(s.id)))
      listen.play([{ id: 'intro', label: 'Your briefing', text: `Here is your briefing. ${full.length} stories.` },
        ...full.map((s) => ({ id: s.id, label: s.headline, text: storyToSpeech(s) }))])
    } catch (e) { toast(e.message) }
  }

  useEffect(() => {
    const h = (e) => {
      if (/input|textarea|select/i.test(e.target.tagName) || e.metaKey || e.ctrlKey) return
      const list = plan ? plan.items : flat
      if (e.key === 'j') setSel((i) => Math.min(list.length - 1, i + 1))
      else if (e.key === 'k') setSel((i) => Math.max(0, i - 1))
      else if (e.key === 'Enter' && list[sel]) nav(`/story/${list[sel].id}`)
      else if (e.key === 'l' && list[sel]) react(list[sel], 'like')
      else if (e.key === 'x' && list[sel]) react(list[sel], 'dislike')
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [flat, plan, sel, nav, react])
  useEffect(() => { document.querySelector('.kb')?.scrollIntoView({ block: 'center', behavior: 'smooth' }) }, [sel])

  rememberChannels(data?.channels)
  const topics = useMemo(() => Object.entries(data?.topic_counts || {}).filter(([t]) => !t.startsWith('ch:')).sort((a, b) => b[1] - a[1]), [data])
  const channel = topic.startsWith('ch:') ? (data?.channels || []).find((c) => `ch:${c.id}` === topic) : null
  useEffect(() => { if (data && topic.startsWith('ch:') && !channel) setTopic('all') }, [data, topic, channel])   // the channel was deleted or switched off
  const llmDown = health && !health.llm?.available
  const empty = data && !data.lead
  const ringTotal = Math.min(10, flat.length)
  const rest = data?.stories || []
  const top = topic === 'all' ? rest.slice(0, 6) : rest.slice(0, 12)
  const tail = topic === 'all' ? rest.slice(6) : rest.slice(12)
  const tailMax = channel ? 40 : 8                                  // a channel page is the whole story list for that topic
  const byTopic = useMemo(() => {
    const m = {}
    for (const s of tail) (m[s.topic] ||= []).push(s)
    return Object.entries(m).sort((a, b) => b[1].length - a[1].length)
  }, [tail])
  const running_threads = useMemo(() => flat.filter((s) => s.thread_size > 1).slice(0, 4), [flat])

  return (
    <div className="container wide">
      <div className="today">
        <div style={{ minWidth: 0 }}>
          <div className="greet">
            <div>
              <div className="eyebrow">{todayLabel()}</div>
              <h1 style={{ marginTop: 6 }}>{hello()}. <em>Here’s what matters.</em></h1>
            </div>
            <div className="edition">
              {log.streak > 0 && <span className="streak"><Flame size={14} />{log.streak}-day streak</span>}
              {flat.length > 0 && <Ring done={log.todayCount} total={ringTotal} />}
            </div>
          </div>

          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 14 }}>
            <Link className="btn primary" to="/flow"><Flow size={16} /> Flow mode</Link>
            <Link className="btn talk" to="/live"><Mic size={16} /> Talk to Lens</Link>
            {listen.supported && flat.length > 0 && <button className="btn" onClick={listenBrief}>{listen.state !== 'idle' ? '■ Stop' : <><Play size={13} /> Listen</>}</button>}
            <button className="btn" onClick={refresh} disabled={running || llmDown}><Refresh size={16} />{running ? 'Updating…' : 'Refresh'}</button>
            <Link className="btn ghost" to="/papers"><Upload size={16} /> Upload a newspaper</Link>
            {data?.updated_at && !running && <span className="small muted">Updated {timeAgo(data.updated_at)}</span>}
          </div>

          {data?.funnel && !running && (
            <div className="funnel" title="Every refresh reads the newspapers, groups them into events, ranks them for you, then the writers explain the best ones one by one.">
              {data.funnel.map((f, i) => <span key={f.label} style={{ display: 'contents' }}>{i > 0 && <i>→</i>}<span><b>{f.n.toLocaleString()}</b> {f.label}</span></span>)}
            </div>
          )}

          {llmDown && (
            <div className="banner bad" role="alert">
              <div className="grow"><b>The language-model server is offline.</b> Start LM Studio’s local server (port 1235). Stories below stay readable; new explanations wait for the model.</div>
            </div>
          )}

          {running && (
            <div className="banner" style={{ display: 'block' }}>
              <RunPanel run={run} showLog={showLog} title="Building your briefing" />
              <button className="btn ghost sm" style={{ marginTop: 8, marginLeft: -8 }} onClick={() => setShowLog((v) => !v)}>{showLog ? 'Hide details' : 'Show what the editors are doing'}</button>
            </div>
          )}
          {!running && data?.building?.length > 0 && (
            <div className="banner"><span className="spinner" /><div className="grow small">Writers are explaining {data.building.length} {data.building.length === 1 ? 'story' : 'stories'} in the background. They appear here as they finish.</div></div>
          )}

          {(flat.length > 0 || data?.channels?.length > 0 || topic !== 'all') && (
            <PillRow>
              <button className={'pill' + (topic === 'all' && !budget ? ' on' : '')} onClick={() => { setTopic('all'); setBudget(0) }}>All</button>
              {topics.map(([t, n]) => (
                <button key={t} className={'pill' + (topic === t && !budget ? ' on' : '')} data-topic={t} onClick={() => { setTopic(t); setBudget(0) }}><i />{topicLabel(t)}<span className="n">{n}</span></button>
              ))}
              <span style={{ width: 1, background: 'var(--line-2)', margin: '4px 6px', flex: 'none' }} />
              {(data?.channels || []).map((c) => (
                <button key={c.id} className={'pill chan' + (topic === `ch:${c.id}` && !budget ? ' on' : '')} data-topic={`ch:${c.id}`} onClick={() => { setTopic(`ch:${c.id}`); setBudget(0) }}><i />{c.name}<span className="n">{c.count}</span></button>
              ))}
              <Link className="pill chan-add" to="/channels" title="Create your own category, even a very niche one">＋ Channel</Link>
              <span style={{ width: 1, background: 'var(--line-2)', margin: '4px 6px', flex: 'none' }} />
              <button className={'pill calm' + (calm ? ' on' : '')} onClick={() => setCalm(!calm)} aria-pressed={calm} title="Hide graphic and distressing stories from this page. They stay in Everything.">{calm ? '☾ Calm mode on' : '☾ Calm mode'}</button>
              {BUDGETS.map((b) => <button key={b} className={'pill' + (budget === b ? ' on' : '')} onClick={() => setBudget(budget === b ? 0 : b)}>⏱ {b} min</button>)}
            </PillRow>
          )}

          {channel && (
            <div className="chan-head">
              <div><div className="eyebrow">Your channel</div><h2>{channel.name}</h2>{channel.description && <p className="small muted">{channel.description}</p>}</div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <button className="btn sm" onClick={() => refreshChannel(channel.id)} disabled={running || llmDown}><Refresh size={15} />{running ? 'Updating…' : 'Update this channel'}</button>
                <Link className="btn sm ghost" to="/channels">Sources &amp; settings</Link>
              </div>
            </div>
          )}
          {calm && data?.hidden > 0 && <p className="help" style={{ marginTop: -4 }}>Calm mode is hiding {data.hidden} distressing {data.hidden === 1 ? 'story' : 'stories'}. <Link to="/all">See everything</Link> whenever you want the full picture.</p>}
          {loading && !data && <FeedSkeleton />}
          {error && !data && <div className="empty"><h3>Can’t reach the server</h3><p>{error.message}</p><button className="btn primary" onClick={() => reload()}>Try again</button></div>}

          {empty && !running && !loading && (
            <div className="empty">
              <h3>{topic === 'all' ? 'Your first edition is one click away' : `No ${topicLabel(topic)} stories right now`}</h3>
              <p>{topic === 'all' ? 'ContextLens reads the newspapers, keeps what matches your interests, and explains each story — what happened, why, and how we got here. The front page appears in a few minutes; explanations keep arriving after that.'
                : channel ? 'This channel has no stories yet. Find sources for it, then fill it — only this channel’s sources are read, so it is much faster than a full refresh.' : 'Try another topic, or refresh to look for new stories.'}</p>
              {topic === 'all' ? <button className="btn primary lg" onClick={refresh} disabled={llmDown}>Build my briefing</button>
                : channel ? <div style={{ display: 'flex', gap: 8, justifyContent: 'center', flexWrap: 'wrap' }}>
                  <button className="btn primary" onClick={() => refreshChannel(channel.id)} disabled={running || llmDown}>Fill this channel now</button>
                  <Link className="btn" to="/channels">Find sources</Link></div>
                  : <button className="btn" onClick={() => setTopic('all')}>Show everything</button>}
            </div>
          )}
          {empty && running && <FeedSkeleton />}

          {plan && (
            <section>
              <div className="sec-head"><h2>Your {budget}-minute briefing</h2><span className="sub">{plan.items.length} stories · about {fmtSec(plan.seconds)}</span></div>
              <div className="brief-list">
                {plan.items.map((s, i) => (
                  <div key={s.id} className={'brief-row' + (sel === i ? ' kb' : '')} onClick={() => nav(`/story/${s.id}`)}>
                    <div><h3>{s.headline}</h3><p className="dek">{s.gist || s.dek}</p></div>
                    <span className="t">{fmtSec(Math.max(35, s.brief_seconds))}</span>
                  </div>
                ))}
              </div>
              <p className="help">Picked by Signal — the most significant stories that fit your time, explained ones first.</p>
            </section>
          )}

          {!plan && data?.lead && (
            <>
              <Hero s={data.lead} active={sel === 0} building={building.has(data.lead.id)} />
              <div className="sec-head" style={{ marginTop: 0 }}><h2>{topic === 'all' ? 'Top for you' : topicLabel(topic)}</h2><span className="sub">{data.built} of {data.total} explained{data.built < data.total ? ' — open any to explain it now' : ''}</span></div>
              <div className="grid2" style={{ marginTop: 10 }}>
                {top.map((s, i) => <Card key={s.id} s={s} onReact={react} active={sel === i + 1} read={log.isRead(s.id)} building={building.has(s.id)} />)}
              </div>

              {topic === 'all' && !channel && <><LocalShelf /><BlindShelf /></>}

              {byTopic.map(([t, list]) => (
                <section key={t} data-topic={t}>
                  <div className="sec-head"><h2><span className="kicker" style={{ fontSize: 'inherit', textTransform: 'none', letterSpacing: 0 }}>{topicLabel(t)}</span></h2><span className="sub">{list.length} more</span></div>
                  <div className="wire-list">{list.slice(0, tailMax).map((s) => <WireRow key={s.id} s={s} building={building.has(s.id)} />)}</div>
                </section>
              ))}

              {data.more.length > 0 && (
                <section style={{ marginTop: 30 }}>
                  <button className="btn" onClick={() => setShowMore((v) => !v)}>{showMore ? 'Hide' : 'Show'} {data.more.length} lower-priority stories</button>
                  <p className="help">Fine, but the editors did not judge them front-page material.</p>
                  {showMore && <div className="wire-list" style={{ marginTop: 10 }}>{data.more.map((s) => <WireRow key={s.id} s={s} />)}</div>}
                </section>
              )}

              <div className="caughtup">
                <h3>{log.todayCount >= Math.min(6, flat.length) ? 'You’re all caught up' : 'That’s the edition'}</h3>
                <p>{log.todayCount} of {flat.length} read today. New stories arrive every hour; anything you follow will be flagged.</p>
                <div style={{ display: 'flex', gap: 8, justifyContent: 'center', flexWrap: 'wrap' }}>
                  <Link className="btn primary" to="/flow"><Flow size={16} /> Read it in Flow mode</Link>
                  <Link className="btn" to="/all"><List size={16} /> Browse every story</Link>
                </div>
              </div>
            </>
          )}
        </div>

        <aside className="today-side">
          <Link to="/live" className="widget talk-widget">
            <span className="mini-orb" aria-hidden="true"><i /><i /><i /></span>
            <span><h4 style={{ marginBottom: 2 }}>Talk to Lens</h4><span className="small muted">Get the highlights read to you, then ask “why?” — it researches the web while it talks.</span></span>
          </Link>
          <Following items={follows.data} reload={follows.reload} />
          {running_threads.length > 0 && (
            <div className="widget">
              <h4>Running stories</h4>
              {running_threads.map((s) => (
                <div key={s.id} className="thread-item" onClick={() => nav(`/story/${s.id}`)}><b>{s.headline}</b><small>{s.thread_size} reports in this thread</small></div>
              ))}
            </div>
          )}
          <div className="widget">
            <h4>Have a newspaper?</h4>
            <p className="small muted" style={{ marginBottom: 10 }}>Drop in today’s e-paper PDF. We split it into articles, keep what matters to you and explain it.</p>
            <Link className="btn sm" to="/papers"><Upload size={15} /> Upload PDF</Link>
          </div>
          <div className="widget">
            <h4>Select any text</h4>
            <p className="small muted">Highlight a word or sentence in any story to explain it, see why it happened, who someone is, or run a forensic check on the claim.</p>
          </div>
        </aside>
      </div>
    </div>
  )
}
