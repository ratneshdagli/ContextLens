import { Close } from './Icons'

const I = (d) => (p) => <svg width={p.size || 18} height={p.size || 18} viewBox="0 0 24 24" fill="currentColor">{d}</svg>
const Play = I(<path d="M7 4.5v15l13-7.5z" />)
const Pause = I(<path d="M6 4h4v16H6zM14 4h4v16h-4z" />)
const Prev = I(<path d="M6 5h2v14H6zM20 5v14L9 12z" />)
const Next = I(<path d="M16 5h2v14h-2zM4 5v14l11-7z" />)

/** Persistent mini-player for the on-device voice. */
export default function ListenBar({ listen }) {
  if (listen.state === 'idle' || !listen.current) return null
  const rates = [1, 1.25, 1.5]
  return (
    <div className="listenbar" role="region" aria-label="Audio briefing player">
      <div className="lb-info">
        <div className="eyebrow" style={{ fontSize: 10.5 }}>Listening · {listen.index + 1} of {listen.queue.length}</div>
        <div className="lb-title">{listen.current.label}</div>
      </div>
      <div className="lb-ctrl">
        <button className="icon-btn" onClick={() => listen.skip(-1)} aria-label="Previous"><Prev /></button>
        <button className="icon-btn lb-play" onClick={listen.state === 'playing' ? listen.pause : listen.resume} aria-label={listen.state === 'playing' ? 'Pause' : 'Play'}>
          {listen.state === 'playing' ? <Pause /> : <Play />}
        </button>
        <button className="icon-btn" onClick={() => listen.skip(1)} aria-label="Next"><Next /></button>
        <button className="btn sm ghost" onClick={() => listen.changeRate(rates[(rates.indexOf(listen.rate) + 1) % rates.length])} aria-label="Speed">{listen.rate}×</button>
        <button className="icon-btn" onClick={listen.stop} aria-label="Stop"><Close size={16} /></button>
      </div>
    </div>
  )
}
