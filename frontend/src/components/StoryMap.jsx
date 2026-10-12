import { useEffect, useRef, useState } from 'react'
import * as d3 from 'd3'

const TYPE_COLOR = { person: '#c8401e', organization: '#2f6fdb', place: '#1f7a4d', event: '#9a6a00', other: '#7a766d' }
const TYPE_LABEL = { person: 'Person', organization: 'Organisation', place: 'Place', event: 'Event', other: 'Other' }

/** Renders an evidenced relationship graph. Nodes/edges come from the server; nothing is generated here. */
export default function StoryMap({ nodes, edges }) {
  const ref = useRef(null)
  const [hover, setHover] = useState(null)

  useEffect(() => {
    const svg = d3.select(ref.current)
    svg.selectAll('*').remove()
    const W = ref.current.clientWidth || 700
    const H = 380
    svg.attr('viewBox', `0 0 ${W} ${H}`)
    const ns = nodes.map((n) => ({ ...n, id: n.name }))
    const es = edges.map((e) => ({ ...e }))
    const sim = d3.forceSimulation(ns)
      .force('link', d3.forceLink(es).id((d) => d.id).distance(120).strength(0.6))
      .force('charge', d3.forceManyBody().strength(-420))
      .force('center', d3.forceCenter(W / 2, H / 2))
      .force('collide', d3.forceCollide(46))
      .force('x', d3.forceX(W / 2).strength(0.05))
      .force('y', d3.forceY(H / 2).strength(0.08))

    const css = getComputedStyle(document.documentElement)
    const line = css.getPropertyValue('--line-2').trim() || '#ccc'
    const ink = css.getPropertyValue('--ink').trim() || '#111'
    const ink3 = css.getPropertyValue('--ink-3').trim() || '#777'
    const bg = css.getPropertyValue('--surface').trim() || '#fff'

    const link = svg.append('g').selectAll('line').data(es).join('line').attr('stroke', line).attr('stroke-width', 1.6)
      .style('cursor', 'pointer').on('mouseenter', (_, d) => setHover({ edge: d })).on('mouseleave', () => setHover(null))
    const hit = svg.append('g').selectAll('line').data(es).join('line').attr('stroke', 'transparent').attr('stroke-width', 14).style('cursor', 'pointer')
      .on('mouseenter', (_, d) => setHover({ edge: d })).on('mouseleave', () => setHover(null))
    const label = svg.append('g').selectAll('text').data(es).join('text').text((d) => d.label).attr('font-size', 11).attr('fill', ink3)
      .attr('text-anchor', 'middle').attr('paint-order', 'stroke').attr('stroke', bg).attr('stroke-width', 4).style('pointer-events', 'none')
    const node = svg.append('g').selectAll('g').data(ns).join('g').style('cursor', 'grab')
      .call(d3.drag().on('start', (ev, d) => { sim.alphaTarget(0.25).restart(); d.fx = d.x; d.fy = d.y })
        .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y })
        .on('end', (ev, d) => { sim.alphaTarget(0); d.fx = null; d.fy = null }))
      .on('mouseenter', (_, d) => setHover({ node: d })).on('mouseleave', () => setHover(null))
    const r = (d) => 9 + Math.min(9, (d.mentions || 1) * 1.2)
    node.append('circle').attr('r', r).attr('fill', (d) => TYPE_COLOR[d.type] || TYPE_COLOR.other).attr('stroke', bg).attr('stroke-width', 2.5)
    node.append('text').text((d) => d.name).attr('dy', (d) => r(d) + 14).attr('text-anchor', 'middle').attr('font-size', 12.5).attr('font-weight', 600)
      .attr('fill', ink).attr('paint-order', 'stroke').attr('stroke', bg).attr('stroke-width', 4)

    sim.on('tick', () => {
      const clampX = (x) => Math.max(40, Math.min(W - 40, x))
      const clampY = (y) => Math.max(28, Math.min(H - 34, y))
      ns.forEach((d) => { d.x = clampX(d.x); d.y = clampY(d.y) })
      for (const sel of [link, hit]) sel.attr('x1', (d) => d.source.x).attr('y1', (d) => d.source.y).attr('x2', (d) => d.target.x).attr('y2', (d) => d.target.y)
      label.attr('x', (d) => (d.source.x + d.target.x) / 2).attr('y', (d) => (d.source.y + d.target.y) / 2 - 5)
      node.attr('transform', (d) => `translate(${d.x},${d.y})`)
    })
    return () => sim.stop()
  }, [nodes, edges])

  const types = [...new Set(nodes.map((n) => n.type))]
  return (
    <div className="map-wrap">
      <svg ref={ref} role="img" aria-label="Relationship map of the people, organisations and places in this story" />
      <div className="map-evidence" aria-live="polite">
        {hover?.edge && <><b>{hover.edge.source.name ?? hover.edge.source} → {hover.edge.label} → {hover.edge.target.name ?? hover.edge.target}</b><br />
          <span className="serif" style={{ fontStyle: 'italic' }}>“{hover.edge.evidence}”</span> <a href={hover.edge.url} target="_blank" rel="noreferrer" className="small" style={{ textDecoration: 'underline' }}>source</a></>}
        {hover?.node && <><b>{hover.node.name}</b> · {TYPE_LABEL[hover.node.type] || 'Other'} · mentioned {hover.node.mentions}× across sources</>}
        {!hover && <span className="muted">Hover a connection to see the sentence it comes from. Every link on this map is backed by a quote from the reporting.</span>}
      </div>
      <div className="legend">{types.map((t) => <span key={t}><i style={{ background: TYPE_COLOR[t] || TYPE_COLOR.other }} />{TYPE_LABEL[t] || t}</span>)}</div>
    </div>
  )
}
