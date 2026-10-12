import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { useAsync } from '../lib/hooks'
import { Card } from '../components/StoryCards'

export default function Saved() {
  const { data, loading } = useAsync(() => api.saved(), [])
  return (
    <div className="container narrow">
      <div className="eyebrow">Saved</div>
      <h1 className="h-page" style={{ margin: '6px 0 18px' }}>Your reading list</h1>
      {loading && <div className="sk" style={{ height: 90 }} />}
      {data?.length === 0 && (
        <div className="empty"><h3>Nothing saved yet</h3><p>Save any story to keep it here. Saving also teaches your briefing what you like.</p><Link className="btn primary" to="/">Back to Today</Link></div>
      )}
      <div style={{ display: 'grid', gap: 12 }}>{data?.map((s) => <Card key={s.id} s={s} />)}</div>
    </div>
  )
}
