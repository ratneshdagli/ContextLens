import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, uploadPaper } from '../lib/api'
import { timeAgo } from '../lib/format'
import { useAsync, useRun } from '../lib/hooks'
import RunPanel from '../components/RunPanel'
import { Paper, Upload } from '../components/Icons'
import { useApp } from '../components/Shell'

export default function Papers() {
  const nav = useNavigate()
  const { toast, health } = useApp()
  const { data, reload } = useAsync(() => api.papers(), [])
  const [over, setOver] = useState(false)
  const [busy, setBusy] = useState(false)
  const [runId, setRunId] = useState(null)
  const [paperId, setPaperId] = useState(null)
  const [top, setTop] = useState(5)
  const input = useRef(null)
  const run = useRun(runId, { onDone: (st) => { reload(true); if (st === 'complete' && paperId) nav(`/papers/${paperId}`) } })
  const down = health && !health.llm?.available

  const send = async (file) => {
    if (!file) return
    if (!/\.pdf$/i.test(file.name)) { toast('Please choose a PDF file'); return }
    setBusy(true)
    try {
      const r = await uploadPaper(file, top)
      setPaperId(r.paper_id); setRunId(r.run_id)
    } catch (e) { toast(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="container narrow">
      <div className="eyebrow">Newspapers</div>
      <h1 className="h-page" style={{ margin: '6px 0 8px' }}>Read a whole paper in minutes</h1>
      <p className="lede" style={{ marginBottom: 22 }}>Drop in today’s e-paper PDF. ContextLens splits it into articles, skips the ads and classifieds, keeps what matters to <i>you</i>, and explains each one — with background, timeline and links to what you already know.</p>

      {runId && run.status !== 'idle' ? (
        <div className="card panel">
          <RunPanel run={run} showLog title={run.status === 'failed' ? 'That did not work' : 'Reading your newspaper'} />
          {run.status === 'failed' && <button className="btn" style={{ marginTop: 12 }} onClick={() => { setRunId(null); setPaperId(null) }}>Try another file</button>}
        </div>
      ) : (
        <div className={'drop' + (over ? ' over' : '')}
          onDragOver={(e) => { e.preventDefault(); setOver(true) }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); send(e.dataTransfer.files?.[0]) }}>
          <Upload size={34} />
          <h3>Drop a newspaper PDF here</h3>
          <p>Works with digital e-papers (text-based PDFs). Scanned images have no text to read and are not supported yet. Everything stays on this computer.</p>
          <input ref={input} type="file" accept="application/pdf,.pdf" hidden onChange={(e) => send(e.target.files?.[0])} />
          <div style={{ display: 'flex', gap: 10, justifyContent: 'center', alignItems: 'center', flexWrap: 'wrap' }}>
            <button className="btn primary lg" disabled={busy || down} onClick={() => input.current?.click()}>{busy ? 'Uploading…' : 'Choose a PDF'}</button>
            <label className="small muted" style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>Explain the best
              <select className="field" style={{ width: 72, height: 34, padding: '0 8px' }} value={top} onChange={(e) => setTop(Number(e.target.value))}>{[3, 5, 8, 12].map((n) => <option key={n} value={n}>{n}</option>)}</select>
              now
            </label>
          </div>
          {down && <p className="help" style={{ color: 'var(--bad)' }}>The model server is offline; start LM Studio to explain articles.</p>}
        </div>
      )}

      {data?.length > 0 && (
        <>
          <div className="sec-head"><h2>Your newspapers</h2></div>
          <div className="paper-grid">
            {data.map((p) => (
              <div key={p.id} className="paper-card" onClick={() => nav(`/papers/${p.id}`)}>
                <span className="kicker" style={{ '--tc': 'var(--teal)' }}><Paper size={14} /> {p.status === 'ready' ? 'Ready' : p.status === 'failed' ? 'Failed' : 'Reading…'}</span>
                <b>{p.name}</b>
                <span className="small muted">{p.pages} {p.pages === 1 ? 'page' : 'pages'} · {p.article_count} articles · <b style={{ color: 'var(--ink)' }}>{p.matched_count}</b> matter to you</span>
                <span className="small muted">Uploaded {timeAgo(p.uploaded_at)}{p.error ? ` — ${p.error}` : ''}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
