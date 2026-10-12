import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import { formatDate, timeAgo, topicLabel, TRUST } from '../lib/format'
import { markRead, useAsync, useRun } from '../lib/hooks'
import { storyToSpeech } from '../lib/speech'
import ChatPanel from '../components/ChatPanel'
import { BuildStepper, GoDeeper, TermList, TermText, WhyChain } from '../components/Explain'
import { Back, Bookmark, BookmarkFill, Chat, Close, Link as LinkIcon, Mic, Play, Sparkle, ThumbDown, ThumbUp, Users, Why, Clock } from '../components/Icons'
import Inspector from '../components/Inspector'
import RunPanel from '../components/RunPanel'
import StoryMap from '../components/StoryMap'
import Timeline from '../components/Timeline'
import { useApp } from '../components/Shell'

const COUNTRY = { US: 'United States', UK: 'United Kingdom', IN: 'India', QA: 'Qatar', DE: 'Germany', FR: 'France', HK: 'Hong Kong', PK: 'Pakistan', INT: 'International' }
const splitSentences = (t) => (t || '').replace(/\s+/g, ' ').trim().split(/(?<=[.!?])\s+(?=[A-Z0-9“"'])/).map((x) => x.trim()).filter((x) => x.length > 15)
const DEFAULT_QS = [{ kind: 'why', q: 'Why did this happen?' }, { kind: 'history', q: 'What led up to this?' }, { kind: 'views', q: 'What are the different views?' }, { kind: 'next', q: 'What happens next?' }]

function Cites({ ns }) {
  return <>{ns.map((n) => <a key={n} className="cite" href={`#src-${n}`} title={`Source ${n}`}>{n}</a>)}</>
}
function ClaimChip({ claim, open, onToggle }) {
  return <button className={'cite claim' + (open ? ' open' : '')} onClick={onToggle} aria-expanded={open} title={`Supported by ${claim.outlet} — click to see the sentence`}>{claim.n}</button>
}
function Evidence({ claim }) {
  return (
    <div className="evidence fade-in">
      <div className="small muted" style={{ marginBottom: 4 }}>Where this comes from — <a href={claim.url} target="_blank" rel="noreferrer" style={{ textDecoration: 'underline' }}>{claim.outlet}</a></div>
      <blockquote>“{claim.quote}”</blockquote>
    </div>
  )
}
const Sec = ({ id, glyph, tone, title, sub, children }) => (
  <section className="section" id={id}>
    <h2 className="h-section">{glyph && <span className={'glyph ' + (tone || '')}>{glyph}</span>}{title}{sub && <span className="sub">{sub}</span>}</h2>
    {children}
  </section>
)

export default function Story() {
  const { id } = useParams()
  const [params] = useSearchParams()
  const nav = useNavigate()
  const { toast, listen, health } = useApp()
  const { data: s, loading, error, reload, setData } = useAsync(() => api.story(id), [id])
  const [sheet, setSheet] = useState(false)
  const [openClaim, setOpenClaim] = useState(null)
  const [buildRun, setBuildRun] = useState(null)
  const [buildErr, setBuildErr] = useState('')
  const [seed, setSeed] = useState(null)
  const [active, setActive] = useState('gist')
  const started = useRef('')
  const articleRef = useRef(null)
  const run = useRun(params.get('run') || buildRun, { onDone: () => { setBuildRun(null); reload(true) } })

  useEffect(() => { window.scrollTo(0, 0); setOpenClaim(null); setBuildRun(null); setBuildErr(''); markRead(id); api.feedback(id, 'open').catch(() => {}) }, [id])
  useEffect(() => {
    if (s?.status !== 'researching' || params.get('run')) return undefined
    const t = setInterval(() => reload(true), 4000)
    return () => clearInterval(t)
  }, [s?.status, params, reload])

  const explainNow = useCallback(async (force = false) => {
    setBuildErr('')
    try {
      const r = await api.build(id, force)
      if (r.built) { reload(true); return }
      if (r.run_id) setBuildRun(r.run_id)
      else setBuildErr('The writers are busy with this story — it will appear shortly.')
    } catch (e) { setBuildErr(e.message) }
  }, [id, reload])

  // a wire story opened by a reader is explained right away (interactive priority)
  useEffect(() => {
    if (s && s.kind !== 'research' && !s.built && s.status !== 'researching' && started.current !== id && health?.llm?.available && !s.build_error) {
      started.current = id
      explainNow()
    }
  }, [s, id, health, explainNow])
  useEffect(() => {                                          // a feed cycle is already building it: poll until done
    if (!s?.building || buildRun) return undefined
    const t = setInterval(() => reload(true), 6000)
    return () => clearInterval(t)
  }, [s?.building, buildRun, reload])

  const claimsBy = useMemo(() => {
    const m = {}
    for (const c of s?.claims || []) m[`${c.kind}:${c.idx}`] = c
    return m
  }, [s])
  const sentences = useMemo(() => splitSentences(s?.what_happened), [s])

  const toggleSave = async () => {
    try {
      if (s.saved) await api.unsave(id); else await api.save(id)
      setData({ ...s, saved: !s.saved }); toast(s.saved ? 'Removed from Saved' : 'Saved')
    } catch (e) { toast(e.message) }
  }
  const toggleFollow = async () => {
    try {
      if (s.following) await api.unfollow(id); else await api.follow(id)
      setData({ ...s, following: !s.following }); toast(s.following ? 'Unfollowed' : 'Following — we’ll flag new developments on Today')
    } catch (e) { toast(e.message) }
  }
  const react = async (a) => {
    try { await api.feedback(id, a); setData({ ...s, reaction: a }); toast(a === 'like' ? 'We’ll show more like this' : 'We’ll show less like this') } catch (e) { toast(e.message) }
  }
  const shareWhatsApp = () => {
    const ex = s.explain
    const why = (ex?.why || []).slice(0, 3).map((w, i) => `${i + 1}. ${w.cause} — ${w.link}`).join('\n')
    const lead = s.sources?.find((r) => /^https?:/.test(r.url))
    const parts = [`*${s.headline}*`, ex?.gist || s.dek || s.what_happened]      // WhatsApp renders *bold*
    if (why) parts.push(`Why:\n${why}`)
    if (lead) parts.push(`${lead.outlet}: ${lead.url}`)
    parts.push('via ContextLens')
    const text = parts.join('\n\n')
    window.open(`https://wa.me/?text=${encodeURIComponent(text)}`, '_blank', 'noopener')
  }
  const copyBrief = () => {
    const ex = s.explain
    const text = `${s.headline}\n\n${ex?.gist || s.what_happened}\n\n${(ex?.why || []).map((w, i) => `${i + 1}. ${w.cause} — ${w.link}`).join('\n')}\n\nSources: ${s.sources.map((r) => `${r.outlet} (${r.url})`).join('; ')}`
    navigator.clipboard?.writeText(text).then(() => toast('Brief copied'))
  }
  const listenNow = () => (listen.state !== 'idle' && listen.current?.id === id) ? listen.stop() : listen.play([{ id, label: s.headline, text: storyToSpeech(s) }])
  const ask = (text) => { setSeed({ text: `Explain this: “${text.slice(0, 300)}”`, n: Date.now() }); setSheet(true) }

  useEffect(() => {
    if (!s) return undefined
    const h = (e) => {
      if (/input|textarea|select/i.test(e.target.tagName) || e.metaKey || e.ctrlKey || window.getSelection()?.toString()) return
      if (e.key === 's') toggleSave()
      else if (e.key === 'f') toggleFollow()
      else if (e.key === 'l') listenNow()
      else if (e.key === 'b' || e.key === 'Escape') nav(-1)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  })

  // highlight the layer you are reading
  useEffect(() => {
    const onScroll = () => {
      const els = [...document.querySelectorAll('.story .section[id]')]
      let cur = 'gist'
      for (const el of els) if (el.getBoundingClientRect().top < 140) cur = el.id
      setActive(cur)
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [s])

  if (loading && !s) return <div className="container narrow"><div className="sk" style={{ height: 44, width: '80%', marginTop: 40 }} /><div className="sk" style={{ height: 20, width: '60%', marginTop: 16 }} /></div>
  if (error && !s) return <div className="container"><div className="empty"><h3>Story not found</h3><p>{error.message}</p><Link className="btn" to="/">Back to Today</Link></div></div>

  if (s.status === 'researching') {
    return (
      <div className="container narrow" style={{ paddingTop: 40 }}>
        <button className="btn ghost sm" onClick={() => nav(-1)}><Back size={16} /> Back</button>
        <div className="eyebrow" style={{ marginTop: 22 }}>Researching</div>
        <h1 className="h-page" style={{ margin: '8px 0 24px' }}>{s.headline}</h1>
        <div className="card panel"><RunPanel run={run} showLog title="Reading the sources" /></div>
      </div>
    )
  }
  if (s.status === 'failed') {
    return <div className="container narrow" style={{ paddingTop: 40 }}><div className="empty"><h3>We couldn’t finish this one</h3><p>{s.error || 'Research failed.'}</p><Link className="btn primary" to="/explore">Try another topic</Link></div></div>
  }

  const isResearch = s.kind === 'research'
  const ex = s.explain
  const terms = ex?.terms || []
  const total = s.verification?.checked
  const lens = s.lens
  const angleByUrl = Object.fromEntries((lens?.angles || []).map((a) => [a.url, a]))
  const listening = listen.state !== 'idle' && listen.current?.id === id
  const openC = openClaim ? claimsBy[openClaim] : null
  const building = !isResearch && !s.built
  const th = s.thread
  const layers = [['gist', 'The gist'], ['what', 'What happened'], ...(ex ? [['why', 'Why']] : []), ['deeper', 'Go deeper'],
    ...(s.developments?.length ? [['angles', 'Other angles']] : []), ...(s.timeline?.length ? [['timeline', 'How it unfolded']] : []), ...(th?.earlier?.length || th?.later?.length || s.kb_related?.length ? [['thread', 'Story so far']] : []),
    ...(ex?.who?.length || s.graph || s.players?.length ? [['people', 'Who’s who']] : []), ['coverage', 'Coverage']]

  return (
    <div className="container wide">
      <button className="btn ghost sm" onClick={() => nav(-1)} style={{ marginLeft: -10 }}><Back size={16} /> Back</button>
      <div className="story-grid" style={{ marginTop: 14 }}>
        <article className="story fade-in" ref={articleRef} data-topic={s.topic}>
          <div className="story-hero">
            <div className="kicker" style={{ marginBottom: 2 }}>{isResearch ? (s.report?.mode === 'claim' ? 'Fact-check' : 'Research briefing') : s.kind === 'paper' ? 'From your newspaper' : s.kind === 'link' ? 'Explained link' : (s.channel_name || topicLabel(s.topic))}
              <span className="muted" style={{ textTransform: 'none', letterSpacing: 0, fontWeight: 500 }}> · {timeAgo(s.published_at || s.created_at)}{s.built ? ` · ${s.reading_minutes} min read` : ''}</span>
              {!isResearch && s.built && <span className="signal" title="Signal: how significant this story is for anyone — coverage breadth, source quality and the editors’ read of impact. Fit to you is shown separately.">Signal {s.signal}/10</span>}
            </div>
            <h1>{s.headline}</h1>
            {!ex && s.dek && <p className="dek">{s.dek}</p>}
            <div className="story-actions">
              <button className={'btn sm' + (s.saved ? ' primary' : '')} onClick={toggleSave}>{s.saved ? <BookmarkFill size={15} /> : <Bookmark size={15} />} {s.saved ? 'Saved' : 'Save'}</button>
              <button className={'btn sm' + (s.following ? ' primary' : '')} onClick={toggleFollow} title="Get flagged when new coverage of this story appears (f)">{s.following ? '✓ Following' : '+ Follow story'}</button>
              {listen.supported && s.built && <button className={'btn sm' + (listening ? ' primary' : '')} onClick={listenNow} title="Listen (l)">{listening ? '■ Stop' : <><Play size={12} /> Listen</>}</button>}
              {!isResearch && <Link className="btn sm talk" to={`/live?ask=${encodeURIComponent('Tell me about ' + s.headline)}`} title="Talk this story through with Lens, out loud"><Mic size={14} /> Talk it through</Link>}
              {!isResearch && <>
                <button className={'btn sm' + (s.reaction === 'like' ? ' accent' : '')} onClick={() => react('like')}><ThumbUp size={15} /> More like this</button>
                <button className={'btn sm' + (s.reaction === 'dislike' ? ' accent' : '')} onClick={() => react('dislike')}><ThumbDown size={15} /> Less</button>
              </>}
              <button className="btn sm ghost" onClick={copyBrief}>Copy brief</button>
              <button className="btn sm ghost" onClick={shareWhatsApp} title="Send the gist and the reasons to a friend or family group, with the source link">Share on WhatsApp</button>
              <button className="btn sm ghost" onClick={() => navigator.clipboard?.writeText(location.href).then(() => toast('Link copied'))}><LinkIcon size={15} /> Link</button>
            </div>
          </div>

          {s.report?.verdict && (
            <div className={'verdict ' + s.report.verdict} style={{ marginTop: 20 }}>
              <div className="eyebrow">Verdict on “{s.query}”</div>
              <div className="v-label">{s.report.verdict_label}</div>
              <p>{s.report.verdict_reason}</p>
            </div>
          )}

          {/* not explained yet: show what is happening */}
          {building && (
            <div className="gistbox" style={{ fontFamily: 'var(--sans)' }}>
              <span className="lab">{s.building || buildRun ? 'Explaining this story now' : 'Not explained yet'}</span>
              {(buildRun || params.get('run')) && run.status === 'running' ? <BuildStepper run={run} /> : (
                <>
                  <p style={{ fontSize: 16, color: 'var(--ink-2)' }}>{s.building ? 'The writers are on this story right now — it will appear here in a few minutes.' : 'This story is on your front page but has not been read in full yet. Explaining it reads every outlet, traces how it developed and works out why it happened.'}</p>
                  {s.build_error && <p className="small" style={{ color: 'var(--bad)', marginTop: 8 }}>Last attempt failed: {s.build_error}</p>}
                  {buildErr && <p className="small" style={{ color: 'var(--bad)', marginTop: 8 }}>{buildErr}</p>}
                  {!s.building && <button className="btn primary" style={{ marginTop: 12 }} onClick={() => explainNow()} disabled={!health?.llm?.available}><Sparkle size={15} /> Explain this story</button>}
                </>
              )}
            </div>
          )}
          {building && <p className="help" style={{ marginTop: 6 }}>Meanwhile, the outlets that covered it are listed under Coverage below.</p>}

          {!isResearch && ex && (
            <div className="gistbox" id="gist"><span className="lab">The gist, in plain words</span><TermText text={ex.gist} terms={terms} /></div>
          )}
          {!isResearch && s.built && !ex && (
            <div className="callout" style={{ marginTop: 18 }}>
              <b>This story was built before explanations existed.</b> Add the “why did this happen” chain, glossary and history?
              <div style={{ marginTop: 10 }}>
                {run.status === 'running' && buildRun ? <BuildStepper run={run} /> : <button className="btn primary sm" onClick={() => explainNow(true)}><Sparkle size={14} /> Explain it now</button>}
                {buildErr && <span className="small" style={{ color: 'var(--bad)', marginLeft: 10 }}>{buildErr}</span>}
              </div>
            </div>
          )}

          {!building && layers.length > 2 && (
            <nav className="layerbar" aria-label="Layers of this story">
              {layers.map(([k, l]) => <a key={k} href={`#${k}`} className={active === k ? 'on' : ''} onClick={(e) => { e.preventDefault(); document.getElementById(k)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }}>{l}</a>)}
            </nav>
          )}

          {!isResearch && s.built && (
            <>
              <Sec id="what" title="What happened" sub="click a number to see the exact source sentence">
                <div className="prose"><p>
                  {(sentences.length ? sentences : [s.what_happened]).map((t, i) => {
                    const c = claimsBy[`summary:${i}`]
                    return <span key={i}><TermText text={t} terms={terms} />{c && <ClaimChip claim={c} open={openClaim === `summary:${i}`} onToggle={() => setOpenClaim(openClaim === `summary:${i}` ? null : `summary:${i}`)} />} </span>
                  })}
                </p></div>
                {openC && openClaim.startsWith('summary') && <Evidence claim={openC} />}
                {s.key_points?.length > 0 && (
                  <ul className="points" style={{ marginTop: 18 }}>{s.key_points.map((k, i) => {
                    const c = claimsBy[`point:${i}`]
                    const key = `point:${i}`
                    return <li key={i}><div><TermText text={k} terms={terms} />{c && <ClaimChip claim={c} open={openClaim === key} onToggle={() => setOpenClaim(openClaim === key ? null : key)} />}
                      {c && openClaim === key && <Evidence claim={c} />}</div></li>
                  })}</ul>
                )}
              </Sec>

              {ex && (
                <Sec id="why" glyph={<Why size={15} />} title="Why this happened" sub="oldest cause first — tap any underlined word">
                  <WhyChain steps={ex.why} terms={terms} />
                  {ex.so_what && <div className="callout" style={{ marginTop: 6 }}><b>What it means. </b><TermText text={ex.so_what} terms={terms} />{s.why_for_you && <div className="help">For you: {s.why_for_you}.</div>}</div>}
                  {ex.next?.length > 0 && (
                    <div style={{ marginTop: 16 }}>
                      <div className="eyebrow" style={{ marginBottom: 8 }}>What to watch next (from the reporting)</div>
                      <ul className="points">{ex.next.map((n, i) => <li key={i}><div>{n}</div></li>)}</ul>
                    </div>
                  )}
                  {terms.length > 0 && (
                    <details style={{ marginTop: 18 }}>
                      <summary style={{ cursor: 'pointer', fontWeight: 600 }}>Words you might not know ({terms.length})</summary>
                      <div style={{ marginTop: 10 }}><TermList terms={terms} /></div>
                    </details>
                  )}
                </Sec>
              )}

              {!ex && s.why_it_matters && <Sec id="why" title="Why it matters"><div className="callout">{s.why_it_matters}</div></Sec>}

              <Sec id="deeper" glyph={<Sparkle size={15} />} title="Go deeper" sub="pulls in fresh reporting — takes a minute or two">
                <GoDeeper storyId={id} questions={ex?.questions?.length ? ex.questions : DEFAULT_QS} />
              </Sec>
            </>
          )}

          {isResearch && (
            <>
              <section className="section"><div className="prose"><p>{s.what_happened}</p></div></section>
              {s.key_points?.length > 0 && <section className="section"><h2 className="h-section">Key points</h2><ul className="points">{s.key_points.map((k, i) => <li key={i}><div>{k}</div></li>)}</ul></section>}
              {s.report?.sections?.map((sec, i) => (
                <section className="section" key={i}><h2 className="h-section">{sec.heading}</h2><div className="prose"><p>{sec.text}<Cites ns={sec.cites} /></p></div></section>
              ))}
              {s.report?.disagreements?.length > 0 && <section className="section"><h2 className="h-section">Where sources differ</h2><ul className="points">{s.report.disagreements.map((d, i) => <li key={i}><div>{d}</div></li>)}</ul></section>}
              {s.report?.open_questions?.length > 0 && <section className="section"><h2 className="h-section">Still unanswered</h2><ul className="points">{s.report.open_questions.map((d, i) => <li key={i}><div>{d}</div></li>)}</ul></section>}
            </>
          )}

          {s.developments?.length > 0 && (
            <Sec id="angles" glyph={<Users size={15} />} tone="gold" title="Other angles, merged into this story" sub={`${s.developments.length} more ${s.developments.length === 1 ? 'report' : 'reports'} about the same event`}>
              <div className="conn">
                {s.developments.map((d) => (
                  <a key={d.url} href={d.url} target="_blank" rel="noreferrer"><b>{d.title}</b><span>{d.outlet}{d.at ? ` · ${timeAgo(d.at)}` : ''}</span></a>
                ))}
              </div>
              <p className="help">These were separate headlines (a reaction, a follow-up, a profile). They are one story, so they live here.</p>
            </Sec>
          )}

          {s.timeline?.length > 0 && (
            <Sec id="timeline" glyph={<Clock size={15} />} tone="teal" title="How it unfolded" sub={`${s.timeline.length} milestones, each linked to its source`}>
              <Timeline events={s.timeline} today={s.headline} />
            </Sec>
          )}

          {(th?.earlier?.length > 0 || th?.later?.length > 0 || s.kb_related?.length > 0) && (
            <Sec id="thread" glyph={<Clock size={15} />} tone="gold" title="The story so far" sub="from your knowledge base">
              {(th?.earlier?.length > 0 || th?.later?.length > 0) && (
                <div className="threadbox">
                  {th.earlier.map((t) => <div key={t.story_id} className="tb" onClick={() => nav(`/story/${t.story_id}`)}><span className="when">{formatDate(t.at.slice(0, 10))}</span><span>{t.headline}</span></div>)}
                  <div className="tb now"><span className="when">Today</span><b>{s.headline}</b></div>
                  {th.later.map((t) => <div key={t.story_id} className="tb" onClick={() => nav(`/story/${t.story_id}`)}><span className="when">{formatDate(t.at.slice(0, 10))}</span><span>{t.headline}</span></div>)}
                </div>
              )}
              {s.kb_related?.length > 0 && (
                <div style={{ marginTop: 16 }}>
                  <div className="eyebrow" style={{ marginBottom: 8 }}>Connected stories</div>
                  <div className="conn">{s.kb_related.map((r) => <Link key={r.id} to={`/story/${r.id}`}><b>{r.headline}</b><span>{r.reason} · {timeAgo(r.published_at || r.created_at)}</span></Link>)}</div>
                </div>
              )}
            </Sec>
          )}

          {(ex?.who?.length > 0 || s.graph || s.players?.length > 0) && (
            <Sec id="people" glyph={<Users size={15} />} title="Who’s who" sub={s.graph ? `${s.graph.nodes.length} people & organisations, ${s.graph.edges.length} sourced links` : undefined}>
              {ex?.who?.length > 0 && (
                <div className="people" style={{ marginBottom: 14 }}>
                  {ex.who.map((p) => {
                    const kb = s.players?.find((x) => x.name.toLowerCase().includes(p.name.split(' ').slice(-1)[0].toLowerCase()) && x.kb_id)
                    return (
                      <div key={p.name} className="person" onClick={() => kb && nav(`/entity/${kb.kb_id}`)} style={{ cursor: kb ? 'pointer' : 'default' }}>
                        <b>{p.name}</b><span>{p.role}</span>{kb?.story_count > 1 && <span style={{ color: 'var(--teal)' }}>{kb.story_count} stories →</span>}
                      </div>
                    )
                  })}
                </div>
              )}
              {s.graph && <StoryMap nodes={s.graph.nodes} edges={s.graph.edges} />}
              {!ex?.who?.length && !s.graph && <div className="tagbox">{s.players.map((p) => <span key={p.name} className="chip" style={{ cursor: 'pointer' }} onClick={() => nav(`/entity/${p.kb_id}`)}>{p.name}<span className="muted small">{p.type}</span></span>)}</div>}
            </Sec>
          )}

          {s.background?.length > 0 && (
            <Sec id="bg" title="Background" sub="what you may need to know first · from Wikipedia">
              <div className="bg-grid">
                {s.background.map((b) => (
                  <a key={b.title} className="bg-card" href={b.url} target="_blank" rel="noreferrer">
                    {b.thumb && <img src={b.thumb} alt="" loading="lazy" />}
                    <div><b>{b.title}</b><p>{b.extract}</p></div>
                  </a>
                ))}
              </div>
            </Sec>
          )}

          <Sec id="coverage" title="Coverage" sub={`${s.sources.length} sources${lens?.countries?.length > 1 ? ` · ${lens.countries.length} countries` : ''}`}>
            {lens?.countries?.length > 0 && <div className="tagbox" style={{ marginBottom: 14 }}>{lens.countries.map((c) => <span key={c} className="chip outline">{COUNTRY[c] || c}</span>)}</div>}
            {lens?.unique?.length > 0 && (
              <div className="unique">
                <div className="eyebrow" style={{ marginBottom: 8 }}>Reported by only one outlet</div>
                {lens.unique.map((u, i) => <div key={i} className="unique-item"><a href={u.url} target="_blank" rel="noreferrer"><b>{u.outlet}</b></a><span className="serif">“{u.sentence}”</span></div>)}
              </div>
            )}
            <div className="src-list">
              {s.sources.map((r) => {
                const a = angleByUrl[r.url]
                const internal = r.url.startsWith('paper://') || r.url.startsWith('pasted://')
                return (
                  <div className="src" id={`src-${r.n}`} key={r.n}>
                    <div className="n">{r.n}</div>
                    <div>
                      {internal ? <span className="t">{r.title}</span> : <a className="t" href={r.url} target="_blank" rel="noreferrer">{r.title}</a>}
                      <div className="o">{r.outlet}{a?.country ? ` · ${COUNTRY[a.country] || a.country}` : ''} · {timeAgo(r.published_at)}{s.built ? (r.has_full_text ? ' · read in full' : ' · headline only') : ''}</div>
                      {a?.lead && <div className="lead-line">Opens with: {a.lead}</div>}
                    </div>
                    <span className={'trust t' + r.trust_rank}>{r.factcheck ? 'Fact-check' : TRUST[r.trust_rank]}</span>
                  </div>
                )
              })}
            </div>
            {s.built && <p className="help">Headlines above are the outlets’ own. The headline at the top was rewritten to be clearer, using only facts from these sources.</p>}
          </Sec>

          {s.built && (
            <details className="section card" style={{ padding: '14px 18px' }}>
              <summary style={{ cursor: 'pointer', fontWeight: 600 }}>How this story was checked</summary>
              <div style={{ marginTop: 12, display: 'grid', gap: 8 }} className="small">
                {s.verification?.score != null && <div><b>Grounding:</b> {Math.round(s.verification.score * 100)}% of {total} claims traced to the sources{s.verification.unsupported?.length > 0 && ` (${s.verification.unsupported.length} flagged)`}.</div>}
                {s.gate?.explain && <div><b>Explanation:</b> {s.gate.explain.ok ? `${s.gate.explain.grounded_steps} of ${ex?.why?.length ?? 0} steps are backed by a quoted sentence; the rest are labelled “how it works”.` : 'not generated.'}{s.gate.explain.dropped?.length > 0 && ` ${s.gate.explain.dropped.length} unsupported step(s) were dropped.`}</div>}
                {s.gate?.publish && <div><b>Editor review:</b> {s.gate.publish.join('; ')}</div>}
                {s.gate?.triage && <div><b>Selection:</b> {s.gate.triage.join('; ')}</div>}
                {s.gate?.timeline && <div><b>Timeline:</b> {s.gate.timeline}</div>}
                {s.gate?.graph && <div><b>Story map:</b> {s.gate.graph}</div>}
                {s.verification?.unsupported?.map((u, i) => <div key={i} className="chip warn" style={{ height: 'auto', padding: '6px 10px', whiteSpace: 'normal' }}>Flagged: {u.claim} ({u.problems.join(', ')})</div>)}
              </div>
            </details>
          )}
        </article>

        <aside className="story-side"><div className="card side-card"><ChatPanel storyId={id} seed={seed} /></div></aside>
      </div>

      <button className="btn primary fab" onClick={() => setSheet(true)}><Chat size={16} /> Ask</button>
      {sheet && (
        <div className="sheet" onClick={() => setSheet(false)}>
          <div className="card" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Ask about this story">
            <button className="icon-btn" style={{ position: 'absolute', right: 10, top: 10, zIndex: 2 }} onClick={() => setSheet(false)} aria-label="Close"><Close /></button>
            <ChatPanel storyId={id} seed={seed} />
          </div>
        </div>
      )}
      {!isResearch && <Inspector containerRef={articleRef} storyId={id} onAsk={ask} />}
    </div>
  )
}
