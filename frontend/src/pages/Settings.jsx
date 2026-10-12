import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { AlertsCard } from '../components/Alerts'
import { useApp } from '../components/Shell'

const TABS = [['interests', 'Interests'], ['feed', 'Feed'], ['models', 'Models'], ['sources', 'Sources'], ['data', 'Data'], ['activity', 'Activity']]

function Seg({ value, options, onChange }) {
  return <div className="seg" role="group">{options.map(([v, l]) => <button key={v} className={value === v ? 'on' : ''} onClick={() => onChange(v)} aria-pressed={value === v}>{l}</button>)}</div>
}
function Switch({ checked, onChange, label }) {
  return <label className="switch"><input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} aria-label={label} /><i /></label>
}
function Row({ title, desc, children }) {
  return <div className="row"><div className="l"><b>{title}</b>{desc && <span className="small muted">{desc}</span>}</div>{children}</div>
}
function TagInput({ values, onChange, placeholder }) {
  const [v, setV] = useState('')
  const add = () => { const t = v.trim(); if (t && !values.some((x) => x.toLowerCase() === t.toLowerCase())) onChange([...values, t]); setV('') }
  return (
    <div>
      <div className="tagbox" style={{ marginBottom: values.length ? 10 : 0 }}>
        {values.map((t) => <span key={t} className="chip">{t}<button aria-label={`Remove ${t}`} onClick={() => onChange(values.filter((x) => x !== t))}>×</button></span>)}
      </div>
      <input className="field" value={v} onChange={(e) => setV(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add() } }} onBlur={add} placeholder={placeholder} />
    </div>
  )
}

function Interests({ toast }) {
  const { data: p, setData } = useAsync(() => api.profile(), [])
  const { data: topics } = useAsync(() => api.topics(), [])
  const [draft, setDraft] = useState('')
  const save = useCallback(async (patch) => {
    try { const np = await api.saveProfile(patch); setData({ ...p, ...np }); toast('Saved') } catch (e) { toast(e.message) }
  }, [p, setData, toast])
  if (!p || !topics) return <div className="sk" style={{ height: 200 }} />
  const addInterest = () => { const t = draft.trim(); if (!t) return; save({ interests: [...p.interests, { text: t, weight: 2 }] }); setDraft('') }
  return (
    <>
      <div className="card panel">
        <h3>What you care about</h3>
        <p className="help" style={{ marginTop: 0 }}>Plain-language interests. Every story is scored against these, and an editor model checks them again before anything reaches your front page.</p>
        <div style={{ marginTop: 10 }}>
          {p.interests.map((it) => (
            <div className="weight" key={it.id}>
              <span>{it.text}</span>
              <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <Seg value={it.weight >= 3 ? 3 : 2} options={[[2, 'Normal'], [3, 'High priority']]} onChange={(w) => save({ interests: p.interests.map((x) => (x.id === it.id ? { ...x, weight: w } : x)) })} />
                <button className="icon-btn" aria-label={`Remove ${it.text}`} onClick={() => save({ interests: p.interests.filter((x) => x.id !== it.id) })}>×</button>
              </span>
            </div>
          ))}
          {p.interests.length === 0 && <p className="muted small" style={{ padding: '10px 0' }}>No specific interests yet — your topic weights below are used on their own.</p>}
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <input className="field" value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && addInterest()} placeholder="Add an interest, e.g. “electric vehicles and battery tech”" />
          <button className="btn" onClick={addInterest} disabled={!draft.trim()}>Add</button>
        </div>
      </div>

      <div className="card panel">
        <h3>Topics</h3>
        <div className="weights" style={{ marginTop: 8 }}>
          {topics.map((t) => (
            <div className="weight" key={t.id}>
              <div><b style={{ fontWeight: 600 }}>{t.label}</b><div className="small muted">{t.desc.split(',').slice(0, 3).join(', ')}</div></div>
              <Seg value={p.topics[t.id] ?? 1} options={[[0, 'Off'], [1, 'Some'], [2, 'More'], [3, 'Most']]} onChange={(w) => save({ topics: { [t.id]: w } })} />
            </div>
          ))}
        </div>
      </div>

      <div className="card panel">
        <h3>Follow &amp; mute</h3>
        <div style={{ marginTop: 12 }}><label className="label">Always surface stories mentioning</label>
          <TagInput values={p.followed} onChange={(v) => save({ followed: v })} placeholder="Nvidia, RBI, Verstappen… (press Enter)" /></div>
        <div style={{ marginTop: 18 }}><label className="label">Never show stories mentioning</label>
          <TagInput values={p.muted_keywords} onChange={(v) => save({ muted_keywords: v })} placeholder="horoscope, celebrity… (press Enter)" /></div>
        <div style={{ marginTop: 18 }}><label className="label">Mute outlets (domain)</label>
          <TagInput values={p.muted_outlets} onChange={(v) => save({ muted_outlets: v })} placeholder="example.com (press Enter)" /></div>
      </div>

      <div className="card panel">
        <h3>Learned taste</h3>
        <Row title={`${p.taste_signals || 0} signals learned`} desc="From your likes, “less like this” and saves. It gently nudges ranking on top of your stated interests.">
          <button className="btn sm" disabled={!p.taste_signals} onClick={async () => { await api.resetTaste(); setData({ ...p, taste_signals: 0 }); toast('Taste reset') }}>Reset</button>
        </Row>
      </div>
    </>
  )
}

