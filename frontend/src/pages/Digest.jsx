import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, uploadDocWatch } from '../lib/api'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { useApp } from '../components/Shell'
import { Close, External, Refresh, Repo, Upload } from '../components/Icons'

function RepoCard({ w, onRefresh, onRemove, busy }) {
  const d = w.digest || {}
  return (
    <div className="digest-card">
      <div className="digest-head">
        <span className="kicker" style={{ '--tc': 'var(--accent)' }}><Repo size={14} /> GitHub</span>
        <div className="digest-actions">
          <button className="icon-btn sm" onClick={() => onRefresh(w.id)} disabled={busy} aria-label="Refresh" title="Check again"><Refresh size={14} /></button>
          <button className="icon-btn sm" onClick={() => onRemove(w.id)} aria-label="Stop tracking" title="Stop tracking"><Close size={14} /></button>
        </div>
      </div>
      <a href={`https://github.com/${w.label}`} target="_blank" rel="noreferrer" className="digest-title">{w.label} <External size={12} /></a>
      {w.meta?.description && <p className="small muted">{w.meta.description}</p>}
      <p className="digest-summary">{d.summary || (busy ? 'Checking…' : 'Not checked yet.')}</p>
      {d.highlights?.length > 0 && (
        <ul className="digest-list">{d.highlights.map((h, i) => <li key={i}>{h}</li>)}</ul>
      )}
      <span className="small muted">{d.generated_at ? `Checked ${timeAgo(d.generated_at)}` : ''}</span>
    </div>
  )
}

function TopicCard({ w, onRefresh, onRemove, busy }) {
  const d = w.digest || {}
  const topics = w.meta?.topics || [w.label]
  return (
    <div className="digest-card">
      <div className="digest-head">
        <span className="kicker" style={{ '--tc': 'var(--teal)' }}>{w.meta?.source === 'doc' ? 'From your document' : 'Topic'}</span>
        <div className="digest-actions">
          <button className="icon-btn sm" onClick={() => onRefresh(w.id)} disabled={busy} aria-label="Refresh" title="Check again"><Refresh size={14} /></button>
          <button className="icon-btn sm" onClick={() => onRemove(w.id)} aria-label="Stop tracking" title="Stop tracking"><Close size={14} /></button>
        </div>
      </div>
      <b className="digest-title">{w.label}</b>
      <div className="chip-row">{topics.map((t) => <span key={t} className="chip">{t}</span>)}</div>
      <p className="digest-summary">{d.summary || (busy ? 'Checking…' : 'Not checked yet.')}</p>
      {d.items?.length > 0 && (
        <ul className="digest-list">
          {d.items.map((s) => <li key={s.id}><Link to={`/story/${s.id}`}>{s.headline}</Link></li>)}
        </ul>
      )}
      <span className="small muted">{d.generated_at ? `Checked ${timeAgo(d.generated_at)}` : ''}</span>
    </div>
  )
}

export default function Digest() {
  const { toast } = useApp()
  const { data, reload } = useAsync(() => api.digest(), [])
  const [repoText, setRepoText] = useState('')
  const [topicText, setTopicText] = useState('')
  const [busyId, setBusyId] = useState(null)
  const [addingRepo, setAddingRepo] = useState(false)
  const [addingTopic, setAddingTopic] = useState(false)
  const [uploading, setUploading] = useState(false)
  const fileInput = useRef(null)
  const items = data?.items || []
  const repos = items.filter((w) => w.kind === 'repo')
  const topics = items.filter((w) => w.kind === 'topic')

  const addRepo = async (e) => {
    e.preventDefault()
    const v = repoText.trim()
    if (!v) return
    setAddingRepo(true)
    try { await api.addRepoWatch(v); setRepoText(''); reload(true) } catch (err) { toast(err.message) } finally { setAddingRepo(false) }
  }
  const addTopic = async (e) => {
    e.preventDefault()
    const v = topicText.trim()
    if (!v) return
    setAddingTopic(true)
    try { await api.addTopicWatch(v); setTopicText(''); reload(true) } catch (err) { toast(err.message) } finally { setAddingTopic(false) }
  }
  const uploadDoc = async (file) => {
    if (!file) return
    setUploading(true)
    try { await uploadDocWatch(file); reload(true) } catch (err) { toast(err.message) } finally { setUploading(false) }
  }
  const refresh = async (id) => {
    setBusyId(id)
    try { await api.refreshWatch(id); reload(true) } catch (err) { toast(err.message) } finally { setBusyId(null) }
  }
  const remove = async (id) => {
    try { await api.removeWatch(id); reload(true) } catch (err) { toast(err.message) }
  }

  return (
    <div className="container narrow">
      <div className="eyebrow">Your digest</div>
      <h1 className="h-page" style={{ margin: '6px 0 8px' }}>What's new, beyond the news</h1>
      <p className="lede" style={{ marginBottom: 22 }}>
        Track a GitHub project you're learning from, or a subject from a document you care about, and ContextLens
        checks in on it for you — real commits for a repo, this week's matching coverage for a topic. Following a
        <i> story</i> instead? That lives on the <Link to="/">Today</Link> page next to it.
      </p>

      <div className="digest-adders">
        <form className="digest-add" onSubmit={addRepo}>
          <Repo size={17} />
          <input className="field" value={repoText} onChange={(e) => setRepoText(e.target.value)} placeholder="owner/repo, e.g. facebook/react" aria-label="GitHub repo" />
          <button className="btn primary" disabled={!repoText.trim() || addingRepo}>{addingRepo ? 'Adding…' : 'Track repo'}</button>
        </form>
        <form className="digest-add" onSubmit={addTopic}>
          <span className="digest-add-icon">#</span>
          <input className="field" value={topicText} onChange={(e) => setTopicText(e.target.value)} placeholder="A topic, e.g. WebGPU, Indian monsoon policy" aria-label="Topic" />
          <button className="btn primary" disabled={!topicText.trim() || addingTopic}>{addingTopic ? 'Adding…' : 'Track topic'}</button>
        </form>
        <div className="digest-add">
          <Upload size={17} />
          <span className="small muted" style={{ flex: 1 }}>Or upload a document — ContextLens reads it and tracks the topics it's about.</span>
          <input ref={fileInput} type="file" accept=".pdf,.txt,text/plain,application/pdf" hidden onChange={(e) => uploadDoc(e.target.files?.[0])} />
          <button className="btn" disabled={uploading} onClick={() => fileInput.current?.click()}>{uploading ? 'Reading…' : 'Upload document'}</button>
        </div>
      </div>

      {repos.length > 0 && (
        <>
          <div className="sec-head"><h2>Repositories</h2></div>
          <div className="digest-grid">{repos.map((w) => <RepoCard key={w.id} w={w} onRefresh={refresh} onRemove={remove} busy={busyId === w.id} />)}</div>
        </>
      )}
      {topics.length > 0 && (
        <>
          <div className="sec-head"><h2>Topics</h2></div>
          <div className="digest-grid">{topics.map((w) => <TopicCard key={w.id} w={w} onRefresh={refresh} onRemove={remove} busy={busyId === w.id} />)}</div>
        </>
      )}
      {items.length === 0 && (
        <p className="small muted" style={{ marginTop: 20 }}>Nothing tracked yet — add a repo, a topic, or a document above.</p>
      )}
    </div>
  )
}
