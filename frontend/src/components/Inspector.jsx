import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { timeAgo } from '../lib/format'
import { Chat, Clock, Close, Flask, Users, Wand, Why } from './Icons'

const TABS = [
  ['explain', 'Explain', Wand],
  ['why', 'Why?', Why],
  ['who', 'Who / what', Users],
  ['forensics', 'Forensics', Flask],
  ['history', 'History', Clock],
  ['check', 'Fact-check', null],
]

function Notes({ notes }) {
  return notes.map((n, i) => (
    <div key={i} className={'note ' + (n.level === 'warn' ? 'warn' : n.level === 'good' ? 'good' : '')}>
      <b>{n.level === 'warn' ? '⚠' : n.level === 'good' ? '✓' : '•'}</b><span>{n.text}</span>
    </div>
  ))
}

function Forensics({ d }) {
  return (
    <>
      <Notes notes={d.notes || []} />
      <div className="fx-grid">
        <div className="fx-stat"><span>Type of evidence</span><b>{d.evidence_type}</b></div>
        <div className="fx-stat"><span>Outlets that say it</span><b>{d.outlets_supporting} of {d.outlets_total}</b></div>
        <div className="fx-stat"><span>Attributed to</span><b>{d.attributed_to?.length ? d.attributed_to.join(', ') : d.unnamed_sources?.length ? 'unnamed source' : 'no one in the passage'}</b></div>
        <div className="fx-stat"><span>First seen</span><b>{d.first_seen ? timeAgo(d.first_seen) : '—'}</b></div>
      </div>
      {d.support?.length > 0 && (
        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>Where else it appears</div>
          {d.support.map((s, i) => <div key={i} className="fx-quote">“{s.quote}”<small><a href={s.url} target="_blank" rel="noreferrer" style={{ textDecoration: 'underline' }}>{s.outlet}</a></small></div>)}
        </div>
      )}
      {d.conflicts?.length > 0 && (
        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>Where outlets disagree</div>
          {d.conflicts.map((s, i) => <div key={i} className="fx-quote bad">“{s.quote}”<small><a href={s.url} target="_blank" rel="noreferrer" style={{ textDecoration: 'underline' }}>{s.outlet}</a> · numbers: {s.numbers.join(', ')}</small></div>)}
        </div>
      )}
      {d.also_reported?.length > 0 && (
        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>Other outlets in your archive</div>
          {d.also_reported.map((a, i) => <div key={i} className="small" style={{ padding: '4px 0' }}><a href={a.url} target="_blank" rel="noreferrer" style={{ textDecoration: 'underline' }}>{a.outlet}</a>: {a.title}</div>)}
        </div>
      )}
      {d.loaded_words?.length > 0 && <div className="small muted">Charged wording found: {d.loaded_words.map((w) => <span key={w} className="hl">{w}</span>)}</div>}
      <div className="hint">Forensics is rule-based and uses no AI model — it only measures what the text and the other outlets actually say.</div>
    </>
  )
}

function Result({ tab, s }) {
  if (!s || s.status === 'loading') return <div className="small muted"><span className="spinner" style={{ marginRight: 8, verticalAlign: -3 }} />Working on it… local models can take up to a minute.</div>
  if (s.status === 'error') return <div className="note warn"><b>⚠</b><span>{s.error}</span></div>
  const d = s.data
  if (tab === 'explain') {
    return (
      <>
        <p className="ans">{d.answer}</p>
        <div className="hint">{d.curated ? 'Curated plain-language definition.' : 'Written by the local model from this story’s reporting plus general definitions.'}</div>
      </>
    )
  }
  if (tab === 'why') {
    return (
      <>
        <p className="ans">{d.answer}</p>
        <div className="hint">{d.known ? `Based on this story’s reporting${d.earlier_events ? ` and ${d.earlier_events} earlier events` : ''}.` : 'The reporting does not fully explain this — nothing was guessed.'}</div>
      </>
    )
  }
  if (tab === 'who') {
    return (
      <>
        <div><b style={{ fontFamily: 'var(--display)', fontSize: 20 }}>{d.name}</b> {d.type !== 'unknown' && <span className="chip">{d.type}</span>}</div>
        {d.role_in_story && <p className="ans">In this story: {d.role_in_story}.</p>}
        {d.summary && <p className="ans">{d.summary} {d.wiki_url && <a href={d.wiki_url} target="_blank" rel="noreferrer" className="small" style={{ textDecoration: 'underline' }}>Wikipedia</a>}</p>}
        {!d.known && <p className="muted">No reliable background found for this. Try selecting the full name.</p>}
        {d.kb_id && <Link className="btn sm" to={`/entity/${d.kb_id}`}>All stories about {d.name}</Link>}
        {d.stories?.length > 0 && d.stories.map((x) => <div key={x.id} className="thread-item"><Link to={`/story/${x.id}`}><b>{x.headline}</b></Link></div>)}
      </>
    )
  }
  if (tab === 'forensics') return <Forensics d={d} />
  if (tab === 'history') {
    return d.stories?.length
      ? d.stories.map((x) => <div key={x.id} className="thread-item"><Link to={`/story/${x.id}`}><b>{x.headline}</b></Link><small>{timeAgo(x.at)} · {x.why}</small></div>)
      : <p className="muted">Nothing earlier about this in your knowledge base yet.</p>
  }
  return null
}

