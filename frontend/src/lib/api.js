// ContextLens v4 — API client

async function request(path, opts = {}) {
  const res = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const d = body.detail
    throw new Error((Array.isArray(d) ? d.map((x) => x.msg).join('; ') : d) || body.error || `Request failed (${res.status})`)   // 422 detail is a list
  }
  return res.json()
}
const post = (path, body) => request(path, { method: 'POST', body: JSON.stringify(body ?? {}) })
const patch = (path, body) => request(path, { method: 'PATCH', body: JSON.stringify(body ?? {}) })
const q = encodeURIComponent

export const api = {
  health: () => request('/health'),
  feed: (topic = 'all', calm = false) => request(`/feed?topic=${q(topic)}${calm ? '&calm=true' : ''}`),
  refresh: () => post('/feed/refresh'),
  runs: () => request('/runs'),
  run: (id) => request(`/runs/${id}`),
  story: (id) => request(`/stories/${id}`),
  build: (id, force = false) => post(`/stories/${id}/build${force ? '?force=true' : ''}`),
  dive: (id, kind, arg = '') => post(`/stories/${id}/dive`, { kind, arg }),
  diveGet: (id, kind, arg = '') => request(`/stories/${id}/dive?kind=${q(kind)}&arg=${q(arg)}`),
  inspect: (body) => post('/inspect', body),
  feedback: (id, action, dwell_ms) => post(`/stories/${id}/feedback`, { action, dwell_ms }),
  save: (id) => post(`/stories/${id}/save`),
  unsave: (id) => request(`/stories/${id}/save`, { method: 'DELETE' }),
  saved: () => request('/saved'),
  search: (text) => request(`/search?q=${q(text)}`),
  chatHistory: (id) => request(`/stories/${id}/chat`),
  follow: (id) => post(`/stories/${id}/follow`),
  unfollow: (id) => request(`/stories/${id}/follow`, { method: 'DELETE' }),
  following: () => request('/following'),
  seen: (id) => post(`/following/${id}/seen`),
  explain: (id) => post(`/stories/${id}/explain`, { level: 'simple' }),
  research: (query, mode = 'research') => post('/research', { query, mode }),
  researchList: () => request('/research'),
  atlas: (days = 21) => request(`/atlas?days=${days}`),
  entity: (id) => request(`/entities/${id}`),
  followEntity: (id) => post(`/entities/${id}/follow`),
  unfollowEntity: (id) => request(`/entities/${id}/follow`, { method: 'DELETE' }),
  papers: () => request('/papers'),
  paper: (id) => request(`/papers/${id}`),
  deletePaper: (id) => request(`/papers/${id}`, { method: 'DELETE' }),
  openArticle: (pid, aid) => post(`/papers/${pid}/articles/${aid}/open`),
  explainLink: (body) => post('/explain', body),
  topics: () => request('/topics'),
  profile: () => request('/profile'),
  saveProfile: (p) => patch('/profile', p),
  onboard: (b) => post('/onboarding', b),
  onboardingStatus: () => request('/onboarding/status'),
  resetTaste: () => post('/profile/reset-taste'),
  settings: () => request('/settings'),
  saveSettings: (s) => patch('/settings', s),
  testModel: (role) => post('/settings/test', { role }),
  modelsStatus: () => request('/models/status'),
  unloadModels: () => post('/models/unload'),
  sources: () => request('/sources'),
  sourcesOverview: () => request('/sources/overview'),
  voiceSuggest: () => request('/voice/suggest'),
  feedAll: ({ topic = 'all', q: text = '', source = '', factcheck = false, offset = 0, limit = 40 } = {}) =>
    request(`/feed/all?topic=${q(topic)}&q=${q(text)}&source=${q(source)}&factcheck=${factcheck}&offset=${offset}&limit=${limit}`),
  channels: () => request('/channels'),
  globe: (qs, signal) => request('/globe?' + qs, { signal }),
  globeNear: (qs, signal) => request('/globe/near?' + qs, { signal }),
  globeLive: (qs, signal) => request('/globe/live?' + qs, { signal }),
  globeArea: (qs, signal) => request('/globe/area?' + qs, { signal }),
  globePlaces: (places, signal) => request('/globe/places', { method: 'POST', body: JSON.stringify({ places }), signal }),
  globeSearch:(text, signal) => request('/globe/search?q=' + q(text), { signal }),
  globeStats: () => request('/globe/stats'),
  local: () => request('/local'),
  localSearch: (text) => request('/local/search?q=' + q(text)),
  setHome: (b) => request('/local', { method: 'PUT', body: JSON.stringify(b) }),
  blindspots: () => request('/blindspots'),
  shorts: (topic = '') => request('/shorts?topic=' + q(topic)),
  translate: (id, lang) => post(`/translate/${id}`, { lang }),
  alerts: (since, minSignal) => request(`/alerts?since=${q(since)}&min_signal=${minSignal}`),
  createChannel: (b) => post('/channels', b),
  saveChannel: (id, b) => patch(`/channels/${id}`, b),
  deleteChannel: (id) => request(`/channels/${id}`, { method: 'DELETE' }),
  reorderChannels: (ids) => post('/channels/reorder', { ids }),
  discoverChannel: (id) => post(`/channels/${id}/discover`),
  addChannelFeed: (id, url) => post(`/channels/${id}/feeds`, { url }),
  toggleChannelFeed: (id, url, off) => patch(`/channels/${id}/feeds`, { url, off }),
  removeChannelFeed: (id, url) => request(`/channels/${id}/feeds?url=${q(url)}`, { method: 'DELETE' }),
  refreshChannel: (id) => post(`/channels/${id}/refresh`),
  clearData: () => post('/data/clear-stories'),
  exportData: () => request('/data/export'),
  digest: () => request('/digest'),
  addRepoWatch: (repo) => post('/digest/repo', { repo }),
  addTopicWatch: (topic) => post('/digest/topic', { topic }),
  refreshWatch: (id) => post(`/digest/${id}/refresh`),
  removeWatch: (id) => request(`/digest/${id}`, { method: 'DELETE' }),
}

