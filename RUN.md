# ContextLens — running it

## Every day

```powershell
.\start.ps1          # then open http://localhost:8000
```

Prerequisites running first: **LM Studio** local server (port 1235) and **SearXNG** (Docker, port 8080).
You do **not** need to load any chat model in LM Studio: ContextLens loads the one it needs (via the `lms` command-line tool that ships with LM Studio) and
unloads the previous chat model first, so only one is ever resident. The embedding model (~80 MB) is loaded on demand too.
The Today page shows a red banner if the model server is unreachable; existing stories stay readable.

## First run

1. Choose topics and describe your interests in your own words (Settings → Interests later).
2. **~2–5 minutes**: 224 feeds are read and clustered (duplicates merged), then ~200 stories appear on Today / Everything immediately (dashed cards = "tap to explain").
3. In the background the writers explain the best stories one by one (Settings → Feed → *Explained per refresh*). Each takes several minutes on a laptop GPU.
   Opening any story explains it right away (it jumps the queue). Cards get a **Why** badge, and a *Peek* button, as they finish.
4. After that the feed refreshes automatically (default hourly; Settings → Feed). **Refresh** forces it.

## Features you'll want to try

* **Select text** in any story → Explain · Why? · Who/what · Forensics · Ask (and a full fact-check from the panel).
* **Newspapers** → drop an e-paper PDF (text-based). The digest shows what matters to you; open any article to explain it.
* **Discover → Explain an article** → paste a link or the text.
* **Flow** → swipe deck; **Atlas** → who/what keeps appearing across your stories; entity pages have Follow.
* **Talk to Lens** → tap the orb (Chrome/Edge) or type. Say "give me the highlights", then "why?", "how did we get here?", "what happens next?", "next", or ask anything.
  `Esc` interrupts; in the ⚙ settings you can pick the voice/speed, turn the web research off, or allow talking over Lens (use headphones).
* **Ctrl/⌘+K** → jump to any page, search every story, or send a question to Lens.
* **Everything** → the unranked list (topic / outlet / text / fact-check filters). **Sources** → where the news comes from.
* **Calm mode** (Today) → hides graphic stories; they stay in Everything.

## Adding a news source

Add `{ "url": …, "outlet": …, "topic": … }` to `backend/feeds_extra.json` (topics: world, politics, business, markets, tech, science, health, india, sports, culture),
and, if the outlet is new, an entry in `backend/trusted_sources.json` (domain, name, `trust_rank` 1–3, type, country). Dead feeds are skipped at fetch time.
Use a feed that carries publication dates (undated feeds would show old items as new).

## Upgrading from earlier versions

On first start of v3 the old v2 `contextlens.db` was moved to `contextlens.v2-legacy.db`. v4 keeps your v3 database: existing stories stay as they were
(open one and press **Explain it now** to add the why-chain), and new tables are created automatically.

## Models on a small GPU (6 GB VRAM)

Settings → Models shows what is loaded now. "Keep only one model loaded" (default on) makes ContextLens run `lms unload` / `lms load` around each model switch
(~10–30 s). Work is ordered so switches are rare: triage batches, then one story at a time (writer → editor gate). If swapping is too slow:
**Single-model mode** (writer does everything). "Unload when idle" frees the GPU after N minutes.
If the `lms` CLI can't be found the manager is skipped and LM Studio's own loading applies (set `LMS_PATH` in `backend/.env` to point at `lms.exe`).

## Cloud models first, LM Studio last (v6.1)
Every chat call (stories, Ask, voice answers, research, digest) tries these providers first, in order, and uses LM Studio only if all of them fail:
Groq, Gemini, Mistral, NVIDIA, Cloudflare Workers AI, OpenRouter (the editor role leads with Mistral, Cloudflare, Gemini so it judges with a different model family than the writer).
A provider that returns 429, an auth error or a timeout is parked for 1 to 10 minutes and the next one answers. Keys live in `backend/.env` (names in `.env.example`); a provider without a key is skipped.
`GET /api/health` -> `llm.cloud` shows `ready`/`cooling` per provider. Set `CLOUD_FALLBACK=0` to keep everything on LM Studio.
Embeddings (clustering, interests, retrieval) always stay local: LM Studio's embedding model must be running for the feed to refresh.
Everything sent to the cloud leaves your machine, including Ask/voice questions.

