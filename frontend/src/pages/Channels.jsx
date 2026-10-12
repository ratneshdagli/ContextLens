import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import '../v62.css'
import { api } from '../lib/api'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { useApp } from '../components/Shell'
import { Refresh } from '../components/Icons'

const WEIGHTS = [[0, 'Off'], [1, 'Some'], [2, 'More'], [3, 'Most']]
const DEPTHS = [[1, 'Quick'], [2, 'Standard'], [3, 'Deep']]
const ARTICLES = { 1: 2, 2: 3, 3: 5 }              // mirrors DEPTH_ARTICLES on the server: full-text articles read per story
const EXAMPLES = [
  ['Classical music', 'orchestras, conductors, new recordings, opera, festivals'],
  ['Indian startups', 'funding rounds, founders, unicorns, shutdowns, policy that affects startups'],
  ['Formula 1', 'race weekends, team news, driver moves, technical regulations'],
]

function Seg({ value, options, onChange, label }) {
  return <div className="seg" role="group" aria-label={label}>{options.map(([v, l]) => <button key={v} className={value === v ? 'on' : ''} onClick={() => onChange(v)} aria-pressed={value === v}>{l}</button>)}</div>
}

function Chips({ values, onChange, placeholder }) {
  const [v, setV] = useState('')
  const add = () => { const t = v.trim(); if (t && !values.includes(t)) onChange([...values, t]); setV('') }
  return (
    <div className="chips">
      {values.map((t) => <span key={t} className="chip">{t}<button aria-label={`Remove ${t}`} onClick={() => onChange(values.filter((x) => x !== t))}>×</button></span>)}
      <input className="field" value={v} onChange={(e) => setV(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add() } }} onBlur={add} placeholder={placeholder} />
    </div>
  )
}

function Sources({ ch, reload, toast }) {
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const act = async (fn) => { try { await fn(); await reload(true) } catch (e) { toast(e.message) } }
  const add = async () => {
    if (!url.trim()) return
    setBusy(true)
    await act(async () => { await api.addChannelFeed(ch.id, url.trim()); setUrl('') })
    setBusy(false)
  }
  return (
    <div className="chan-sources">
      {ch.feeds.length === 0 && <p className="small muted">No sources yet. “Find sources” searches the web for sites that cover this topic and keeps only those with a working feed.</p>}
      {ch.feeds.map((f) => (
        <div key={f.url} className={'feed-row' + (f.off ? ' off' : '')}>
          <div style={{ minWidth: 0 }}>
            <b>{f.outlet || f.domain}</b>{f.manual && <span className="tag">added by you</span>}
            <div className="small muted" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.domain} · {f.n} recent items{f.checked_at ? ` · checked ${timeAgo(f.checked_at)}` : ''}</div>
          </div>
          <button className="btn sm ghost" onClick={() => act(() => api.toggleChannelFeed(ch.id, f.url, !f.off))}>{f.off ? 'Enable' : 'Disable'}</button>
          <button className="btn sm ghost" aria-label={`Remove ${f.outlet || f.domain}`} onClick={() => act(() => api.removeChannelFeed(ch.id, f.url))}>Remove</button>
        </div>
      ))}
      <div className="feed-add">
        <input className="field" value={url} onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && add()} placeholder="Add a site or feed address, e.g. gramophone.co.uk" aria-label="Add a source" />
        <button className="btn sm" onClick={add} disabled={busy || !url.trim()}>{busy ? 'Checking…' : 'Add'}</button>
      </div>
    </div>
  )
}

