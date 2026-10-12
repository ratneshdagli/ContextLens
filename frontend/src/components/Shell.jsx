import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { rememberChannels } from '../lib/format'
import { useHealth, useModelStatus, useRun, useToast } from '../lib/hooks'
import { useListen } from '../lib/speech'
import ListenBar from './ListenBar'
import Palette from './Palette'
import { useAlerts } from './Alerts'
import { Atlas, Bookmark, Compass, Flow, Flame, Gear, Globe, List, Logo, Mic, Moon, Paper, Planet, Refresh, Repo, Search, Sun, Today } from './Icons'

const Ctx = createContext(null)
export const useApp = () => useContext(Ctx)

const NAV = [
  { group: 'Read' },
  { to: '/', label: 'Today', icon: Today, end: true },
  { to: '/live', label: 'Talk to Lens', icon: Mic, live: true },
  { to: '/flow', label: 'Flow', icon: Flow },
  { to: '/shorts', label: 'In short', icon: Flame },
  { to: '/all', label: 'Everything', icon: List },
  { group: 'Explore' },
  { to: '/explore', label: 'Discover', icon: Compass },
  { to: '/papers', label: 'Newspapers', icon: Paper },
  { to: '/sources', label: 'Sources', icon: Globe },
  { to: '/atlas', label: 'Atlas', icon: Atlas },
  { to: '/globe', label: 'Globe', icon: Planet },
  { group: 'Yours' },
  { to: '/channels', label: 'My channels', icon: Compass },
  { to: '/saved', label: 'Saved', icon: Bookmark },
  { to: '/digest', label: 'Your Digest', icon: Repo },
  { to: '/settings', label: 'Settings', icon: Gear },
]
const byTo = (to) => NAV.find((n) => n.to === to)
const TABS = [byTo('/'), byTo('/flow'), byTo('/live'), byTo('/all'), byTo('/saved')]

export default function Shell() {
  const health = useHealth()
  const models = useModelStatus()
  const [feedRun, setFeedRun] = useState(null)
  const [refreshTick, setRefreshTick] = useState(0)
  const [toast, showToast] = useToast()
  const [theme, setTheme] = useState(() => localStorage.getItem('cl_theme') || '')
  const nav = useNavigate()
  useAlerts(useCallback((id) => nav(`/story/${id}`), [nav]))
  const [palette, setPalette] = useState(false)

  useEffect(() => {                                            // Ctrl/⌘+K anywhere
    const h = (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setPalette((p) => !p) } }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  useEffect(() => { if (health?.feed_running && !feedRun) setFeedRun(health.feed_running) }, [health, feedRun])
  // the reader's channel names label every story card on every page, so they load once here (pages render after, to avoid a flash of "ch:…")
  const [chReady, setChReady] = useState(false)
  useEffect(() => { api.channels().then(rememberChannels).catch(() => {}).finally(() => setChReady(true)) }, [refreshTick])

  const run = useRun(feedRun, {
    onDone: () => { setRefreshTick((t) => t + 1); setTimeout(() => setFeedRun(null), 1500) },
  })
  useEffect(() => {
    if (['wire', 'publish', 'score'].includes(run.last?.stage)) setRefreshTick((t) => t + 1)
  }, [run.last])

  const refresh = useCallback(async () => {
    try {
      const r = await api.refresh()
      setFeedRun(r.run_id)
      if (r.already_running) showToast('A refresh is already running')
    } catch (e) { showToast(e.message) }
  }, [showToast])

  const refreshChannel = useCallback(async (id) => {           // fetch only one channel's sources (a fast first fill)
    try {
      const r = await api.refreshChannel(id)
      if (!r.run_id) { showToast('Could not start the refresh — try again'); return }
      setFeedRun(r.run_id)
      if (r.already_running) showToast('A refresh is already running — run this channel again when it finishes')
    } catch (e) { showToast(e.message) }
  }, [showToast])

  const isDark = () => document.documentElement.dataset.theme === 'dark' ||
    (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches)
  const toggleTheme = () => {
    const next = isDark() ? 'light' : 'dark'
    document.documentElement.dataset.theme = next
    localStorage.setItem('cl_theme', next)
    setTheme(next)
  }

  const listen = useListen()
  const running = !!feedRun && run.status === 'running'
  const llmOk = health?.llm?.available
  const chat = (models?.loaded || []).find((m) => m.type === 'llm')
  const value = { health, run, running, refresh, refreshChannel, refreshTick, feedRun, toast: showToast, nav, listen, models }
  const dark = (theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark'

  return (
    <Ctx.Provider value={value}>
      <div className="shell">
        <aside className="rail">
          <div className="brand"><span className="brand-mark"><Logo /></span>ContextLens</div>
          <button className="rail-search" onClick={() => setPalette(true)} aria-label="Search or jump to (Ctrl K)"><Search size={15} /> Search or ask… <kbd>Ctrl K</kbd></button>
          {NAV.map((n, i) => n.group
            ? <div key={i} className="rail-label" style={i ? { marginTop: 10 } : undefined}>{n.group}</div>
            : (
              <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => 'nav-link' + (isActive ? ' active' : '') + (n.live ? ' live' : '')}>
                <n.icon /> {n.label}{n.live && <span className="live-dot" aria-hidden="true" />}
              </NavLink>
            ))}
          <div className="rail-foot">
            <div className="status-pill" title={health?.llm?.error || (chat ? `${chat.id} is loaded in LM Studio (only one chat model is kept loaded)` : 'No chat model is loaded right now; it loads on demand.')}>
              <span className={'dot ' + (running ? 'busy' : llmOk ? 'on' : health ? 'off' : '')} />
              <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {running ? (run.last?.message?.slice(0, 34) || 'Updating…') : !llmOk && health ? 'Model server offline' : chat ? chat.id.split('/').pop() : llmOk ? 'Models idle' : 'Connecting…'}
              </span>
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              <button className="btn sm" style={{ flex: 1 }} onClick={refresh} disabled={running || !llmOk}>
                <Refresh size={15} /> {running ? 'Updating…' : 'Refresh'}
              </button>
              <button className="icon-btn" onClick={toggleTheme} aria-label="Toggle theme" title="Toggle theme">{dark ? <Sun /> : <Moon />}</button>
            </div>
          </div>
        </aside>
        <main className="main">
          <div className="mobile-top">
            <div className="brand" style={{ padding: 0 }}><span className="brand-mark"><Logo /></span>ContextLens</div>
            <div style={{ display: 'flex', gap: 6 }}>
              <button className="icon-btn" onClick={refresh} disabled={running || !llmOk} aria-label="Refresh"><Refresh /></button>
              <button className="icon-btn" onClick={() => setPalette(true)} aria-label="Search"><Search /></button>
              <NavLink className="icon-btn" to="/globe" aria-label="Globe"><Planet /></NavLink>
              <NavLink className="icon-btn" to="/explore" aria-label="Discover"><Compass /></NavLink>
              <NavLink className="icon-btn" to="/settings" aria-label="Settings"><Gear /></NavLink>
              <button className="icon-btn" onClick={toggleTheme} aria-label="Toggle theme">{dark ? <Sun /> : <Moon />}</button>
            </div>
          </div>
          {chReady && <Outlet />}
        </main>
        <nav className="tabbar">
          {TABS.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} className={({ isActive }) => (isActive ? 'active' : '')}><Icon size={21} />{label}</NavLink>
          ))}
        </nav>
        <ListenBar listen={listen} />
        {palette && <Palette onClose={() => setPalette(false)} />}
        {toast && <div className="toast" role="status">{toast}</div>}
      </div>
    </Ctx.Provider>
  )
}