## Your own channels (v6.2)
**My channels** (left menu) lets you make your own categories, as niche as "Classical music": a name plus a short description of what it covers.
* **Find sources** searches Google News and SearXNG (:8080) for the sites that cover the topic and keeps only those with a working RSS/Atom feed (takes 1-2 minutes).
  Nothing is hard-coded: disable, remove or add a source (a site address is enough) per channel. Sites already in the general feeds are matched by meaning instead.
* **Fill now** reads only that channel's sources and writes its stories (about 4 minutes); a normal **Refresh** also covers every channel. Each channel is a tab on Today (`/?c=<id>`).
* On the main **All** feed a channel shows only a taste (2 stories, below the day's biggest news); its own tab shows everything.
* Per channel: priority (Off/Some/More/Most), detail per story (Quick/Standard/Deep = 2/3/5 articles read), how many stories, how far back (1-7 days), words to never show, search phrases.
* A story belongs to a channel when its meaning matches the channel's name + description (embeddings) and key terms; every card says why ("in your channel ..."). The editor judges a channel's stories as a follower
  of that topic would, so single-outlet and "routine" niche news is kept. Off-topic items from a channel's feeds are dropped.
* Limits: free search/cloud quotas apply (a channel adds about 4 searches and up to 10 story builds per refresh); some feeds are noisy, so disable the ones that are not on topic.

## Globe (v6.3)
**Globe** (left menu, `/globe`) is a draggable 3D Earth that zooms to street level (roads, labels, 3D buildings from OpenFreeMap / OpenStreetMap) with the news pinned where it happened.
* Dots: your stories (big, coloured by topic), the day's other headlines (small), and **live local headlines** from Google News, Bing News and SearXNG for ~260 places (the busiest cities, every capital, the biggest cities) (refreshed every 20 minutes; opening any city fetches its live headlines too).
* Where a dot sits is worked out offline from the place named in the story's own text (GeoNames gazetteer in `backend/data/geonames`, CC BY 4.0). If no place is clear, the story is simply not on the globe; a country-only match is pinned at the capital and labelled "country", and dots in one place fan out a little so they do not stack.
* Click a dot for the story / headline; the **Here** panel lists everything near the centre of the map. Search flies to any city or country.
* The map tiles need internet (OpenFreeMap, no key). Nothing about you is sent except the map position when you use "My region".

## Articles that block bots (stealth fetcher)
Some outlets answer a plain fetch with 403 (NDTV, Axios...). `tools/camofox-browser` ([jo-inc/camofox-browser](https://github.com/jo-inc/camofox-browser), MIT, a Firefox build that passes bot checks) is used as a fallback for exactly those, only if its server is up.
It is a plain Node process (no Docker). `start.ps1` starts it on `127.0.0.1:9377` (about 200 MB RAM); control it yourself with `.\camofox.ps1 start | stop | status`. It does **not** open real paywalls (NYT, Bloomberg stay unreadable and fail closed). `CAMOFOX_URL=` (empty) turns it off; reinstall with `cd tools/camofox-browser; npm install --omit=dev`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Model server offline" | Start LM Studio's server on port 1235 (`lms server start --port 1235`) or set `LLM_BASE_URL` in `backend/.env`. |
| Story stays "Not explained yet" | Open it again (it retries), or check Settings → Activity; a failed attempt shows its error on the story. |
| PDF: "no text layer" | The paper is a scan. Use a digital e-paper, or run OCR first. |
| Research: "not enough readable sources" | Topic too narrow or outlets paywalled. Broaden the wording. |
| First story is slow / memory errors | The NER model loads on first use (~600 MB RAM). Close heavy apps, or turn it off in Settings → Feed (regex fallback). |
| Frontend shows a stale UI | `.\start.ps1 -Rebuild` |

## Useful commands

```powershell
venv\python -m pytest backend/tests                            # 123 tests, offline (DISABLE_LMS=1: never touches your loaded models)
venv\python scripts\eval_feed.py --fresh --size 6              # dry-run the whole pipeline into a scratch DB
& "$env:USERPROFILE\.lmstudio\bin\lms.exe" ps                  # what LM Studio has loaded
```
