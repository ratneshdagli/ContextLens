import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { useAsync, useRun } from '../lib/hooks'
import { Logo } from '../components/Icons'
import RunPanel from '../components/RunPanel'

const EXAMPLES = ['AI chips & semiconductors', 'Indian economy and RBI', 'Formula 1', 'Climate policy', 'Startups and venture funding', 'Middle East conflict', 'Space exploration', 'Cricket']

export default function Onboarding() {
  const nav = useNavigate()
  const { data: topics } = useAsync(() => api.topics(), [])
  const [step, setStep] = useState(1)
  const [picked, setPicked] = useState([])
  const [about, setAbout] = useState('')
  const [muted, setMuted] = useState('')
  const [level, setLevel] = useState('simple')
  const [runId, setRunId] = useState(null)
  const [err, setErr] = useState('')
  const run = useRun(runId)

  const toggle = (id) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]))
  const addExample = (t) => setAbout((a) => (a.toLowerCase().includes(t.toLowerCase()) ? a : (a ? a.replace(/[,\s]+$/, '') + ', ' : '') + t))

  const build = async () => {
    setErr('')
    try {
      await api.onboard({ topics: picked, about, muted: muted.split(',').map((s) => s.trim()).filter(Boolean) })
      await api.saveSettings({ reading_level: level })
      setStep(3)
      const r = await api.refresh()
      setRunId(r.run_id)
    } catch (e) { setErr(e.message) }
  }

  return (
    <div className="ob">
      <div className="ob-card fade-in">
        <div className="brand" style={{ padding: '0 0 22px' }}><span className="brand-mark"><Logo /></span>ContextLens</div>
        <div className="ob-steps">{[1, 2, 3].map((n) => <i key={n} className={step >= n ? 'on' : ''} />)}</div>

        {step === 1 && (
          <>
            <h1 className="h-page">What do you want to follow?</h1>
            <p className="lede" style={{ marginTop: 10 }}>Pick the areas you care about. Every story is checked against these before you see it.</p>
            <div className="topic-grid">
              {(topics || []).map((t) => (
                <button key={t.id} className={'topic' + (picked.includes(t.id) ? ' on' : '')} onClick={() => toggle(t.id)} aria-pressed={picked.includes(t.id)}>
                  <b>{t.label}</b><span>{t.desc.split(',').slice(0, 3).join(',')}</span>
                </button>
              ))}
            </div>
            <button className="btn primary lg" style={{ width: '100%' }} disabled={picked.length < 2} onClick={() => setStep(2)}>
              {picked.length < 2 ? `Pick ${2 - picked.length} more` : 'Continue'}
            </button>
          </>
        )}

        {step === 2 && (
          <>
            <h1 className="h-page">Anything more specific?</h1>
            <p className="lede" style={{ marginTop: 10 }}>Describe your interests in your own words. This is what the editors use to decide what’s worth your time — the more specific, the better.</p>
            <label className="label" style={{ marginTop: 22 }} htmlFor="about">I care about…</label>
            <textarea id="about" className="field" value={about} onChange={(e) => setAbout(e.target.value)} placeholder="e.g. AI chips and semiconductors, Indian economy and RBI decisions, Formula 1" />
            <div className="suggest" style={{ marginTop: 10 }}>{EXAMPLES.map((e) => <button key={e} className="chip outline" onClick={() => addExample(e)}>+ {e}</button>)}</div>
            <label className="label" style={{ marginTop: 22 }} htmlFor="muted">Never show me (optional)</label>
            <input id="muted" className="field" value={muted} onChange={(e) => setMuted(e.target.value)} placeholder="celebrity gossip, horoscope, …" />
            <p className="help">Comma-separated. You can change all of this later in Settings.</p>
            <label className="label" style={{ marginTop: 22 }}>How should stories be explained?</label>
            <div className="seg" role="group">{[['simple', 'Simple words'], ['general', 'Everyday adult'], ['expert', 'Expert']].map(([v, l]) => <button key={v} className={level === v ? 'on' : ''} onClick={() => setLevel(v)} aria-pressed={level === v}>{l}</button>)}</div>
            <p className="help">“Simple words” explains jargon and the background as if you had never followed the topic. You can change this any time.</p>
            {err && <p className="help" style={{ color: 'var(--bad)' }}>{err}</p>}
            <div style={{ display: 'flex', gap: 10, marginTop: 26 }}>
              <button className="btn lg" onClick={() => setStep(1)}>Back</button>
              <button className="btn primary lg" style={{ flex: 1 }} onClick={build}>Build my briefing</button>
            </div>
          </>
        )}

        {step === 3 && (
          <>
            <h1 className="h-page">Building your first briefing</h1>
            <p className="lede" style={{ marginTop: 10, marginBottom: 24 }}>We’re reading today’s newspapers and grouping them by event. Your front page appears within a few minutes; after that, writers explain each story — what happened, why, and how we got here — one by one, best first.</p>
            <div className="card panel"><RunPanel run={run} showLog /></div>
            <div style={{ display: 'flex', gap: 10, marginTop: 20 }}>
              <button className="btn primary lg" style={{ flex: 1 }} onClick={() => nav('/', { replace: true })}>
                {run.events.some((e) => e.stage === 'wire') ? 'Open my briefing' : run.status === 'running' ? 'Continue in the background' : 'Open my briefing'}
              </button>
            </div>
            {run.status === 'failed' && <p className="help" style={{ color: 'var(--bad)' }}>The run failed: {run.last?.message}. Check that LM Studio is running, then use Refresh on the home page.</p>}
          </>
        )}
      </div>
    </div>
  )
}
