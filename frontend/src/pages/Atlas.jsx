import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import * as d3 from 'd3'
import { api } from '../lib/api'
import { useAsync } from '../lib/hooks'

const TYPE_COLOR = { person: '#d6431c', organization: '#2f6fdb', place: '#1f7a4d', event: '#a9770a', other: '#80776a' }
const TYPE_LABEL = { person: 'People', organization: 'Organisations', place: 'Places', event: 'Events', other: 'Other' }

function Graph({ nodes, edges, onOpen }) {
  const ref = useRef(null)
  const [hover, setHover] = useState(null)
  useEffect(() => {
    const svg = d3.select(ref.current)
    svg.selectAll('*').remove()
    const W = ref.current.clientWidth || 900
    const H = ref.current.clientHeight || 600
    svg.attr('viewBox', `0 0 ${W} ${H}`)
    const ns = nodes.map((n) => ({ ...n }))
    const es = edges.map((e) => ({ ...e }))
    const css = getComputedStyle(document.documentElement)
    const line = css.getPropertyValue('--line-2').trim() || '#ccc'
    const ink = css.getPropertyValue('--ink').trim() || '#111'
    const bg = css.getPropertyValue('--surface').trim() || '#fff'
    const r = (d) => 6 + Math.min(16, d.stories * 2.2)
    const sim = d3.forceSimulation(ns)
      .force('link', d3.forceLink(es).id((d) => d.id).distance((e) => 130 - Math.min(40, e.weight * 8)).strength(0.5))
      .force('charge', d3.forceManyBody().strength(-520))
      .force('center', d3.forceCenter(W / 2, H / 2))
      .force('collide', d3.forceCollide((d) => r(d) + 14))
    const g = svg.append('g')
    svg.call(d3.zoom().scaleExtent([0.4, 3]).on('zoom', (ev) => g.attr('transform', ev.transform)))
    const link = g.append('g').selectAll('line').data(es).join('line').attr('stroke', line).attr('stroke-width', (d) => 1 + Math.min(4, d.weight)).attr('stroke-opacity', 0.75)
    const node = g.append('g').selectAll('g').data(ns).join('g').style('cursor', 'pointer')
      .on('click', (_, d) => onOpen(d)).on('mouseenter', (_, d) => setHover(d)).on('mouseleave', () => setHover(null))
      .call(d3.drag().on('start', (ev, d) => { sim.alphaTarget(0.25).restart(); d.fx = d.x; d.fy = d.y })
        .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y }).on('end', (ev, d) => { sim.alphaTarget(0); d.fx = null; d.fy = null }))
    node.append('circle').attr('r', r).attr('fill', (d) => TYPE_COLOR[d.type] || TYPE_COLOR.other).attr('stroke', bg).attr('stroke-width', 2.5)
    node.append('text').text((d) => d.name).attr('dy', (d) => r(d) + 13).attr('text-anchor', 'middle').attr('font-size', 12).attr('font-weight', 600)
      .attr('fill', ink).attr('paint-order', 'stroke').attr('stroke', bg).attr('stroke-width', 4)
    sim.on('tick', () => {
      link.attr('x1', (d) => d.source.x).attr('y1', (d) => d.source.y).attr('x2', (d) => d.target.x).attr('y2', (d) => d.target.y)
      node.attr('transform', (d) => `translate(${d.x},${d.y})`)
    })
    return () => sim.stop()
  }, [nodes, edges, onOpen])
  return (
    <div className="atlas">
      <svg ref={ref} role="img" aria-label="Map of the people, organisations and places that keep appearing in your stories" />
      <div className="tip" aria-live="polite">
        {hover ? <><b>{hover.name}</b> · {TYPE_LABEL[hover.type] || 'Other'} · in {hover.stories} stories — click to open</> : <span className="muted">Each dot is someone or something that keeps appearing in your news. A line means they appear together in at least two stories. Drag, scroll to zoom, click to open.</span>}
      </div>
    </div>
  )
}

export default function Atlas() {
  const nav = useNavigate()
  const [days, setDays] = useState(21)
  const { data, loading } = useAsync(() => api.atlas(days), [days])
  const open = (d) => nav(`/entity/${d.id}`)
  const types = [...new Set((data?.nodes || []).map((n) => n.type))]
  return (
    <div className="container wide">
      <div className="eyebrow">Atlas</div>
      <h1 className="h-page" style={{ margin: '6px 0 8px' }}>Everything is connected</h1>
      <p className="lede" style={{ marginBottom: 16, maxWidth: 720 }}>Every story you read is linked to the people, organisations and places in it. Over time this becomes a map of your news: who keeps turning up, and who appears alongside whom.</p>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
        <div className="seg">{[[7, 'Week'], [21, '3 weeks'], [90, '3 months']].map(([v, l]) => <button key={v} className={days === v ? 'on' : ''} onClick={() => setDays(v)}>{l}</button>)}</div>
        <div className="legend" style={{ border: 0, padding: 0 }}>{types.map((t) => <span key={t}><i style={{ background: TYPE_COLOR[t] || TYPE_COLOR.other }} />{TYPE_LABEL[t] || t}</span>)}</div>
      </div>
      {loading && <div className="sk" style={{ height: 420, borderRadius: 20 }} />}
      {data && data.nodes.length < 3 && !loading && (
        <div className="empty">
          <h3>The map is still empty</h3>
          <p>Entities are added as stories get explained. Explain a few stories (open them from Today) and come back — the more you read, the richer this gets.</p>
        </div>
      )}
      {data && data.nodes.length >= 3 && <Graph nodes={data.nodes} edges={data.edges} onOpen={open} />}
    </div>
  )
}
