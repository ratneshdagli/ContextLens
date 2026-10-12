# ContextLens

**A news assistant that explains.** Most news apps show you what happened. ContextLens tells you *why it happened*, *what led up to it*,
and *who is involved* — in plain language — and lets you pull on any thread until you understand it.

Local-first: it runs on your own machine with [LM Studio](https://lmstudio.ai) (language models) and SearXNG (web search).
Nothing about your reading leaves your computer.

## What makes it different

| | |
|---|---|
| **Layers, not articles** | Every story is *gist → what happened → why it happened → how it unfolded → who's who → coverage*. Skip to the layer you need. |
| **Why-chains with receipts** | The "why" is a causal chain, oldest cause first. Each step shows the exact sentence it comes from ("show the sentence"), or is honestly labelled *how it works* (general knowledge). Unsupported steps are dropped. |
| **Select-to-understand** | Highlight any text in a story: **Explain** it, ask **Why?**, see **Who / what** it is, run **Forensics** (who asserts it, hedges, unnamed sources, charged wording, conflicting numbers, how many outlets corroborate it) or launch a full **fact-check**. |
| **Newspaper upload** | Drop an e-paper PDF. It is split into articles, ads/classifieds are skipped, your interests pick what matters, and the best are explained — with background from the web. |
| **Explain any link** | Paste a URL or article text and get the same explainer. |
| **A knowledge base that grows** | Every story joins a *thread* (the same story across days), and its people/organisations are tracked across stories. "The story so far", connected stories, entity pages and the **Atlas** graph. |
| **Talk to Lens (live voice)** | Talk to the news. Lens reads the highlights, answers "why?" and "how did we get here?" straight from what it has already explained (≈1 s), and *while it talks* a second process searches the web and adds what it can back up. Just start talking to cut it off — no button — and it remembers the conversation if you step away and come back. Every spoken sentence is checked against its sources first. |
| **Your Digest** | Track a GitHub repo (a real, LLM-summarised weekly commit digest) or a topic — typed, or pulled straight out of a document you upload — and see what's new in it alongside the news. |
| **One story, not ten** | A reaction ("Zoho founder backs the professor"), a follow-up and a profile of the same event are merged into one story with the other angles listed inside it. |
| **Where news comes from** | 90+ outlets, 224 live feeds: Indian national/city press, world press, fact-checkers, explainers, specialists. The **Sources** page shows trust tiers and live counts. |
| **Everything, not just what ranks** | Today ranks; **Everything** lists all ~200 stories of the last three days with topic, outlet, text and *fact-check* filters. |
| **Calm mode · Fact-checks · WhatsApp** | Hide distressing stories from Today; see when a fact-checker covered a story; share the gist + reasons + source link on WhatsApp. |
| **Instant front page** | ~200 stories appear right after the newspapers are read (no waiting for the AI). Writers explain them one by one, best first; opening one explains it immediately. |
| **Flow mode** | A finite swipe deck: tap deeper for *why*, then *history*, then *what it means*. "You're all caught up." |
| **Jargon that explains itself** | Hand-written definitions for FIR, repo rate, bond yield, status quo order… underlined in the text; tap for the meaning. |
| **Interest-driven, two-model editorial gate** | Onboarding collects your interests; a writer model and a *different* editor model decide what is worth showing, and why (visible under "How this story was checked"). |
| **Globe** | `/globe`: a movable 3D globe on OpenFreeMap tiles. Zoom to a place and see news tied to it. An offline gazetteer (GeoNames, CC BY 4.0) plus a deterministic geotagger that fails closed (no LLM) puts stories on the map; live headlines per city come from Google News RSS, Bing RSS, SearXNG and the stealth fetcher. |
| **Your own categories (channels)** | Add a reader channel such as "Classical music". ContextLens discovers sources, matches stories against your brief, and judges them with a niche-aware editor. |
| **Local shelf & blind spots** | A Local shelf for your home place, and a Blind spots shelf for stories covered by one country's outlets only. |
| **In short** | A 60-word card deck made of whole sentences from the story's own text, with a language picker (Hindi, Marathi, Bengali, Tamil, Telugu, Gujarati, Kannada, Malayalam) and a spoken "Play briefing". Translations pass a gate (script, length and numbers must match) or are refused. |
| **Breaking alerts** | Opt-in browser notifications for breaking stories (settings kept in your browser only). |
| **Cloud-first chat, local embeddings** | Chat calls try free cloud tiers first (Groq, Gemini, Mistral, NVIDIA, Cloudflare, OpenRouter) and fall back to LM Studio; embeddings always stay local. `CLOUD_FALLBACK=0` turns the cloud off. Article text is sent to a provider only when you add its key. |
| **Also** | Follow a story/entity, claim provenance chips, coverage lens, Signal score, "I have 5 minutes" mode, Listen (browser TTS), keyboard shortcuts, offline PWA, light/dark. |

**One model at a time.** On a small GPU two chat models cannot coexist, so ContextLens unloads the previous chat model in LM Studio before loading the
next one (the ~80 MB embedding model stays). See Settings → Models.

## Quick start (Windows)

1. LM Studio: start the local server on port **1235** and have these models downloaded (they are loaded on demand — you don't need to load them yourself):
   `meta-llama-3-8b-instruct` (writer), `google/gemma-3n-e4b` (editor), `text-embedding-nomic-embed-text-v1.5` (embeddings).
2. SearXNG on port **8080** with JSON output enabled (Docker).
3. ```powershell
   .\start.ps1
   ```
   then open **http://localhost:8000**. First run: choose your interests; the front page appears in a few minutes and explanations keep arriving.

Details, environment variables and troubleshooting: [RUN.md](RUN.md). Architecture: [docs/PLAN_v4.md](docs/PLAN_v4.md). Reader research and the v5/v6 changes: [docs/RESEARCH_v5.md](docs/RESEARCH_v5.md), [docs/RESEARCH_v6.md](docs/RESEARCH_v6.md).
Press **Ctrl/⌘+K** anywhere to jump, search every story, or ask Lens.

## How it works

```
RSS (224 feeds, 90+ outlets) → embed + cluster same-event articles → merge clusters that are one story → score vs. your interests (deterministic)
   → WIRE tier: front page immediately (cleaned headline + snippet, no LLM)
   → BUILD queue, best first:   editor triage → writer card → entities (GLiNER) → history timeline (own KB + SearXNG + GDELT, dated by publication)
                                → explainer (why-chain, glossary, what next, who's who) → editor publish gate → persist + link into the knowledge base
Reader opens a wire story → built immediately at interactive priority.

Lens (voice):  question ─► rules pick the route ─┬─ stored explanations (no model, ≈1 s): highlights · why · timeline · next · "what happened with X"
                                                 └─ retrieve by embedding → one streamed answer, each sentence grounding-checked before it is spoken
               after the answer ─► reasoner: web search → read sources → new, supported facts only → "I dug a little deeper…"   (a new question cancels it)
```

Design rules: every AI step is *classify* (schema-constrained), *render* (no LLM) or *constrained-generate* (short, grounded, validated).
Gates fail closed and store their reasons. The model never chooses dates — timelines use publication dates and dates quoted in articles.

## Layout

```
backend/   FastAPI + SQLite (aiosqlite). services/: llm, lms (one-model-at-a-time), pipeline, explainer, background (history), kb (threads+entities),
           inspector, dives, paper (PDF), research, timeline, graph, glossary, voice, watch (digest) …   routers/: feed, stories, inspect, kb, papers, research, settings, voice, sources, digest …
           feeds_extra.json (probed feeds) · trusted_sources.json (outlets, trust tiers, fact-checkers)
frontend/  React 19 + Vite + d3, plain-CSS design system (index.css + v4.css + v5.css), PWA
scripts/   eval_feed.py, bench_editor.py — offline evaluation helpers
docs/      PLAN_v4.md, RESEARCH_v5.md, RESEARCH_v6.md and archive/ (older specs)
```

## Honest limits

* 4–8 B local models can still err. Guardrails (grounding checks, verbatim evidence, fail-closed gates, visible reasons) reduce it; they don't remove it.
* Explaining a story takes several minutes on a laptop GPU. That is why the front page is instant and explanation is progressive.
* Paywalled outlets (Reuters, WSJ, FT…) usually can't be read in full. Scanned/image-only PDFs need OCR, which is not bundled.
* Timelines exist only when earlier dated coverage or dated sentences are found; many stories legitimately have none.
* Voice *input* uses the browser's speech recognition (Chrome/Edge; needs internet). Typing always works. A spoken question jumps ahead of background pipeline work on the shared GPU, but still waits out whatever single call is already in flight; highlights and already-explained stories are instant. Interrupting Lens mid-sentence is energy-based mic detection, not a trained model — it's reliable with headphones, less so through laptop speakers.
* A repo digest reads GitHub's public, unauthenticated API (60 requests/hour) and only the last 7 days of commits; a topic digest only ever lists stories already in this app's own database.
* Single user, local. Hosted multi-user use needs accounts and a hosted model endpoint (the model layer already accepts any OpenAI-compatible API).

Tests: `venv\python -m pytest backend/tests` (175; they never touch your real LM Studio models and never spend cloud quota). Frontend: `cd frontend && npm run lint && npm run build`.

Optional: a stealth browser ([camofox-browser](https://github.com/jo-inc/camofox-browser), not bundled) on port 9377 lets ContextLens read articles that block plain fetches. Copy `backend/.env.example` to `backend/.env` before the first run.

## License

MIT, see [LICENSE](LICENSE). Place data: GeoNames (CC BY 4.0). Map tiles: OpenFreeMap / OpenStreetMap contributors.
