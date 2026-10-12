import { useEffect, useRef } from 'react'

/** Live progress for a pipeline run: bar + current step + optional detail log. */
export default function RunPanel({ run, showLog = false, title }) {
  const ref = useRef(null)
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight }, [run.events.length])
  const pct = Math.round((run.progress || 0) * 100)
  return (
    <div>
      {title && <div style={{ fontWeight: 600, marginBottom: 8 }}>{title}</div>}
      <div className="progress" aria-label="progress" role="progressbar" aria-valuenow={pct}><i style={{ width: `${Math.max(pct, 3)}%` }} /></div>
      <div className="small muted" style={{ marginTop: 8, minHeight: 20 }}>{run.last?.message || 'Starting…'}</div>
      {showLog && (
        <div className="run-log" ref={ref} style={{ marginTop: 12 }}>
          {run.events.map((e, i) => (
            <div key={i} className={e.level}><span className="st">{e.stage}</span>{e.message}</div>
          ))}
        </div>
      )}
    </div>
  )
}
