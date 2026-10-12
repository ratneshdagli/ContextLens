import { formatDate } from '../lib/format'

/** "How it unfolded": dated milestones ending in a "Today" node. */
export default function Timeline({ events, today }) {
  return (
    <ol className="tl2">
      {events.map((e, i) => (
        <li key={i}>
          <div className="d">{formatDate(e.date, e.precision)}{e.kind === 'history' && <span className="k" title="Dated by the day it was first reported">reported</span>}</div>
          <div className="x">{e.text}</div>
          <div className="s">
            <a href={e.source_url} target="_blank" rel="noreferrer">{e.source_outlet}</a>
            {e.quote && e.kind !== 'history' && (
              <details className="quote" style={{ display: 'inline', marginLeft: 10 }}>
                <summary>· see the sentence</summary>
                <blockquote>“{e.quote}”</blockquote>
              </details>
            )}
          </div>
        </li>
      ))}
      {today && <li className="today"><div className="d">Today</div><div className="x">{today}</div></li>}
    </ol>
  )
}
