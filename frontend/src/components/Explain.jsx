import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { formatDate } from '../lib/format'
import { useRun } from '../lib/hooks'
import { Close } from './Icons'

/* ── glossary popovers inside running text ─────────────────────────────────── */

function Term({ term, meaning, curated, children }) {
  const [pos, setPos] = useState(null)
  const ref = useRef(null)
  useEffect(() => {
    if (!pos) return undefined
    const off = (e) => { if (!ref.current?.contains(e.target)) setPos(null) }
    document.addEventListener('mousedown', off)
    document.addEventListener('touchstart', off)
    return () => { document.removeEventListener('mousedown', off); document.removeEventListener('touchstart', off) }
  }, [pos])
  const open = () => {
    const r = ref.current.getBoundingClientRect()
    setPos({ x: Math.max(12, Math.min(window.innerWidth - 332, r.left)), y: r.bottom + 8 > window.innerHeight - 140 ? r.top - 130 : r.bottom + 8 })
  }
  return (
    <span ref={ref} className={'term' + (pos ? ' open' : '')} tabIndex={0} role="button" onClick={() => (pos ? setPos(null) : open())}
      onKeyDown={(e) => { if (e.key === 'Enter') open(); if (e.key === 'Escape') setPos(null) }} aria-label={`${term}: tap for meaning`}>
      {children}
      {pos && (
        <span className="termpop" style={{ left: pos.x, top: pos.y }} role="tooltip">
          <b>{term}</b>{meaning}
          <small>{curated ? 'Plain-language definition' : 'General definition — not from the reporting'}</small>
        </span>
      )}
    </span>
  )
}

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** Underline the first occurrence of each glossary term in `text`; tap for the meaning. */
export function TermText({ text, terms }) {
  if (!text) return null
  const list = (terms || []).filter((t) => t.term && t.meaning)
  if (!list.length) return <>{text}</>
  const out = []
  let rest = text
  const used = new Set()
  let k = 0
  for (;;) {
    let best = null
    for (const t of list) {
      if (used.has(t.term.toLowerCase())) continue
      const m = new RegExp(`(?<![A-Za-z])${esc(t.term)}(?![A-Za-z])`, 'i').exec(rest)
      if (m && (!best || m.index < best.m.index)) best = { m, t }
    }
    if (!best) break
    const { m, t } = best
    if (m.index) out.push(rest.slice(0, m.index))
    out.push(<Term key={k++} term={t.term} meaning={t.meaning} curated={t.curated}>{m[0]}</Term>)
    used.add(t.term.toLowerCase())
    rest = rest.slice(m.index + m[0].length)
  }
  out.push(rest)
  return <>{out}</>
}

export function TermList({ terms }) {
  if (!terms?.length) return null
  return (
    <dl className="termlist">
      {terms.map((t) => <div key={t.term}><dt>{t.term}</dt><dd>{t.meaning}</dd></div>)}
    </dl>
  )
}

/* ── the causal chain ("why did this happen?") ─────────────────────────────── */

const BASIS = {
  reported: ['Reported today', 'Stated in today’s coverage'],
  history: ['Earlier coverage', 'Stated in earlier reporting on this story'],
  general: ['How it works', 'General knowledge about how this kind of thing works — not from the reporting'],
}

export function WhyChain({ steps, terms }) {
  const [open, setOpen] = useState(null)
  if (!steps?.length) return null
  return (
    <ol className="chain">
      {steps.map((s, i) => (
        <li className="step" key={i}>
          <span className="num">{i + 1}</span>
          <div>
            <div className="cause"><TermText text={s.cause} terms={terms} /></div>
            <div className="link"><TermText text={s.link} terms={terms} /></div>
            <span className={'basis ' + s.basis} title={BASIS[s.basis]?.[1]}>{BASIS[s.basis]?.[0] || s.basis}{s.date ? ` · ${formatDate(s.date)}` : ''}</span>
            {s.evidence && <button className="evi-btn" onClick={() => setOpen(open === i ? null : i)}>{open === i ? 'hide the sentence' : 'show the sentence'}</button>}
            {open === i && s.evidence && (
              <div className="evi fade-in">“{s.evidence}”{s.url && <a href={s.url} target="_blank" rel="noreferrer">{s.outlet || 'source'}</a>}</div>
            )}
          </div>
        </li>
      ))}
    </ol>
  )
}

/* ── build progress ("explaining this story…") ────────────────────────────── */

const STEPS = [
  ['Reading', 'Reading the outlets that covered it'],
  ['Writing the story card', 'Writing a clear headline and summary'],
  ['Finding the people', 'Finding the people, places and organisations'],
  ['Tracing', 'Tracing how the story developed'],
  ['Working out', 'Working out why it happened'],
  ['Editor', 'A second model is checking the draft'],
]

