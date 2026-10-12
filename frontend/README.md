# ContextLens frontend

React 19 + Vite. Plain CSS design system in `src/index.css` (light/dark tokens, editorial type — Newsreader + Inter).

```
src/
  App.jsx                 routes + error boundaries
  lib/                    api.js (REST + SSE), hooks.js (useAsync, useRun, useHealth), speech.js (on-device TTS), format.js
  components/             Shell (rail, tab bar, global refresh/listen state), StoryCards, StoryMap (D3), Timeline,
                          ChatPanel, AskBriefing, RunPanel, ListenBar, Icons
  pages/                  Today, Story, Explore, Saved, Settings, Onboarding
public/                   favicon, PWA manifest, service worker (network-first navigations; offline story reading)
```

```powershell
npm install
npm run dev      # http://localhost:5173, proxies /api to the backend on :8000
npm run build    # -> dist/ (served by the backend on :8000)
npm run lint
```

The story map only renders `{nodes, edges}` returned by the API; nothing on the client generates graph structure.
