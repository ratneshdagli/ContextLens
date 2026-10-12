import { useEffect, useRef, useState } from 'react'
import { api, streamChat } from '../lib/api'
import { Send } from './Icons'

const SUGGEST = ['What are the key facts?', 'Why does this matter?', 'What happens next?', 'Explain it simply']

export default function ChatPanel({ storyId, seed }) {
  const [msgs, setMsgs] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const end = useRef(null)
  const ctl = useRef(null)

  useEffect(() => { api.chatHistory(storyId).then(setMsgs).catch(() => {}); return () => ctl.current?.abort() }, [storyId])
  useEffect(() => { end.current?.scrollIntoView({ block: 'end' }) }, [msgs])
  const lastSeed = useRef(0)

  const ask = async (text) => {
    const q = (text ?? input).trim()
    if (!q || busy) return
    setInput(''); setErr(''); setBusy(true)
    setMsgs((m) => [...m, { role: 'user', content: q }, { role: 'assistant', content: '' }])
    ctl.current = new AbortController()
    try {
      await streamChat(storyId, q, (c) => setMsgs((m) => { const n = [...m]; n[n.length - 1] = { ...n[n.length - 1], content: n[n.length - 1].content + c }; return n }), ctl.current.signal)
    } catch (e) {
      if (e.name !== 'AbortError') { setErr(e.message || 'Something went wrong'); setMsgs((m) => (m[m.length - 1]?.content ? m : m.slice(0, -1))) }
    } finally { setBusy(false) }
  }

  useEffect(() => {
    if (seed?.text && seed.n !== lastSeed.current) { lastSeed.current = seed.n; ask(seed.text) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed])

  return (
    <>
      <div className="chat-head">Ask about this story<div className="small muted" style={{ fontWeight: 400 }}>Answers use only this story’s reporting.</div></div>
      <div className="chat-body">
        {msgs.length === 0 && <div className="suggest">{SUGGEST.map((s) => <button key={s} className="chip outline" onClick={() => ask(s)}>{s}</button>)}</div>}
        {msgs.map((m, i) => <div key={i} className={'msg ' + m.role}>{m.content || (busy && i === msgs.length - 1 ? <span className="spinner" /> : '')}</div>)}
        {err && <div className="chip bad" style={{ height: 'auto', padding: '6px 10px', whiteSpace: 'normal' }}>{err}</div>}
        <div ref={end} />
      </div>
      <form className="chat-input" onSubmit={(e) => { e.preventDefault(); ask() }}>
        <input className="field" value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask a question…" aria-label="Ask a question" />
        <button className="btn primary" disabled={busy || !input.trim()} aria-label="Send"><Send size={16} /></button>
      </form>
    </>
  )
}