function FeedTab({ s, set }) {
  return (
    <div className="card panel">
      <h3>How your briefing is built</h3>
      <Row title="Automatic refresh" desc="Read the newspapers and update your briefing in the background."><Switch checked={s.auto_refresh} onChange={(v) => set({ auto_refresh: v })} label="Automatic refresh" /></Row>
      <Row title="Refresh every"><select className="field" style={{ width: 150 }} value={s.refresh_minutes} onChange={(e) => set({ refresh_minutes: +e.target.value })}>{[30, 60, 120, 240, 480].map((m) => <option key={m} value={m}>{m >= 60 ? `${m / 60} hour${m > 60 ? 's' : ''}` : `${m} minutes`}</option>)}</select></Row>
      <Row title="Stories shown instantly" desc="After each refresh this many stories appear on your front page immediately (no waiting for the AI). The writers then explain them one by one, best first."><select className="field" style={{ width: 150 }} value={s.wire_size} onChange={(e) => set({ wire_size: +e.target.value })}>{[60, 120, 200, 300, 400].map((n) => <option key={n} value={n}>{n} stories</option>)}</select></Row>
      <Row title="Explained per refresh" desc="How many stories the writers fully explain (why it happened, timeline, background) in each refresh. Every story can also be explained on demand by opening it. On a laptop GPU each takes several minutes."><select className="field" style={{ width: 150 }} value={s.build_budget} onChange={(e) => set({ build_budget: +e.target.value })}>{[4, 10, 20, 30, 60].map((n) => <option key={n} value={n}>{n} stories</option>)}</select></Row>
      <Row title="How far back to look"><select className="field" style={{ width: 150 }} value={s.max_age_hours} onChange={(e) => set({ max_age_hours: +e.target.value })}>{[24, 48, 72].map((n) => <option key={n} value={n}>{n} hours</option>)}</select></Row>
      <Row title="Editor strictness" desc="Lenient shows more; strict keeps only what clearly matters to you.">
        <Seg value={s.strictness} options={[['lenient', 'Lenient'], ['balanced', 'Balanced'], ['strict', 'Strict']]} onChange={(v) => set({ strictness: v })} /></Row>
      <Row title="Minimum Signal" desc="Hide front-page stories below this significance score (1–10). Raise it for a shorter, more selective briefing.">
        <Seg value={s.min_signal} options={[[0, 'Any'], [4, '4+'], [6, '6+'], [8, '8+']]} onChange={(v) => set({ min_signal: v })} /></Row>
      <Row title="Research depth" desc="How many full articles are read per story. Deeper is slower and unlocks timelines and story maps.">
        <Seg value={s.depth} options={[[1, 'Quick'], [2, 'Balanced'], [3, 'Thorough']]} onChange={(v) => set({ depth: v })} /></Row>
      <Row title="Entity recognition" desc="A small on-device model that finds people and organisations for story maps and key players. Uses about 600 MB of RAM — turn it off on a tight machine (a simpler fallback is used).">
        <Switch checked={s.ner_enabled} onChange={(v) => set({ ner_enabled: v })} label="Entity recognition" /></Row>
      <Row title="Writing level"><Seg value={s.reading_level} options={[['simple', 'Simple'], ['general', 'General'], ['expert', 'Expert']]} onChange={(v) => set({ reading_level: v })} /></Row>
    </div>
  )
}

