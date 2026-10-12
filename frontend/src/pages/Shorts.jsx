import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { timeAgo, topicLabel } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { useListen } from '../lib/speech'
import '../shorts.css'

const TOPICS = ['', 'world', 'politics', 'business', 'markets', 'technology', 'science', 'health', 'india', 'sports', 'culture']

const LANGS = { hi: 'हिन्दी', mr: 'मराठी', bn: 'বাংলা', ta: 'தமிழ்', te: 'తెలుగు', gu: 'ગુજરાતી', kn: 'ಕನ್ನಡ', ml: 'മലയാളം' }

/** In short (Inshorts): every story in under 60 words, one card per screen. The text is the story's own validated summary, cut at a sentence end. */
export default function Shorts() {
  const [topic, setTopic] = useState('')
  const { data, loading, error } = useAsync(() => api.shorts(topic), [topic])
  const [i, setI] = useState(0)
  const [note, setNote] = useState('')
  const deck = useRef(null)
  const n = data?.length || 0
  const [lang, setLang] = useState(() => { try { return localStorage.getItem('cl_lang') || '' } catch { return '' } })
  const [tr, setTr] = useState({})                                // `${lang}:${id}` -> {headline, body} | 'x' (gate refused or model offline)
  const listen = useListen()
  const shown = (s) => (tr[`${lang}:${s.id}`]?.body ? { ...s, ...tr[`${lang}:${s.id}`] } : s)
  const pickLang = (l) => { setLang(l); try { localStorage.setItem('cl_lang', l) } catch { /* not persisted */ } }

  useEffect(() => {                                           // translate the card on screen and the next two, one at a time
    if (!lang || !data) return undefined
    let dead = false
    ;(async () => {
      for (const s of data.slice(i, i + 3)) {
        const k = `${lang}:${s.id}`
        if (tr[k]) continue
        const r = await api.translate(s.id, lang).catch(() => 'x')
        if (dead) return
        setTr((t) => ({ ...t, [k]: r }))
      }
    })()
    return () => { dead = true }
  }, [lang, data, i]) // eslint-disable-line react-hooks/exhaustive-deps

  const briefing = () => {                                    // one tap: read the top stories aloud, in the chosen language where translated
    const items = data.slice(0, 8).map((s) => { const v = shown(s); return { id: s.id, label: v.headline, text: `${v.headline}. ${v.body}`, lang: v === s ? '' : lang } })
    listen.play([{ id: 'intro', label: 'Briefing', text: `Your ContextLens briefing. ${items.length} stories.` }, ...items])
  }

  const go = useCallback((d) => {
    const next = Math.max(0, Math.min(n - 1, i + d))
    setI(next)
    deck.current?.children[next]?.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
  }, [i, n])

  useEffect(() => {
    const h = (e) => {
      if (/input|textarea|select/i.test(e.target.tagName)) return
      if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); go(1) }
      if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); go(-1) }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [go])

  const onScroll = (e) => {                                   // keep the counter in step with the snap position
    const h = e.currentTarget.clientHeight
    if (h) setI(Math.round(e.currentTarget.scrollTop / h))
  }
  const share = async (s) => {
    const text = `${s.headline}\n\n${s.body}`
    const link = `${location.origin}/story/${s.id}`
    try {
      if (navigator.share) await navigator.share({ title: s.headline, text: s.body, url: link })
      else { await navigator.clipboard.writeText(`${text}\n${link}`); setNote('Copied to the clipboard'); setTimeout(() => setNote(''), 1800) }
    } catch { /* share sheet dismissed */ }
  }

  return (
    <div className="sh-wrap">
      <div className="sh-bar">
        <div className="sh-topics" role="group" aria-label="Topic">
          {TOPICS.map((t) => <button key={t || 'all'} className={'chip' + (topic === t ? ' on' : '')} aria-pressed={topic === t} onClick={() => { setTopic(t); setI(0) }}>{t ? topicLabel(t) : 'All'}</button>)}
        </div>
        <select className="field sh-lang" value={lang} onChange={(e) => pickLang(e.target.value)} aria-label="Language">
          <option value="">English</option>
          {Object.entries(LANGS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        {listen.supported && n > 0 && (listen.state === 'idle'
          ? <button className="btn" onClick={briefing}>Play briefing</button>
          : <button className="btn" onClick={listen.stop}>Stop ({listen.index}/{listen.queue.length - 1})</button>)}
        <span className="sh-count" aria-live="polite">{n ? `${Math.min(i + 1, n)} / ${n}` : ''}{note && ` · ${note}`}</span>
      </div>
      {loading && !data && <div className="sh-empty">Loading…</div>}
      {error && !data && <div className="sh-empty">Can’t reach the server.</div>}
      {data && n === 0 && <div className="sh-empty"><h3>Nothing to show in short yet</h3><p>Stories appear here once they are explained. Build your briefing from Today, then come back.</p><Link className="btn primary" to="/">Go to Today</Link></div>}
      <div className="sh-deck" ref={deck} onScroll={onScroll} tabIndex={0} aria-label="Stories, one per screen. Arrow keys move between them.">
        {data?.map((s0) => { const s = shown(s0); return (
          <article key={s.id} className="sh-card" data-topic={s.topic}>
            {s.image_url && <img className="sh-img" src={s.image_url} alt="" loading="lazy" referrerPolicy="no-referrer" onError={(e) => { e.currentTarget.style.display = 'none' }} />}
            <div className="sh-body">
              <div className="sh-meta"><span className="chip">{topicLabel(s.topic)}</span><span>{s.outlet_count} outlet{s.outlet_count === 1 ? '' : 's'} · {timeAgo(s.published_at)}</span></div>
              <h2>{s.headline}</h2>
              <p lang={s === s0 ? 'en' : lang}>{s.body}</p>
              {lang && s === s0 && <p className="help">{tr[`${lang}:${s.id}`] === 'x' ? 'Translation unavailable for this story.' : 'Translating…'}</p>}
              <div className="sh-actions">
                <Link className="btn primary" to={`/story/${s.id}`}>Read the full story</Link>
                <button className="btn" onClick={() => share(s)}>Share</button>
              </div>
            </div>
          </article>
        ) })}
      </div>
    </div>
  )
}
