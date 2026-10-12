import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './index.css'
import './v4.css'
import './v5.css'

// cards glow where the pointer is: one delegated listener instead of one per card
document.addEventListener('pointermove', (e) => {
  const el = e.target instanceof Element ? e.target.closest('.scard, .hero, .arow, .outlet') : null
  if (!el) return
  const r = el.getBoundingClientRect()
  el.style.setProperty('--mx', `${e.clientX - r.left}px`)
  el.style.setProperty('--my', `${e.clientY - r.top}px`)
}, { passive: true })

createRoot(document.getElementById('root')).render(<StrictMode><App /></StrictMode>)

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}))
}
