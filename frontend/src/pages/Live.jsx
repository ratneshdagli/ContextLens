import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import { topicLabel } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { englishVoices, useVoice, voiceSupport } from '../lib/voice'
import { useApp } from '../components/Shell'
import { Clock, Close, Compass, External, Flow, Gear, Mic, Repeat, Send, Sliders, Sparkle, Stop, Users, Why } from '../components/Icons'

const LABEL = { idle: 'Tap the orb and talk', listening: 'Listening…', thinking: 'Thinking…', speaking: 'Speaking' }
const LANGS = [['en-IN', 'English (India)'], ['en-US', 'English (US)'], ['en-GB', 'English (UK)'], ['en-AU', 'English (Australia)']]
const FOLLOW = [
  { q: 'Why did this happen?', icon: Why },
  { q: 'How did we get here?', icon: Clock },
  { q: 'What happens next?', icon: Sparkle },
  { q: 'Tell me more', icon: Users },
]
// What the small status pill shows while a turn is in flight — an icon plus a short label, so you can tell
// "thinking" from "stuck behind the newsroom's own refresh" from "reading the web" at a glance, not just guess.
const PHASE = {
  think: { icon: Gear, label: 'Thinking' },
  queued: { icon: Clock, label: 'Waiting for the GPU' },
  build: { icon: Compass, label: 'Reading the coverage' },
  answering: { icon: Sparkle, label: 'Answering' },
  dig: { icon: Flow, label: 'Digging on the web' },
}

function Orb({ state, level, onClick }) {
  const el = useRef(null)
  useEffect(() => {
    let raf
    const tick = () => { el.current?.style.setProperty('--lvl', level.current.toFixed(3)); raf = requestAnimationFrame(tick) }
    tick()
    return () => cancelAnimationFrame(raf)
  }, [level])
  return (
    <button ref={el} className="orb" data-state={state} onClick={onClick} aria-label={state === 'idle' ? 'Start talking to Lens' : 'Stop the conversation'}>
      <span className="ripple r1" /><span className="ripple r2" /><span className="ripple r3" />
      <span className="core"><span className="blob b1" /><span className="blob b2" /><span className="blob b3" /><span className="sheen" /></span>
    </button>
  )
}

