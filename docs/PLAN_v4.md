# ContextLens v4 — "the news assistant that explains" (plan)

## 1. What the research showed

**Google News (India edition, studied live):** ~100 items on one screen. *Top stories* are event clusters (4 outlets each,
"See more headlines and perspectives" = Full coverage), then *Local*, *Picks for you*, and *Your topics* (India, World,
Business, Tech, Entertainment, Sports — 3 stories each), a markets ticker, and per-topic Customise. It is fast and dense —
and it explains nothing. Inshorts and Times of India are the same: 60-word summaries or long articles, no *why*.

**What readers say they want** (recurring themes in news-app reviews and the "news avoidance" / explainer-journalism
literature — themes, not measured numbers; this plan did not run its own survey):
* "I don't understand the background" — explainers and context are the most-requested missing piece.
* Too many stories, too little meaning → fatigue and avoidance. People want to *finish* and feel caught up.
* They want to choose depth: 20-second gist → why it happened → history → analysis.
* They distrust framing and want to see *who says what* and whether it is corroborated.

**What v3 got wrong for you (measured on the real database):**
| Problem | Evidence | Root cause |
|---|---|---|
| Only 12–17 stories after 10 min | 610 events found, 18 built | every story had to pass the whole LLM chain before it could be shown |
| Key points generic | "Sensex jumped 564 points… driven by oil prices" | the prompt only restates facts; nothing asks *why* or *what led to it* |
| No timelines at all | 0 timeline events across 18 stories | events were only taken from dated sentences inside one day's article; daily news rarely has them |
| Not "a knowledge graph" | entities were per story, thrown away | no memory across stories; nothing links today's story to last week's |
| Feels like a random news site | — | no identity, no reason to come back, no interaction beyond reading |
| Two LLMs loaded at once | LM Studio JIT-loaded Gemma next to Llama | no model management |

## 2. Product idea

> **A patient friend who has read everything.** Every story is explained in plain language, from "what happened" to
> "why did it happen" to "how did we get here", and you can pull on any thread — select any sentence and ask.

Signature features (none of the big apps has them together):
1. **Layers, not articles** — Gist → What happened → *Why it happened* → *How we got here* (timeline) → Who's who → What to watch → Deep dives. Start from zero / skip to the layer you need.
2. **Select-to-understand (Inspector)** — select any text: Explain simply · Why was this said/done · Who/what is this · Forensics (attribution, hedges, unnamed sources, loaded words, corroboration) · Ask.
3. **Living knowledge base** — every story is linked into a persistent graph of entities and story threads. New story → "Previously on this story" + connected stories; entity pages; a global Atlas.
4. **Newspaper upload** — drop a PDF; it is split into articles, filtered by your interests, explained, and linked into the same knowledge base ("38 articles → 9 matter to you").
5. **Paste a link / text** → the same explainer.
6. **Flow mode** — swipe deck; tap deeper on any card; finite ("you're caught up") with a streak.
7. **Instant feed** — up to 60 stories appear right after ingestion (wire tier, no LLM); the writer upgrades them in the background; opening one builds it immediately at high priority.

## 3. Architecture (changes to v3)

* `services/lms.py` — LM Studio model manager (via `lms` CLI): exactly **one chat LLM resident**; swaps unload the old one first; embedding model (80 MB) stays; optional idle-unload.
* `services/llm.py` — priority gate (interactive requests jump the queue), calls `lms.ensure()` before every chat.
* **Wire tier** in `pipeline.py`: ingest → cluster → score → persist wire stories (no LLM) → background *builder queue* (triage → card → explainer → gate) ordered by rank; `POST /stories/{id}/build` for on-demand.
* `services/explainer.py` — second writer pass: causal chain ("why"), glossary, what's next, who's who, curious questions; banned-filler validator; simple-language default.
* `services/background.py` — history retrieval (own KB + GDELT + SearXNG + Wikipedia), dated by publish date → grounded "How we got here" timeline.
* `services/kb.py` — entities, threads, story links; `/api/atlas`, `/api/entities/{id}`, `/api/stories/{id}/thread`.
* `services/inspector.py` — select-text actions + deterministic forensic language analysis.
* `services/paper.py` — PDF → articles (PyMuPDF, headline-size heuristics) → score → explain; `/api/papers`.
* Frontend: new design language (warm paper / ink, serif display, tactile motion), Today (sections, quick-peek), Flow, Story (layer rail, glossary popovers, inspector), Papers, Atlas/Entity, Discover.

## 4. Build order & verification
1. lms manager + priority gate → verify with `lms ps` that one LLM is ever resident.
2. DB migrations → wire tier + builder queue → verify feed has ≥40 items right after ingest.
3. explainer + background/timeline + KB → run on the 18 existing stories; inspect output quality.
4. inspector + paper upload (test with a generated newspaper-style PDF) + link explain.
5. Frontend makeover; drive it in the browser at desktop and mobile widths.
6. Tests (unit + API), lint, build, docs; full live cycle.

Honest limits: 4–8 B local models can still err (grounding checks and visible reasons mitigate); scanned/image-only PDFs need OCR (not included); paywalled outlets stay unreadable; a full deep build of 60 stories takes hours on this GPU — that is why the wire tier exists.

## 5. Status (end of this pass)

Built and verified live on this machine (RTX 4050 6 GB, LM Studio + SearXNG):

| Area | State |
|---|---|
| One chat LLM at a time | `lms.py` + priority gate; verified with `lms ps` (embedder + one chat model). |
| Instant front page | 57–60 stories live 17 s after a warm refresh (`wire` tier); writers upgrade them per story; opening one explains it now. |
| Explainer | Why-chains with verbatim evidence, glossary, next/who; validated against hallucination (prompt-example leak found and fixed). |
| Timelines | Real dated timelines from recap articles + earlier coverage (IIT Bombay story: 5 events over 3 days); gated, absent when unsupported. |
| Knowledge base | Threads (member-level matching), entities, links, Atlas, entity pages. |
| Select-to-understand | Explain / Why / Who / Forensics / History / Fact-check; forensics is rule-based. |
| Newspaper upload | PDF → articles → interest scoring → explained stories; tested on a generated e-paper and unit tests. |
| Explain link/text | `POST /api/explain` (public http(s) only). |
| UI | New "Lumen" skin, Today (sections, peek, streak/ring), Flow, Story (layer bar, chain, glossary popovers), Papers, Atlas, Entity, Discover, Settings. |
| Tests | 87 backend tests, lint clean, frontend build OK. |

Not done / next: OCR for scanned PDFs; multi-user hosting; notifications; a "quiz me" recall card; better causal chains need a stronger writer model
(the pipeline accepts any OpenAI-compatible endpoint, so a hosted model is a settings change).