function LoadedModels() {
  const { data, reload } = useAsync(() => api.modelsStatus(), [])
  const [busy, setBusy] = useState(false)
  if (!data) return null
  const chat = data.loaded.filter((m) => m.type === 'llm')
  return (
    <div className="model-line" style={{ marginTop: 12 }}>
      <span className={'dot ' + (chat.length === 1 ? 'on' : chat.length > 1 ? 'off' : '')} />
      <span style={{ flex: 1 }}>
        {data.managed ? (chat.length ? <>Loaded now: <b>{chat.map((m) => m.id).join(', ')}</b>{chat.length > 1 && ' — more than one chat model is loaded!'}</> : 'No chat model loaded (it loads on demand).') : 'Model manager unavailable (LM Studio CLI not found) — LM Studio’s own loading applies.'}
        {data.swaps > 0 && <span className="muted"> · {data.swaps} model swap{data.swaps > 1 ? 's' : ''} this session</span>}
      </span>
      <button className="btn sm" disabled={busy || !chat.length} onClick={async () => { setBusy(true); await api.unloadModels().catch(() => {}); await reload(); setBusy(false) }}>Unload now</button>
    </div>
  )
}

function ModelsTab({ s, set, info }) {
  const [res, setRes] = useState({})
  const [busy, setBusy] = useState('')
  const models = info.available_models || []
  const chat = models.filter((m) => !/embed/i.test(m))
  const emb = models.filter((m) => /embed/i.test(m))
  const test = async (role) => { setBusy(role); const r = await api.testModel(role).catch((e) => ({ ok: false, detail: e.message })); setRes((x) => ({ ...x, [role]: r })); setBusy('') }
  const Sel = ({ k, list, role, title, desc }) => (
    <Row title={title} desc={desc}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
        <select className="field" style={{ width: 260 }} value={s[k]} onChange={(e) => set({ [k]: e.target.value })}>
          {!list.includes(s[k]) && <option value={s[k]}>{s[k]} (not found)</option>}
          {list.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
        <button className="btn sm" onClick={() => test(role)} disabled={busy === role}>{busy === role ? 'Testing…' : 'Test'}</button>
        {res[role] && <span className={'chip ' + (res[role].ok ? 'good' : 'bad')} title={res[role].detail}>{res[role].ok ? `OK · ${res[role].seconds}s` : 'Failed'}</span>}
      </div>
    </Row>
  )
  return (
    <>
      <div className="card panel">
        <h3>Language models</h3>
        <p className="help" style={{ marginTop: 0 }}>Connected to <code>{info.llm_url}</code> — {info.llm_available ? `${models.length} models available` : <b style={{ color: 'var(--bad)' }}>offline</b>}. Any OpenAI-compatible server works.</p>
        <Sel k="model_writer" list={chat} role="writer" title="Writer" desc="Writes headlines, summaries, timelines and answers questions." />
        <Sel k="model_editor" list={chat} role="editor" title="Editor" desc="An independent second model that decides what’s worth showing and audits every draft. Pick a different model family from the writer." />
        <Sel k="model_embed" list={emb.length ? emb : [s.model_embed]} role="embed" title="Embeddings" desc="Matches stories to your interests and groups the same event across outlets." />
        <Row title="Single-model mode" desc="Use the writer for everything. Choose this if switching models is slow on your GPU."><Switch checked={s.single_model_mode} onChange={(v) => set({ single_model_mode: v })} label="Single-model mode" /></Row>
        <Row title="Keep only one model loaded" desc="Before a different model is needed, the previous one is unloaded from LM Studio (embeddings, ~80 MB, stay loaded). Recommended on GPUs under 12 GB."><Switch checked={s.manage_models} onChange={(v) => set({ manage_models: v })} label="Manage loaded models" /></Row>
        <Row title="Unload when idle" desc="LM Studio frees the GPU after this long without a request."><select className="field" style={{ width: 150 }} value={s.idle_unload_minutes} onChange={(e) => set({ idle_unload_minutes: +e.target.value })}>{[0, 5, 10, 30, 60].map((m) => <option key={m} value={m}>{m ? `${m} minutes` : 'never'}</option>)}</select></Row>
        <LoadedModels />
      </div>
      <div className="card panel">
        <h3>Search</h3>
        <Row title="SearXNG" desc="Used by Research to find articles beyond your feeds."><code>{info.searxng_url}</code></Row>
      </div>
    </>
  )
}

function SourcesTab() {
  const { data } = useAsync(() => api.sources(), [])
  const [q, setQ] = useState('')
  const rows = useMemo(() => (data || []).filter((s) => (s.name + s.domain).toLowerCase().includes(q.toLowerCase())), [data, q])
  return (
    <div className="card panel">
      <h3>Trusted sources</h3>
      <p className="help" style={{ marginTop: 0 }}>Outlets are ranked by tier: Tier 1 wires and papers of record, Tier 2 reputable national outlets, Tier 3 specialist. Coverage by more independent outlets raises a story’s priority.</p>
      <input className="field" style={{ margin: '12px 0' }} placeholder="Filter…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter sources" />
      <table className="tbl"><thead><tr><th>Outlet</th><th>Domain</th><th>Tier</th></tr></thead>
        <tbody>{rows.map((s) => <tr key={s.domain}><td>{s.name}</td><td className="muted">{s.domain}</td><td><span className={'trust t' + s.trust_rank}>Tier {s.trust_rank}</span></td></tr>)}</tbody></table>
    </div>
  )
}

function DataTab({ toast }) {
  const [confirm, setConfirm] = useState(false)
  const exp = async () => {
    const d = await api.exportData()
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: 'application/json' })); a.download = 'contextlens-export.json'; a.click()
  }
  return (
    <div className="card panel">
      <h3>Your data</h3>
      <p className="help" style={{ marginTop: 0 }}>Everything is stored locally in a SQLite file. Nothing leaves your machine except requests to the news sites and your own model server.</p>
      <Row title="Export" desc="Profile, settings, saved stories and feedback as JSON."><button className="btn" onClick={exp}>Download</button></Row>
      <Row title="Clear all stories" desc="Deletes stories, articles and history. Your interests and settings are kept.">
        {confirm ? <span style={{ display: 'flex', gap: 8 }}><button className="btn" onClick={() => setConfirm(false)}>Cancel</button><button className="btn accent" onClick={async () => { await api.clearData(); setConfirm(false); toast('All stories cleared') }}>Yes, delete</button></span>
          : <button className="btn" onClick={() => setConfirm(true)}>Clear…</button>}
      </Row>
    </div>
  )
}