function ChannelCard({ ch, index, count, reload, move }) {
  const { toast, refreshChannel, running, health } = useApp()
  const [edit, setEdit] = useState(false)
  const [draft, setDraft] = useState({ name: ch.name, description: ch.description })
  const [finding, setFinding] = useState(false)
  const [note, setNote] = useState('')
  const [open, setOpen] = useState(ch.feeds.length === 0)
  const llmDown = health && !health.llm?.available
  const save = async (patch) => { try { await api.saveChannel(ch.id, patch); await reload(true); return true } catch (e) { toast(e.message); return false } }

  const find = async () => {
    setFinding(true); setNote('')
    try {
      const r = await api.discoverChannel(ch.id)
      const d = r.discovery
      setNote(d.search_hits === 0 ? 'The search server returned nothing, so no sources could be found. Is SearXNG running on port 8080?'
        : `Checked ${d.domains_tried} sites, found ${d.feeds_found} with a working feed.` + (d.covered_by_general_feeds.length ? ` ${d.covered_by_general_feeds.join(', ')} are already in your general feeds and are matched by meaning.` : ''))
      setOpen(true)
      await reload(true)
    } catch (e) { toast(e.message) }
    setFinding(false)
  }
  const remove = async () => {
    if (!window.confirm(`Delete the channel “${ch.name}”? Its stories stay as ordinary news.`)) return
    try { await api.deleteChannel(ch.id); await reload(true) } catch (e) { toast(e.message) }
  }

  return (
    <section className={'card panel chan-card' + (ch.weight === 0 ? ' off' : '')}>
      <div className="chan-top">
        <div style={{ minWidth: 0, flex: 1 }}>
          {edit ? (
            <div className="chan-edit">
              <input className="field" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} aria-label="Channel name" maxLength={60} />
              <textarea className="field" rows={2} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} aria-label="Channel description" maxLength={400} placeholder="What should this channel cover?" />
              <div style={{ display: 'flex', gap: 8 }}>
                <button className="btn sm primary" onClick={async () => { if (await save(draft)) setEdit(false) }} disabled={!draft.name.trim()}>Save</button>
                <button className="btn sm ghost" onClick={() => { setDraft({ name: ch.name, description: ch.description }); setEdit(false) }}>Cancel</button>
              </div>
            </div>
          ) : (
            <>
              <h3>{ch.name}</h3>
              {ch.description && <p className="small muted">{ch.description}</p>}
              <p className="small muted" style={{ marginTop: 4 }}>{ch.stats.stories} {ch.stats.stories === 1 ? 'story' : 'stories'} · {ch.stats.built} explained · {ch.feeds_live} live {ch.feeds_live === 1 ? 'source' : 'sources'}{ch.stats.last ? ` · latest ${timeAgo(ch.stats.last)}` : ''}</p>
            </>
          )}
        </div>
        <div className="chan-actions">
          <button className="icon-btn" aria-label="Move up" disabled={index === 0} onClick={() => move(index, -1)}>↑</button>
          <button className="icon-btn" aria-label="Move down" disabled={index === count - 1} onClick={() => move(index, 1)}>↓</button>
        </div>
      </div>

      <div className="chan-buttons">
        <Link className="btn sm primary" to={`/?c=${ch.id}`}>Open</Link>
        <button className="btn sm" onClick={() => refreshChannel(ch.id)} disabled={running || llmDown || ch.weight === 0} title="Read only this channel's sources and write its stories"><Refresh size={14} /> Fill now</button>
        <button className="btn sm" onClick={find} disabled={finding}>{finding ? <><span className="spinner" /> Finding sources…</> : 'Find sources'}</button>
        <button className="btn sm ghost" onClick={() => setEdit((v) => !v)}>Rename / describe</button>
        <button className="btn sm ghost" onClick={() => setOpen((v) => !v)} aria-expanded={open}>{open ? 'Hide' : 'Show'} sources &amp; settings</button>
        <button className="btn sm ghost danger" onClick={remove}>Delete</button>
      </div>
      {finding && <p className="help">Searching Google News and the web, then checking each site for a working feed. This takes one to two minutes.</p>}
      {note && <p className="help">{note}</p>}

      {open && (
        <div className="chan-body">
          <div className="chan-settings">
            <div><label>Priority</label><Seg label="Priority" value={ch.weight} options={WEIGHTS} onChange={(v) => save({ weight: v })} /></div>
            <div><label>Detail per story</label><Seg label="Detail" value={ch.depth} options={DEPTHS} onChange={(v) => save({ depth: v })} /><span className="help">reads {ARTICLES[ch.depth]} articles per story</span></div>
            <div><label>Stories</label><select className="field" value={ch.size} onChange={(e) => save({ size: +e.target.value })}>{[6, 12, 20, 30].map((n) => <option key={n} value={n}>{n}</option>)}</select></div>
            <div><label>Look back</label><select className="field" value={ch.days} onChange={(e) => save({ days: +e.target.value })}>{[1, 3, 5, 7].map((n) => <option key={n} value={n}>{n} {n === 1 ? 'day' : 'days'}</option>)}</select></div>
          </div>
          <h4>Sources</h4>
          <Sources ch={ch} reload={reload} toast={toast} />
          <h4>Search phrases <span className="small muted">used to find sources and to look for fresh stories on every refresh</span></h4>
          <Chips values={ch.queries} onChange={(q) => save({ queries: q })} placeholder="Add a phrase, press Enter" />
          <h4>Never show in this channel</h4>
          <Chips values={ch.muted} onChange={(m) => save({ muted: m })} placeholder="Add a word to mute, press Enter" />
        </div>
      )}
    </section>
  )
}

