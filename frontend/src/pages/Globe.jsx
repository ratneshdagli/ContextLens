import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import * as maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'      // bundled worker asset (the default sibling-file lookup 404s once built)
import '../globe.css'
import { api } from '../lib/api'
import { timeAgo, topicLabel, TRUST } from '../lib/format'
import { Close, External, Planet, Search } from '../components/Icons'

// ContextLens v7 — Globe: a draggable 3D earth (MapLibre, OpenFreeMap tiles) with live news dots.
// All map state lives in refs; React state only changes on data loads and clicks, never per frame.

maplibregl.setWorkerUrl(workerUrl)

const STYLE = 'https://tiles.openfreemap.org/styles/liberty'
const PALETTE = {
  world: '#4f8cff', politics: '#e0506a', business: '#f0a030', markets: '#2bb673', tech: '#9b6bff',
  science: '#22c5d6', health: '#ff7a9a', india: '#ff8a3d', sports: '#8bd233', culture: '#d66bd0',
}
const CHANNEL_COLOR = '#e8704a'
const color = (t) => PALETTE[t] || CHANNEL_COLOR
const TOPIC_MATCH = ['match', ['get', 'topic'], ...Object.entries(PALETTE).flat(), CHANNEL_COLOR]
const HOURS = [[6, '6 h'], [24, '24 h'], [168, '7 d']]
const HOME = { center: [15, 20], zoom: 1.6 }
const PANEL_PAD = 392                          // px the open side panel covers, so the globe centres in what is left
const POLL_MS = 120000
const FRESH_MS = 3 * 3600e3
const EMPTY = { type: 'FeatureCollection', features: [] }
const ALL_LAYERS = ['news-story', 'news-headline', 'news-cluster']
const AREA_MIN_ZOOM = 4                        // below this the whole-world dataset is enough
const PLACES_MIN_ZOOM = 10                     // from here on the map's own place labels name the places to look up
const PLACES_MAX = 40
const LIVE_MS = 30000                          // re-run the collection while the user stays in a view
const AREA_POLL_MS = 2500
const AREA_MAX_MS = 25000
const POP_MS = 1400                            // new dots ping once, then settle

const sleep = (ms, sig) => new Promise((res, rej) => {
  const t = setTimeout(res, ms)
  sig.addEventListener('abort', () => { clearTimeout(t); rej(new DOMException('Aborted', 'AbortError')) }, { once: true })
})
const bboxOf = (map) => {
  const [[w, s], [e, n]] = map.getBounds().toArray()
  const c = (v, lo, hi) => Math.min(hi, Math.max(lo, v)).toFixed(3)
  return `${c(w, -180, 180)},${c(s, -90, 90)},${c(e, -180, 180)},${c(n, -90, 90)}`
}

const isDark = () => document.documentElement.dataset.theme === 'dark' ||
  (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches)
