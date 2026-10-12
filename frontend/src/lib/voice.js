// ContextLens v5 — live voice engine.
//
// Ears:   the browser's SpeechRecognition (Chrome / Edge). Typing works everywhere.
// Mouth:  the browser's speechSynthesis, one sentence at a time so the server can keep streaming while Lens talks.
// Brain:  POST /api/voice/turn (SSE). The server sends small events in speaking order:
//         say · card · focus · sources · status · turn_done · dig · done
// Events are *played* in order: a card appears exactly when Lens starts talking about it.

import { useCallback, useEffect, useRef, useState } from 'react'

const SR = typeof window !== 'undefined' ? window.SpeechRecognition || window.webkitSpeechRecognition : null
const synth = typeof window !== 'undefined' && 'speechSynthesis' in window ? window.speechSynthesis : null
export const voiceSupport = { listen: !!SR, speak: !!synth }

const LS = {
  get: (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v) } catch { return d } },
  set: (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)) } catch { /* private mode */ } },
}

export function englishVoices() {
  return synth ? synth.getVoices().filter((v) => /^en/i.test(v.lang)) : []
}

function pickVoice(name, lang) {
  const all = englishVoices()
  if (name) { const v = all.find((x) => x.name === name); if (v) return v }
  const inLang = all.filter((v) => v.lang.replace('_', '-').toLowerCase() === lang.toLowerCase())
  const pool = inLang.length ? inLang : all
  for (const re of [/natural|neural|online/i, /google/i, /aria|jenny|libby|ryan|sonia|neerja|prabhat/i]) {
    const v = pool.find((x) => re.test(x.name)); if (v) return v
  }
  return pool[0] || all[0] || null
}

let seq = 0
const uid = () => `${Date.now().toString(36)}${(seq++).toString(36)}`

// Conversation memory: kept in localStorage so leaving the Live page (or reloading) doesn't lose "what we were
// just talking about" — this is a single-user local app, so the browser's own storage is the only memory that
// makes sense. Expires after a few hours so a stale half-finished chat doesn't greet you a week later.
const MEM_TTL_MS = 6 * 60 * 60 * 1000
function loadMemory() {
  try {
    const at = Number(localStorage.getItem('cl_vmem_at') || 0)
    if (!at || Date.now() - at > MEM_TTL_MS) return { msgs: [], focus: {} }
    return { msgs: LS.get('cl_vmsgs', []), focus: LS.get('cl_vfocus', {}) }
  } catch { return { msgs: [], focus: {} } }
}
function saveMemory(msgs, focus) {
  try {
    LS.set('cl_vmsgs', msgs.slice(-40)); LS.set('cl_vfocus', focus)
    localStorage.setItem('cl_vmem_at', String(Date.now()))
  } catch { /* private mode */ }
}

// Barge-in voice detection: a *separate* echo-cancelled mic stream (never the one SpeechRecognition opens
// internally — that one can't take external constraints) is sampled continuously while Lens is speaking. Its
// energy is compared against a rolling floor learned from the start of that same utterance (so it adapts to
// however loud the TTS bleed happens to be), and a sustained run of frames clearly above that floor — not a
// single spike — is treated as "the reader started talking": Lens is cut off immediately and real speech
// recognition takes over from there. Headphones remove TTS bleed entirely and make this far more reliable.
const BARGE_MIN = 20          // ignore near-silence outright, whatever the learned floor says
const BARGE_MARGIN = 16       // how far above the learned floor counts as "someone is talking"
const BARGE_FRAMES = 6        // ~100ms of sustained energy at 60fps, so a single click/pop can't trigger it

