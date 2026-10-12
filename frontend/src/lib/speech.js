import { useCallback, useEffect, useRef, useState } from 'react'

const supported = typeof window !== 'undefined' && 'speechSynthesis' in window

function pickVoice() {
  if (!supported) return null
  const voices = window.speechSynthesis.getVoices().filter((v) => /^en/i.test(v.lang))
  const pref = [/natural/i, /google uk english/i, /google us english/i, /samantha|daniel|aria|jenny/i]
  for (const re of pref) { const v = voices.find((x) => re.test(x.name)); if (v) return v }
  return voices[0] || null
}

/** Split into speakable chunks (Chrome cuts off long utterances). */
function chunks(text) {
  return text.replace(/\s+/g, ' ').match(/[^.!?]+[.!?]*/g)?.map((s) => s.trim()).filter(Boolean) ?? [text]
}

/**
 * Sequential text-to-speech over a queue of items [{id, label, text}].
 * Uses the browser's on-device voices — nothing leaves the machine.
 */
export function useListen() {
  const [queue, setQueue] = useState([])
  const [index, setIndex] = useState(-1)
  const [state, setState] = useState('idle') // idle | playing | paused
  const [rate, setRate] = useState(() => Number(localStorage.getItem('cl_rate')) || 1)
  const runId = useRef(0)
  const rateRef = useRef(rate)
  rateRef.current = rate

  const speakItem = useCallback((items, i, id) => {
    if (id !== runId.current) return
    if (i >= items.length) { setState('idle'); setIndex(-1); return }
    setIndex(i)
    const parts = chunks(items[i].text)
    const lang = items[i].lang                                  // set for non-English items; the browser picks a matching voice if it has one
    const voice = lang ? window.speechSynthesis.getVoices().find((v) => v.lang.toLowerCase().startsWith(lang)) : pickVoice()
    let p = 0
    const next = () => {
      if (id !== runId.current) return
      if (p >= parts.length) { speakItem(items, i + 1, id); return }
      const u = new SpeechSynthesisUtterance(parts[p++])
      if (voice) u.voice = voice
      else if (lang) u.lang = lang
      u.rate = rateRef.current
      u.onend = next
      u.onerror = () => { if (id === runId.current) next() }
      window.speechSynthesis.speak(u)
    }
    next()
  }, [])

  const play = useCallback((items, start = 0) => {
    if (!supported || !items.length) return
    window.speechSynthesis.cancel()
    runId.current += 1
    setQueue(items); setState('playing')
    speakItem(items, start, runId.current)
  }, [speakItem])

  const pause = useCallback(() => { if (supported) { window.speechSynthesis.pause(); setState('paused') } }, [])
  const resume = useCallback(() => { if (supported) { window.speechSynthesis.resume(); setState('playing') } }, [])
  const stop = useCallback(() => { if (supported) window.speechSynthesis.cancel(); runId.current += 1; setState('idle'); setIndex(-1); setQueue([]) }, [])
  const skip = useCallback((d) => {
    if (!queue.length) return
    const i = Math.max(0, Math.min(queue.length, index + d))
    play(queue, i)
  }, [queue, index, play])
  const changeRate = useCallback((r) => { setRate(r); localStorage.setItem('cl_rate', String(r)) }, [])

  useEffect(() => () => { if (supported) window.speechSynthesis.cancel() }, [])
  return { supported, state, queue, index, current: queue[index], rate, changeRate, play, pause, resume, stop, skip }
}

/** Compose what gets read aloud for a story detail object. */
export function storyToSpeech(s) {
  const ex = s.explain
  if (ex) {
    const why = (ex.why || []).map((w) => `${w.cause}. ${w.link}`).join(' ')
    return `${s.headline}. ${ex.gist} Why did this happen? ${why} ${ex.so_what || ''}`
  }
  const pts = (s.key_points || []).slice(0, 3).join('. ')
  return `${s.headline}. ${s.what_happened} ${pts ? 'Key points. ' + pts + '.' : ''}`
}
