import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { streamAsk } from '../lib/api'
import { Send } from './Icons'

const EXAMPLES = ['What happened in the markets today?', 'What should I know about AI policy this week?', 'Which stories involve India?']

/** Render answer text turning [S1] markers into links to the source stories. */
function Answer({ text, sources }) {
  const parts = text.split(/(\[S\d+\])/g)
  return (
    <>
      {parts.map((p, i) => {
        const m = p.match(/^\[S(\d+)\]$/)
        const src = m && sources[Number(m[1]) - 1]
        if (src) return <Link key={i} to={`/story/${src.id}`} className="cite" title={src.headline}>{m[1]}</Link>
        return <span key={i}>{p.replace(/\*\*(.+?)\*\*/g, '$1')}</span>
      })}
    </>
  )
}

export default function AskBriefing({ disabled }) {
  const [q, setQ] = useState('')
  const [answer, setAnswer] = useState('')
  const [sources, setSources] = useState([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const ctl = useRef(null)

  const ask = async (text) => {
    const question = (text ?? q).trim()
    if (question.length < 4 || busy) return
    setQ(question); setAnswer(''); setSources([]); setErr(''); setBusy(true)
    ctl.current?.abort()
    ctl.current = new AbortController()
    try {
      await streamAsk(question, setSources, (c) => setAnswer((a) => a + c), ctl.current.signal)
    } catch (e) { if (e.name !== 'AbortError') setErr(e.message || 'Something went wrong') } finally { setBusy(false) }
  }

  return (
    <div className="ask-box">
      <div className="eyebrow" style={{ marginBottom: 8 }}>Ask your briefing</div>
      <form onSubmit={(e) => { e.preventDefault(); ask() }} style={{ display: 'flex', gap: 8 }}>
        <input className="field" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask a question about today’s stories…" aria-label="Ask your briefing" disabled={disabled} />
        <button className="btn primary" disabled={busy || disabled || q.trim().length < 4} aria-label="Ask"><Send size={16} /></button>
      </form>
      {!answer && !busy && <div className="suggest" style={{ marginTop: 10 }}>{EXAMPLES.map((e) => <button key={e} className="chip outline" onClick={() => ask(e)} disabled={disabled}>{e}</button>)}</div>}
      {(answer || busy) && (
        <div className="ask-answer" aria-live="polite">
          {answer ? <Answer text={answer} sources={sources} /> : <span className="spinner" />}
        </div>
      )}
      {err && <p className="help" style={{ color: 'var(--bad)' }}>{err}</p>}
      {sources.length > 0 && answer && (
        <div className="ask-src">{sources.map((s) => <Link key={s.id} to={`/story/${s.id}`} className="chip outline" title={s.headline}><b>{s.i ?? ''}</b> {s.headline.slice(0, 48)}{s.headline.length > 48 ? '…' : ''}</Link>)}</div>
      )}
    </div>
  )
}
