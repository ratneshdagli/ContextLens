import { Component, lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Shell from './components/Shell'
import All from './pages/All'
import Atlas from './pages/Atlas'
import Digest from './pages/Digest'
import Entity from './pages/Entity'
import Explore from './pages/Explore'
import Flow from './pages/Flow'
import Live from './pages/Live'
import Onboarding from './pages/Onboarding'
import PaperView from './pages/PaperView'
import Papers from './pages/Papers'
import Channels from './pages/Channels'
import Saved from './pages/Saved'
import Settings from './pages/Settings'
import Sources from './pages/Sources'
import Story from './pages/Story'
import Today from './pages/Today'

import Shorts from './pages/Shorts'
const Globe = lazy(() => import('./pages/Globe'))            // maplibre is ~1 MB: its own chunk, fetched on first visit

class Boundary extends Component {
  state = { error: null }
  static getDerivedStateFromError(error) { return { error } }
  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="container"><div className="empty">
        <h3>Something went wrong on this page</h3>
        <p>{this.state.error.message}</p>
        <button className="btn primary" onClick={() => this.setState({ error: null })}>Try again</button>
      </div></div>
    )
  }
}

const wrap = (el) => <Boundary>{el}</Boundary>

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/welcome" element={wrap(<Onboarding />)} />
        <Route element={<Shell />}>
          <Route index element={wrap(<Today />)} />
          <Route path="story/:id" element={wrap(<Story />)} />
          <Route path="flow" element={wrap(<Flow />)} />
          <Route path="shorts" element={wrap(<Shorts />)} />
          <Route path="live" element={wrap(<Live />)} />
          <Route path="all" element={wrap(<All />)} />
          <Route path="sources" element={wrap(<Sources />)} />
          <Route path="explore" element={wrap(<Explore />)} />
          <Route path="papers" element={wrap(<Papers />)} />
          <Route path="papers/:id" element={wrap(<PaperView />)} />
          <Route path="atlas" element={wrap(<Atlas />)} />
          <Route path="digest" element={wrap(<Digest />)} />
          <Route path="entity/:id" element={wrap(<Entity />)} />
          <Route path="channels" element={wrap(<Channels />)} />
          <Route path="globe" element={wrap(<Suspense fallback={<div className="container"><p className="muted">Loading the globe…</p></div>}><Globe /></Suspense>)} />
          <Route path="saved" element={wrap(<Saved />)} />
          <Route path="settings" element={wrap(<Settings />)} />
          <Route path="*" element={<div className="container"><div className="empty"><h3>Page not found</h3></div></div>} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
