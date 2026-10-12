// ContextLens service worker.
//  - navigations + index.html: NETWORK first (so updates show immediately), cached copy only when offline
//  - /assets/* (content-hashed): cache first
//  - GET /api/feed, /api/stories/*, /api/saved, /api/following: network first, last good response when offline
//    (this is what makes stories you've opened readable offline). Live streams are never cached.
const VERSION = 'v4'
const SHELL = `cl-shell-${VERSION}`
const DATA = `cl-data-${VERSION}`

self.addEventListener('install', () => { self.skipWaiting() })
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => ![SHELL, DATA].includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

const put = (name, req, res) => { if (res && res.ok) { const c = res.clone(); caches.open(name).then((cache) => cache.put(req, c)) } return res }

self.addEventListener('fetch', (e) => {
  const req = e.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  if (url.origin !== location.origin) return

  if (url.pathname.startsWith('/api/')) {
    if (url.pathname.includes('/stream') || url.pathname.endsWith('/chat') || url.pathname === '/api/ask') return
    const cacheable = /^\/api\/(feed|stories\/|saved|following|topics|profile)/.test(url.pathname)
    e.respondWith(
      fetch(req).then((res) => (cacheable ? put(DATA, req, res) : res))
        .catch(() => caches.match(req).then((m) => m || new Response(JSON.stringify({ error: 'You are offline.' }), { status: 503, headers: { 'Content-Type': 'application/json' } }))),
    )
    return
  }

  if (url.pathname.startsWith('/assets/')) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => put(SHELL, req, res))))
    return
  }

  // navigations and everything else: network first
  e.respondWith(fetch(req).then((res) => put(SHELL, req, res)).catch(() => caches.match(req).then((m) => m || caches.match('/'))))
})