function NewChannel({ onDone }) {
  const { toast } = useApp()
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [step, setStep] = useState('')
  const busy = !!step
  const create = async () => {
    try {
      setStep('Creating the channel…')
      const ch = await api.createChannel({ name, description: desc })
      setStep('Finding sources that cover it (1–2 minutes)…')
      await api.discoverChannel(ch.id).catch((e) => toast(`Channel created, but finding sources failed: ${e.message}`))
      setName(''); setDesc('')
      toast('Channel ready — press “Fill now” to read its sources')
      await onDone()
    } catch (e) { toast(e.message) }
    setStep('')
  }
  return (
    <section className="card panel chan-new">
      <h3>Create a channel</h3>
      <p className="small muted">Your own category — as broad (“Technology”) or as niche (“Classical music”) as you like. Describe what it should cover and ContextLens finds the sources by itself.</p>
      <input className="field" value={name} onChange={(e) => setName(e.target.value)} placeholder="Name, e.g. Classical music" aria-label="Channel name" maxLength={60} disabled={busy} />
      <textarea className="field" rows={2} value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="What should it cover? e.g. orchestras, conductors, new recordings, opera, festivals" aria-label="Channel description" maxLength={400} disabled={busy} />
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <button className="btn primary" onClick={create} disabled={busy || !name.trim()}>{busy ? <><span className="spinner" /> {step}</> : 'Create & find sources'}</button>
        {!busy && EXAMPLES.map(([n, d]) => <button key={n} className="pill" onClick={() => { setName(n); setDesc(d) }}>{n}</button>)}
      </div>
    </section>
  )
}

export default function Channels() {
  const { toast, refreshTick } = useApp()
  const { data, loading, error, reload } = useAsync(() => api.channels(), [])
  useEffect(() => { if (refreshTick) reload(true) }, [refreshTick]) // eslint-disable-line react-hooks/exhaustive-deps
  const list = data || []
  const move = async (i, d) => {
    const ids = list.map((c) => c.id)
    ;[ids[i], ids[i + d]] = [ids[i + d], ids[i]]
    try { await api.reorderChannels(ids); await reload(true) } catch (e) { toast(e.message) }
  }
  return (
    <div className="container">
      <div className="eyebrow">Yours</div>
      <h1 style={{ margin: '6px 0 4px' }}>My channels</h1>
      <p className="muted" style={{ marginBottom: 18 }}>Follow exactly what you care about. Every channel brings its own sources, gets its own tab on Today, and is written in as much detail as you choose.</p>
      <NewChannel onDone={() => reload(true)} />
      {loading && !data && <div className="sk" style={{ height: 120, marginTop: 16 }} />}
      {error && !data && <div className="empty"><h3>Can’t load channels</h3><p>{error.message}</p><button className="btn primary" onClick={() => reload()}>Try again</button></div>}
      {data && list.length === 0 && <div className="empty" style={{ padding: '36px 20px' }}><h3>No channels yet</h3><p>Create your first one above. Pick an example to see how it works.</p></div>}
      <div className="chan-list">
        {list.map((ch, i) => <ChannelCard key={ch.id} ch={ch} index={i} count={list.length} reload={reload} move={move} />)}
      </div>
    </div>
  )
}