export function BuildStepper({ run }) {
  let cur = 0
  for (const e of run.events) {
    const i = STEPS.findIndex(([k]) => e.message?.includes(k))
    if (i > cur) cur = i
  }
  const done = run.status === 'complete'
  return (
    <ol className="stepper">
      {STEPS.map(([, label], i) => (
        <li key={label} className={done || i < cur ? 'done' : i === cur ? 'cur' : ''}><i>{(done || i < cur) && '✓'}</i>{label}</li>
      ))}
    </ol>
  )
}

/* ── go deeper ─────────────────────────────────────────────────────────────── */

function DivePanel({ storyId, kind, arg, label, onClose }) {
  const [state, setState] = useState({ status: 'starting', result: null, runId: null, error: '' })
  const finish = () => api.diveGet(storyId, kind, arg).then((result) => setState((s) => ({ ...s, status: 'done', result })))
    .catch(() => setState((s) => ({ ...s, status: 'error', error: 'Could not finish this one. The reporting may not cover it.' })))
  useEffect(() => {
    let alive = true
    api.dive(storyId, kind, arg).then((r) => {
      if (!alive) return
      if (r.cached) setState({ status: 'done', result: r.result, runId: null, error: '' })
      else setState({ status: 'running', result: null, runId: r.run_id, error: '' })
    }).catch((e) => alive && setState({ status: 'error', result: null, runId: null, error: e.message }))
    return () => { alive = false }
  }, [storyId, kind, arg])
  const run = useRun(state.status === 'running' ? state.runId : null, {
    onDone: (st) => (st === 'complete' ? finish() : setState((s) => ({ ...s, status: 'error', error: 'The reporting did not support a reliable answer.' }))),
  })
  const r = state.result
  return (
    <div className="dive">
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
        <h4>{r?.title || label}</h4>
        <button className="icon-btn" onClick={onClose} aria-label="Close"><Close size={16} /></button>
      </div>
      {(state.status === 'starting' || state.status === 'running') && (
        <div className="small muted"><span className="spinner" style={{ marginRight: 8, verticalAlign: -3 }} />{run.last?.message || 'Looking for more reporting…'}</div>
      )}
      {state.status === 'error' && <p className="small" style={{ color: 'var(--bad)' }}>{state.error}</p>}
      {r?.paragraphs?.map((p, i) => (
        <p key={i}>
          {p.text}
          {p.sources?.length > 0 && <span className="cites">{p.sources.map((s, j) => <a key={j} href={s.url} target="_blank" rel="noreferrer" title={s.outlet}>{s.outlet?.slice(0, 14) || j + 1}</a>)}</span>}
          {p.basis === 'general' && <span className="small muted"> (general definition)</span>}
        </p>
      ))}
      {r?.kind === 'who' && r.kb_id && <p style={{ marginTop: 10 }}><Link className="btn sm" to={`/entity/${r.kb_id}`}>Everything we know about {r.title}</Link></p>}
      {r?.stories?.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="eyebrow" style={{ marginBottom: 4 }}>Also in the news</div>
          {r.stories.map((s) => <div key={s.id} className="thread-item"><Link to={`/story/${s.id}`}><b>{s.headline}</b></Link></div>)}
        </div>
      )}
      {r?.extra_sources > 0 && <div className="hint">Used this story’s reporting plus {r.extra_sources} additional article{r.extra_sources > 1 ? 's' : ''} found just now.</div>}
    </div>
  )
}

export function GoDeeper({ storyId, questions }) {
  const [open, setOpen] = useState([])
  const bottom = useRef(null)
  const key = (q) => `${q.kind}|${q.arg || ''}`
  const toggle = (q) => setOpen((o) => (o.some((x) => key(x) === key(q)) ? o.filter((x) => key(x) !== key(q)) : [...o, q]))
  useLayoutEffect(() => { /* keep the newest panel in view */ if (open.length) bottom.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }) }, [open.length])
  if (!questions?.length) return null
  return (
    <>
      <div className="godeeper">
        {questions.map((q) => <button key={key(q)} className={'q' + (open.some((x) => key(x) === key(q)) ? ' on' : '')} onClick={() => toggle(q)}>{q.q}</button>)}
      </div>
      {open.map((q) => <DivePanel key={key(q)} storyId={storyId} kind={q.kind} arg={q.arg || ''} label={q.q} onClose={() => toggle(q)} />)}
      <div ref={bottom} />
    </>
  )
}