export function useVoice() {
  const mem0 = useRef(loadMemory())
  const [state, setState] = useState('idle')        // idle | listening | thinking | speaking
  const [live, setLive] = useState(false)           // conversation mode: keep the mic open between turns
  const [msgs, setMsgs] = useState(mem0.current.msgs)
  const [interim, setInterim] = useState('')
  const [focus, setFocus] = useState(mem0.current.focus)
  const [status, setStatus] = useState('')
  const [phase, setPhase] = useState('')            // think | queued | answering | build | dig — for the status indicator's icon
  const [digging, setDigging] = useState(false)
  const [error, setError] = useState('')
  const [prefs, setPrefsState] = useState(() => ({
    lang: LS.get('cl_vlang', 'en-IN'), rate: LS.get('cl_vrate', 1), voice: LS.get('cl_vvoice', ''),
    web: LS.get('cl_vweb', true), speak: LS.get('cl_vspeak', true), bargeIn: LS.get('cl_vbarge', true),
  }))

  // refs mirror state for callbacks that outlive a render
  const R = useRef({})
  R.current = { state, live, msgs, focus, prefs }
  const rec = useRef(null)
  const recOn = useRef(false)
  const ctl = useRef(null)
  const queue = useRef([])          // events waiting to be played
  const playing = useRef(false)
  const talkerDone = useRef(true)
  const turn = useRef(0)            // increments per user turn; stale callbacks compare against it
  const bubble = useRef(null)       // id of the assistant bubble currently being written
  const interimRef = useRef('')
  const level = useRef(0)           // 0..1 mic/voice energy for the orb
  const vad = useRef({ floor: 0, hits: 0 })   // barge-in detector state while Lens is speaking

  useEffect(() => { saveMemory(msgs, focus) }, [msgs, focus])
  const analyser = useRef(null)

  const setPrefs = useCallback((patch) => {
    setPrefsState((p) => {
      const n = { ...p, ...patch }
      LS.set('cl_vlang', n.lang); LS.set('cl_vrate', n.rate); LS.set('cl_vvoice', n.voice)
      LS.set('cl_vweb', n.web); LS.set('cl_vspeak', n.speak); LS.set('cl_vbarge', n.bargeIn)
      return n
    })
  }, [])

  // ── mouth ──────────────────────────────────────────────────────────────────
  const speak = useCallback((text, done) => {
    const p = R.current.prefs
    if (!synth || !p.speak) { setTimeout(done, Math.min(2500, 250 + text.length * 22)); return }   // silent mode: pace like speech
    const u = new SpeechSynthesisUtterance(text)
    const v = pickVoice(p.voice, p.lang)
    if (v) { u.voice = v; u.lang = v.lang } else u.lang = p.lang
    u.rate = p.rate
    u.onboundary = () => { level.current = 0.9 }
    u.onend = done
    u.onerror = done
    synth.speak(u)
  }, [])

  const stopSpeaking = useCallback(() => { if (synth) synth.cancel(); level.current = 0 }, [])

  // ── ears ───────────────────────────────────────────────────────────────────
  const stopEars = useCallback(() => {
    recOn.current = false
    try { rec.current?.abort() } catch { /* not running */ }
    rec.current = null
    setInterim(''); interimRef.current = ''
  }, [])

  const afterSpeech = useCallback(() => {
    if (playing.current || queue.current.length || !talkerDone.current) return
    if (R.current.live) startEars()   // eslint-disable-line no-use-before-define
    else setState('idle')
  }, [])                              // eslint-disable-line react-hooks/exhaustive-deps

  // ── playing the server's script in order ──────────────────────────────────
  const pump = useCallback(() => {
    if (playing.current) return
    while (queue.current.length) {
      if (interimRef.current && !R.current.prefs.bargeIn) return          // the reader is talking: hold Lens's next sentence
      const ev = queue.current.shift()
      if (ev.t === 'say') {
        playing.current = true
        stopEarsForSpeech()                                              // eslint-disable-line no-use-before-define
        setState('speaking')
        const my = turn.current
        setMsgs((m) => {
          const id = bubble.current
          if (id && m.some((x) => x.id === id)) return m.map((x) => (x.id === id ? { ...x, text: (x.text + ' ' + ev.c).trim() } : x))
          const nid = uid(); bubble.current = nid
          return [...m, { id: nid, role: 'assistant', text: ev.c, kind: ev.dig ? 'dig' : 'talk' }]
        })
        speak(ev.c, () => { if (my !== turn.current) return; playing.current = false; pump(); if (!playing.current) afterSpeech() })
        return
      }
      if (ev.t === 'card') {
        bubble.current = null
        setMsgs((m) => [...m, { id: uid(), role: 'card', story: ev.story }])
      } else if (ev.t === 'focus') setFocus({ story_id: ev.story_id, queue: ev.queue || [], pos: ev.pos ?? 0, headline: ev.headline })
      else if (ev.t === 'sources') setMsgs((m) => [...m, { id: uid(), role: 'sources', items: ev.items }])
      else if (ev.t === 'digmark') bubble.current = null
    }
    if (!playing.current) afterSpeech()
  }, [speak, afterSpeech])            // eslint-disable-line react-hooks/exhaustive-deps

  function stopEarsForSpeech() {
    // Speech recognition and speech synthesis fighting over the same microphone is unreliable (the mic hears
    // Lens's own voice). Barge-in instead watches raw mic *energy* on a dedicated stream (below) and only starts
    // real recognition once that energy says someone is actually talking — so recognition never runs during TTS.
    stopEars()
  }

  // ── turns ──────────────────────────────────────────────────────────────────
  const interrupt = useCallback(() => {
    turn.current += 1
    ctl.current?.abort()
    queue.current = []; playing.current = false; talkerDone.current = true
    stopSpeaking(); setStatus(''); setPhase(''); setDigging(false)
  }, [stopSpeaking])

  const send = useCallback(async (raw, opts = {}) => {
    const text = (raw || '').trim()
    if (!text) return
    interrupt()
    stopEars()
    if (R.current.prefs.bargeIn) startMeter()      // eslint-disable-line no-use-before-define -- arm barge-in before Lens starts speaking
    const my = turn.current
    const history = R.current.msgs.filter((m) => m.role === 'user' || m.role === 'assistant').slice(-8).map((m) => ({ role: m.role, text: m.text }))
    bubble.current = null
    setError(''); setState('thinking'); setStatus('')
    setMsgs((m) => [...m, { id: uid(), role: 'user', text }])
    talkerDone.current = false
    let inDig = false
    ctl.current = new AbortController()
    try {
      const res = await fetch('/api/voice/turn', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: ctl.current.signal,
        body: JSON.stringify({ text, history, focus: opts.focus ?? R.current.focus, web: R.current.prefs.web }),
      })
      if (!res.ok || !res.body) throw new Error(`Lens could not answer (${res.status})`)
      const reader = res.body.getReader(); const dec = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done || my !== turn.current) break
        buf += dec.decode(value, { stream: true })
        const parts = buf.split('\n\n'); buf = parts.pop() || ''
        for (const part of parts) {
          const line = part.replace(/^data: /, '').trim()
          if (!line) continue
          let ev; try { ev = JSON.parse(line) } catch { continue }
          if (my !== turn.current) return
          if (ev.t === 'status') { setStatus(ev.c); setPhase(ev.phase || ''); continue }
          if (ev.t === 'error') { setError(ev.c); setPhase(''); continue }
          if (ev.t === 'turn_done') { talkerDone.current = true; if (!playing.current && !queue.current.length) afterSpeech(); continue }
          if (ev.t === 'dig') {
            if (ev.state === 'start') { inDig = true; setDigging(true); setPhase('dig'); queue.current.push({ t: 'digmark' }) }
            else { setDigging(false); setStatus(''); setPhase(''); if (ev.state === 'empty') setStatus('Nothing new on the web this time.') }
            continue
          }
          if (ev.t === 'done') { setDigging(false); setPhase(''); talkerDone.current = true; continue }
          if (ev.t === 'say' && inDig) ev = { ...ev, dig: true }
          if (ev.t === 'intent' || ev.t === 'turn_open') continue
          queue.current.push(ev)
          pump()
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') { setError(e.message || 'Something went wrong'); setState('idle') }
    } finally {
      if (my === turn.current) { talkerDone.current = true; setStatus((s) => (s.startsWith('Nothing new') ? s : '')); setPhase(''); pump(); if (!playing.current && !queue.current.length) afterSpeech() }
    }
  }, [interrupt, stopEars, pump, afterSpeech])

  // ── ears (defined after send) ──────────────────────────────────────────────
  function startEars() {
    if (!SR || recOn.current) return
    const p = R.current.prefs
    const r = new SR()
    r.lang = p.lang; r.interimResults = true; r.continuous = false; r.maxAlternatives = 1
    let heard = false
    r.onstart = () => { recOn.current = true; setState('listening'); setError('') }
    r.onresult = (e) => {
      let interimT = '', finalT = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) finalT += e.results[i][0].transcript
        else interimT += e.results[i][0].transcript
      }
      const t = (finalT || interimT).trim()
      interimRef.current = t; setInterim(t)
      if (finalT.trim()) { heard = true; interimRef.current = ''; setInterim(''); send(finalT.trim()) }
    }
    r.onerror = (e) => {
      if (e.error === 'no-speech' || e.error === 'aborted') return
      recOn.current = false
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') setError('The microphone is blocked. Allow it in the address bar, or type instead.')
      else if (e.error === 'network') setError('Speech recognition needs an internet connection in this browser. You can type instead.')
      else setError(`Speech recognition error: ${e.error}`)
      setLive(false); setState('idle')
    }
    r.onend = () => {
      recOn.current = false
      if (!heard && interimRef.current) { interimRef.current = ''; setInterim(''); pump() }   // nothing final was heard: release held speech
      if (!heard && R.current.live && R.current.state === 'listening' && rec.current === r) setTimeout(() => { if (R.current.live && !recOn.current && !playing.current) startEars() }, 200)
    }
    rec.current = r
    try { r.start() } catch { /* already started */ }
  }

  const startLive = useCallback(() => {
    if (!SR) { setError('Voice input works in Chrome or Edge. You can still type to Lens.'); return }
    setLive(true); R.current.live = true
    startMeter()                                                                                // eslint-disable-line no-use-before-define
    if (!playing.current) startEars()
  }, [])                                                                                        // eslint-disable-line react-hooks/exhaustive-deps

  const stopLive = useCallback(() => {
    setLive(false); R.current.live = false
    stopEars(); interrupt(); setState('idle')
  }, [stopEars, interrupt])

  // "be quiet": cut Lens off, then go back to listening (live) or rest (typed)
  const stopTalking = useCallback(() => {
    interrupt()
    if (R.current.live) startEars()                                                             // eslint-disable-line no-use-before-define
    else setState('idle')
  }, [interrupt])                                                                               // eslint-disable-line react-hooks/exhaustive-deps

  // ── the orb's pulse: real microphone energy when we are allowed to read it ─
  async function startMeter() {
    if (analyser.current || !navigator.mediaDevices?.getUserMedia) return
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      const ctx = new (window.AudioContext || window.webkitAudioContext)()
      const a = ctx.createAnalyser(); a.fftSize = 256
      ctx.createMediaStreamSource(stream).connect(a)
      analyser.current = { a, ctx, stream, buf: new Uint8Array(a.frequencyBinCount) }
    } catch { /* the orb just breathes without it */ }
  }
  useEffect(() => {
    let raf
    const tick = () => {
      const m = analyser.current
      if (m && R.current.state === 'listening') {
        m.a.getByteFrequencyData(m.buf)
        let s = 0; for (let i = 0; i < m.buf.length; i++) s += m.buf[i]
        level.current = Math.min(1, (s / m.buf.length) / 90)
        vad.current.floor = 0; vad.current.hits = 0
      } else if (m && R.current.state === 'speaking' && R.current.prefs.bargeIn) {
        m.a.getByteFrequencyData(m.buf)
        let s = 0; for (let i = 0; i < m.buf.length; i++) s += m.buf[i]
        const e = s / m.buf.length
        const v = vad.current
        v.floor = v.floor ? v.floor * 0.98 + e * 0.02 : e     // learns the current TTS-bleed floor as this utterance plays
        const over = e > v.floor + BARGE_MARGIN && e > BARGE_MIN
        v.hits = over ? v.hits + 1 : 0
        if (v.hits >= BARGE_FRAMES) {                          // sustained, not a click/pop: the reader is actually talking
          v.floor = 0; v.hits = 0
          interrupt(); startEars()                             // eslint-disable-line no-use-before-define
        }
        level.current *= 0.9
      } else { level.current *= 0.9; vad.current.floor = 0; vad.current.hits = 0 }
      raf = requestAnimationFrame(tick)
    }
    tick()
    return () => cancelAnimationFrame(raf)
  }, [interrupt])                                                                                 // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => {                                                                       // leaving the page silences everything
    turn.current += 1; ctl.current?.abort(); stopSpeaking(); try { rec.current?.abort() } catch { /* */ }
    analyser.current?.stream.getTracks().forEach((t) => t.stop()); analyser.current?.ctx.close().catch(() => {}); analyser.current = null
  }, [stopSpeaking])

  const repeat = useCallback(() => {
    const last = [...R.current.msgs].reverse().find((m) => m.role === 'assistant')
    if (!last) return
    interrupt(); stopEars(); setState('speaking'); playing.current = true
    const my = turn.current
    speak(last.text, () => { if (my === turn.current) { playing.current = false; afterSpeech() } })
  }, [interrupt, stopEars, speak, afterSpeech])

  const reset = useCallback(() => { interrupt(); stopEars(); setMsgs([]); setFocus({}); setState('idle'); setError('') }, [interrupt, stopEars])

  return { state, live, msgs, interim, focus, status, phase, digging, error, prefs, setPrefs, level, send, interrupt, stopTalking, startLive, stopLive, repeat, reset, setFocus }
}
