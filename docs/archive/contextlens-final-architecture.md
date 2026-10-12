# ContextLens — Final Architecture Specification (consolidated)

> This document supersedes and merges the two earlier documents (`contextlens-v2-build-spec.md` and `contextlens-ai-architecture-research.md`) into one complete build brief. Hand this single document to the build agent (e.g. Antigravity). It assumes an existing ContextLens V1 codebase (FastAPI + React/Vite, local model runtime via LM Studio, SQLite, trafilatura crawler) and specifies what to keep, replace, and add — for both the product/UX layer and the AI/knowledge-graph layer.

---

## Table of Contents

1. [Product Vision](#1-product-vision)
2. [The Big Flip — UX Order](#2-the-big-flip--ux-order)
3. [Signature Visual Identity — The Investigation Board](#3-signature-visual-identity--the-investigation-board)
4. [Onboarding Flow](#4-onboarding-flow)
5. [The Live Agent Console](#5-the-live-agent-console)
6. [The Intelligence Architecture](#6-the-intelligence-architecture)
7. [Research Pipeline — Stage by Stage](#7-research-pipeline--stage-by-stage)
8. [Trust & Citation System](#8-trust--citation-system)
9. [Story Intelligence — Layered Disclosure](#9-story-intelligence--layered-disclosure)
10. [Product Decisions, With Reasoning](#10-product-decisions-with-reasoning)
11. [Pages & Navigation](#11-pages--navigation)
12. [Data Model](#12-data-model)
13. [Backend Architecture — Keep / Replace / Add](#13-backend-architecture--keep--replace--add)
14. [Frontend Architecture — Keep / Replace / Add](#14-frontend-architecture--keep--replace--add)
15. [Design Tokens](#15-design-tokens)
16. [Complete Technology Stack](#16-complete-technology-stack)
17. [Annotated Reading List](#17-annotated-reading-list)
18. [Known Risks & Honest Limitations](#18-known-risks--honest-limitations)
19. [Definition of Done — Fix Map](#19-definition-of-done--fix-map)
20. [Build Order](#20-build-order)

---

## 1. Product Vision

ContextLens is a research board that fills itself in, not a search box waiting for input. You open the app once, tell it what you care about, and it goes to work immediately and visibly — pulling in stories, crawling sources, building timelines, and pinning connected stories together. Search still exists but is a way to go deeper on demand, not the front door.

Underneath, the product's real differentiator is that it treats news as a graph that accumulates over time, not a stream of disconnected documents. When a chairman resigns "without warning," ContextLens should be the app that shows you the three quiet years of context that made it not actually sudden — because that context was never deleted, only timestamped.

---

## 2. The Big Flip — UX Order

**Old flow (V1):** open app → empty home / RSS list → type a search → hope something loads.

**New flow:**
```
Open app (first time)
   │
   ▼
Onboarding: pick interests + depth preference        (30 seconds)
   │
   ▼
Background pipeline starts immediately for your topics
   │
   ▼
Board screen opens WHILE research is still running —
you watch cards pin themselves on, one by one, live       (~30–90s, fully visible)
   │
   ▼
Board is populated, clustered, strung together by relationship
   │
   ▼
Click a card → Case File opens (layered story detail)
   │
   ▼
Search → real web search + full pipeline, adds new pins
   │
   ▼
"Dig Deeper" on any story → re-runs pipeline at next depth tier
```

The board is never empty and never silent. If it has nothing yet, it is actively shown building itself.

---

## 3. Signature Visual Identity — The Investigation Board

The home screen is a corkboard. Stories are index cards pinned to it. Related stories are connected with string — not decoration, but the actual information architecture: pin color encodes trust tier, string encodes the "these are actually one story" connective tissue that generic feeds destroy by showing fragmented articles.

**Balance rule:** the frame is playful and spatial; the content inside each card and inside the Case File stays calm, high-contrast, editorial. Corkboard is a container, not an excuse for clutter.

**Card anatomy:** index-card proportions, subtle paper texture, small matte shadow (no blur/glassmorphism). Random rotation ±2–5°, seeded by story id so it's stable across reloads. Single pushpin icon, colored by trust tier: green (tier 1), amber (tier 2), grey (tier 3), red-striped ("unverified — new source"). Card front: serif headline, one-line summary, source-count + freshness, category chip. Same-cluster cards sit near each other, connected by a red string (SVG line, slight sag, animated "pull taut" on first connection). Hovering a string shows why the two are connected (shared entity, causal, same event thread, market reaction).

**Board vs Clean view:** persistent toggle. Board view is the corkboard described above; Clean view is the same data in a plain card grid — same underlying data, different rendering, for small screens or users who want the calm original layout.

**Layout mechanics:** reuse D3 (already a dependency for the timeline/mind map) with a force simulation for board layout — cards repel, connected cards attract along their string, clusters settle into neighborhoods. Run to convergence once per update, don't animate continuously. Persist settled positions (`BoardPin` table) so the board doesn't reshuffle on every visit.

---

## 4. Onboarding Flow

Shown once on first launch, revisitable from Settings.

1. **Interests** — multi-select from the existing category list. Minimum 3 required.
2. **Depth default** — "Quick digest" / "Give me everything" / "Balanced" (default: fast on breaking news, deeper automatically on stories the pipeline judges complex or evolving).
3. **Transition screen** — not a spinner. States plainly that building the board takes about a minute and that you'll see exactly what's being found as it's found, then routes straight into the Board with the live console already streaming.

Preferences drive the background pre-warm scheduler from then on, every time the app opens, not just once.

---

## 5. The Live Agent Console

Fixes "I don't think anything is working." A slim, persistent, collapsible log panel streams structured events as they happen — dispatch-log style:

```
10:42:03  Searching 112 trusted sources for "Fed rate decision"
10:42:05  Crawling reuters.com/markets/fed-decision-2026 ...
10:42:07  ✓ Extracted 1,840 words — reuters.com (tier 1)
10:42:09  ✗ Paywall detected — wsj.com (keeping metadata only)
10:42:11  Deduplicating — removed 4 near-duplicate articles
10:42:14  Synthesizing summary and timeline...
10:42:20  Extracting entities — found 7 people, 3 organizations
10:42:22  Checking graph — found 2 related stories, drawing connections
10:42:23  Verifying generated claims against sources...
10:42:24  Pinning to board — done. 14 sources, trust score 0.87
```

Every line corresponds to a real pipeline step — no fake filler messages. Persisted per story (`AgentEvent` table) and viewable later from the Case File's "How this was researched" tab. In parallel, cards physically pin themselves onto the board and strings draw themselves as connections are found — driven by the same event stream.

**Mechanism:** extend the job model to emit structured events `{job_id, timestamp, stage, message, data}`, streamed via SSE (reuse the pattern already used for chat via `sse-starlette`).

---

## 6. The Intelligence Architecture

This is the core of what makes "connections between news" actually work, and it's the part that needed real research rather than invention. It rests on two loops sharing one persistent graph.

### 6.1 Two loops, one graph

```
INGESTION LOOP (continuous, cheap, no LLM reasoning about connections)
  RSS feeds → dedup → cluster new articles into "events" (entity + embedding
  similarity against recently-active graph nodes only) → merge into existing
  story-cluster or create new node

ON-DEMAND LOOP (triggered by search or "dig deeper")
  Real web search fills gaps the RSS list didn't cover → same clustering
  step → merges into the SAME graph as the ingestion loop
```

Nothing is recomputed from scratch on a new query. A search for a five-year-old-context story is a graph traversal, not a re-derivation.

### 6.2 The temporal knowledge graph — Graphiti + FalkorDB

**Graphiti** (Zep AI, open source) is purpose-built for this: it makes real-time incremental updates to a graph with no batch recomputation, continuously ingesting new data as discrete "episodes," extracting and immediately resolving entities/relationships against existing nodes. It is bi-temporal — every edge carries both when a fact was true and when it was ingested, and when new information conflicts with old, the old fact is invalidated rather than deleted. That is exactly the "chairman resigned without warning" case: the graph doesn't overwrite "is chairman," it timestamps "was chairman until X" and adds "resigned on Y," and both remain queryable for the timeline.

This is a deliberately different tool from Microsoft's GraphRAG. GraphRAG builds its graph by batch-processing an entire corpus at once (LLM-driven entity extraction + Leiden-algorithm community detection) and its own maintainers warn indexing is expensive and should be started small — the right tool for "summarize the structure of a fixed pile of documents once," the wrong tool for "new documents arrive every few minutes forever," where you'd be re-paying that indexing cost on a rolling basis.

**Backend choice — use FalkorDB, not Neo4j.** As of Graphiti's current documentation, its embedded Kuzu backend has been deprecated (the upstream Kuzu project is no longer maintained) — new projects are directed to Neo4j or FalkorDB. Between those two, FalkorDB is the better fit here: it's Redis-based and substantially lighter on RAM than Neo4j, ships with native vector search, and Graphiti has first-class support for it. A "FalkorDB Lite" embedded, file-based, no-server variant is also landing — worth adopting once stable, since it removes the last piece of infrastructure overhead (no Docker/server process at all, just a file path, matching the project's local-first philosophy). Until then, a single lightweight FalkorDB Docker container is still far less overhead than Neo4j.

**The small-model risk, and how to route around it.** Graphiti's documentation explicitly states it works best with LLM services that reliably support structured output (OpenAI, Gemini) and that using smaller or non-compliant models can produce incorrect output schemas and ingestion failures — a real risk for a local 8B model. The fix: don't let Graphiti's automatic unstructured-text extraction pathway touch the local LLM at all. Instead:
1. Run **GLiNER** (see 6.5) on the crawled article text yourself to get clean entities and relations.
2. Construct a structured JSON payload from that output.
3. Feed it into Graphiti as a **JSON episode** (Graphiti explicitly supports structured/JSON episodes, not only raw text) rather than relying on its LLM-driven text-extraction path.
4. Graphiti's remaining job — resolving entities against existing nodes, temporal bookkeeping, invalidating superseded facts — is comparatively mechanical and far less dependent on generation quality than raw extraction would be.

This sidesteps the exact failure mode the docs warn about, and reserves the local LLM for the one task it's actually needed for: writing prose from already-verified structured facts.

### 6.3 Event clustering & story threading — Story Forest

The academic grounding for "this news led to that news" is Tencent's production system, described in *Story Forest: Extracting Events and Telling Stories from Breaking News* (ACM TKDD 2020, extended from arXiv:1803.00189). It automatically clusters streaming documents into events (a group of articles reporting the same incident, possibly worded differently), then connects related events into growing trees, built specifically so that story structure develops online without repeatedly restructuring what's already been formed — the same non-recomputation principle as Graphiti, independently arrived at.

Their own evaluation is worth internalizing before over-building: out of 221 real evaluated stories, the tree structure gave the best readability on 187 of them, while a flat list and a simple timeline were the best fit for 88 and 52 respectively. In other words: most stories are genuinely just a timeline. Build the tree/graph structure, but let simple timelines render as the common case rather than forcing every story into a visually busy graph. This precedent traces back to classic Topic Detection and Tracking (TDT) work, specifically *Event Threading within News Topics* (Nallapati et al., ACM CIKM 2004) — worth reading as background before implementing the threading logic.

There is no public Story Forest repository — it's a described algorithm, not a library — so this is implementation work, not a dependency to install.

### 6.4 Deduplication pipeline

News dedup has a specific failure mode that generic dedup research doesn't cover well: near-verbatim wire-service copy (the same AP/Reuters story appearing on forty outlets) versus genuinely independent coverage of the same event with different wording. A 2025 industry paper on exactly this problem (near-duplicate detection in paywalled/wire news) found SimHash faster and more memory-efficient than MinHash-LSH for this case, since it stores a single hash per document rather than one per shingle. Recommended two-stage approach:
1. **Cheap first pass — SimHash.** Catches near-verbatim wire copy fast, before spending any embedding compute.
2. **Semantic second pass — embeddings.** Use the embedding model already available (`nomic-embed-text`) plus cosine similarity to catch independently-written coverage of the same event, which hash-based methods miss by design. This is the pass that feeds the Story Forest-style event clustering in 6.3.

(If dedup quality becomes a bottleneck later, Google's RETSim neural similarity model outperformed both MinHash and SimHash on a dedicated news-copy benchmark — worth revisiting then, not a v1 requirement.)

### 6.5 Entity/relation extraction — GLiNER

**GLiNER** is a small, free, zero-shot named-entity model: you specify entity types as natural-language labels at runtime rather than fine-tuning, it runs efficiently on CPU, and it's specifically positioned as the practical alternative to asking a full LLM to do extraction. Use it for every article's entity/relation extraction — this is the piece that both frees the local LLM from a task it's not well-suited for and produces the clean structured input Graphiti needs (per 6.2).

### 6.6 Real web search — SearXNG

For the on-demand loop (user search, gaps the trusted RSS list doesn't cover), self-host **SearXNG**, an open-source metasearch engine that queries dozens of upstream engines and returns aggregated, tracker-stripped results via a JSON API. It's genuinely lightweight — roughly 300MB as a Docker image, comfortable on 512MB RAM — so it doesn't meaningfully add to your infrastructure footprint, and it keeps search local/private rather than depending on a paid API. (A hosted API with a free tier, e.g. Brave Search, remains a reasonable fallback if self-hosting is inconvenient — keep `search_client.py` as an abstraction either way.)

### 6.7 Verification / anti-hallucination — MiniCheck

**MiniCheck** (Tang, Laban, Durrett — EMNLP 2024, extended by Bespoke Labs) is a purpose-built, free, open-source sentence-level fact-checking model: given a document and a single claim sentence, it returns supported/unsupported. The smallest variants (RoBERTa-large or DeBERTa-v3-large, both under 0.5B parameters) reach GPT-4-level performance on this narrow task at a fraction of the cost, and are pip-installable (`pip install minicheck`). Run every sentence the local LLM generates back through MiniCheck against its cited source span before showing it to the user; drop or flag anything unsupported. This is a cheap, purpose-built check — not another expensive LLM call — and it's the concrete mechanism behind "no hallucinations" rather than a hope.

### 6.8 What the local LLM actually does

With 6.3–6.7 offloaded, the local model's job shrinks to exactly one thing: turning already-extracted, already-clustered, already-cited, already-verified structured facts into readable prose. That's a fundamentally easier task than "read 6000 raw characters and simultaneously extract, judge, and summarize them," which is closer to what a single-prompt synthesis step tends to ask of it. This is also why the context-window worry was somewhat misplaced: Llama 3.1 8B Instruct natively supports a 128K-token window (local runtimes like LM Studio often just default to loading only 8K unless explicitly configured higher) — but the real fix isn't a bigger window, it's giving the model less to reason about per call.

---

## 7. Research Pipeline — Stage by Stage

```
5%   Classify intent + depth tier
15%  Search (RSS feeds + SearXNG for on-demand queries)
30%  Crawl candidates (trafilatura + BS4 fallback — unchanged from V1)
40%  Deduplicate (SimHash pass, then embedding pass)
50%  Extract entities/relations (GLiNER)
58%  Update knowledge graph (Graphiti + FalkorDB, JSON episodes)
65%  Cluster/thread into story arc (Story Forest-style, using graph output)
75%  Synthesize summary + master timeline (local LLM, from structured facts)
82%  Synthesize per-thread timelines (Tier 3 stories only)
90%  Verify generated claims (MiniCheck pass)
95%  Pin to board, draw connections
100% Done — persist AgentEvent log
```

### Depth tiers

| Tier | Sources crawled | Output | When used |
|---|---|---|---|
| 1 — Quick | 3–5 | One-line + short summary, no timeline | Default for routine breaking items |
| 2 — Standard | 8–12 | Full summary, single timeline, key entities | Default under "Balanced" preference |
| 3 — Deep | 15–25 | Multiple thread timelines, full mind map, glossary, controversy notes | "Give me everything" default; reached via Dig Deeper; auto-selected when the story matches an existing multi-story cluster |

---

## 8. Trust & Citation System

Expand `TRUSTED_SOURCES` to 100+ domains in an editable JSON config, tiered, loaded at startup. Domains discovered via real web search but not in the curated list get a fourth tier — "Unverified, new source" — checked against a blocklist of known low-quality domains, rather than being silently trusted or silently dropped.

Every generated claim (summary sentence, timeline event, entity fact) must trace to the specific source(s) it came from — inline superscript markers, hover preview showing domain/trust badge/publish date/the exact excerpt used. Combined with the MiniCheck verification pass (6.7), this means a citation isn't just a UI nicety, it's something programmatically checked to actually be true before display.

---

## 9. Story Intelligence — Layered Disclosure

Structure every Case File as explicit, separately-revealed layers rather than one long page:

1. Explain like I just woke up — 2–3 plain sentences, zero jargon.
2. Five-minute summary.
3. Timeline(s) — master + per-thread (Tier 3).
4. Key people & organizations — entity pills, mini-bio on click.
5. Glossary — every acronym/term used, defined inline.
6. Deep analysis — collapsed by default.
7. Sources & documents — the citation system above.
8. Controversy / what's disputed — distinct section when present, never flattened into the main summary.

Layers 1–3 expanded by default, the rest collapsed. When the clustering step (6.3) detects that today's article continues an existing `StoryCluster`, it must be added to that cluster and its timeline, not created as a disconnected new card — this is the single most important behavior for making connected events feel connected.

---

## 10. Product Decisions, With Reasoning

**Headlines.** Show the publisher's real headline verbatim on source cards, always attributed — rewriting a real headline and presenting it as the source's own is an attribution problem. Generate a separate, visually distinct synthesis title for the multi-source cluster view (`canonical_title`, already in the data model), styled so users can always tell "what a publisher wrote" from "what the AI wrote." This mirrors how Google News' Full Coverage groups stories from multiple sources without generating its own summary or headline for the group.

**Timeline depth.** Don't hardcode a lookback on the story. Bound the merge decision (only compare new articles against recently-active clusters, days not years, for efficiency) but let an existing cluster's timeline run as long as its real connected chain does. Put the 5-day / 5-month / 1-year / all-time control in the UI as a view filter over the stored graph, not an ingestion-time limit.

**Home page segments**, informed by how Google News actually personalizes (a "For you" section from declared interests, a separate "Following" section for explicitly tracked topics/sources):
- **Following** — categories/topics from onboarding (pre-warm scheduler output).
- **Developing** — clusters that received a new connected event in the last few hours; cheap to compute since every graph edge is already timestamped.
- **Threads reopened** — dormant clusters (no activity in weeks/months) that just received a new edge. This is the "chairman resigned without warning" case made into a home-page feature, and it's close to free to compute once the graph exists — a genuinely differentiating segment, not decoration.

**No-hallucination summaries.** Every generated sentence cites a literal span from a source (substring/fuzzy match, not a paraphrase check), verified by the MiniCheck pass before display; unsupported sentences are dropped or flagged rather than trusted on the first generation pass.

---

## 11. Pages & Navigation

| Page | Route | Notes |
|---|---|---|
| Onboarding | `/welcome` | First-run only, revisitable from Settings |
| The Board | `/` | Corkboard or Clean view, live-populating, never empty |
| Category Board | `/category/:cat` | Filtered board view |
| Search | `/search` | Backed by SearXNG + trusted RSS; results pin onto the board |
| Case File | `/story/:id` | Layered disclosure; "How this was researched" tab shows the persisted agent log |
| Topic Research | `/research` | Explicit depth-tier selection |
| Timeline (full page) | `/timeline/:id` | Tabs between master and per-thread timelines |
| Mind Map (full page) | `/mindmap/:id` | Represents the whole `StoryCluster`, sourced from the knowledge graph |
| Chat | `/chat` | Unchanged from V1 |
| Pinned | `/saved` | Renamed from Saved to match board metaphor |
| Settings | `/settings` | Onboarding preferences, trusted-source registry browser, search provider config, depth defaults |

---

## 12. Data Model

New/changed tables on top of V1's `stories`, `sources`, `conversations`, `saved_stories`, `jobs`:

```
UserPreferences
  id, categories[], default_depth, board_view ("board"|"clean"), created_at

StoryCluster
  id, title, story_ids[], master_timeline_id, arc_status ("developing"|"resolved"), updated_at

TimelineThread
  id, story_id (or cluster_id), thread_label, events[] (subset of TimelineEvent ids)

BoardPin
  story_id, x, y, rotation, pin_color, cluster_id, pinned_at

Connection
  id, from_story_id, to_story_id, relation_type ("causal"|"shared_entity"|"same_thread"|"temporal"), label

AgentEvent
  id, job_id, story_id, timestamp, stage, message, data (json)   -- persisted, replayable

GraphEpisode  (bookkeeping row, mirrors what's sent to Graphiti)
  id, source_article_id, entities[], relations[], sent_at, graphiti_episode_id
```

`Source.trust_rank` gains a 4th tier ("unverified"); `Source` gains `verified_by`. The actual graph structure (entities, temporal edges) lives inside FalkorDB via Graphiti, not in SQLite — SQLite/the tables above remain the system of record for the app's own objects (stories, board layout, UI state), while the knowledge graph is queried for clustering/connection decisions and mind-map data.

---

## 13. Backend Architecture — Keep / Replace / Add

**Keep as-is:** `database.py`'s async pattern; `extraction.py` (trafilatura crawler, rate limiting, retry/backoff — solid, no reason to touch); `ranking.py`'s trust-scoring logic (extend for the 4th tier); the model abstraction pattern in `ollama_client.py` (rename to `model_client.py`, keep swappable); CORS/lifespan setup in `main.py`.

**Replace:** the trigger model (on-demand-only → pre-warm scheduler + manual triggers as secondary path); job status shape (flat `{progress, message}` → structured `AgentEvent` stream over SSE); `synthesis.py` (extend to consume graph output, produce `StoryCluster` assignment and `TimelineThread`s); search (RSS-keyword-match-only → `search_client.py` wrapping SearXNG as a first-class pre-crawl step).

**Add:** `services/graph_client.py` (Graphiti + FalkorDB wrapper — accepts pre-extracted JSON episodes, never raw text, per 6.2); `services/entity_extraction.py` (GLiNER wrapper); `services/dedup.py` (SimHash pass + embedding pass, per 6.4); `services/verification.py` (MiniCheck wrapper, per 6.7); `services/clustering.py` (Story Forest-style event/story threading, consuming graph output); `services/board_layout.py` (D3-equivalent force layout, persists `BoardPin`); routers `board.py` and `preferences.py`; move `TRUSTED_SOURCES` into external editable JSON.

---

## 14. Frontend Architecture — Keep / Replace / Add

**Keep as-is:** D3 dependency and the run-to-convergence rendering approach already used in `TimelineViz.jsx`/`MindMapViz.jsx` — reuse for the board's force layout; `ChatPanel.jsx`, `useChat.js`, the SSE-reading pattern in `api.js`'s `streamChat()` — copy this exact pattern for the agent event stream; design tokens as the base layer.

**Replace:** `Home.jsx` → `Board.jsx` (corkboard/clean-view toggle); `StoryDetail.jsx` → `CaseFile.jsx` (layered disclosure + "How this was researched" tab); `LoadingState.jsx`'s `ResearchProgress` → the live agent console, reusable across Board and Case File.

**Add:** `pages/Onboarding.jsx` + `hooks/usePreferences.js`; `components/board/BoardCanvas.jsx`, `BoardCard.jsx`, `ConnectionString.jsx`; `components/agent/AgentConsole.jsx` + `hooks/useAgentEvents.js`; `components/story/LayerSection.jsx` (generic collapsible layer).

---

## 15. Design Tokens

Base palette (unchanged — it was never the problem):
```css
--bg: #0d0d0f  --surface: #1e1e23  --border: #2a2a32
--text-1: #f0f0ee  --text-2: #a8a8b3  --text-3: #6e6e7a
--accent: #4f7ef7  --green: #2ecc71  --amber: #f59e0b  --red: #ef4444
--font-sans: Inter  --font-serif: Playfair Display
```

New, additive board tokens:
```css
--cork: #3d3229            /* board background, subtle texture, not busy */
--card-paper: #f4f1e8      /* card face — warm off-white */
--card-paper-alt: #ece7d8  /* alternate card shade */
--pin-tier1: #2ecc71  --pin-tier2: #f59e0b  --pin-tier3: #9aa0a6  --pin-unverified: #ef4444
--string-red: #c0392b
```

Rules: cork texture subtle, card rotation capped at ±5°, tight realistic drop shadow only. The board reads as confident and a little playful, never cluttered.

---

## 16. Complete Technology Stack

| Layer | Choice | Cost |
|---|---|---|
| Backend framework | FastAPI + uvicorn (unchanged) | Free |
| Relational storage | SQLite via aiosqlite (unchanged) | Free |
| Crawler | trafilatura + BeautifulSoup fallback (unchanged) | Free |
| RSS ingestion | feedparser + httpx (unchanged) | Free |
| Real web search | SearXNG, self-hosted | Free (own infra) |
| Entity/relation extraction | GLiNER | Free |
| Deduplication | SimHash (first pass) + `nomic-embed-text` embeddings (second pass) | Free |
| Temporal knowledge graph | Graphiti | Free |
| Graph database backend | FalkorDB (FalkorDB Lite once stable) | Free |
| Narrative threading | Story Forest algorithm (self-implemented, no library) | — |
| Fact verification | MiniCheck (RoBERTa-large or DeBERTa-v3-large variant) | Free |
| Local LLM inference | LM Studio (existing) | Free |
| Frontend | React 18 + Vite + React Router v6 (unchanged) | Free |
| Visualization | D3.js (unchanged, reused for board layout) | Free |
| Streaming | SSE via `sse-starlette` (unchanged, extended) | Free |

Everything in this stack is free and open source, runs locally, and requires no paid API — consistent with the project's original constraint.

---

## 17. Annotated Reading List

- Rasmussen, Paliychuk, Beauvais, Ryan, Chalef — *Zep: A Temporal Knowledge Graph Architecture for Agent Memory* — arXiv:2501.13956 (the paper behind Graphiti)
- Liu et al. — *Growing Story Forest Online from Massive Breaking News* — arXiv:1803.00189 / ACM TKDD 2020 extended version
- Nallapati, Feng, Peng, Allan — *Event Threading within News Topics* — ACM CIKM 2004 (classic TDT formulation)
- Zaratiana, Tomeh, Holat, Charnois — *GLiNER: Generalist Model for Named Entity Recognition* — NAACL 2024 — github.com/urchade/GLiNER
- Tang, Laban, Durrett — *MiniCheck: Efficient Fact-Checking of LLMs on Grounding Documents* — EMNLP 2024 — github.com/Liyan06/MiniCheck
- ACL 2025 (NAACL-industry) — *Benchmarking Near-Duplicate Detection in the era of Pay-walled News* (NDD-NS dataset, SimHash vs MinHash-LSH on exactly this problem)
- Silcock et al. — RETSim neural near-duplicate detection, benchmarked on the NEWS-COPY dataset (future upgrade path for dedup)
- getzep/graphiti — github.com/getzep/graphiti
- FalkorDB — falkordb.com / github.com/FalkorDB/FalkorDB
- SearXNG — github.com/searxng/searxng
- GDELT Project (supplementary event data, not ground truth — see risks) — gdeltproject.org

---

## 18. Known Risks & Honest Limitations

- **No off-the-shelf project already does this.** A search for existing open-source "AI news app with a knowledge graph" turned up knowledge-graph tooling and news-summarizer tools separately, but nothing combining continuous news ingestion with a temporal graph and story-threading the way this spec describes. This is genuine assembly of separate, individually mature building blocks, not a shortcut — expect real integration work, especially at the Graphiti/GLiNER/clustering boundary.
- **Graphiti + small local models.** As covered in 6.2, Graphiti's raw-text extraction path is documented to be unreliable with smaller/local LLMs. The JSON-episode workaround (feed pre-extracted GLiNER output instead of raw text) is the mitigation, but it hasn't been battle-tested at your specific scale — budget time to validate it early rather than assuming it works out of the box.
- **GDELT accuracy.** If used as a supplementary/bootstrap data source, an independent 2025 study found its key-field accuracy around 55%, with ~20% redundancy. Treat it as a cross-check signal, never as ground truth.
- **FalkorDB Lite is new.** As of this research, the embedded/no-server variant is landing but not confirmed stable — start with a regular FalkorDB Docker container and migrate once Lite is proven.
- **Story Forest has no library.** The threading algorithm is implementation work based on a paper, not a dependency you install — scope accordingly.

---

## 19. Definition of Done — Fix Map

| Original complaint | Requirement | Acceptance test |
|---|---|---|
| Feed is empty | Onboarding + pre-warm scheduler | Fresh install → onboarding → board has 15+ pinned stories within ~90s, no manual search |
| "Not researching" / search loads nothing | Live agent console, structured SSE events | Every trigger streams real, per-step events — never a bare spinner |
| Search should search the web | SearXNG integration | A query with no RSS match still returns real, freshly crawled results |
| Autonomous, more on request | Depth tiers + Dig Deeper | Every story shows its depth tier; Dig Deeper adds sources/events without duplicating the card |
| Connections between news, not isolated articles | Graphiti temporal graph + Story Forest threading | A follow-up story to an existing cluster is added to that cluster's timeline, not created as a new disconnected card |
| No hallucinations, correct summaries | MiniCheck verification pass, span-level citation | Every displayed sentence is checked against its cited source before rendering |
| 100s of trusted sources, which data to take | Expanded, tiered, editable source registry | 100+ curated domains, browsable in Settings; unknown domains get a distinct "unverified" tier |
| Bunch of timelines, then a mind map | Multi-timeline synthesis + cluster mind map | Tier 3 stories show 2–4 thread timelines plus one cluster-level mind map |
| See everything the AI is doing, live | Persisted, replayable `AgentEvent` log | "How this was researched" tab shows the full real pipeline trace for any story |
| Pinboard, criminal-board feel | Investigation Board UI | Pinned, tilted, trust-colored cards connected by string; Clean-view fallback available |
| Fun to read, not timepass | Board interactions carry the "fun"; content stays calm | No fun added at the cost of readability inside the Case File |

---

## 20. Build Order

1. Backend: `AgentEvent` model + SSE stream, retrofit pipeline stages to emit structured events.
2. Frontend: `AgentConsole.jsx` wired to the stream, dropped into the existing V1 story page as a sanity check.
3. Backend: `services/entity_extraction.py` (GLiNER) + `services/dedup.py` (SimHash + embeddings) — these have no dependency on the graph and can be validated standalone first.
4. Backend: `services/graph_client.py` (Graphiti + FalkorDB), tested with hand-built JSON episodes before wiring to real extraction output.
5. Backend: wire GLiNER output → JSON episodes → Graphiti, validate against the small-model risk in §18 before building anything on top.
6. Backend: `services/clustering.py` (Story Forest-style threading, consuming graph queries) + `StoryCluster`/`Connection`/`TimelineThread` models.
7. Backend: pre-warm scheduler + `UserPreferences` + onboarding endpoints.
8. Frontend: `Onboarding.jsx`.
9. Backend: `search_client.py` (SearXNG) wired into the pipeline as a pre-crawl step.
10. Backend: `board_layout.py` + `BoardPin` persistence.
11. Frontend: `BoardCanvas.jsx` + card/string components, replacing `Home.jsx`.
12. Backend: `services/verification.py` (MiniCheck) wired into the synthesis stage.
13. Backend: depth tiers + Dig Deeper endpoint.
14. Frontend: `CaseFile.jsx` layered disclosure, citation hover cards, "How this was researched" tab.
15. Trust registry expansion (100+ domains) + Settings page updates.
16. Polish pass: pin-drop/string-draw animations, Clean-view toggle, mobile responsive board fallback.

---

*End of specification. Hand this document to the build agent in full.*