export default function Inspector({ containerRef, storyId, onAsk }) {
  const nav = useNavigate()
  const [sel, setSel] = useState(null)             // floating toolbar
  const [panel, setPanel] = useState(null)         // {text, context, tab}
  const [results, setResults] = useState({})
  const [hint, setHint] = useState(() => localStorage.getItem('cl_selhint') !== '1')
  const timer = useRef(null)

  const read = useCallback(() => {
    const s = window.getSelection()
    const root = containerRef.current
    if (!s || s.isCollapsed || !root || s.rangeCount === 0) { setSel(null); return }
    const range = s.getRangeAt(0)
    if (!root.contains(range.commonAncestorContainer)) { setSel(null); return }
    if (range.commonAncestorContainer.parentElement?.closest('.insp, input, textarea, .chat-body')) { setSel(null); return }
    const text = s.toString().replace(/\s+/g, ' ').trim()
    if (text.length < 2 || text.length > 1500) { setSel(null); return }
    const rect = range.getBoundingClientRect()
    const block = (range.startContainer.nodeType === 3 ? range.startContainer.parentElement : range.startContainer)?.closest('p,li,dd,div,blockquote')
    const context = (block?.textContent || text).replace(/\s+/g, ' ').trim().slice(0, 800)
    setSel({ text, context, x: Math.max(150, Math.min(window.innerWidth - 150, rect.left + rect.width / 2)), y: Math.max(56, rect.top - 10) })
  }, [containerRef])

  useEffect(() => {
    const later = () => { clearTimeout(timer.current); timer.current = setTimeout(read, 60) }
    document.addEventListener('mouseup', later)
    document.addEventListener('keyup', later)
    document.addEventListener('touchend', later)
    document.addEventListener('selectionchange', later)
    return () => {
      clearTimeout(timer.current)
      for (const ev of ['mouseup', 'keyup', 'touchend', 'selectionchange']) document.removeEventListener(ev, later)
    }
  }, [read])

  const run = useCallback(async (tab, text, context) => {
    if (tab === 'check') return
    const key = `${tab}|${text}`
    setResults((r) => (r[key]?.status === 'ok' ? r : { ...r, [key]: { status: 'loading' } }))
    try {
      const data = await api.inspect({ story_id: storyId, text, action: tab, context })
      setResults((r) => ({ ...r, [key]: { status: 'ok', data } }))
    } catch (e) {
      setResults((r) => ({ ...r, [key]: { status: 'error', error: e.message } }))
    }
  }, [storyId])

  const open = (tab) => {
    if (!sel) return
    if (tab === 'ask') { onAsk?.(sel.text); setSel(null); window.getSelection()?.removeAllRanges(); return }
    setPanel({ text: sel.text, context: sel.context, tab })
    run(tab, sel.text, sel.context)
    setSel(null)
    setHint(false); localStorage.setItem('cl_selhint', '1')
  }
  const switchTab = (tab) => { setPanel((p) => ({ ...p, tab })); run(tab, panel.text, panel.context) }
  const factCheck = async () => {
    try {
      const r = await api.research(panel.text.slice(0, 200), 'claim')
      nav(`/story/${r.story_id}?run=${r.run_id}`)
    } catch (e) { setResults((x) => ({ ...x, [`check|${panel.text}`]: { status: 'error', error: e.message } })) }
  }

  const cur = panel && results[`${panel.tab}|${panel.text}`]
  return (
    <>
      {sel && (
        <div className="selbar" style={{ left: sel.x, top: sel.y }} onMouseDown={(e) => e.preventDefault()} role="toolbar" aria-label="Understand the selected text">
          <button onClick={() => open('explain')}><Wand size={14} />Explain</button>
          <button onClick={() => open('why')}><Why size={14} />Why?</button>
          <button onClick={() => open('who')}><Users size={14} />Who / what</button>
          <button onClick={() => open('forensics')}><Flask size={14} />Forensics</button>
          <button onClick={() => open('ask')}><Chat size={14} />Ask</button>
        </div>
      )}
      {hint && !panel && !sel && <div className="sel-hint"><Wand size={14} />Select any text to explain it, trace it or check it<button className="icon-btn" style={{ width: 22, height: 22 }} onClick={() => { setHint(false); localStorage.setItem('cl_selhint', '1') }} aria-label="Dismiss"><Close size={12} /></button></div>}
      {panel && (
        <aside className="insp" role="dialog" aria-label="Understand this text">
          <div className="insp-head">
            <div className="sel">“{panel.text}”</div>
            <button className="icon-btn" onClick={() => setPanel(null)} aria-label="Close"><Close size={16} /></button>
          </div>
          <div className="insp-tabs">
            {TABS.map(([k, l]) => <button key={k} className={panel.tab === k ? 'on' : ''} onClick={() => switchTab(k)}>{l}</button>)}
          </div>
          <div className="insp-body">
            {panel.tab === 'check'
              ? (
                <>
                  <p className="ans">Run a full fact-check on this passage: ContextLens searches several outlets, reads them, and returns a verdict with citations. It never gives a decisive verdict on fewer than two independent outlets.</p>
                  <button className="btn accent" onClick={factCheck}>Fact-check this (about 3 minutes)</button>
                  {cur?.status === 'error' && <div className="note warn"><b>⚠</b><span>{cur.error}</span></div>}
                </>
              )
              : <Result tab={panel.tab} s={cur} />}
          </div>
        </aside>
      )}
    </>
  )
}