/** Upload a document (PDF or text) to pull topics from and track related news. Returns the new watch. */
export async function uploadDocWatch(file) {
  const fd = new FormData()
  fd.append('file', file)
  const res = await fetch('/api/digest/doc', { method: 'POST', body: fd })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || body.error || `Upload failed (${res.status})`)
  }
  return res.json()
}

/** Upload a newspaper PDF (multipart). Returns {paper_id, run_id}. */
export async function uploadPaper(file, explainTop = 5) {
  const fd = new FormData()
  fd.append('file', file)
  const res = await fetch(`/api/papers?explain_top=${explainTop}`, { method: 'POST', body: fd })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || body.error || `Upload failed (${res.status})`)
  }
  return res.json()
}

/** Subscribe to a run's progress over SSE. Returns an unsubscribe fn. */
export function streamRun(runId, onEvent, onDone) {
  const es = new EventSource(`/api/runs/${runId}/stream`)
  es.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data)
      if (d.__done__) { es.close(); onDone?.(d.status) } else onEvent?.(d)
    } catch { /* ignore malformed */ }
  }
  es.onerror = () => { es.close(); onDone?.('error') }
  return () => es.close()
}

async function readSSE(res, onEvent) {
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    const parts = buf.split('\n\n')
    buf = parts.pop() || ''
    for (const p of parts) {
      const line = p.replace(/^data: /, '').trim()
      if (!line) continue
      let ev
      try { ev = JSON.parse(line) } catch { continue }
      onEvent(ev)
    }
  }
}

/** POST chat and stream text chunks. */
export async function streamChat(storyId, message, onChunk, signal) {
  const res = await fetch(`/api/stories/${storyId}/chat`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message }), signal,
  })
  if (!res.ok || !res.body) throw new Error('Chat request failed')
  await readSSE(res, (ev) => {
    if (ev.t === 'chunk') onChunk(ev.c)
    if (ev.t === 'error') throw new Error(ev.c)
  })
}

/** Ask across the whole briefing. onMeta receives the numbered source stories. */
export async function streamAsk(question, onMeta, onChunk, signal) {
  const res = await fetch('/api/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question }), signal })
  if (!res.ok || !res.body) throw new Error('Request failed')
  await readSSE(res, (ev) => {
    if (ev.t === 'meta') onMeta(ev.sources)
    if (ev.t === 'chunk') onChunk(ev.c)
    if (ev.t === 'error') throw new Error(ev.c)
  })
}
