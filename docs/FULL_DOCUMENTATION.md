# ContextLens — Full Technical Documentation

*Generated 2026-09-22. Covers system design, every workflow, every AI model, the full backend and frontend, database schema, configuration, and tests, as of the v5 codebase at `D:\newaai`.*

This is the deep-reference companion to [README.md](../README.md) (product pitch, quick start) and [RUN.md](../RUN.md) (day-to-day operation, troubleshooting). Those stay the entry points; this document exists so any part of the system can be understood without reading source. See also [docs/PLAN_v4.md](PLAN_v4.md) (v4 design rationale) and [docs/RESEARCH_v5.md](RESEARCH_v5.md) (v5 reader-research rationale).

---

## Table of contents

1. [What this is](#1-what-this-is)
2. [System architecture](#2-system-architecture)
3. [The feed pipeline — end to end](#3-the-feed-pipeline--end-to-end)
4. [Every AI model used](#4-every-ai-model-used)
5. [Backend services reference](#5-backend-services-reference)
6. [Backend routers / API reference](#6-backend-routers--api-reference)
7. [Database schema](#7-database-schema)
8. [Configuration & environment](#8-configuration--environment)
9. [Frontend architecture](#9-frontend-architecture)
10. [Major workflows, step by step](#10-major-workflows-step-by-step)
11. [Design rules the codebase follows](#11-design-rules-the-codebase-follows)
12. [Tests](#12-tests)
13. [Honest limits](#13-honest-limits)

---

## 1. What this is

**ContextLens** is a local-first news reader that explains, not just reports. It ingests ~230 RSS feeds, clusters articles into events, and — instead of just summarizing — builds for each story a plain-language *why-chain* (a causal chain, oldest cause first, each step tied to the exact source sentence it came from), a dated history timeline, an entity/relationship map, and a live voice assistant ("Lens") that can talk through any story.

It runs entirely on one machine: **FastAPI + SQLite** backend, **React 19 + Vite** frontend, **LM Studio** for local LLMs, **SearXNG** for web search. Nothing about a user's reading leaves the machine except the outbound RSS/SearXNG/GDELT/Wikipedia HTTP calls the pipeline itself makes to fetch news.

Repository layout:

```
backend/   FastAPI + SQLite (aiosqlite). services/ (26 modules), routers/ (11 modules)
           feeds_extra.json (probed feeds) · trusted_sources.json (outlets, trust tiers)
frontend/  React 19 + Vite + d3, plain-CSS design system (index.css + v4.css + v5.css), PWA
scripts/   eval_feed.py, bench_editor.py, backfill_extras.py — offline evaluation helpers
docs/      PLAN_v4.md, RESEARCH_v5.md, this file, archive/ (older specs)
contextlens.db            live SQLite database (WAL mode)
contextlens.v2-legacy.db  auto-preserved v2 database (never overwritten, never deleted)
start.ps1                 launches backend (and frontend dev server / build)
```

---

## 2. System architecture

### 2.1 Component diagram (text form)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  LM Studio (localhost:1235)          SearXNG (localhost:8080, Docker)       │
│  - writer model (chat)               - JSON news search                     │
│  - editor model (chat)                                                      │
│  - embedding model (always resident) │  GDELT Doc API (external, rate-ltd)  │
│  - `lms` CLI for load/unload         │  Wikipedia REST API (external)       │
└───────────────┬───────────────────────────────────┬─────────────────────────┘
                │ OpenAI-compatible HTTP             │ HTTP
                ▼                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Backend — FastAPI process (single port, e.g. :8000)                        │
│                                                                               │
│  main.py — app, lifespan (init DB, start scheduler, health check),          │
│             CORS, "reader-first" priority middleware, SPA static serving    │
│                                                                               │
│  routers/  (11 modules — thin HTTP layer, see §6)                           │
│      ask · feed · follow · inspect · kb · papers · profile ·                │
│      research · settings · sources · stories · voice                       │
│                                                                               │
│  services/ (26 modules — all business logic, see §5)                        │
│      llm · lms          → LLM client + one-model-at-a-time GPU manager      │
│      pipeline           → orchestrates the whole feed cycle                 │
│      feeds · clustering · ranking · editor · story_builder                 │
│      explainer · background · timeline · graph · glossary · lens           │
│      kb · context · interests · inspector · dives · paper · research       │
│      verify                → grounding checks used by almost everything    │
│      runs · scheduler      → progress tracking + the 120s auto-refresh tick│
│      ner_client / ner_worker → isolated GLiNER subprocess                  │
│      voice                 → talker/reasoner live assistant                │
│                                                                               │
│  db.py — aiosqlite/WAL, schema + migrations, JSON (de)serialization         │
│  config.py — static defaults, feed list, topic vocabulary, trust sources    │
│  settings_store.py — runtime-editable settings layered over config, in kv   │
└───────────────┬───────────────────────────────────────────────────────────────┘
                │ REST + SSE, same-origin (backend serves the built frontend)
                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Frontend — React 19 SPA (frontend/dist, served by FastAPI in production;   │
│              Vite dev server on :5173 with an /api proxy during development)│
│                                                                               │
│  App.jsx (routing) → Shell.jsx (nav/theme/toasts/global context)            │
│  pages/ (14)  Today · Story · Flow · Live · All · Sources · Explore ·       │
│               Papers · PaperView · Atlas · Entity · Saved · Settings ·      │
│               Onboarding                                                    │
│  components/ (11)  Inspector · Explain · StoryMap · Timeline · StoryCards · │
│                     ChatPanel · AskBriefing · RunPanel · ListenBar ·        │
│                     Palette · Icons                                        │
│  lib/ (5)  api.js (REST client) · hooks.js · format.js · speech.js · voice.js│
│  Service worker (public/sw.js) — offline shell + cached story data          │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Why one model at a time

The target machine is a single small GPU (RTX 4050, 6 GB VRAM — see the user's hardware memory). Two chat LLMs cannot be resident together. `services/lms.py` wraps LM Studio's `lms` CLI: before every chat call, `services/llm.py` calls `lms.ensure(model)`, which unloads every *other* loaded chat model and loads the wanted one (`--gpu max --context-length 8192 --parallel 1`). The ~80 MB embedding model is exempt and stays resident. Work is deliberately batched (triage in batches, then one story at a time) to keep swaps rare. `DISABLE_LMS=1` turns this off entirely for tests, so the test suite never touches a developer's real LM Studio instance.

### 2.3 Priority: readers before background work

A global FastAPI middleware (`main.reader_first`) marks every incoming HTTP request as **interactive** via a context variable. `services/llm.py`'s `_Gate` is a custom async priority semaphore: exactly one chat call is in flight at a time, and interactive (reader-facing) requests are served ahead of background pipeline work, using a priority heap. This is why opening an unbuilt story explains it "right away — it jumps the queue" (per RUN.md), even while a scheduled feed refresh is running in the background.

### 2.4 Fail-closed, grounded-by-construction design

Every generative step is validated against the source text it claims to come from, using `services/verify.py`:
- `ground(claims, source_text)` — recall-based support scoring against the source, flags unsupported numbers/names.
- `check_claim` — a looser grounding check used for causal/explanatory sentences.
- `provenance(claims, sources)` — maps each summary sentence to the exact source sentence it came from ("show the sentence").

Generation that fails these checks is dropped, not shown with a caveat — "gates fail closed." Timelines require an explicit date-bearing quote actually present in the source text (`timeline.quote_in_text`); the model never picks dates from its own knowledge. Relation/graph extraction constrains the LLM's JSON schema so `subject`/`object` fields are an `enum` of only the entities already extracted — the model cannot invent new graph nodes.

---

## 3. The feed pipeline — end to end

This is the single most important workflow: it runs on every scheduled refresh (every `refresh_minutes`, default 60) and on manual refresh, orchestrated by `services/pipeline.py`.

```
1. INGEST         feeds.ingest()             224 RSS feeds fetched concurrently (semaphore 12),
                                              junk/opinion/live-blog titles filtered, deduped by
                                              normalized URL hash, inserted into `articles`.

2. EMBED           llm.embed() via            new articles get embedding vectors (nomic-embed,
                    clustering.embed_new_      search_document: prefix), stored as BLOBs.
                    articles()

3. CLUSTER          clustering.run_           same-event articles grouped by cosine similarity
                    clustering()               (≥0.90 alone, or ≥0.79 + shared specific anchor
                                                like a proper noun/number). A second pass
                                                (`consolidate`) merges clusters that turn out to
                                                be the same story — survivor absorbs matches,
                                                never chains through an intermediate cluster.

4. SCORE            ranking.score_cluster()    deterministic (no LLM) score per cluster:
                                                0.42·interest + 0.10·topic + 0.18·coverage +
                                                0.06·trust + 0.14·recency + 0.10·taste(±).
                     ranking.mmr_select()       Maximal Marginal Relevance (λ=0.72) picks a
                                                diverse candidate pool, not just top-score.

5. WIRE TIER         pipeline.persist_wire()    top `wire_size` (default 200) clusters become
                                                instant, LLM-free stories: cleaned outlet
                                                headline, best snippet as dek, image. Shown on
                                                the front page within seconds of ingest.

6. BUILD QUEUE        pipeline.write_phase()    for the best `build_budget` (default 30)
                       (batches of 4, ordered     candidates, in rank order:
                       by rank)
   a. editor.triage()         — editor model classifies story_type/impact/matches_reader/
                                 reader_value; a DETERMINISTIC function (decide_triage) applies
                                 hard/soft vetoes and a strictness threshold — the model informs,
                                 code decides.
   b. story_builder.write_card()  — writer model drafts headline/dek/what_happened/key_points/
                                     why_it_matters/tags from up to 5 full-text sources.
   c. graph.extract_entities()    — GLiNER NER (isolated subprocess) or regex fallback.
   d. kb.thread_history()         — pulls this story's thread (same evolving story across days).
   e. background.build_story_timeline() — merges dated-in-article events (timeline.py) with
                                            earlier-coverage events found via own KB +
                                            SearXNG + GDELT, dated by PUBLICATION date, never
                                            invented.
   f. context.background_for()    — a short Wikipedia primer on the story's main entity.
   g. explainer.write_explainer() — the core differentiator: 2-5 step WHY chain (oldest cause
                                     first), each step tied to a fact id or capped/labelled
                                     "general knowledge"; glossary terms; grounded "what next";
                                     who's who. Validated against filler-phrase banlist,
                                     restated-headline gist, unsupported claims, duplicate steps.
   h. editor.publish_gate()       — editor model audits the drafted card + explainer; combined
                                     deterministically with the `verify.ground()` score to set
                                     the final tier: primary | more | rejected.

7. PERSIST            pipeline.persist_built()  one DB transaction: stories, story_sources,
                                                timeline_events, entities.

8. KB LINKING          kb.assign_thread()        for primary/more tier stories only:
                        kb.register_entities()   - assign to a thread (or create one)
                        kb.link_related()         - upsert canonical entities
                                                    - link to related stories by shared entities

9. HOUSEKEEPING         pipeline._cycle()         delete stale wire/rejected stories and orphaned
                                                   clusters/articles older than 7 days.
```

A reader opening any wire-tier story before the queue reaches it triggers `pipeline.build_now(story_id)` — the same write_phase, at interactive priority, immediately. It is designed to never end "rejected": if the editor would reject it, it falls back to tier "more" with the rejection reason recorded, because a story the reader explicitly asked for should never disappear.

**Enrichment pass** (`pipeline.enrich_modules`): after primary stories are visible, a slower separate pass extracts evidenced entity *relations* (not just entities) for the story map, gated by `graph.gate` (needs ≥4 connected entities and ≥3 evidenced relations, else the UI shows a plain "Key players" list instead of a graph).

---

## 4. Every AI model used

| Model | Role | Configured via | Where called | Notes |
|---|---|---|---|---|
| **`meta-llama-3-8b-instruct`** (default) | **Writer** — headlines, story cards, why-chains, timelines, chat, deep dives, voice answers, research reports | `.env MODEL_WRITER`; `config.DEFAULT_SETTINGS["model_writer"]`; editable in Settings → Models | `llm.chat/chat_json/stream_chat(role="writer", ...)` from `story_builder`, `explainer`, `background`, `timeline`, `graph`, `research`, `voice`, `dives`, `inspector` | Any OpenAI-compatible model works; this is just the shipped default |
| **`google/gemma-3n-e4b`** (default) | **Editor** — independent second judge (triage + publish gate) | `.env MODEL_EDITOR`; `config.DEFAULT_SETTINGS["model_editor"]` | `editor.triage()`, `editor.publish_gate()` via `llm.chat_json(role="editor", ...)` | Deliberately a *different model family* from the writer ("two-model editorial gate"); falls back to the writer model if `single_model_mode` is on |
| **`text-embedding-nomic-embed-text-v1.5`** (default) | **Embeddings** — clustering, ranking, threading, retrieval, similarity search everywhere | `.env MODEL_EMBED`; `config.DEFAULT_SETTINGS["model_embed"]` | `llm.embed(texts, kind)` | nomic-style `search_query:`/`search_document:` prefixes; batched 24 at a time, SHA1-cached in memory; kept resident alongside whichever chat model is loaded (~80 MB) |
| **`all-MiniLM-L6-v2`** (sentence-transformers) | **Embedding fallback** | hardcoded in `llm._minilm_embed`; only used if `EMBED_FALLBACK=1` and the primary embedder is unreachable | `llm.embed()` fallback path | Loaded locally via `sentence_transformers`; no GPU/LM Studio dependency; ~1 GB RAM |
| **`urchade/gliner_small-v2.1`** (GLiNER) | **Named-entity recognition** — person/organization/country/city/government agency/company/political party/event/law | hardcoded in `services/ner_worker.py` | `ner_client.predict()` → isolated subprocess `ner_worker.py` | Runs in a separate OS process so a native GLiNER/torch crash or OOM cannot take down the API server; toggled by the `ner_enabled` setting; regex fallback (`graph._CAP`) when off or unavailable |

**Model endpoint**: any OpenAI-compatible server — `LLM_BASE_URL` (default `http://localhost:1235/v1`, LM Studio), `LLM_API_KEY` (default `lm-studio`, LM Studio ignores the value). The pipeline never hardcodes LM Studio specifically at the API-call layer; only the `lms` CLI manager (§2.2) is LM-Studio-specific, and it degrades to a no-op if the CLI isn't found.

**Model selection mechanism**: model *ids* are a runtime setting (`kv` table, key `"settings"`, layered over `config.DEFAULT_SETTINGS` by `settings_store.py`) — changeable live from Settings → Models, no restart needed. `llm.resolve_model(role)` fuzzy-matches the configured id against `GET /v1/models`.

**Structured generation**: nearly every LLM call uses `response_format: json_schema` (grammar-constrained decoding) via `llm.chat_json(role, system, user, schema, validate, retries)`, with a fallback to prompt-only JSON parsing if the server rejects `response_format`, and up to `retries` re-prompts carrying the validation error back to the model.

---

## 5. Backend services reference

`backend/services/` — 26 modules. Everything here is pure business logic; routers only translate HTTP ⇄ these functions.

### Core infrastructure

**`llm.py`** — the only place that talks to the LLM/embedding server.
- `_Gate`: custom async priority semaphore (interactive priority 0, background priority 1) serializing all chat calls onto the one loaded model.
- `resolve_model(role)`: settings → model id, with single-model-mode collapsing editor→writer.
- `_ensure_resident(model)`: calls `lms.ensure()` before every chat call if `manage_models` is on.
- `chat` / `chat_json` / `stream_chat`: the three call shapes — plain, schema-validated-with-retry, and streaming (strips `<think>` reasoning spans mid-stream, retries once if the model was unloaded underneath).
- `embed(texts, kind)`: batched, cached, retried; falls back to local MiniLM if configured.
- `to_blob` / `from_blob`: numpy float32 ⇄ SQLite BLOB for embeddings/centroids.

**`lms.py`** — the one-model-at-a-time GPU manager. `find_cli()` locates the `lms` binary (`LMS_PATH` env, `PATH`, or `~/.lmstudio/bin/lms[.exe]`; `DISABLE_LMS=1` disables it entirely for tests). `ensure(model_id, ttl_seconds)` unloads every other loaded LLM then loads the wanted one (`--gpu max --context-length 8192 --parallel 1 -y`). `unload_all_chat()` runs on app shutdown to free the GPU. `status()` powers Settings → Models' live "loaded models" view.

**`verify.py`** — the grounding toolkit used almost everywhere generation happens: `ground(claims, source_text)` (recall-based support + missing-number/name checks), `provenance(claims, sources)` ("show the sentence"), `check_claim` (looser variant for causal sentences with connective words not literally in the source).

**`runs.py`** — `Run` class: progress tracking persisted to `runs`/`run_events`, fanned out live to SSE subscribers via in-memory `asyncio.Queue`s. Powers every progress bar (`RunPanel` on the frontend).

**`scheduler.py`** — background asyncio task ticking every 120s; triggers `pipeline.run_feed_cycle("scheduled")` when `auto_refresh` is on and `refresh_minutes` have elapsed. `DISABLE_SCHEDULER=1` disables it (tests).

### The pipeline

**`pipeline.py`** — orchestrator, see §3 in full.

**`feeds.py`** — RSS ingestion via `feedparser` (parsed from raw bytes to avoid encoding/mojibake bugs). Filters junk/opinion/live-blog titles before anything is embedded. Resolves outlet trust tier from `trusted_sources.json`. Fetches all feeds concurrently (semaphore 12), dedupes by normalized-URL hash.

**`clustering.py`** — pure, unit-tested clustering functions. `anchors(title)` extracts proper-noun/number tokens. `should_merge(sim, a, b, generic)`: cosine ≥0.90 alone, or ≥0.79 plus a shared specific (non-generic) anchor. `consolidate`/`plan_merges`: a second pass folding clusters that turn out to be the same story — rank-ordered survivor absorption, never chains through an intermediate cluster (this is the v5 fix that merged 27 duplicate stories in one refresh, e.g. an IIT Bombay story going from 4 separate stories to 1 with 16 outlets).

**`ranking.py`** — deterministic scoring: `score_cluster` (interest/topic/coverage/trust/recency/taste weighted sum), `coverage_score` (log-scaled by outlet count), `mmr_select` (vectorized Maximal Marginal Relevance, λ=0.72, for feed diversity, O(k·n) via numpy).

**`editor.py`** — the independent second model's harness. `triage()` (editor model classifies story_type/impact/matches_reader/reader_value) feeding `decide_triage` (deterministic hard/soft vetoes + strictness threshold). `publish_gate()` (editor audits the drafted card) feeding `decide_publish` (combined with `verify.ground()` score to set tier primary/more/rejected). `validate_headline`: rejects garbage/clickbait/all-caps/unsupported-number headlines.

**`story_builder.py`** — builds the story "card". `gather()` fetches full text for up to `DEPTH_ARTICLES[depth]` articles (best-trust, one per domain). `write_card()` calls the writer model, validates, and grounding-scores. Computes claim-level provenance mapping each summary sentence to its exact source sentence.

**`extract.py`** — full-text extraction via **trafilatura** (primary) with a **BeautifulSoup** fallback. Per-domain rate limiting (0.6s), paywall keyword detection, results cached in `articles.full_text`/`is_paywall`.

### The explainer (the core differentiator)

**`explainer.py`** — produces gist / why-chain / so_what / terms / next / who.
- `fact_sheet()`: scores and selects the most informative, non-duplicate sentences across full-text sources (content-word overlap + causal-cue regex + presence of a number + early-position bonus).
- `write_explainer()`: builds a FACTS/EARLIER EVENTS/BACKGROUND/UPCOMING prompt, calls the writer model with a schema requiring 2–5 why-steps each with a fact id, source sentence, and `basis` tag (`reported`|`history`|`general`).
- Post-processing beyond the LLM's own validation: every why-step must trace to a real fact id (general-knowledge steps capped at 2, must introduce no new numbers/names); `next` items must be grounded in an actual upcoming-tense sentence; `who` entries must be traceable to visible text; banned-filler-phrase regex (`"significant implications"`, `"underscores"`, etc.); gist can't just restate the headline; duplicate why-steps rejected.
- `EXPLAIN_VERSION = 2` — bumping this triggers a rebuild of previously-built stories in the queue (used once already, per `db.py`'s startup fixup that strips unversioned explanations).

**`background.py`** — "how did we get here?" history retrieval, entirely by *finding earlier coverage*, never by extracting dates from today's article (the thing v3 got wrong).
- Two deterministic search queries (entity-led, headline-keyword-led) sent to SearXNG (`_searx`) and GDELT Doc API (`_gdelt`, rate-limited to ~1 request/5.6s).
- `_kb_candidates`: own DB — earlier stories in the same KB thread plus archived articles in a 45-day/embedding-similarity window.
- `find_history()`: merges all three sources, dedupes, filters by embedding similarity (≥0.66) + shared anchor, resolves missing dates (URL pattern → snippet regex → page `<meta>` tags), keeps items ≥8h older and within 120 days.
- `render_history_events()`: turns earlier items into one-sentence timeline captions via the writer model, validated against the source headline/snippet; falls back to raw headlines if the LLM is unavailable.
- `build_story_timeline()`: merges dated-in-article events (`timeline.py`) with rendered history events, thins to ≤7 entries at ≤2/day, applies `timeline.gate`.

**`timeline.py`** — extracts ONLY explicitly-dated events actually present in article text. Every returned event's `quote` must really occur in the article (fuzzy 4-gram match), and its `date_text` must appear inside that quote, resolved via `dateparser` relative to the article's own publish date, never in the future. `is_future_or_plan` flags scheduled/planned language (excluded — timelines record what happened). `gate()` requires ≥3 events on ≥2 distinct dates spanning ≥1 day.

**`graph.py`** — entity + evidenced-relation extraction for the story map. GLiNER labels: person, organization, country, city, government agency, company, political party, event, law. `_relation_schema`: the LLM's JSON schema constrains `subject`/`object` to an `enum` of already-extracted entity names — the model literally cannot invent a new node. `filter_relations` still double-checks the evidence sentence really occurs in the article and mentions both endpoints. `gate()`: needs ≥4 connected entities and ≥3 evidenced relations.

**`glossary.py`** — hand-written, deterministic dictionary of ~80 terms (FIR, repo rate, bond yield, Sensex, GDP, NATO, IIT, etc.), grouped by domain. `find_terms()` regex-matches in order of first appearance. The explainer only asks the model for terms not already covered here — this is why jargon definitions never hallucinate.

**`lens.py`** ("Coverage lens", fully deterministic) — per-outlet framing (own headline + lead sentence), sentences reported by only one outlet (novelty = 1 − shared-content-word overlap with other sources), which countries' press covered the story.

### Knowledge base & retrieval

**`kb.py`** — pure-embeddings linking layer, no LLM calls.
- `assign_thread()`: matches a story's centroid against *individual member story centroids* of existing threads (not a running mean, to avoid drift) within a 21-day window.
- `register_entities()`: upserts canonical `kb_entities`/`kb_mentions`, filtering junk (generic words, news-org names, single lowercase tokens).
- `link_related()`: bidirectional `story_links` between stories sharing ≥2 specific entities.
- `thread_history`, `related_stories`, `entity_page`, `ensure_summary` (lazy Wikipedia lookup), `search_entities`, `atlas` (entity co-mention graph for the Atlas page).

**`context.py`** — Wikipedia lead-text lookup for entity "background" primers. Deterministic, no LLM.

**`interests.py`** — the reader's interest profile (interests text, per-topic weights, muted keywords/outlets, followed items), stored in `kv`. `calibrate(sim)`: maps raw embedding cosine (0.62 floor, 0.76 ceiling, empirically measured on nomic-embed-v1.5) to a 0-1 interest score. `apply_feedback`/`get_taste`: Rocchio-style taste vector (positive minus 0.6×negative, decayed 0.95 per update) learned from like/dislike/save/dismiss.

**`inspector.py`** — backs "select any text → Explain / Why? / Who / Forensics / Ask".
- Deterministic language analysis: `language_flags` (hedge words, unnamed-source phrases, loaded vocabulary, attribution, quotes, numbers), `forensic_report` (cross-checks a claim against every source's full text for support/conflict, counts corroborating outlets).
- `archive_matches`: other outlets in the 7-day archive saying something semantically equivalent.
- LLM-backed: `explain_selection` (curated glossary first, else grounded explanation), `why_selection`, `who_selection` (KB lookup, else Wikipedia), `history_selection`.

**`dives.py`** — "go deeper" on-demand deep dives (why/history/views/next/who/term), cached in the `dives` table. Fetches fresh reporting beyond the story's own sources via SearXNG, builds a fact sheet, and drops any paragraph that fails grounding or introduces unseen numbers.

**`paper.py`** — newspaper PDF upload. Uses **PyMuPDF** to segment pages into headline/body blocks by font-size heuristics, column-aware reading order; filters ads/classifieds/puzzles. `classify_topic`/`score_article`: embeds and scores against the reader's interest profile. `process_paper()`: parse → split → score → persist → build the top-N matched articles immediately.

**`research.py`** — "Loop B": on-demand research briefings and claim fact-checks. Query → LLM query expansion (2-4 alternates) → parallel retrieval (own corpus + SearXNG + GDELT) → relevance filter → full-text fetch (max 2 per domain, hard-paywall list skipped for scraping) → cited sectioned report or claim verdict → per-section grounding check (sections scoring <0.5 dropped) → `settle_verdict` (a decisive claim verdict requires ≥2 independent cited outlets — deterministic downgrade even if the model wanted to be more confident) → optional timeline/graph at depth≥2.

### NER isolation

**`ner_client.py`** / **`ner_worker.py`** — the GLiNER model runs in a fully separate OS process (`python -m backend.services.ner_worker`), communicating over stdin/stdout JSON-lines. If it fails twice in a row, the client backs off for 10 minutes and callers fall back to regex extraction (`graph._CAP`). Rationale: a native torch/GLiNER crash or OOM must never take the API server down.

### Voice

**`voice.py`** — the talker/reasoner live assistant ("Lens"), see §10.6 for the full turn sequence.
- `classify()`: rule-based intent router (stop/hello/brief/next/deeper/history/why/future/who/question) — no model call.
- Speech shaping: `clean_for_speech`, `split_ready`/`speakable` (abbreviation-aware sentence splitting), `shorten`.
- `load_story`/`brief_line`/`why_lines`/`history_lines`: read directly out of already-stored `stories.extras.explain` / `timeline_events` — **zero LLM calls** for common intents, which is why they answer in ≈1s.
- `_retrieve`: cosine similarity of the question's embedding against story centroids (floor 0.62), with keyword and current-focus boosts.
- `_stream_answer`: streams the writer model, buffering into sentences and dropping any sentence that fails grounding **before it is spoken**.
- `_dig`: the reasoner — runs after the talker has already answered, searches the web, adds up to 3 new grounding-checked facts ("I dug a little deeper…").

---

## 6. Backend routers / API reference

All routes are mounted with prefix `/api` (research and voice further nest under `/api/research`, `/api/voice`).

| Router | Method & path | Purpose |
|---|---|---|
| **feed** | `GET /api/feed` | Front page: lead + primary/wire stories, ranked, split by tier, with a funnel stat and topic counts. Filters: `topic`, `calm` |
| | `GET /api/feed/all` | "Everything": paginated, filterable (topic/text/source/factcheck/days) list |
| | `POST /api/feed/refresh` | Kick off `pipeline.run_feed_cycle("manual")`, returns `run_id` |
| | `GET /api/runs`, `GET /api/runs/{id}` | Pipeline run history / detail |
| | `GET /api/runs/{id}/stream` (SSE) | Live pipeline progress |
| **stories** | `GET /api/stories/{id}` | Full story detail (sources, timeline, entity graph, thread, related, gate reasons) |
| | `POST /api/stories/{id}/build` | Explain a story now, interactive priority |
| | `POST /api/stories/{id}/feedback` | like/dislike/open/dismiss → feeds taste learning |
| | `POST/DELETE /api/stories/{id}/save`, `GET /api/saved` | Bookmarking |
| | `GET /api/search` | Multi-word AND search over headline/dek/summary/tags |
| | `GET /api/stories/{id}/chat`, `POST .../chat` (SSE) | Grounded per-story chat, `[1]`-style citations, persisted |
| | `POST /api/stories/{id}/explain` | Cached plain-language rewrite |
| **follow** | `POST/DELETE /api/stories/{id}/follow` | Follow/unfollow a story by centroid |
| | `GET /api/following` | Followed stories with newer related coverage (similarity ≥0.80) |
| | `POST /api/following/{id}/seen` | Mark updates seen |
| **inspect** | `POST /api/inspect` | Select-to-understand actions (explain/who/why/forensics/history), cached |
| | `POST/GET /api/stories/{id}/dive` | Start / fetch a deep dive |
| **kb** | `GET /api/atlas` | Entity co-mention graph |
| | `GET /api/entities/search`, `GET /api/entities/{id}` | Entity search / profile |
| | `POST/DELETE /api/entities/{id}/follow` | Follow an entity |
| | `GET /api/threads/{id}` | A thread's stories over time |
| **papers** | `POST /api/papers` | Upload a PDF newspaper (≤60 MB), background-processed |
| | `GET /api/papers`, `GET /api/papers/{id}`, `DELETE /api/papers/{id}` | List / detail / delete |
| | `POST /api/papers/{id}/articles/{article_id}/open` | Turn one paper article into a full story |
| | `POST /api/explain` | Explain a pasted link or text (SSRF-guarded) |
| **profile** | `GET /api/topics`, `GET/PATCH /api/profile` | Interest profile |
| | `POST /api/onboarding`, `GET /api/onboarding/status` | Onboarding flow |
| | `POST /api/profile/reset-taste` | Reset the learned taste vector |
| **research** | `POST /api/research` | Start a research briefing or claim fact-check (409 if one is running) |
| | `GET /api/research` | Recent research briefings with status |
| **settings** | `GET /api/health` | LLM availability + version + current settings |
| | `GET /api/models/status`, `POST /api/models/unload` | Model manager status / manual unload |
| | `GET/PATCH /api/settings` | Runtime settings |
| | `POST /api/settings/test` | Round-trip test a model role, with latency |
| | `GET /api/sources` | Full trusted-sources list |
| | `POST /api/data/clear-stories` | Wipe all story-related tables |
| | `GET /api/data/export` | Export profile/settings/saved/feedback |
| **sources** | `GET /api/sources/overview` | Grouped outlets with live 72h article/feed counts (Sources page) |
| **ask** | `POST /api/ask` (SSE) | RAG Q&A over the last 5 days of complete stories |
| **voice** | `POST /api/voice/turn` (SSE) | One Lens voice turn |
| | `GET /api/voice/suggest` | Prompt suggestions from today's top stories |

---

## 7. Database schema

SQLite via `aiosqlite`, WAL journal mode, `busy_timeout=30000`. Schema lives in `backend/db.py`, applied idempotently at startup (`init_db()`), with a small `MIGRATIONS` list that `ALTER TABLE ADD COLUMN`s new fields onto existing installs without a migration framework.

| Table | Key columns | Purpose |
|---|---|---|
| `kv` | `key` (PK), `value` (JSON) | Generic store: settings, profile, taste vector, last-run state |
| `articles` | `id`, `url_hash` (UNIQUE), `title`, `outlet`, `domain`, `trust_rank`, `topic`, `published_at`, `embedding` (BLOB), `cluster_id`, `full_text`, `is_paywall` | Raw ingested RSS items |
| `clusters` | `id`, `title`, `topic`, `outlet_count`, `centroid` (BLOB), `story_id`, `score`, `triage`, `merged` (JSON) | Same-event article groups |
| `stories` | `id`, `kind` (feed\|research\|paper\|link), `headline`, `dek`, `what_happened`, `why_it_matters`, `key_points` (JSON), `report` (JSON), `topic`, `tags` (JSON), `trust_score`, `interest_match`, `tier` (primary\|more\|wire\|rejected\|pending\|failed), `gate` (JSON), `verification` (JSON), `status`, `extras` (JSON — explain/lens/background), `centroid`, `built_at` | The core entity — every story in the system |
| `story_sources` | `story_id`, `n`, `url`, `outlet`, `trust_rank`, `excerpt`, `has_full_text` | Per-story attributed sources |
| `timeline_events` | `story_id`, `date`, `precision`, `text`, `quote`, `source_url`, `kind` (reported\|history) | Dated timeline entries |
| `entities` | `story_id`, `name`, `type`, `mentions` | Per-story entity mentions (GLiNER/regex output) |
| `relations` | `story_id`, `subject`, `relation`, `object`, `evidence`, `source_url` | Evidenced entity relations (story map) |
| `follows` | `id`, `story_id`, `centroid` (BLOB) | Followed stories |
| `feedback` | `story_id`, `action` (like\|dislike\|save\|open\|dismiss), `dwell_ms` | Reader signals → taste learning |
| `saved` | `story_id` (PK), `note` | Bookmarks |
| `messages` | `story_id`, `role`, `content` | Per-story chat history |
| `runs` / `run_events` | `id`, `kind`, `status`, `progress`, `stage`, `stats` (JSON) / `run_id`, `stage`, `level`, `message` | Pipeline run tracking + event log (SSE source) |
| `kb_entities` | `key` (UNIQUE), `name`, `type`, `summary`, `wiki_url`, `story_count` | Canonical knowledge-base entities |
| `kb_mentions` | `(entity_id, story_id)` PK, `mentions` | Entity ↔ story mention counts |
| `threads` | `id`, `title`, `centroid` (BLOB), `story_count` | Evolving story threads (same story across days) |
| `thread_stories` | `story_id` (PK), `thread_id`, `at` | Thread membership |
| `story_links` | `(a, b)` PK, `kind` (thread\|related), `score`, `reason` | Bidirectional related-story links |
| `dives` | `story_id`, `kind`, `key`, `payload` (JSON) — unique on `(story_id, kind, key)` | Cached deep-dive and inspector results |
| `papers` | `id`, `name`, `status`, `stats` (JSON) | Uploaded newspaper PDFs |
| `paper_articles` | `id`, `paper_id`, `headline`, `body`, `topic`, `interest`, `kind` (article\|skipped), `story_id` | Segmented articles from an uploaded PDF |

**Startup fixups** (`init_db()`): pre-wire-tier v3 stories are marked `built_at=updated_at` so they don't appear unbuilt; explanations written by an older, unvalidated explainer version (no `extras.explain.v` field) are stripped so the build queue regenerates them.

**Legacy DB handling** (`_sidestep_legacy_db`): if the existing `contextlens.db` has the old v2 schema (no `headline` column on `stories`), it's moved aside to `contextlens.v2-legacy.db` (never deleted) and a fresh v3+ database is created — this is why both files exist in the repo root today.

---

## 8. Configuration & environment

### `backend/config.py` — static defaults
- `DB_PATH` (default `<repo root>/contextlens.db`), `FRONTEND_DIST`.
- `LLM_BASE_URL` / `LLM_API_KEY` — any OpenAI-compatible endpoint (accepts legacy `LM_STUDIO_*` env names too).
- `SEARXNG_URL`.
- `DEFAULT_SETTINGS`: model ids, `single_model_mode`, `manage_models`, `idle_unload_minutes`, `feed_size` (14), `wire_size` (200), `build_budget` (30), `candidate_pool` (36), `max_age_hours` (48), `refresh_minutes` (60), `auto_refresh`, `strictness` (lenient\|balanced\|strict), `depth` (1-3), `reading_level` (simple\|general\|expert), `ner_enabled`, `min_signal`, `min_outlets_primary`.
- `STRICTNESS`: three named profiles with `triage_min`/`publish_min`/`topk_extra` thresholds.
- `DEPTH_ARTICLES`: `{1:2, 2:3, 3:5}` full-text articles fetched per story by depth.
- `TOPICS`: the 10-topic vocabulary (world, politics, business, markets, tech, science, health, india, sports, culture).
- `TRUSTED_SOURCES` / `BLOCKLIST_DOMAINS` / `FEEDS` (~60 hardcoded feeds, extended at import time with everything in `feeds_extra.json`).
- HTTP tuning: `FEED_TIMEOUT=12`, `CRAWL_TIMEOUT=15`, `USER_AGENT`, `CORS_ORIGINS`.

### `backend/settings_store.py` — runtime-editable layer
Settings a user can change live (Settings page) are layered over `DEFAULT_SETTINGS` and persisted in `kv["settings"]`. `_REV=5` with a reset list — bumping the revision resets specific keys (e.g. `wire_size`) to their new defaults even for existing installs. Every numeric setting is clamped to a sane range on update (e.g. `feed_size` 4–40, `refresh_minutes` 15–720).

### Environment variables
```
LLM_BASE_URL=http://localhost:1235/v1
LLM_API_KEY=lm-studio
MODEL_WRITER=meta-llama-3-8b-instruct
MODEL_EDITOR=google/gemma-3n-e4b
MODEL_EMBED=text-embedding-nomic-embed-text-v1.5
SEARXNG_URL=http://localhost:8080
DB_PATH=                 # optional override
DISABLE_SCHEDULER=1      # test isolation
DISABLE_LMS=1            # test isolation — never touch the real LM Studio
EMBED_FALLBACK=1         # allow local MiniLM embedding fallback
LMS_PATH=                # explicit path to lms.exe if not auto-discovered
```

### `backend/feeds_extra.json` / `trusted_sources.json`
- `feeds_extra.json`: flat array of `{"url", "outlet", "topic"}` — feeds probed live (parsed to 3+ items, fresh) and appended to `config.FEEDS`, deduplicated by URL.
- `trusted_sources.json`: flat array of `{"domain", "name", "trust_rank" (1=top), "type", "verified_by", "country"}` — drives trust-rank scoring, the Sources page grouping, and fact-checker tagging.

---

## 9. Frontend architecture

Stack: **React 19** (StrictMode, function components + hooks only), **React Router DOM v7** (`BrowserRouter`), **Vite 8**, plain hand-written CSS (three layered stylesheets, no CSS-in-JS/Tailwind), **d3 v7** for force-directed graphs, native **Web Speech API** for voice, a hand-rolled **service worker** for offline/PWA. No global state library — local `useState`/custom hooks plus one small React Context (`Shell`'s `useApp`).

### 9.1 Routing (`App.jsx`)

`/welcome` → `Onboarding` (outside the app shell). Everything else nests under `Shell` (persistent nav/theme/toasts), each route wrapped in a local error boundary:

| Path | Page | Purpose |
|---|---|---|
| `/` | `Today` | Home / daily briefing |
| `/story/:id` | `Story` | Story detail |
| `/flow` | `Flow` | Swipeable story deck |
| `/live` | `Live` | Talk-to-Lens voice assistant |
| `/all` | `All` | "Everything" unranked feed |
| `/sources` | `Sources` | Outlet directory |
| `/explore` | `Explore` | Discover (search/research/fact-check/ask) |
| `/papers`, `/papers/:id` | `Papers`, `PaperView` | Newspaper upload / breakdown |
| `/atlas`, `/entity/:id` | `Atlas`, `Entity` | Entity relationship map / profile |
| `/saved` | `Saved` | Bookmarks |
| `/settings` | `Settings` | Preferences (tabbed) |
| `*` | inline 404 | — |

### 9.2 Pages (`frontend/src/pages/`, 14 files)

- **`Today.jsx`** (`/`) — the home page: greeting header, reading-streak ring, topic pill filters + Calm mode toggle, time-budget briefing picker, funnel stats, hero + grid + per-topic wire rows, "Listen" (TTS), keyboard nav (j/k/Enter/l/x), sidebar (Talk to Lens, Following, Running stories, Upload a newspaper).
- **`Story.jsx`** (`/story/:id`) — the core explain page: embedded `Inspector` (select-to-understand), why-chain, glossary tooltips, "Go Deeper" dive panel, `StoryMap` + `Timeline`, thread/related/background cards, sourced coverage list, "How this story was checked" gate details, right-rail `ChatPanel`, Save/Follow/Listen/"Talk it through"/Share/Copy actions.
- **`Flow.jsx`** (`/flow`) — full-screen swipe deck through top stories, layer at a time (gist → why → history → what's next), touch/keyboard navigation.
- **`Live.jsx`** (`/live`) — Talk-to-Lens: animated mic-level orb, continuous listen/speak loop, text fallback, voice settings (language/voice/rate/barge-in), transcript with story cards and source chips, "Now discussing" panel with follow-up quick buttons.
- **`All.jsx`** (`/all`) — Everything: searchable/filterable full feed (topic/fact-check/outlet), debounced search, pagination.
- **`Atlas.jsx`** (`/atlas`) — d3 force-directed entity graph (7/21/90-day windows), colored by type, sized by story count.
- **`Entity.jsx`** (`/entity/:id`) — one entity's profile, related entities, chronological story list, follow.
- **`Explore.jsx`** (`/explore`) — Discover hub: `AskBriefing` (streaming Q&A over the briefing), Explain-an-article, Upload-a-newspaper, Research/Fact-check form.
- **`Papers.jsx`** / **`PaperView.jsx`** (`/papers`, `/papers/:id`) — PDF upload/drag-drop with live progress; per-newspaper article breakdown ("Worth your time" vs skipped, with reasons).
- **`Saved.jsx`** (`/saved`) — bookmarked stories list.
- **`Settings.jsx`** (`/settings`) — tabbed: Interests (weighted chips/sliders, mute lists, taste reset), Feed (cadence/budgets/strictness/depth/level), Models (writer/editor/embed pickers + test button, single-model mode, model-manager toggle, live loaded-model status), Sources (trust-tier table), Data (export/clear), Activity (run history).
- **`Sources.jsx`** (`/sources`) — outlet directory, grouped, searchable, live counts.
- **`Onboarding.jsx`** (`/welcome`) — 3-step wizard: topics → interests/muted-keywords/reading-level → first refresh with progress.

### 9.3 Reusable components (`frontend/src/components/`, 11 files)

`Shell` (nav rail, theme toggle, global `Ctrl/⌘+K` listener, `ListenBar`, toast host, the app-wide `useApp()` Context) · `Palette` (command palette: fuzzy page match + live story search + "Ask Lens") · `Explain` (`Term`/`TermText` jargon tooltips, `WhyChain`, `BuildStepper`, `GoDeeper`/`DivePanel`) · `Inspector` (floating select-to-understand toolbar + tabbed result panel, rule-based Forensics view) · `StoryMap` (d3 entity/relationship graph) · `Timeline` (dated milestone list) · `StoryCards` (`Hero`/`Card`/`WireRow`/`MoreRow`/`FeedSkeleton`) · `ChatPanel` (per-story streaming chat) · `AskBriefing` (whole-briefing streaming Q&A with `[S1]`-style citations) · `RunPanel` (generic pipeline progress bar/log, reused across Today/Story/Onboarding/Papers) · `ListenBar` (persistent TTS mini-player) · `Icons` (inline-SVG icon set).

### 9.4 `frontend/src/lib/` — non-visual logic

- **`api.js`** — REST client (~45 methods over `fetch('/api'+path)`), plus SSE helpers `streamRun`/`streamChat`/`streamAsk` and `uploadPaper` (multipart).
- **`hooks.js`** — `useAsync` (the primary data-fetching pattern, load/error/reload — a hand-rolled lightweight React Query), `useRun` (SSE progress), `useHealth`, `useToast`, `useReadLog` (localStorage streak tracking), `useModelStatus`.
- **`format.js`** — `timeAgo`, `formatDate`, topic/trust label lookups.
- **`speech.js`** — `useListen()`: on-device TTS queue (voice pick, chunking, play/pause/skip/rate).
- **`voice.js`** — `useVoice()`: the Live-page voice engine — `SpeechRecognition` (ears) + `speechSynthesis` (mouth) + `/api/voice/turn` SSE (brain), conversation state machine, barge-in, mic-level metering.

**State management**: no Redux/Zustand — server state fetched per-page via `useAsync`/`useRun`; the one shared global is `Shell`'s Context; persistent client-only state (theme, calm mode, TTS rate, read log, voice prefs) lives in `localStorage`, each with its own small hook.

### 9.5 CSS architecture

Three layered, additive plain-CSS files loaded in order: **`index.css`** (v3 foundation — the entire design-token set on `:root` plus a dark-theme override, layout/typography/buttons/cards/banners/settings/onboarding, and the "differentiators" section for signal/provenance/lens/background/follow/brief/listen UI) → **`v4.css`** ("Lumen" re-skin — warm paper/deep ink/one accent color, same class names, topic colors, nav rail, Today/Flow/Story/Inspector/Papers/Atlas/Discover) → **`v5.css`** (micro-interactions, nav search, the voice orb, Live page, Sources, Everything, command palette, Calm mode, fact-check styling). No CSS Modules — global selectors matched against dynamic `className` strings.

### 9.6 PWA / offline

Hand-rolled (no `vite-plugin-pwa`). `public/manifest.webmanifest` + `public/sw.js` (versioned, two cache names). Three caching strategies: navigations/`index.html` network-first (so app updates aren't stuck behind a stale cached shell); `/assets/*` (content-hashed build output) cache-first; `GET /api/feed|stories|saved|following|topics|profile` network-first-with-cache-fallback (this is what makes previously-opened stories readable offline). **SSE/streaming endpoints (`*/stream`, `*/chat`, `/api/ask`) are deliberately excluded from the fetch handler** — a documented gotcha: never let the service worker intercept a streaming endpoint. `main.jsx` only registers the SW in production builds (`import.meta.env.PROD`), never in `vite dev`.

---

## 10. Major workflows, step by step

### 10.1 Scheduled/manual feed refresh
`scheduler` ticks every 120s → if due, or a reader hits Refresh → `POST /api/feed/refresh` → `pipeline.run_feed_cycle("manual"|"scheduled")` (single-flight per kind) → the full §3 pipeline → `runs`/`run_events` stream live progress to any open `RunPanel` via SSE.

### 10.2 Opening a story that hasn't been explained yet
Reader clicks a wire-tier card → `Story.jsx` calls `api.build(id)` → `POST /api/stories/{id}/build` → `pipeline.build_now()` at **interactive priority** (jumps the background queue via the `_Gate`) → same write_phase as the queue, but for one story, streamed via `useRun`.

### 10.3 Select-to-understand (Inspector)
Reader highlights text on a Story page → `Inspector.jsx` shows a floating toolbar → chosen action (Explain/Why/Who/Forensics/Ask) → `POST /api/inspect` → `services/inspector.py` — either a deterministic rule-based pass (Forensics: hedges, unnamed sources, loaded words, cross-source support/conflict) or a grounded LLM call (Explain/Why/Who), cached in `dives` by content hash.

### 10.4 Newspaper upload
Drop a PDF on `/papers` → `POST /api/papers` (multipart, ≤60 MB) → background `paper.process_paper()`: PyMuPDF segmentation (headline/body by font size, column-aware reading order, ads/classifieds filtered) → per-article topic classification + interest scoring → `paper_articles` persisted → top-N matched articles built immediately via `pipeline.build_now`. Frontend polls/streams progress via `RunPanel`, then shows "Worth your time" vs skipped-with-reasons.

### 10.5 Explain a link or pasted text
`Explore.jsx` "Explain an article" → `POST /api/explain` ({url} or {text}) → SSRF-guarded fetch of the public URL (rejects private/loopback/link-local IPs) or the pasted text is used directly → `paper.create_story_from_text()` creates a wire story → immediately built (`pipeline.build_now`).

### 10.6 Research briefing / claim fact-check
`Explore.jsx` search form → `POST /api/research` (mode `research` or `claim`) → `research._research_task()`: writer-model query expansion (2-4 alternates) → parallel SearXNG + GDELT + own-corpus search → relevance filter (floor = best-match − 0.12, min 0.58) → iterative full-text fetch (max 2/domain) until enough readable sources → cited sectioned report (or claim verdict) from the writer model → per-section grounding check (`verify.ground`, drop sections <0.5) → for claims, `settle_verdict` deterministically requires ≥2 independent cited outlets before allowing a decisive verdict → optional timeline/graph at depth≥2 → persisted as `stories.kind="research"`.

### 10.7 Talk to Lens (live voice) — the talker/reasoner turn
1. Browser `SpeechRecognition` (or typed text) → `POST /api/voice/turn` (SSE).
2. `voice.classify()` routes the intent — no model call for routing itself.
3. **Talker**: for highlights/why/history/next/"what happened with X", the answer is read straight from already-stored `stories.extras.explain` / `timeline_events` (≈1s, zero model calls). For open-ended questions, `_retrieve()` finds the best-matching story by embedding, then `_stream_answer()` streams the writer model, checking and dropping any ungrounded sentence **before** it is spoken.
4. Events stream in order: `intent` → `card`/`focus` → `say` (sentence by sentence) → `sources` → `turn_done`.
5. **Reasoner** (if enabled): after `turn_done`, `_dig()` searches the web in the background, and if it finds new, grounded facts, emits a `dig` event ("I dug a little deeper…"). A new question cancels it.
6. Interruptible any time (Esc, or talk over it in headphone/barge-in mode).

### 10.8 Onboarding
`/welcome` wizard → pick ≥2 topics → free-text interests + muted keywords + reading level (`POST /api/onboarding`) → triggers the first `POST /api/feed/refresh` → progress shown via `RunPanel` → navigates to `/` once the wire tier is populated (a few minutes).

---

## 11. Design rules the codebase follows

From `README.md` and confirmed throughout the service layer:

1. **Every AI step is one of three shapes**: *classify* (schema-constrained, e.g. editor triage), *render* (no LLM at all — wire tier, glossary, coverage lens, timeline gating, voice talker fast-path), or *constrained-generate* (short, grounded, validated — why-chains, cards, timeline captions, dive answers).
2. **Gates fail closed** and store their reasons (`stories.gate`, `stories.verification` JSON) — a story that doesn't pass publish gate becomes tier `rejected` or `more`, never shown as if it passed.
3. **The model never chooses dates.** Timelines use article publication dates and dates quoted verbatim inside articles (`timeline.quote_in_text`), resolved via `dateparser`, never invented.
4. **Every generated claim traces to a source.** `verify.ground`/`check_claim`/`provenance` run after generation, not just as a prompt instruction — unsupported output is dropped programmatically.
5. **Two independent models for editorial judgment.** Writer (generates) and editor (judges) are meant to be different model families so the editor isn't just rubber-stamping its own output; `single_model_mode` exists as an explicit, visible degradation for weaker hardware.
6. **One chat model resident at a time**, readers always jump the queue ahead of background pipeline work.

---

## 12. Tests

`backend/tests/` — **111 test functions**, offline (never touch a real LM Studio instance): `DISABLE_LMS=1` and `DISABLE_SCHEDULER=1` are set in `conftest.py` before any backend module is imported.

| File | Lines | Covers |
|---|---|---|
| `conftest.py` | 13 | Test bootstrap: temp DB, disables scheduler + `lms` CLI |
| `test_api.py` | 109 | FastAPI integration: health, onboarding, profile/settings roundtrip + clamping, feed shape, story detail/save/search, `min_signal` filtering, 404s, graceful no-LLM behavior, `clear-stories`, SPA fallback routing |
| `test_features.py` | 85 | Signal score bounds/monotonicity, claim provenance + number matching, Coverage Lens single-outlet detection, claim-verdict two-outlet requirement, graph relation endpoint validation |
| `test_logic.py` | 257 | Thinking-tag stripping, garbage detection, JSON parsing, mojibake repair, URL normalization, junk-title filtering, clustering anchors/merge rules, coverage score, MMR diversity, headline validation, triage/publish-gate decision logic, grounding checks, quote-in-text, date resolution, timeline gating, entity canonicalization, market-chatter demotion |
| `test_v4.py` | 477 | Model manager unload/swap/degrade (`FakeLMS`), priority gate, glossary determinism, explainer validator rejections, history date-hints/grounding, timeline gating, cluster consolidation (never chains), thread joining, entity linking, forensics flags, PDF segmentation (incl. scanned PDFs), wire/build/inspect/upload/explain-link endpoints (incl. SSRF rejection), atlas/entities/models endpoints |
| `test_voice.py` | 411 | Voice intent classification, speech shaping, no-model-call fast paths, unbuilt-story on-demand build with talker fill, grounding-checked streaming, web reasoner adding only new info, model-outage handling (spoken, not thrown), voice suggestions, Sources/Everything filtering, Calm-mode classification, fact-check tagging, reload-and-retry-once on mid-call model unload, MMR reference-algorithm equivalence |

Run: `venv\Scripts\python -m pytest backend/tests`. Frontend: `cd frontend && npm run lint && npm run build` (no frontend test suite defined — `oxlint` is the linter, no Jest/Vitest present).

Offline evaluation scripts (`scripts/`): `eval_feed.py` (dry-runs the whole pipeline into a scratch DB), `bench_editor.py` (editor-gate benchmarking), `backfill_extras.py`.

---

## 13. Honest limits

(From README.md / RUN.md / PLAN_v4.md / RESEARCH_v5.md, consolidated.)

- 4–8B local models can still err; grounding checks, verbatim evidence, fail-closed gates, and visible reasons reduce this but don't remove it.
- Explaining a story takes several minutes on a laptop GPU — this is why the wire tier exists (instant, unexplained) and the build queue is progressive.
- Paywalled outlets (Reuters, WSJ, FT, Bloomberg, Economist, NYT, WaPo, Times of London, Telegraph, Barron's, Business Insider — `research.HARD_PAYWALL`) usually can't be scraped for full text; scanned/image-only PDFs need OCR, not bundled.
- Timelines exist only when earlier dated coverage or dated sentences are actually found; many stories legitimately have none.
- Voice *input* uses the browser's `SpeechRecognition` (Chrome/Edge only, needs internet — sends audio to the browser vendor). Typing always works. A local Whisper model would remove that dependency but isn't implemented.
- A question that needs the model waits for any model call already in flight on the shared GPU (up to ~30s during a feed build); highlights and already-explained stories are instant regardless.
- Calm mode is a keyword classifier, not a judgment call — it errs toward hiding.
- Trust tiers in `trusted_sources.json` are curated by hand — a starting point, not a verdict.
- PIB/RBI/SEBI feeds exist but carry no reliable dates, so they're intentionally not ingested (old releases would look new).
- Single user, local, by design. Hosted multi-user use would need accounts and a hosted model endpoint — the model layer already accepts any OpenAI-compatible API, so that swap is a settings change, not a rewrite.