function Activity() {
  const { data } = useAsync(() => api.runs(), [])
  const [open, setOpen] = useState(null)
  const [detail, setDetail] = useState(null)
  useEffect(() => { if (open) api.run(open).then(setDetail); else setDetail(null) }, [open])
  return (
    <div className="card panel">
      <h3>Recent runs</h3>
      <p className="help" style={{ marginTop: 0 }}>Every decision the editors make is logged here — why a story was kept, demoted or dropped.</p>
      {(data || []).map((r) => (
        <div key={r.id}>
          <div className="row" style={{ cursor: 'pointer' }} onClick={() => setOpen(open === r.id ? null : r.id)}>
            <div className="l"><b>{r.kind === 'research' ? `Research: ${r.label}` : `Feed refresh (${r.label})`}</b><span className="small muted">{timeAgo(r.started_at)}{r.stats?.built ? ` · ${r.stats.built.primary} front-page, ${r.stats.built.more} more, ${r.stats.built.rejected} rejected` : ''}</span></div>
            <span className={'chip ' + (r.status === 'complete' ? 'good' : r.status === 'failed' ? 'bad' : 'warn')}>{r.status}</span>
          </div>
          {open === r.id && detail && (
            <div className="run-log" style={{ margin: '0 0 12px' }}>
              {detail.events.map((e, i) => <div key={i} className={e.level}><span className="st">{e.stage}</span>{e.message}</div>)}
              {detail.error && <div className="error">{detail.error}</div>}
            </div>
          )}
        </div>
      ))}
      {data?.length === 0 && <p className="muted small">No runs yet.</p>}
    </div>
  )
}

export default function Settings() {
  const { toast } = useApp()
  const [tab, setTab] = useState('interests')
  const { data: info, setData } = useAsync(() => api.settings(), [])
  const set = useCallback(async (patch) => {
    try { const ns = await api.saveSettings(patch); setData({ ...info, settings: ns }); toast('Saved') } catch (e) { toast(e.message) }
  }, [info, setData, toast])
  return (
    <div className="container">
      <div className="eyebrow">Settings</div>
      <h1 className="h-page" style={{ margin: '6px 0 26px' }}>Make it yours</h1>
      <div className="set-grid">
        <nav className="set-nav" aria-label="Settings sections">{TABS.map(([k, l]) => <button key={k} className={tab === k ? 'active' : ''} onClick={() => setTab(k)}>{l}</button>)}</nav>
        <div>
          {tab === 'interests' && <Interests toast={toast} />}
          {tab === 'feed' && (info ? <><FeedTab s={info.settings} set={set} /><AlertsCard /></> : <div className="sk" style={{ height: 200 }} />)}
          {tab === 'models' && (info ? <ModelsTab s={info.settings} set={set} info={info} /> : <div className="sk" style={{ height: 200 }} />)}
          {tab === 'sources' && <SourcesTab />}
          {tab === 'data' && <DataTab toast={toast} />}
          {tab === 'activity' && <Activity />}
        </div>
      </div>
    </div>
  )
}
