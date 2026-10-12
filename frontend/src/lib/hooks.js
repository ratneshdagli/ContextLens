import { useCallback, useEffect, useRef, useState } from 'react'
import { api, streamRun } from './api'

/** Load data with loading/error state and manual reload. */
export function useAsync(fn, deps = []) {
  const [state, set] = useState({ data: null, error: null, loading: true })
  const alive = useRef(true)
  const load = useCallback((quiet = false) => {
    set((s) => ({ ...s, loading: !quiet, error: null }))
    return fn()
      .then((data) => alive.current && set({ data, error: null, loading: false }))
      .catch((error) => alive.current && set((s) => ({ data: quiet ? s.data : null, error, loading: false })))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => { alive.current = true; load(); return () => { alive.current = false } }, [load])
  return { ...state, reload: load, setData: (data) => set((s) => ({ ...s, data })) }
}

/** Live pipeline run: events + progress from SSE. */
export function useRun(runId, { onDone } = {}) {
  const [events, setEvents] = useState([])
  const [progress, setProgress] = useState(0)
  const [status, setStatus] = useState(runId ? 'running' : 'idle')
  const doneRef = useRef(onDone)
  doneRef.current = onDone
  useEffect(() => {
    if (!runId) { setStatus('idle'); return undefined }
    setEvents([]); setProgress(0); setStatus('running')
    return streamRun(
      runId,
      (e) => {
        setEvents((prev) => [...prev.slice(-200), e])
        if (typeof e.progress === 'number') setProgress(e.progress)
      },
      (st) => { setStatus(st === 'error' ? 'failed' : st); if (st !== 'error') setProgress(1); doneRef.current?.(st) },
    )
  }, [runId])
  const last = events[events.length - 1]
  return { events, progress, status, last }
}

/** Server health polled lightly (LLM online?, feed running?). */
export function useHealth(interval = 15000) {
  const [h, setH] = useState(null)
  useEffect(() => {
    let stop = false
    const tick = () => api.health().then((d) => !stop && setH(d)).catch(() => !stop && setH({ llm: { available: false }, offline: true }))
    tick()
    const id = setInterval(tick, interval)
    return () => { stop = true; clearInterval(id) }
  }, [interval])
  return h
}

export function useToast() {
  const [msg, setMsg] = useState(null)
  const show = useCallback((m) => { setMsg(m); setTimeout(() => setMsg(null), 2600) }, [])
  return [msg, show]
}

/* ── reading log: which stories were opened today, and a daily streak (kept only in this browser) ── */
const dayKey = (d = new Date()) => d.toISOString().slice(0, 10)
function loadLog() {
  try { return JSON.parse(localStorage.getItem('cl_readlog') || '{}') } catch { return {} }
}
export function markRead(id) {
  try {
    const log = loadLog()
    const k = dayKey()
    log[k] = Array.from(new Set([...(log[k] || []), id]))
    const keep = Object.keys(log).sort().slice(-60)
    localStorage.setItem('cl_readlog', JSON.stringify(Object.fromEntries(keep.map((x) => [x, log[x]]))))
    window.dispatchEvent(new Event('cl-readlog'))
  } catch { /* storage unavailable */ }
}
export function useReadLog() {
  const [log, setLog] = useState(loadLog)
  useEffect(() => {
    const h = () => setLog(loadLog())
    window.addEventListener('cl-readlog', h)
    window.addEventListener('storage', h)
    return () => { window.removeEventListener('cl-readlog', h); window.removeEventListener('storage', h) }
  }, [])
  const today = new Set(log[dayKey()] || [])
  const all = new Set(Object.values(log).flat())
  let streak = 0
  const d = new Date()
  if (!log[dayKey(d)]) d.setDate(d.getDate() - 1)
  while ((log[dayKey(d)] || []).length > 0) { streak += 1; d.setDate(d.getDate() - 1) }
  return { todayCount: today.size, isRead: (id) => all.has(id), streak }
}

/** Currently loaded LM Studio models (shows that only one chat model is resident). */
export function useModelStatus(interval = 20000) {
  const [m, setM] = useState(null)
  useEffect(() => {
    let stop = false
    const tick = () => api.modelsStatus().then((d) => !stop && setM(d)).catch(() => {})
    tick()
    const id = setInterval(tick, interval)
    return () => { stop = true; clearInterval(id) }
  }, [interval])
  return m
}
