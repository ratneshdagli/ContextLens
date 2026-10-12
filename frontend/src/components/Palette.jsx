import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { topicLabel } from '../lib/format'
import { Atlas, Bookmark, Compass, Flow, Gear, Globe, List, Mic, Paper, Search, Today } from './Icons'

const PAGES = [
  { label: 'Today', to: '/', icon: Today, hint: 'Your briefing' },
  { label: 'Talk to Lens', to: '/live', icon: Mic, hint: 'Live voice' },
  { label: 'Flow', to: '/flow', icon: Flow, hint: 'Swipe through stories' },
  { label: 'Everything', to: '/all', icon: List, hint: 'All stories, unranked' },
  { label: 'Discover', to: '/explore', icon: Compass, hint: 'Research any topic' },
  { label: 'Newspapers', to: '/papers', icon: Paper, hint: 'Upload a PDF' },
  { label: 'Sources', to: '/sources', icon: Globe, hint: 'Where news comes from' },
  { label: 'Atlas', to: '/atlas', icon: Atlas, hint: 'Connected people & places' },
  { label: 'Saved', to: '/saved', icon: Bookmark, hint: 'Your reading list' },
  { label: 'Settings', to: '/settings', icon: Gear, hint: 'Models, feeds, voice' },
]

/** Ctrl/⌘+K — jump anywhere, search every story, or hand a question to Lens. */
export default function Palette({ onClose }) {
  const nav = useNavigate()
  const [q, setQ] = useState('')
  const [hits, setHits] = useState([])
  const [sel, setSel] = useState(0)
  const input = useRef(null)
  useEffect(() => { input.current?.focus() }, [])
  useEffect(() => {
    if (q.trim().length < 2) { setHits([]); return undefined }
    let live = true
    const t = setTimeout(() => api.search(q.trim()).then((r) => live && setHits((Array.isArray(r) ? r : r?.results || []).slice(0, 6))).catch(() => {}), 200)
    return () => { live = false; clearTimeout(t) }
  }, [q])

  const items = useMemo(() => {
    const s = q.trim().toLowerCase()
    const pages = PAGES.filter((p) => !s || p.label.toLowerCase().includes(s) || p.hint.toLowerCase().includes(s)).map((p) => ({ kind: 'page', ...p, go: () => nav(p.to) }))
    const stories = hits.map((h) => ({ kind: 'story', label: h.headline, hint: topicLabel(h.topic || ''), icon: Search, go: () => nav(`/story/${h.id}`) }))
    const ask = q.trim().length > 2 ? [{ kind: 'ask', label: `Ask Lens: “${q.trim()}”`, hint: 'Answer out loud, with sources', icon: Mic, go: () => nav(`/live?ask=${encodeURIComponent(q.trim())}`) }] : []
    return [...ask, ...stories, ...pages]
  }, [q, hits, nav])
  useEffect(() => setSel(0), [q, hits.length])

  const key = (e) => {
    if (e.key === 'Escape') onClose()
    else if (e.key === 'ArrowDown') { e.preventDefault(); setSel((i) => Math.min(items.length - 1, i + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((i) => Math.max(0, i - 1)) }
    else if (e.key === 'Enter' && items[sel]) { items[sel].go(); onClose() }
  }
  return (
    <div className="pal-back" onMouseDown={onClose}>
      <div className="pal" role="dialog" aria-label="Command palette" onMouseDown={(e) => e.stopPropagation()}>
        <div className="pal-in"><Search size={18} /><input ref={input} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={key} placeholder="Search stories, jump to a page, or ask Lens…" aria-label="Search or jump" /><kbd>Esc</kbd></div>
        <div className="pal-list" role="listbox">
          {items.map((it, i) => (
            <button key={it.kind + it.label} role="option" aria-selected={i === sel} className={'pal-item' + (i === sel ? ' on' : '') + (it.kind === 'ask' ? ' ask' : '')}
              onMouseEnter={() => setSel(i)} onClick={() => { it.go(); onClose() }}>
              <it.icon size={17} /><span>{it.label}</span><small>{it.hint}</small>
            </button>
          ))}
          {!items.length && <div className="pal-empty">No matches</div>}
        </div>
      </div>
    </div>
  )
}
