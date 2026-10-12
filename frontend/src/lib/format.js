export function timeAgo(iso) {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const m = Math.max(0, Math.round((Date.now() - t) / 60000))
  if (m < 1) return 'just now'
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h} h ago`
  const d = Math.round(h / 24)
  return d === 1 ? 'yesterday' : `${d} days ago`
}

export function formatDate(iso, precision = 'day') {
  if (!iso) return ''
  const d = new Date(iso + (iso.length === 10 ? 'T00:00:00' : ''))
  if (Number.isNaN(d.getTime())) return iso
  if (precision === 'year') return String(d.getFullYear())
  if (precision === 'month') return d.toLocaleDateString('en-GB', { month: 'long', year: 'numeric' })
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })
}

export const todayLabel = () =>
  new Date().toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' })

export const TOPIC_LABEL = {
  world: 'World', politics: 'Politics', business: 'Business', markets: 'Markets', tech: 'Technology',
  science: 'Science', health: 'Health', india: 'India', sports: 'Sports', culture: 'Culture',
}
const CHANNEL_NAME = {}                                        // "ch:<id>" -> the reader's own channel name, filled from /api/feed
export const rememberChannels = (list) => { for (const c of list || []) CHANNEL_NAME['ch:' + c.id] = c.name }
export const topicLabel = (t) => TOPIC_LABEL[t] || CHANNEL_NAME[t] || (t?.startsWith('ch:') ? 'Channel' : t)

export const TRUST = { 1: 'Tier 1', 2: 'Tier 2', 3: 'Tier 3', 4: 'Unrated', 5: 'Low' }