const kmFor = (z) => Math.round(600 * Math.pow(8 / 600, Math.min(1, Math.max(0, (z - 4) / 10))))   // 600 km at z4 -> 8 km at z14
const safeUrl = (u) => (/^https?:\/\//i.test(u || '') ? u : null)
const where = (p) => [p.place, p.country && p.country !== p.place ? p.country : ''].filter(Boolean).join(', ')

// ── data prep: stamp every feature with what the layers need (key, weight, freshness, shared-coordinate key) ──
function prepare(fc) {
  const byCoord = new Map(), byKey = new Map(), top = []
  let stories = 0, headlines = 0, fresh = 0
  for (const f of fc.features || []) {
    const p = f.properties || (f.properties = {})
    const [lng, lat] = f.geometry.coordinates
    p.k = `${p.kind}:${p.id}`
    p.lng = lng; p.lat = lat
    p.ck = `${lng.toFixed(4)},${lat.toFixed(4)}`
    const isStory = p.kind === 'story'
    p.w = isStory ? 1 + 3 * (p.rank || 0) : 0.3
    p.fresh = isStory && Date.now() - new Date(p.t).getTime() < FRESH_MS ? 1 : 0
    fresh += p.fresh
    if (isStory) { stories++; top.push(p) } else headlines++
    byKey.set(p.k, p)
    if (!byCoord.has(p.ck)) byCoord.set(p.ck, [])
    byCoord.get(p.ck).push(p)
  }
  for (const g of byCoord.values()) g.sort((a, b) => (b.kind === 'story') - (a.kind === 'story') || (b.rank || 0) - (a.rank || 0))
  top.sort((a, b) => (b.rank || 0) - (a.rank || 0))
  return { fc, byCoord, byKey, top: top.slice(0, 12), stories, headlines, fresh }
}

// ── dark look: Liberty has no dark variant, so recolour its layers once and remember the originals to switch back ──
function darkPaints(layers) {
  const out = []
  for (const l of layers) {
    const id = l.id, t = l.type
    if (t === 'background') out.push([id, 'background-color', '#0a1119'])
    else if (id === 'natural_earth') out.push([id, 'raster-brightness-max', 0.42], [id, 'raster-saturation', -0.25])
    else if (id === 'water') out.push([id, 'fill-color', '#0c2236'])
    else if (/^waterway/.test(id) && t === 'line') out.push([id, 'line-color', '#123049'])
    else if (id === 'park') out.push([id, 'fill-color', '#10241b'], [id, 'fill-outline-color', '#1b3a2c'])
    else if (id === 'landcover_wood') out.push([id, 'fill-color', '#112419'])
    else if (id === 'landcover_grass') out.push([id, 'fill-color', '#14271c'])
    else if (id === 'landcover_ice') out.push([id, 'fill-color', '#2b3845'])
    else if (id === 'landcover_sand') out.push([id, 'fill-color', '#2a2a22'])
    else if (/^landuse_(residential|school|hospital|cemetery|pitch|track)/.test(id)) out.push([id, 'fill-color', '#141b24'])
    else if (id === 'aeroway_fill') out.push([id, 'fill-color', '#18202a'])
    else if (/^aeroway_(runway|taxiway)/.test(id)) out.push([id, 'line-color', '#2a3441'])
    else if (id === 'building') out.push([id, 'fill-color', '#18202a'], [id, 'fill-outline-color', '#222b37'])
    else if (id === 'building-3d') out.push([id, 'fill-extrusion-color', ['interpolate', ['linear'], ['get', 'render_height'], 0, '#222b38', 150, '#3b4a60']], [id, 'fill-extrusion-opacity', 0.92])
    else if (/^boundary/.test(id)) out.push([id, 'line-color', '#56627a'])
    else if (t === 'line' && /^(road|bridge|tunnel)_/.test(id) && !/rail|arrow|pattern/.test(id)) {
      out.push([id, 'line-color', /casing/.test(id) ? '#090e14' : /motorway|trunk/.test(id) ? '#7b8ba3' : /secondary|primary/.test(id) ? '#56647a' : '#3a4557'])
    } else if (t === 'symbol' && l.layout?.['text-field']) out.push([id, 'text-color', '#c9d1dc'], [id, 'text-halo-color', '#0b1118'])
  }
  return out
}
// light mode keeps Liberty's colours but gets the same height-graded buildings
const LIGHT_EXTRA = [
  ['building-3d', 'fill-extrusion-color', ['interpolate', ['linear'], ['get', 'render_height'], 0, 'hsl(35,10%,90%)', 150, 'hsl(215,14%,72%)']],
  ['building-3d', 'fill-extrusion-opacity', 0.9],
]
const SKY = (dark) => ({
  'sky-color': dark ? '#0a1a33' : '#78afff',
  'horizon-color': dark ? '#3f78c4' : '#cfe3ff',
  'fog-color': dark ? '#0c1626' : '#e4edf8',
  'sky-horizon-blend': 0.5, 'horizon-fog-blend': 0.5, 'fog-ground-blend': 0.5,
  'atmosphere-blend': ['interpolate', ['linear'], ['zoom'], 0, 1, 5, 1, 7, 0],
})

// teardrop map pin, drawn once per topic colour: soft drop shadow, light top sheen, white inner dot. Tip sits 5 px above the bottom edge.
function drawPin(hex) {
  const W = 34, H = 46, cx = W / 2, r = 11, cy = 17, tip = H - 5, k = 2
  const cv = document.createElement('canvas'); cv.width = W * k; cv.height = H * k
  const g = cv.getContext('2d'); g.scale(k, k)
  const path = () => {
    g.beginPath(); g.moveTo(cx, tip)
    g.bezierCurveTo(cx - 4, tip - 8, cx - r, cy + r * 0.55, cx - r, cy)
    g.arc(cx, cy, r, Math.PI, 0)
    g.bezierCurveTo(cx + r, cy + r * 0.55, cx + 4, tip - 8, cx, tip)
    g.closePath()
  }
  path(); g.shadowColor = 'rgba(0,0,0,.5)'; g.shadowBlur = 5; g.shadowOffsetY = 2; g.fillStyle = hex; g.fill()
  g.shadowColor = 'transparent'
  path(); const sheen = g.createLinearGradient(0, cy - r, 0, tip); sheen.addColorStop(0, 'rgba(255,255,255,.32)'); sheen.addColorStop(0.6, 'rgba(255,255,255,0)'); sheen.addColorStop(1, 'rgba(0,0,0,.14)')
  g.fillStyle = sheen; g.fill()
  g.lineWidth = 1.4; g.strokeStyle = 'rgba(255,255,255,.95)'; path(); g.stroke()
  g.beginPath(); g.arc(cx, cy, 4.2, 0, Math.PI * 2); g.fillStyle = '#fff'; g.fill()
  return g.getImageData(0, 0, W * k, H * k)
}
const PIN_IMAGE = ['match', ['get', 'topic'], ...Object.keys(PALETTE).flatMap((t) => [t, 'pin-' + t]), 'pin-ch']

function addNewsLayers(map) {
  for (const [t, c] of [...Object.entries(PALETTE), ['ch', CHANNEL_COLOR]]) if (!map.hasImage('pin-' + t)) map.addImage('pin-' + t, drawPin(c), { pixelRatio: 2 })
  map.addSource('news', {
    type: 'geojson', data: EMPTY, cluster: true, clusterRadius: 45, clusterMaxZoom: 9,
    clusterProperties: { w: ['+', ['get', 'w']], stories: ['+', ['case', ['==', ['get', 'kind'], 'story'], 1, 0]] },
  })
  const unclustered = ['!', ['has', 'point_count']]
  const isStory = ['==', ['get', 'kind'], 'story']
  const cr = ['interpolate', ['linear'], ['get', 'point_count'], 2, 11, 10, 14, 50, 18, 200, 23, 1000, 28]
  const rk = ['coalesce', ['get', 'rank'], 0]
  const psz = (m) => ['interpolate', ['linear'], ['zoom'], 1, ['interpolate', ['linear'], rk, 0, 0.62 * m, 1, 0.82 * m], 12, ['interpolate', ['linear'], rk, 0, 0.85 * m, 1, 1.15 * m]]
  const L = (id, type, filter, paint, layout) => map.addLayer({ id, type, source: 'news', filter, paint, ...(layout ? { layout } : {}) })

  const flat = { 'circle-pitch-alignment': 'map', 'circle-pitch-scale': 'map' }      // rings lie on the ground when the map tilts
  const isHead = ['all', unclustered, ['==', ['get', 'kind'], 'headline']]
  const pin = (id, filter, scale) => map.addLayer({
    id, type: 'symbol', source: 'news', filter,
    layout: {
      'icon-image': PIN_IMAGE, 'icon-anchor': 'bottom', 'icon-offset': [0, 5], 'icon-allow-overlap': true, 'icon-ignore-placement': true,
      'icon-size': psz(scale), 'symbol-sort-key': rk,
    },
  })

  // headlines: small white dots with a topic-coloured ring and a dark halo; always beneath the pins
  L('news-headline-glow', 'circle', isHead, { 'circle-color': '#000', 'circle-opacity': 0.4, 'circle-blur': 0.6, 'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, 5, 12, 9.5] })
  L('news-headline', 'circle', isHead, {
    'circle-color': '#fff', 'circle-stroke-width': 2, 'circle-stroke-color': TOPIC_MATCH,       // white core + topic ring: stands out on dark and light maps
    'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, 2.6, 12, 4.8],
  })
  // clusters: small solid dark disc, one hairline light ring, crisp white count (no glow, no colour)
  L('news-cluster', 'circle', ['has', 'point_count'], { 'circle-color': '#12151c', 'circle-opacity': 0.92, 'circle-radius': cr, 'circle-stroke-width': 1, 'circle-stroke-color': 'rgba(226,230,238,.7)', 'circle-stroke-opacity': 1 })
  L('news-cluster-count', 'symbol', ['has', 'point_count'], { 'text-color': '#fff' },
    { 'text-field': ['get', 'point_count_abbreviated'], 'text-font': ['Noto Sans Bold'], 'text-size': ['interpolate', ['linear'], ['get', 'point_count'], 2, 11, 200, 13], 'text-allow-overlap': true, 'text-ignore-placement': true })
  // fresh stories ping on the ground; brand-new dots from an area load ping once
  L('news-pulse', 'circle', ['all', unclustered, isStory, ['==', ['get', 'fresh'], 1]], {
    'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': TOPIC_MATCH, 'circle-stroke-width': 1.5, 'circle-radius': 12, 'circle-stroke-opacity': 0.4, ...flat,
  })
  L('news-pop', 'circle', ['all', unclustered, ['==', ['get', 'nw'], 1]], {
    'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': TOPIC_MATCH, 'circle-stroke-width': 2, 'circle-radius': 3, 'circle-stroke-opacity': 0, ...flat,
  })
  pin('news-story', ['all', unclustered, isStory], 1)
  L('news-sel-dot', 'circle', ['in', ['get', 'k'], ['literal', ['-']]], { 'circle-color': 'rgba(0,0,0,0)', 'circle-radius': 9, 'circle-stroke-width': 2.5, 'circle-stroke-color': '#fff', 'circle-stroke-opacity': 0.95 })
  pin('news-sel', ['in', ['get', 'k'], ['literal', ['-']]], 1.3)
}

function Ago({ t }) {
  const [, tick] = useState(0)
  useEffect(() => { const i = setInterval(() => tick((n) => n + 1), 5000); return () => clearInterval(i) }, [])
  if (!t) return null
  const s = Math.max(0, Math.round((Date.now() - t) / 1000))
  return <>{s < 5 ? 'just now' : s < 60 ? `${s}s ago` : `${Math.round(s / 60)} min ago`}</>
}

function Meta({ p }) {
  return (
    <span className="gl-meta">
      {where(p)}{p.precision === 'country' ? ' (country)' : ''} · {timeAgo(p.t)}
      {p.kind === 'story' ? ` · ${p.outlets} outlet${p.outlets === 1 ? '' : 's'}` : p.outlet ? ` · ${p.outlet}` : ''}
    </span>
  )
}

function Row({ p, onPick, active }) {
  return (
    <li>
      <button className={'gl-row' + (active ? ' on' : '')} onClick={() => onPick(p)}>
        <span className={'gl-pip' + (p.kind === 'headline' ? ' sm' : '')} style={{ background: color(p.topic) }} aria-hidden="true" />
        <span className="gl-row-body"><span className="gl-row-t">{p.title}</span><Meta p={p} /></span>
      </button>
    </li>
  )
}

function Detail({ p }) {
  const url = safeUrl(p.url)
  return (
    <article className="gl-card">
      <div className="gl-chips">
        <span className="chip"><span className="gl-pip sm" style={{ background: color(p.topic) }} aria-hidden="true" />{topicLabel(p.topic)}</span>
        <span className="chip outline">{where(p) || 'Unknown place'} · {p.precision === 'country' ? 'country-level' : 'city'}</span>
      </div>
      <h3 className="gl-card-t">{p.title}</h3>
      <div className="gl-meta">
        {timeAgo(p.t)}{p.kind === 'story' && ` · ${p.outlets} outlet${p.outlets === 1 ? '' : 's'}`}{p.tier ? ` · ${TRUST[p.tier] || ''}` : ''}
      </div>
      {p.kind === 'story'
        ? <Link className="btn primary sm" to={`/story/${p.id}`}>Read the story</Link>
        : <div className="gl-card-src"><span>{p.outlet || 'Headline'}</span>{url && <a className="btn sm" href={url} target="_blank" rel="noopener noreferrer">Open article <External size={14} /></a>}</div>}
    </article>
  )
}

const Skeleton = () => <div className="gl-skels" aria-hidden="true">{[0, 1, 2, 3].map((i) => <div key={i} className="gl-skel" />)}</div>

export default function Globe() {
  const boxRef = useRef(null)
  const mapRef = useRef(null)
  const dataRef = useRef(null)                    // prepared data; the source is fed from here once the map is ready
  const readyRef = useRef(false)
  const rotRef = useRef(true)
  const freshRef = useRef(0)
  const pushRef = useRef(null)
  const featsRef = useRef(new Map())              // every feature loaded so far, keyed by kind:id, so zooming in only ever adds dots
  const areaSeenRef = useRef(new Set())           // grid cells already sent to /api/globe/area this session
  const qsRef = useRef('')
  const popRef = useRef(0)                        // start time of the "new dots" ping, 0 = idle
  const popTimer = useRef(0)
  const reduced = useRef(matchMedia('(prefers-reduced-motion: reduce)').matches)
  const phone = useRef(matchMedia('(max-width: 900px)').matches)

  const [noGl, setNoGl] = useState(false)
  const [mapErr, setMapErr] = useState('')
  const [ready, setReady] = useState(false)
  const [rotate, setRotate] = useState(!reduced.current)
  const [open, setOpen] = useState(!phone.current)
  const [tab, setTab] = useState('here')
  const [filtersOpen, setFiltersOpen] = useState(() => !matchMedia('(max-width: 1100px)').matches)
  const [area, setArea] = useState(null)          // {busy, n}: status chip for the area collection
  const [hours, setHours] = useState(24)
  const [topic, setTopic] = useState('')
  const [channel, setChannel] = useState('')
  const [channels, setChannels] = useState([])
  const [heads, setHeads] = useState(true)
  const [counts, setCounts] = useState(null)
  const [top, setTop] = useState([])
  const [updated, setUpdated] = useState(0)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState([])
  const [near, setNear] = useState(null)
  const [nearBusy, setNearBusy] = useState(false)
  const [nearErr, setNearErr] = useState(false)
  const [live, setLive] = useState(null)
  const [zoomed, setZoomed] = useState(false)     // zoom >= 4: the "Here" tab has a place to talk about
  const [geoMsg, setGeoMsg] = useState('')
  const [q, setQ] = useState('')
  const [hits, setHits] = useState([])
  const [ddOpen, setDdOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const dragY = useRef(null)

  const stopRotate = useCallback(() => { if (rotRef.current) { rotRef.current = false; setRotate(false) } }, [])
  const fly = useCallback((lng, lat, zoom, extra) => {
    const m = mapRef.current
    if (!m) return
    stopRotate()
    m.flyTo({ center: [lng, lat], zoom, duration: reduced.current ? 0 : 2200, essential: true, ...extra })
  }, [stopRotate])

  // merge a FeatureCollection into the persistent set and feed the map; returns how many features were new
  const ingest = useCallback((fc, mark) => {
    const all = featsRef.current
    let added = 0
    for (const f of fc.features || []) {
      const p = f.properties || (f.properties = {}), k = `${p.kind}:${p.id}`
      if (!all.has(k)) { added++; if (mark) p.nw = 1 } else if (all.get(k).properties.nw) p.nw = 1
      all.set(k, f)
    }
    const d = prepare({ type: 'FeatureCollection', features: [...all.values()] })
    dataRef.current = d; freshRef.current = d.fresh
    pushRef.current?.()
    setCounts({ stories: d.stories, headlines: d.headlines }); setTop(d.top); setUpdated(Date.now()); setErr('')
    const m = mapRef.current
    if (!d.fresh && m?.getLayer('news-pulse')) m.setPaintProperty('news-pulse', 'circle-stroke-opacity', 0)
    if (added && mark && !reduced.current) {
      popRef.current = performance.now()
      clearTimeout(popTimer.current)
      popTimer.current = setTimeout(() => { for (const f of all.values()) f.properties.nw = 0; pushRef.current?.() }, POP_MS + 100)
    }
    return added
  }, [])
  useEffect(() => () => clearTimeout(popTimer.current), [])

  // ── map: created once, torn down on unmount ──
  useEffect(() => {
    const el = boxRef.current
    let map
    try {
      map = new maplibregl.Map({
        container: el, style: STYLE, center: HOME.center, zoom: phone.current ? 0.9 : HOME.zoom, maxPitch: 75, minZoom: 0.6, padding: { right: phone.current ? 0 : PANEL_PAD },
        attributionControl: false, fadeDuration: reduced.current ? 0 : 250, canvasContextAttributes: { antialias: true },
      })
    } catch { setNoGl(true); return }
    mapRef.current = map
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right')
    map.addControl(new maplibregl.AttributionControl({ compact: false }), 'bottom-left')

    const root = el.parentElement
    const tip = document.createElement('div')              // custom card (not a maplibre Popup) so it can sit above the side panel
    tip.className = 'gl-tip'; tip.setAttribute('role', 'tooltip')
    root.appendChild(tip)
    const timers = {}, ctls = {}
    let raf = 0, dead = false, orig = null, dark = []

    const push = () => { if (readyRef.current && dataRef.current) map.getSource('news')?.setData(dataRef.current.fc) }
    pushRef.current = push
    const applyTheme = () => {
      if (!readyRef.current) return
      const d = isDark()
      map.setSky(SKY(d))
      for (const [id, prop, val] of dark) {
        if (!map.getLayer(id)) continue
        const key = id + '|' + prop
        if (d) { if (!orig.has(key)) orig.set(key, map.getPaintProperty(id, prop)); map.setPaintProperty(id, prop, val) }
        else if (orig.has(key)) map.setPaintProperty(id, prop, orig.get(key))
      }
      if (!d) for (const [id, prop, val] of LIGHT_EXTRA) if (map.getLayer(id)) map.setPaintProperty(id, prop, val)
    }

    map.on('style.load', () => {                           // as soon as the style is parsed, so there is no flat-map flash
      map.setProjection({ type: 'globe' })
      orig = new Map()
      dark = darkPaints(map.getStyle().layers)
      addNewsLayers(map)
      readyRef.current = true
      applyTheme()
      push()
      setReady(true)
      setZoomed(map.getZoom() >= 4)
    })
    map.on('error', (e) => { if (!readyRef.current) setMapErr(e?.error?.message || 'The map style could not be loaded.') })

    // hover tooltip + pointer cursor
    const hit = (pt) => map.queryRenderedFeatures([[pt.x - 4, pt.y - 4], [pt.x + 4, pt.y + 4]], { layers: ALL_LAYERS.filter((l) => map.getLayer(l)) })
    const mk = (cls, text, tag = 'div') => { const n = document.createElement(tag); n.className = cls; if (text != null) n.textContent = text; return n }
    let tipKey = '', tipAt = null, tipOff = 0
    const hideTip = () => { tipKey = ''; tip.classList.remove('on') }
    const placeTip = () => {                                // above the marker; flips below / clamps so it never leaves the map or slides under the panel
      if (!tipAt) return
      const r = root.getBoundingClientRect(), pan = root.querySelector('.gl-panel')?.getBoundingClientRect()
      const maxR = (phone.current || !pan ? r.width : Math.min(r.width, pan.left - r.left)) - 8
      const w = tip.offsetWidth, h = tip.offsetHeight, pt = map.project(tipAt)
      const left = Math.max(8, Math.min(pt.x - w / 2, maxR - w)), up = pt.y - tipOff - h - 10 >= 8
      tip.style.left = `${left}px`; tip.style.top = `${up ? pt.y - tipOff - h - 10 : pt.y + 16}px`
      tip.style.setProperty('--ax', `${Math.max(16, Math.min(pt.x - left, w - 16))}px`)
      tip.style.transformOrigin = `${pt.x - left}px ${up ? '100%' : '0'}`
      tip.classList.toggle('below', !up)
    }
    const showTip = (f) => {
      const p = f.properties, key = p.point_count ? `c${p.cluster_id}:${p.point_count}` : p.k
      if (key === tipKey) return
      tipKey = key; tipAt = f.geometry.coordinates
      tip.replaceChildren()
      if (p.point_count) {
        tipOff = 20
        const n = +p.stories
        tip.append(mk('gl-tip-t', `${p.point_count} item${+p.point_count === 1 ? '' : 's'}${n ? `, ${n} stor${n === 1 ? 'y' : 'ies'}` : ''}`), mk('gl-tip-hint', 'Click to zoom in'))
        const id = p.cluster_id
        map.getSource('news').getClusterLeaves(id, 40, 0).then((leaves) => {
          if (tipKey !== key) return
          const best = leaves.map((l) => l.properties).sort((a, b) => (b.kind === 'story') - (a.kind === 'story') || (b.rank || 0) - (a.rank || 0)).slice(0, 3)
          const ul = mk('gl-tip-list', null, 'ul')
          for (const q of best) ul.append(mk('', q.title, 'li'))
          tip.insertBefore(ul, tip.lastChild); placeTip()
        }).catch(() => {})
      } else {
        tipOff = p.kind === 'story' ? 40 + 6 * (p.rank || 0) : 10
        const chip = mk('gl-tip-chip'), dot = mk('gl-pip sm', null, 'i')
        dot.style.background = color(p.topic)
        chip.append(dot, topicLabel(p.topic))
        const meta = [p.kind === 'story' ? `${p.outlets} outlet${p.outlets === 1 ? '' : 's'}` : p.outlet, where(p), timeAgo(p.t)].filter(Boolean).join(' · ')
        tip.append(chip, mk('gl-tip-t', p.title), mk('gl-tip-m', meta), mk('gl-tip-hint', 'Click for details'))
      }
      placeTip(); tip.classList.add('on')
    }
    map.on('mousemove', (e) => {
      if (!readyRef.current) return
      const f = e.originalEvent.buttons ? null : hit(e.point)[0]
      map.getCanvas().style.cursor = f ? 'pointer' : ''
      if (f) showTip(f); else hideTip()
    })
    map.on('mouseout', hideTip)
    map.on('move', () => { if (tipKey) placeTip() })       // follows the marker while the globe spins or flies
    map.on('click', (e) => {
      if (!readyRef.current) return
      const f = hit(e.point)[0]
      if (!f) { setSelected([]); return }
      if (f.properties.point_count) {
        map.getSource('news').getClusterExpansionZoom(f.properties.cluster_id).then((z) => {
          if (!dead) map.easeTo({ center: f.geometry.coordinates, zoom: Math.min(z + 0.5, 16), duration: reduced.current ? 0 : 700 })
        }).catch(() => {})
        return
      }
      const d = dataRef.current
      setSelected(d?.byCoord.get(f.properties.ck) || [f.properties])
      setTab('selected'); setOpen(true)
    })

    // first user input stops the idle spin
    for (const ev of ['mousedown', 'touchstart', 'wheel']) map.on(ev, stopRotate)

    // local news + live headlines follow the map centre (debounced, previous request aborted)
    const refresh = () => {
      const z = map.getZoom(), c = map.getCenter()
      setZoomed(z >= 4)
      ctls.near?.abort(); ctls.live?.abort()
      if (z < 4) { setNear(null); setLive(null); setNearBusy(false); return }
      const ll = `lat=${c.lat.toFixed(4)}&lon=${c.lng.toFixed(4)}`
      ctls.near = new AbortController()
      setNearBusy(true)
      api.globeNear(`${ll}&km=${kmFor(z)}&hours=72&limit=40`, ctls.near.signal)
        .then((r) => { setNear(r); setNearErr(false); setNearBusy(false) })
        .catch((e) => { if (e.name !== 'AbortError') { setNearErr(true); setNearBusy(false) } })
      if (z < 6) { setLive(null); return }
      ctls.live = new AbortController()
      api.globeLive(ll, ctls.live.signal).then(setLive).catch((e) => { if (e.name !== 'AbortError') setLive(null) })
    }
    // the base map's own place labels (suburb, village, town...) in view, nearest the centre first: they name where to look for local news
    let placeLayers = null
    const harvest = () => {
      if (map.getZoom() < PLACES_MIN_ZOOM) return []
      placeLayers ||= map.getStyle().layers.filter((l) => l.type === 'symbol' && l['source-layer'] === 'place').map((l) => l.id)
      const c = map.getCenter(), kx = Math.cos(c.lat * Math.PI / 180), seen = new Map()
      for (const f of map.queryRenderedFeatures({ layers: placeLayers })) {
        const p = f.properties, name = (p['name:latin'] || p.name || '').trim()
        if (name.length < 4 || !/^\p{Lu}/u.test(name) || seen.has(name) || f.geometry.type !== 'Point') continue
        const [lon, lat] = f.geometry.coordinates
        seen.set(name, { name, native: p.name && p.name !== name && /\P{ASCII}/u.test(p.name) ? p.name.trim() : '', lat: +lat.toFixed(4), lon: +lon.toFixed(4), d: (lat - c.lat) ** 2 + ((lon - c.lng) * kx) ** 2 })
      }
      return [...seen.values()].sort((a, b) => a.d - b.d).slice(0, PLACES_MAX).map((p) => ({ name: p.name, lat: p.lat, lon: p.lon, native: p.native }))
    }
    // area collection: ask the backend to gather local news for what is on screen (area + named places), poll, and fold new dots in without touching the view.
    // Runs on every settled move and again every LIVE_MS while the user stays put; a move aborts the run in flight, a timer tick never overlaps one.
    let runId = 0, busy = false, newTotal = 0
    const collect = async (force) => {
      const z = map.getZoom()
      if (z < AREA_MIN_ZOOM) { setArea(null); return }
      if (force && busy) return
      const c = map.getCenter(), zi = Math.floor(z), step = 180 / 2 ** zi
      const key = `${zi}:${Math.floor(c.lng / step)}:${Math.floor(c.lat / step)}`
      const useArea = force || !areaSeenRef.current.has(key)
      areaSeenRef.current.add(key)
      ctls.area?.abort()
      const ac = ctls.area = new AbortController(), id = ++runId, sig = ac.signal
      busy = true
      const bb = bboxOf(map), [w, s, e, n] = bb.split(',')
      const qs = `w=${w}&s=${s}&e=${e}&n=${n}&zoom=${zi}`
      const places = harvest()
      if (!places.length && z >= PLACES_MIN_ZOOM) map.once('idle', () => { if (!dead) collect(false) })     // labels still loading: go again as soon as they are
      const quiet = (p) => p.catch((err) => { if (err.name === 'AbortError') throw err; return null })     // 404 / offline: stay silent
      const calls = async () => {
        const [a, b] = await Promise.all([useArea ? quiet(api.globeArea(qs, sig)) : null, places.length ? quiet(api.globePlaces(places, sig)) : null])
        return (a?.pending || 0) + (b?.pending || 0)
      }
      const pull = async () => { const k = ingest(await api.globe(`${qsRef.current}&bbox=${bb}`, sig), true); newTotal += k; return k }
      try {
        let pend = await calls()
        if (pend > 0) setArea((a) => ({ busy: true, n: newTotal, t: a?.t || 0 }))
        const t0 = Date.now()
        while (pend > 0 && Date.now() - t0 < AREA_MAX_MS) {
          await sleep(AREA_POLL_MS, sig)
          pend = await calls(); await pull()
          if (!dead) setArea({ busy: true, n: newTotal, t: 0 })
        }
        await pull()
        if (!dead) setArea({ busy: false, n: newTotal, t: Date.now() })
      } catch (err) { if (err.name !== 'AbortError' && !dead) setArea(null) }
      finally { if (id === runId) busy = false }
    }
    map.on('moveend', () => { clearTimeout(timers.mv); timers.mv = setTimeout(() => { refresh(); collect(false) }, 600) })
    timers.live = setInterval(() => { if (!document.hidden && readyRef.current && !map.isMoving()) collect(true) }, LIVE_MS)

    // one RAF loop: idle spin below zoom 3 and the pulse ring on fresh stories
    let last = performance.now(), lastPulse = 0
    const tick = (now) => {
      raf = requestAnimationFrame(tick)
      const dt = Math.min(0.1, (now - last) / 1000); last = now
      if (document.hidden || !readyRef.current || reduced.current) return
      if (rotRef.current && map.getZoom() < 3 && !map.isMoving()) {
        const c = map.getCenter()
        map.jumpTo({ center: [c.lng + dt, c.lat] })           // ~1 deg/s
      }
      if (freshRef.current && now - lastPulse > 50) {
        lastPulse = now
        const ph = (now % 2600) / 2600
        map.setPaintProperty('news-pulse', 'circle-radius', 8 + ph * 16)
        map.setPaintProperty('news-pulse', 'circle-stroke-opacity', 0.5 * (1 - ph))
      }
      if (popRef.current) {
        const ph = (now - popRef.current) / POP_MS
        if (ph >= 1) { popRef.current = 0; map.setPaintProperty('news-pop', 'circle-stroke-opacity', 0) }
        else {
          map.setPaintProperty('news-pop', 'circle-radius', 3 + ph * 20)
          map.setPaintProperty('news-pop', 'circle-stroke-opacity', 0.9 * (1 - ph))
        }
      }
    }
    raf = requestAnimationFrame(tick)

    const mo = new MutationObserver(applyTheme)
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    const mq = matchMedia('(prefers-color-scheme: dark)')
    mq.addEventListener('change', applyTheme)

    return () => {
      dead = true
      cancelAnimationFrame(raf)
      Object.values(timers).forEach((t) => { clearTimeout(t); clearInterval(t) })
      Object.values(ctls).forEach((c) => c?.abort())
      mo.disconnect(); mq.removeEventListener('change', applyTheme)
      tip.remove(); map.remove()
      mapRef.current = null; readyRef.current = false
      pushRef.current = null
    }
  }, [stopRotate, ingest])

  useEffect(() => { rotRef.current = rotate }, [rotate])
  useEffect(() => {                                       // keep the globe centred beside the panel
    if (!phone.current) mapRef.current?.easeTo({ padding: { right: open ? PANEL_PAD : 0 }, duration: reduced.current ? 0 : 350 })
  }, [open])
  useEffect(() => { api.channels().then((c) => setChannels(Array.isArray(c) ? c : [])).catch(() => {}) }, [])

  // ── news data: load, then re-poll every 2 min while the tab is visible ──
  useEffect(() => {
    let ctl, dead = false, lastLoad = 0
    const qs = new URLSearchParams({ hours, topic: channel ? '' : topic, channel, headlines: heads ? 1 : 0 }).toString()
    qsRef.current = qs
    featsRef.current.clear(); areaSeenRef.current.clear()      // a new filter starts a new merged set
    const load = async () => {
      ctl?.abort(); ctl = new AbortController()
      try {
        const fc = await api.globe(qs, ctl.signal)
        if (dead) return
        ingest(fc, false); lastLoad = Date.now()
        const m = mapRef.current
        if (m && m.getZoom() >= AREA_MIN_ZOOM) {                 // zoomed in: also refresh the visible area so its dots survive the re-poll
          const more = await api.globe(`${qs}&bbox=${bboxOf(m)}`, ctl.signal)
          if (!dead) ingest(more, false)
        }
      } catch (e) { if (e.name !== 'AbortError' && !dead) setErr(e.message || 'offline') }
      finally { if (!dead) setLoading(false) }
    }
    setLoading(true)
    load()
    const timer = setInterval(() => { if (!document.hidden) load() }, POLL_MS)
    const vis = () => { if (!document.hidden && Date.now() - lastLoad > POLL_MS) load() }
    document.addEventListener('visibilitychange', vis)
    return () => { dead = true; ctl?.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', vis) }
  }, [hours, topic, channel, heads, ingest])

  // data may arrive before the map is ready
  useEffect(() => { if (ready) pushRef.current?.() }, [ready])

  // highlight the selection on the map
  useEffect(() => {
    const m = mapRef.current
    if (!ready || !m) return
    const ids = selected.map((p) => p.k)
    const f = ['in', ['get', 'k'], ['literal', ids.length ? ids : ['-']]]
    m.setFilter('news-sel', ['all', f, ['==', ['get', 'kind'], 'story']])
    m.setFilter('news-sel-dot', ['all', f, ['!=', ['get', 'kind'], 'story']])
  }, [selected, ready])

  useEffect(() => {                                     // Esc closes the panel
    const h = (e) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  // ── search ──
  useEffect(() => {
    const t = q.trim()
    if (t.length < 2) { setHits([]); return }
    const ctl = new AbortController()
    const id = setTimeout(() => {
      api.globeSearch(t, ctl.signal).then((r) => { setHits(Array.isArray(r) ? r.slice(0, 8) : []); setActive(-1) }).catch(() => {})
    }, 250)
    return () => { clearTimeout(id); ctl.abort() }
  }, [q])
  const goto = (h) => { setQ(h.name); setDdOpen(false); fly(h.lon, h.lat, 10) }
  const onSearchKey = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setDdOpen(true); setActive((a) => Math.min(hits.length - 1, a + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(0, a - 1)) }
    else if (e.key === 'Enter' && hits.length) { e.preventDefault(); goto(hits[active >= 0 ? active : 0]) }
    else if (e.key === 'Escape' && ddOpen) { e.stopPropagation(); setDdOpen(false) }
  }

  const locate = () => {
    setGeoMsg('')
    if (!navigator.geolocation) { setGeoMsg('Location is not available in this browser. Try the search box.'); return }
    navigator.geolocation.getCurrentPosition(
      (pos) => fly(pos.coords.longitude, pos.coords.latitude, 9),
      () => setGeoMsg('Could not get your location. Allow it in the browser, or use the search box.'),
      { timeout: 8000, maximumAge: 600000 },
    )
  }
  const reset = () => {
    const m = mapRef.current
    if (!m) return
    setSelected([]); setRotate(!reduced.current)
    m.flyTo({ ...HOME, zoom: phone.current ? 0.9 : HOME.zoom, pitch: 0, bearing: 0, duration: reduced.current ? 0 : 1800, essential: true })
  }
  const pick = (p) => {                                  // click a list item: fly to it and highlight it
    const d = dataRef.current, known = d?.byKey.get(p.k || `${p.kind}:${p.id}`) || p
    const lng = known.lng ?? p.lon, lat = known.lat ?? p.lat
    setSelected(d?.byCoord.get(known.ck) || [known])
    setTab('selected')
    if (lng != null && lat != null) fly(lng, lat, Math.max(mapRef.current?.getZoom() || 0, 9))
  }

  const sorted = useMemo(() => {
    const items = (near?.items || []).map((p) => ({ ...p, k: `${p.kind}:${p.id}` }))
    const byTime = (a, b) => new Date(b.t) - new Date(a.t)
    return {
      stories: items.filter((p) => p.kind === 'story').sort((a, b) => (b.rank || 0) - (a.rank || 0) || byTime(a, b)),
      headlines: items.filter((p) => p.kind !== 'story').sort(byTime),
    }
  }, [near])
  const selKeys = useMemo(() => new Set(selected.map((p) => p.k)), [selected])
  const liveItems = (live?.items || []).filter((i) => safeUrl(i.url))
  const offline = err && !counts

  if (noGl) {
    return <div className="gl-root"><div className="gl-fallback"><h3>The globe needs WebGL</h3><p>This browser or device has WebGL turned off, so the 3D map cannot start. Enable hardware acceleration, or try another browser.</p></div></div>
  }

  return (
    <div className={'gl-root' + (open ? ' panel-open' : '')}>
      <div ref={boxRef} className="gl-map" aria-label="Interactive 3D globe with news locations" />

      <div className="gl-ctl">
        <div className="gl-glass gl-search" role="combobox" aria-expanded={ddOpen && hits.length > 0} aria-haspopup="listbox" aria-owns="gl-hits">
          <Search size={16} />
          <input value={q} placeholder="Fly to a city or country" aria-label="Search for a place" aria-autocomplete="list" aria-controls="gl-hits"
            aria-activedescendant={active >= 0 ? `gl-hit-${active}` : undefined}
            onChange={(e) => { setQ(e.target.value); setDdOpen(true) }} onFocus={() => setDdOpen(true)} onBlur={() => setTimeout(() => setDdOpen(false), 120)} onKeyDown={onSearchKey} />
          {q && <button className="gl-x" aria-label="Clear search" onClick={() => { setQ(''); setHits([]) }}><Close size={14} /></button>}
          {ddOpen && hits.length > 0 && (
            <ul id="gl-hits" role="listbox" className="gl-dd">
              {hits.map((h, i) => (
                <li key={`${h.name}${h.lat}${h.lon}`} id={`gl-hit-${i}`} role="option" aria-selected={i === active} className={i === active ? 'on' : ''}
                  onMouseDown={(e) => { e.preventDefault(); goto(h) }} onMouseEnter={() => setActive(i)}>
                  <span>{h.name}</span><small>{h.country}</small>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="gl-tools">
          <button className="gl-pill" onClick={locate}>My region</button>
          <button className="gl-pill" onClick={reset}>Reset view</button>
          <button className={'gl-pill' + (rotate ? ' on' : '')} aria-pressed={rotate} onClick={() => setRotate((r) => !r)} disabled={reduced.current}>Rotate</button>
          <button className={'gl-pill' + (filtersOpen ? ' on' : '')} aria-expanded={filtersOpen} onClick={() => setFiltersOpen((o) => !o)}>Filters</button>
        </div>
        {geoMsg && <div className="gl-glass gl-note" role="status">{geoMsg}</div>}

        {filtersOpen && (
          <div className="gl-glass gl-filters">
            <div className="gl-chiprow" role="group" aria-label="Topic">
              <button className={'chip' + (!topic ? ' sel' : '')} onClick={() => setTopic('')}>All</button>
              {Object.keys(PALETTE).map((t) => (
                <button key={t} className={'chip' + (topic === t && !channel ? ' sel' : '')} onClick={() => { setChannel(''); setTopic(topic === t ? '' : t) }}>
                  <span className="gl-pip sm" style={{ background: PALETTE[t] }} aria-hidden="true" />{topicLabel(t)}
                </button>
              ))}
            </div>
            <div className="gl-row2">
              <div className="gl-seg" role="group" aria-label="Time window">
                {HOURS.map(([h, label]) => <button key={h} className={hours === h ? 'on' : ''} aria-pressed={hours === h} onClick={() => setHours(h)}>{label}</button>)}
              </div>
              <div className="gl-seg" role="group" aria-label="What to show">
                <button className={!heads ? 'on' : ''} aria-pressed={!heads} onClick={() => setHeads(false)}>Stories only</button>
                <button className={heads ? 'on' : ''} aria-pressed={heads} onClick={() => setHeads(true)}>+ Headlines</button>
              </div>
              {channels.length > 0 && (
                <select className="gl-sel" aria-label="Channel" value={channel} onChange={(e) => { setChannel(e.target.value); if (e.target.value) setTopic('') }}>
                  <option value="">All channels</option>
                  {channels.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              )}
            </div>
          </div>
        )}
      </div>

      {area && (
        <div className={'gl-glass gl-area' + (area.busy ? '' : ' done')} role="status">
          <i aria-hidden="true" />
          {area.busy ? `Collecting local news…${area.n ? ` ${area.n} new` : ''}` : <>Live{area.n ? ` · ${area.n} new` : ''} · updated <Ago t={area.t} /></>}
        </div>
      )}

      {!open && <button className="gl-glass gl-reopen" onClick={() => setOpen(true)} aria-label="Open news panel"><Planet size={16} /> News</button>}

      {(offline || mapErr) && (
        <div className="gl-glass gl-state" role="alert">
          <h3>{offline ? 'Cannot reach ContextLens' : 'Map tiles unavailable'}</h3>
          <p>{offline ? 'The backend on :8000 did not answer, so there are no news dots yet. Start it with start.ps1, then retry.' : mapErr}</p>
          {offline && <button className="btn primary sm" onClick={() => window.location.reload()}>Retry</button>}
        </div>
      )}

      <aside className={'gl-glass gl-panel' + (open ? '' : ' closed')} aria-label="News panel">
        <button className="gl-grab" aria-label={open ? 'Collapse panel' : 'Expand panel'}
          onPointerDown={(e) => { dragY.current = e.clientY; e.currentTarget.setPointerCapture(e.pointerId) }}
          onPointerUp={(e) => { const dy = e.clientY - (dragY.current ?? e.clientY); dragY.current = null; setOpen(Math.abs(dy) > 24 ? dy < 0 : !open) }} />
        <header className="gl-head">
          <div className="gl-live">
            <span className={'gl-livedot' + (err ? ' off' : '')} aria-hidden="true" /><b>{err ? 'OFFLINE' : 'LIVE'}</b>
            <span className="gl-count">
              {counts ? `${counts.stories} stories · ${counts.headlines} headlines on the map` : loading ? 'Loading…' : 'No data'}
            </span>
            <button className="gl-x" onClick={() => setOpen(false)} aria-label="Close panel (Esc)"><Close size={15} /></button>
          </div>
          <div className="gl-sub">{updated ? <>Updated <Ago t={updated} />{err ? ' · refresh failed, retrying' : ''}</> : err ? 'Could not load' : ''}</div>
          <div className="gl-tabs" role="tablist">
            <button role="tab" aria-selected={tab === 'here'} className={tab === 'here' ? 'on' : ''} onClick={() => { setTab('here'); setOpen(true) }}>Here</button>
            <button role="tab" aria-selected={tab === 'selected'} className={tab === 'selected' ? 'on' : ''} onClick={() => { setTab('selected'); setOpen(true) }}>
              Selected{selected.length > 1 ? ` (${selected.length})` : ''}
            </button>
          </div>
        </header>

        <div className="gl-body" role="tabpanel">
          {tab === 'selected' && (selected.length
            ? <div className="gl-stack">{selected.map((p) => <Detail key={p.k} p={p} />)}</div>
            : <p className="gl-empty">Click a dot on the map to read it here.</p>)}

          {tab === 'here' && (!zoomed ? (
            <>
              <p className="gl-hint">Zoom in on a region, or search for a city, to see local news here.</p>
              {top.length > 0 && <h4 className="gl-h">Top stories on the map</h4>}
              {loading && !top.length && <Skeleton />}
              <ul className="gl-list">{top.map((p) => <Row key={p.k} p={p} onPick={pick} active={selKeys.has(p.k)} />)}</ul>
            </>
          ) : (
            <>
              <h4 className="gl-h">{near?.place ? `Near ${near.place.name}${near.place.country ? `, ${near.place.country}` : ''}` : 'Around the map centre'}</h4>
              {nearBusy && !near && <Skeleton />}
              {nearErr && !near && <p className="gl-empty">Could not load local news. Move the map to retry.</p>}
              {near && !sorted.stories.length && !sorted.headlines.length && <p className="gl-empty">No reported news here in the last 72 hours. Zoom out or pan to a larger city.</p>}
              {sorted.stories.length > 0 && <><h5 className="gl-h2">Stories</h5><ul className="gl-list">{sorted.stories.map((p) => <Row key={p.k} p={p} onPick={pick} active={selKeys.has(p.k)} />)}</ul></>}
              {sorted.headlines.length > 0 && <><h5 className="gl-h2">Headlines</h5><ul className="gl-list">{sorted.headlines.map((p) => <Row key={p.k} p={p} onPick={pick} active={selKeys.has(p.k)} />)}</ul></>}
              {liveItems.length > 0 && (
                <section aria-label="Live headlines">
                  <h4 className="gl-h">Live headlines for {live.place?.name || 'this area'}</h4>
                  <ul className="gl-list">
                    {liveItems.map((i) => (
                      <li key={i.url}>
                        <a className="gl-row" href={safeUrl(i.url)} target="_blank" rel="noopener noreferrer">
                          <span className="gl-row-body"><span className="gl-row-t">{i.title}</span><span className="gl-meta">{i.outlet}{i.published_at ? ` · ${timeAgo(i.published_at)}` : ''}</span></span>
                          <External size={14} />
                        </a>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          ))}
        </div>

        <footer className="gl-legend" aria-label="Legend">
          {Object.keys(PALETTE).map((t) => <span key={t}><i style={{ background: PALETTE[t] }} />{topicLabel(t)}</span>)}
          <span><i style={{ background: CHANNEL_COLOR }} />Channel</span>
          <small>Pin = story, small dot = headline, ring = under 3 h old.</small>
        </footer>
      </aside>
    </div>
  )
}
