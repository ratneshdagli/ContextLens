import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { Card } from '../components/StoryCards'
import { Atlas, Flask, Link as LinkIcon, Paper, Search, Sparkle } from '../components/Icons'
import { useApp } from '../components/Shell'
import AskBriefing from '../components/AskBriefing'

const IDEAS = ['What is happening with the US–China chip export controls?', 'RBI interest rate outlook', 'Latest on the Russia–Ukraine war', 'How is AI changing hiring?']

export default function Explore() {
  const nav = useNavigate()
  const { toast, health } = useApp()
  const [params, setParams] = useSearchParams()
  const [q, setQ] = useState(params.get('q') || '')
  const [busy, setBusy] = useState(false)
  const [link, setLink] = useState('')
  const [text, setText] = useState('')
  const query = params.get('q') || ''
  const down = health && !health.llm?.available
  const results = useAsync(() => (query ? api.search(query) : Promise.resolve(null)), [query])
  const recent = useAsync(() => api.researchList(), [])
  useEffect(() => setQ(query), [query])

  const submit = (e) => { e.preventDefault(); if (q.trim().length >= 2) setParams({ q: q.trim() }) }
  const research = async (topic, mode = 'research') => {
    setBusy(true)
    try {
      const r = await api.research(topic, mode)
      nav(`/story/${r.story_id}?run=${r.run_id}`)
    } catch (err) { toast(err.message); setBusy(false) }
  }
  const explainLink = async (e) => {
    e.preventDefault()
    const isUrl = /^https?:\/\//i.test(link.trim())
    if (!isUrl && text.trim().length < 300) { toast('Paste a link, or at least a few paragraphs of text'); return }
    setBusy(true)
    try {
      const r = await api.explainLink(isUrl ? { url: link.trim() } : { text: text.trim() })
      nav(`/story/${r.story_id}${r.run_id ? `?run=${r.run_id}` : ''}`)
    } catch (err) { toast(err.message); setBusy(false) }
  }

  return (
    <div className="container narrow">
      <div className="eyebrow">Discover</div>
      <h1 className="h-page" style={{ margin: '6px 0 8px' }}>Understand anything</h1>
      <p className="lede" style={{ marginBottom: 22 }}>Ask your briefing, explain any article, research a topic from scratch, or check a claim.</p>

      <AskBriefing disabled={!!down} />

      <div className="tool-grid">
        <form className="tool-card" onSubmit={explainLink}>
          <h3><LinkIcon size={18} /> Explain an article</h3>
          <p className="small muted">Paste a link — or the article’s text if it’s behind a paywall — and get the gist, why it happened, the background and a timeline.</p>
          <input className="field" value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://…" aria-label="Article link" />
          {!/^https?:\/\//i.test(link.trim()) && <textarea className="field" value={text} onChange={(e) => setText(e.target.value)} placeholder="…or paste the article text here" aria-label="Article text" style={{ minHeight: 70 }} />}
          <button className="btn primary" disabled={busy || down}><Sparkle size={15} /> {busy ? 'Working…' : 'Explain it'}</button>
        </form>
        <div className="tool-card">
          <h3><Paper size={18} /> Upload a newspaper</h3>
          <p className="small muted">Drop in an e-paper PDF: we split it into articles, keep what matters to you and explain the best ones.</p>
          <button className="btn" onClick={() => nav('/papers')}>Open newspapers</button>
          <h3 style={{ marginTop: 10 }}><Atlas size={18} /> Atlas</h3>
          <p className="small muted">See who and what keeps appearing across your stories.</p>
          <button className="btn" onClick={() => nav('/atlas')}>Open the Atlas</button>
        </div>
      </div>

      <div className="eyebrow" style={{ marginBottom: 8 }}>Search, research or fact-check</div>
      <form onSubmit={submit} style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <div style={{ position: 'relative', flex: '1 1 260px' }}>
          <span style={{ position: 'absolute', left: 13, top: 12, color: 'var(--ink-3)' }}><Search size={18} /></span>
          <input className="field" style={{ paddingLeft: 42, height: 46 }} value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Nvidia earnings, RBI repo rate, Formula 1…" aria-label="Search or research a topic" />
        </div>
        <button className="btn lg" type="submit">Search</button>
        <button className="btn accent lg" type="button" disabled={busy || down || q.trim().length < 3} onClick={() => research(q.trim())}>{busy ? 'Starting…' : 'Research'}</button>
        <button className="btn lg" type="button" disabled={busy || down || q.trim().length < 8} onClick={() => research(q.trim(), 'claim')} title="Paste a claim; get a verdict backed by several outlets"><Flask size={16} /> Fact-check</button>
      </form>
      <p className="help">Research writes a cited briefing on a topic. Fact-check takes a claim (“X banned Y last week”) and returns a verdict — never decisive on fewer than two independent outlets.</p>

      {!query && <div style={{ marginTop: 16 }} className="suggest">{IDEAS.map((i) => <button key={i} className="chip outline" onClick={() => { setQ(i); research(i) }} disabled={busy || down}>{i}</button>)}</div>}

      {query && (
        <section style={{ marginTop: 30 }}>
          <div className="eyebrow">Results in your briefing for “{query}”</div>
          {results.loading && <div className="sk" style={{ height: 80, marginTop: 14 }} />}
          {results.data?.length === 0 && (
            <div className="empty" style={{ padding: '36px 0' }}>
              <h3>Nothing in your briefing matches</h3>
              <p>Want a fresh, cited briefing on “{query}”? It takes a couple of minutes.</p>
              <button className="btn accent" disabled={busy || down} onClick={() => research(query)}>Research this topic</button>
            </div>
          )}
          <div style={{ display: 'grid', gap: 12, marginTop: 12 }}>{results.data?.map((s) => <Card key={s.id} s={s} />)}</div>
        </section>
      )}

      {recent.data?.length > 0 && !query && (
        <section style={{ marginTop: 40 }}>
          <div className="eyebrow" style={{ marginBottom: 6 }}>Your research</div>
          {recent.data.map((r) => (
            <div key={r.id} className="more-row" onClick={() => nav(`/story/${r.id}`)}>
              <span className="t">{r.status === 'complete' ? r.headline : r.query}</span>
              <span className="r">{r.status === 'failed' ? 'failed' : r.status === 'researching' ? 'in progress' : timeAgo(r.published_at)}</span>
            </div>
          ))}
        </section>
      )}
    </div>
  )
}