function Settings({ prefs, setPrefs, onClose }) {
  const [, tick] = useState(0)
  useEffect(() => {
    if (!('speechSynthesis' in window)) return undefined
    const h = () => tick((n) => n + 1)
    window.speechSynthesis.addEventListener('voiceschanged', h)
    return () => window.speechSynthesis.removeEventListener('voiceschanged', h)
  }, [])
  const voices = englishVoices()
  return (
    <div className="live-pop" role="dialog" aria-label="Voice settings">
      <div className="live-pop-head"><b>Voice settings</b><button className="icon-btn" onClick={onClose} aria-label="Close"><Close size={16} /></button></div>
      <label>I speak<select className="field" value={prefs.lang} onChange={(e) => setPrefs({ lang: e.target.value })}>{LANGS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label>Lens’s voice
        <select className="field" value={prefs.voice} onChange={(e) => setPrefs({ voice: e.target.value })}>
          <option value="">Best available</option>
          {voices.map((v) => <option key={v.name} value={v.name}>{v.name}</option>)}
        </select>
      </label>
      <label>Speed <span className="muted">{prefs.rate.toFixed(2)}×</span>
        <input type="range" min="0.8" max="1.5" step="0.05" value={prefs.rate} onChange={(e) => setPrefs({ rate: Number(e.target.value) })} />
      </label>
      <label className="check"><input type="checkbox" checked={prefs.speak} onChange={(e) => setPrefs({ speak: e.target.checked })} /> Speak replies aloud</label>
      <label className="check"><input type="checkbox" checked={prefs.web} onChange={(e) => setPrefs({ web: e.target.checked })} /> Dig deeper on the web after answering</label>
      <label className="check"><input type="checkbox" checked={prefs.bargeIn} onChange={(e) => setPrefs({ bargeIn: e.target.checked })} /> Let me talk over Lens to interrupt it <span className="muted">(works best with headphones)</span></label>
      {!voiceSupport.listen && <p className="small muted">This browser has no speech recognition. Chrome or Edge on desktop or Android do. Typing works everywhere.</p>}
    </div>
  )
}

function StoryCardMsg({ story }) {
  return (
    <Link to={`/story/${story.id}`} className={'live-card' + (story.image_url ? ' has-img' : '')} data-topic={story.topic}>
      {story.image_url && <div className="live-card-img" style={{ backgroundImage: `url(${story.image_url})` }} />}
      <div className="live-card-body">
        <span className="kicker">{topicLabel(story.topic)}</span>
        <b>{story.headline}</b>
        {story.gist && <span className="gist">{story.gist}</span>}
        <small>{story.built ? 'Fully explained' : 'Quick read'} · {story.outlet_count} outlets · open story <External size={12} /></small>
      </div>
    </Link>
  )
}

export default function Live() {
  const { listen } = useApp()
  const [params] = useSearchParams()
  const v = useVoice()
  const suggest = useAsync(() => api.voiceSuggest(), [])
  const [text, setText] = useState('')
  const [showSet, setShowSet] = useState(false)
  const end = useRef(null)
  const startedWith = useRef(false)

  useEffect(() => { listen?.stop?.() }, [])                              // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { end.current?.scrollIntoView({ block: 'end', behavior: 'smooth' }) }, [v.msgs.length, v.digging])
  useEffect(() => {                                                       // /live?ask=… (from the command palette or a story)
    const q = params.get('ask')
    if (q && !startedWith.current) { startedWith.current = true; v.send(q) }
  }, [params])                                                            // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const h = (e) => {
      if (/input|textarea|select/i.test(e.target.tagName) || e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key === 'Escape') v.stopTalking()
      else if (e.key === 'm') { if (v.live) v.stopLive(); else v.startLive() }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [v])

  const empty = v.msgs.length === 0
  const restored = useRef(empty ? false : true).current   // conversation loaded from memory on mount, not from a fresh reply
  const label = v.error ? '' : v.live && v.state === 'idle' ? 'Listening…' : LABEL[v.state]
  const onOrb = () => (v.live ? v.stopLive() : v.startLive())
  const submit = (e) => { e.preventDefault(); const t = text.trim(); if (t) { setText(''); v.send(t) } }
  const prompts = useMemo(() => suggest.data?.prompts || ['Give me today’s highlights'], [suggest.data])
  const ph = PHASE[v.phase]

  return (
    <div className="container wide live">
      <div className="live-grid">
        <section className={'live-main' + (empty ? ' empty' : '')}>
          <div className="live-stage">
            <div className="eyebrow">Live · Lens</div>
            {empty && <h1 className="h-page live-title">Ask the news <em>anything.</em></h1>}
            {empty && <p className="lede live-lede">Lens reads today’s stories, explains why they happened, and searches the web while it talks. Just say what you want to know.</p>}
            <Orb state={v.live && v.state === 'idle' ? 'listening' : v.state} level={v.level} onClick={onOrb} />
            <div className="live-state" aria-live="polite">
              {label}
              {v.digging && <span className="dig-pill"><i /><i /><i /> digging deeper on the web</span>}
              {!v.digging && v.status && (
                <span className={'status-line' + (ph ? ' has-phase' : '')}>
                  {ph && <ph.icon size={13} />}{v.status}
                </span>
              )}
            </div>
            <div className="live-interim" aria-live="polite">{v.interim && <>“{v.interim}”</>}</div>
            {v.error && <div className="live-error" role="alert">{v.error}</div>}
            <div className="live-controls">
              <button className={'btn ' + (v.live ? 'danger' : 'primary')} onClick={onOrb}><Mic size={17} /> {v.live ? 'End conversation' : 'Start talking'}</button>
              {(v.state === 'speaking' || v.state === 'thinking') && <button className="btn" onClick={v.stopTalking}><Stop size={16} /> Interrupt <kbd>Esc</kbd></button>}
              {!empty && v.state !== 'speaking' && <button className="btn" onClick={v.repeat}><Repeat size={16} /> Say again</button>}
              <div className="live-set-wrap">
                <button className="icon-btn" onClick={() => setShowSet((s) => !s)} aria-label="Voice settings" aria-expanded={showSet}><Sliders /></button>
                {showSet && <Settings prefs={v.prefs} setPrefs={v.setPrefs} onClose={() => setShowSet(false)} />}
              </div>
            </div>
            {empty && (
              <div className="live-suggest">
                {prompts.map((p) => <button key={p} className="chip outline" onClick={() => v.send(p)}>{p}</button>)}
              </div>
            )}
            {empty && !voiceSupport.listen && <p className="small muted" style={{ marginTop: 12 }}>Voice input needs Chrome or Edge. You can type to Lens below and it will still talk back.</p>}
          </div>

          {!empty && (
            <div className="live-convo">
              {v.msgs.map((m) => {
                if (m.role === 'card') return <StoryCardMsg key={m.id} story={m.story} />
                if (m.role === 'sources') return (
                  <div key={m.id} className="live-sources"><span>Sources</span>
                    {m.items.map((s) => <a key={s.url} href={s.url} target="_blank" rel="noreferrer" title={s.title}>{s.outlet} <External size={11} /></a>)}
                  </div>
                )
                return <div key={m.id} className={'bubble ' + m.role + (m.kind === 'dig' ? ' dig' : '')}>{m.kind === 'dig' && <em>From the web</em>}{m.text}</div>
              })}
              <div ref={end} />
            </div>
          )}

          <form className="live-input" onSubmit={submit}>
            <input className="field" value={text} onChange={(e) => setText(e.target.value)} placeholder="Or type a question…  e.g. why did the rupee fall?" aria-label="Type a question for Lens" />
            <button className="btn primary" disabled={!text.trim()} aria-label="Send"><Send size={16} /></button>
          </form>
        </section>

        <aside className="live-side">
          <div className="widget">
            <h4>Now discussing</h4>
            {v.focus.story_id ? (
              <>
                <Link to={`/story/${v.focus.story_id}`} className="focus-title">{v.focus.headline}</Link>
                <div className="follow-grid">
                  {FOLLOW.map(({ q, icon: Icon }) => <button key={q} className="follow-btn" onClick={() => v.send(q)}><Icon size={16} />{q}</button>)}
                </div>
                {v.focus.queue?.length > 1 && (
                  <button className="btn sm" style={{ marginTop: 10, width: '100%' }} onClick={() => v.send('next')}>Next story · {(v.focus.pos ?? 0) + 1} of {v.focus.queue.length}</button>
                )}
              </>
            ) : <p className="small muted">Ask for the highlights and I’ll keep track of the story we’re on, so you can just say “why?” or “what happens next?”.</p>}
          </div>
          <div className="widget">
            <h4>How Lens works</h4>
            <ul className="how">
              <li><b>Fast talker.</b> Answers at once from stories already explained.</li>
              <li><b>Parallel researcher.</b> While it speaks, it searches the open web and adds only what it can back up.</li>
              <li><b>No bluffing.</b> Every sentence is checked against its sources before it is spoken.</li>
              <li><b>Interrupt any time</b> with <kbd>Esc</kbd>, or just start talking — it'll stop and listen.</li>
              <li><b>Remembers this chat.</b> Leave and come back within a few hours and we'll pick up where we left off.</li>
            </ul>
          </div>
          {!empty && (
            <>
              {restored && <p className="small muted">↺ Picked up your last conversation with Lens.</p>}
              <button className="btn sm" onClick={v.reset}>Clear conversation</button>
            </>
          )}
        </aside>
      </div>
    </div>
  )
}
