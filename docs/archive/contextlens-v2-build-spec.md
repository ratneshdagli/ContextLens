# ContextLens V2 — "The Board" — Full Rebuild Specification

> **How to use this document:** This is a complete build brief for an AI coding agent (e.g. Antigravity). Paste it in as-is. It assumes an existing ContextLens V1 codebase (FastAPI + React/Vite, local model runtime, SQLite, trafilatura crawler) and tells the agent exactly what to **keep**, what to **rip out and replace**, and what to **add**. It is not a from-scratch prompt — treat V1 as a partial skeleton to renovate, not a reference to preserve out of loyalty.

---

## Table of Contents

1. [What Was Broken, and Why](#1-what-was-broken-and-why)
2. [Product Vision V2](#2-product-vision-v2)
3. [The Big Flip — New UX Order](#3-the-big-flip--new-ux-order)
4. [Signature Visual Identity — The Investigation Board](#4-signature-visual-identity--the-investigation-board)
5. [Onboarding Flow](#5-onboarding-flow)
6. [The Live Agent Console — "Watch It Work"](#6-the-live-agent-console--watch-it-work)
7. [Research Pipeline V2](#7-research-pipeline-v2)
8. [Trust & Citation System](#8-trust--citation-system)
9. [Story Intelligence — Layered Disclosure](#9-story-intelligence--layered-disclosure)
10. [Pages & Navigation V2](#10-pages--navigation-v2)
11. [Data Model V2](#11-data-model-v2)
12. [Backend Architecture — Keep / Replace / Add](#12-backend-architecture--keep--replace--add)
13. [Frontend Architecture — Keep / Replace / Add](#13-frontend-architecture--keep--replace--add)
14. [Design Tokens V2](#14-design-tokens-v2)
15. [Definition of Done — Fix Map](#15-definition-of-done--fix-map)
16. [Build Order](#16-build-order)

---

## 1. What Was Broken, and Why

Diagnosis of the current build, so the agent doesn't just re-skin the same bugs:

| Symptom | Root cause in V1 |
|---|---|
| Home feed is empty | Research only ever runs when a user manually triggers it. There is no background job that pre-populates stories, so a fresh session (or one where nothing has been searched yet) has nothing to show. |
| Search "just loads, nothing comes" | Search is built on RSS-feed keyword matching plus an on-demand call to the local model runtime. If the model call is slow, unloaded, or the keyword match misses, there is no visible failure state — it silently produces nothing. |
| Feels like it isn't "researching" | The only progress signal is a percentage bar with a single generic message. There's no per-step visibility, so a stalled job and a working job look identical. |
| Looks basic / boring | The design system is a competent flat dark dashboard with one accent color. It's clean, but it has no identity — nothing that makes opening the app feel different from any other reading app. |
| No sense of depth or control | Research runs once, to one fixed depth, with no way to ask for more and no visible ceiling on how much was actually gathered. |

None of this needs a rewrite of the whole system — it needs the **trigger model**, the **progress model**, and the **visual identity** replaced. Section 12–13 spell out exactly what to keep.

---

## 2. Product Vision V2

ContextLens is a **research board that fills itself in**, not a search box waiting for input.

You open the app once, tell it what you care about, and it goes to work immediately — visibly — pulling in stories, crawling sources, building timelines, and pinning connected stories together. You watch it happen. Within a minute you have a board of real, sourced, connected stories on the topics you picked, each one explained in layers from "explain this like I just woke up" to full source documents.

Search still exists, but it's a way to go deeper on demand, not the front door.

The product should feel like **investigative work**, not like reading a feed. Evidence goes up on the board. Threads get connected with string. Nothing is hidden — you can always see what the AI looked at and why it believes what it's telling you.

---

## 3. The Big Flip — New UX Order

**Old flow (V1):**
```
Open app → empty home / RSS list → type a search → hope something loads
```

**New flow (V2):**
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
Board is populated: 15–25 story cards, clustered,
strung together by relationship                            (steady state — this is home from now on)
   │
   ▼
Click a card → Case File opens (layered story detail)
   │
   ▼
Want more on a topic not covered? → Search
   (now runs a real web search + full pipeline, same
   live visibility, adds new pins to the board)
   │
   ▼
Want more depth on any story? → "Dig Deeper"
   (re-runs pipeline at next depth tier, same story,
   board updates in place)
```

Key rule: **the board is never empty and never silent.** If it has nothing yet, it is actively shown building itself, not sitting blank.

---

## 4. Signature Visual Identity — The Investigation Board

This is the single biggest change and the thing that makes the app feel different from Google News / Inshort / any other feed.

### The metaphor

The home screen is a **corkboard**. Stories are **index cards pinned to it**. Related stories are connected with **string**. This is not decorative chrome — it *is* the information architecture: a pin's color encodes trust, a card's tilt and position encode recency/cluster, and string encodes exactly the "these are actually one story" connective tissue that document 2's critique identified as the whole problem with fragmented news.

**Important balance:** the *frame* is playful and spatial. The *content inside each card and inside the Case File* stays calm, high-contrast, editorial — same restrained typography as before. Corkboard is a container, not an excuse for clutter. Readability is never sacrificed for texture.

### Card anatomy

- Shape: index-card proportions (roughly 4:3), subtle paper texture, soft drop shadow (not glassmorphism — a real, small, matte shadow like paper on cork, not a blur).
- Random rotation: ±2° to ±5°, seeded by story id so it's stable across reloads (not re-randomized every render).
- A single pushpin icon top-center. **Pin color = trust tier**: green (tier 1), amber (tier 2), grey (tier 3), red-and-white striped ("unverified — new source, not yet trust-ranked").
- Card front shows: serif headline, one-line summary, a tiny cluster of source-count + freshness ("14 sources · 12m ago"), category chip.
- Cards belonging to the same story cluster are placed near each other and connected with a **red string** (SVG line, slight catenary sag, drawn with a quick animated "pull taut" effect when the connection is first made).
- Hovering a string highlights both endpoints and shows a one-line label of *why* they're connected (shared entity, causal, same event thread, market reaction).
- Clicking a card opens the Case File (story detail) as a slide-over, board stays visible underneath, dimmed.

### Board vs. Clean view

Provide a view toggle (persistent user preference):
- **Board view** (default): the corkboard, described above.
- **Clean view**: same stories, plain card grid, no cork/pin/string chrome — for users who want the calm original editorial layout, or for small screens where the board doesn't work.

Both views read from the same data — this is a rendering choice, not two separate systems.

### Layout mechanics

- Reuse D3 (already a dependency for the timeline and mind map — no new library needed) with a **force simulation** for board layout: cards repel each other slightly, connected cards attract along their string, clusters settle into loose neighborhoods. Run the simulation to convergence once on load/update, don't animate it continuously (matches existing "run synchronously to avoid flicker" pattern from `MindMapViz.jsx`).
- Persist settled positions per story (`BoardPin` table, section 11) so the board doesn't rearrange itself on every visit — new pins join the existing layout, they don't reshuffle it.

---

## 5. Onboarding Flow

Shown once, on first launch (skippable, revisitable from Settings).

**Step 1 — Interests.** Multi-select chips from the existing category list (Top, World, Politics, Business, Markets, Tech, Science, Health, India, Sports, Opinion). Minimum 3 selections required to proceed (fewer than that starves the board).

**Step 2 — Depth default.** Single choice:
- "Quick digest" — fast summaries, dig deeper only on request.
- "Give me everything" — deeper default research on every story, takes longer but board arrives fuller.
- "Balanced" (default) — quick digest on breaking news, deeper automatically on stories the pipeline judges complex or evolving (see depth heuristic, §7).

**Step 3 — Transition screen.** Not a spinner. Says plainly: *"Building your board — this takes about a minute. You'll see exactly what we're finding as we find it."* Then immediately routes into the Board screen with the live console already streaming (§6). This sets the expectation up front that a wait is coming and that it's not broken — directly answering "let it take time but I want everything visible."

Preferences are stored (`UserPreferences`, §11) and drive the background pre-warm scheduler (§7) from then on, on every subsequent app open, not just once.

---

## 6. The Live Agent Console — "Watch It Work"

This is the direct fix for "I don't think anything is working right now."

### What it looks like

A slim, persistent log panel (collapsible, bottom-of-screen or right-edge drawer) that streams structured events in something close to real time, styled like a dispatch log — monospace timestamps, short declarative lines:

```
10:42:03  Searching 112 trusted sources for "Fed rate decision"
10:42:04  Found 23 candidate articles across 9 domains
10:42:05  Crawling reuters.com/markets/fed-decision-2026 ...
10:42:06  Crawling apnews.com/fed-rate-hike ...
10:42:07  ✓ Extracted 1,840 words — reuters.com (tier 1)
10:42:08  ✓ Extracted 1,120 words — apnews.com (tier 1)
10:42:09  ✗ Paywall detected — wsj.com (keeping metadata only)
10:42:11  Deduplicating — removed 4 near-duplicate articles
10:42:12  Ranking 14 sources by trust + freshness
10:42:14  Synthesizing summary and timeline...
10:42:19  Timeline: 9 events, earliest 2025-01
10:42:20  Extracting entities...
10:42:22  Found 7 people, 3 organizations, 2 policies
10:42:23  Pinning to board — connecting to "US-India Tariffs" cluster
10:42:23  Done — 14 sources, trust score 0.87
```

Every line corresponds to something real that happened in the pipeline — no fake filler "thinking..." messages. This log is also **persisted per story** (`AgentEvent` table) and viewable later from the Case File as a "How this was researched" tab — turning the transparency feature into a permanent trust artifact, not just a loading animation.

### On the board itself

In parallel with the text log, cards physically **pin themselves onto the board** as their research completes (a quick 200–300ms "pin drop" animation), and strings **draw themselves** between related cards as connections are found. The text log and the board animation are driven by the same event stream — one backend concept, two renderings.

### Technical mechanism

- Extend the existing job model: instead of a bare `{progress: %, message: str}`, emit structured events: `{job_id, timestamp, stage, message, data}`.
- Stream via SSE (the stack already has `sse-starlette` for chat — reuse the same pattern for `/api/stories/job/{job_id}/events`).
- Frontend: a `useAgentEvents(jobId)` hook (sibling to existing `useChat.js`) that appends events to a local array and feeds both the log panel and the board's pin-drop trigger.

---

## 7. Research Pipeline V2

### Trigger model (replaces "only on manual search")

1. **Pre-warm scheduler** (new, background, runs on an interval — e.g. every 20–30 minutes, plus once immediately after onboarding): for each category in the user's preferences, pull trending headlines, pick the top N not already researched or stale, and run the pipeline at the default depth. This is what keeps the board populated without the user doing anything.
2. **Manual search** (existing entry point, fixed): now performs a **real web search**, not just RSS keyword matching (see below), then runs the same pipeline.
3. **Dig Deeper** (new): re-runs the pipeline for an existing story at the next depth tier, merging new sources/timeline events/entities into the existing story rather than replacing it. Board updates the existing card and its strings in place.

### Real web search (fixes "search should actually search the web")

RSS feeds only surface what's already in the 100+ trusted feeds — fine for pre-warming trending topics, not enough for an arbitrary user query. Add a dedicated search step:

- **Recommended:** self-hosted **SearXNG** (open-source metasearch, no external API dependency, stays consistent with the local-first / nothing-leaves-your-machine philosophy already established for the AI inference layer).
- **Fallback if self-hosting is inconvenient:** a search API with a usable free tier (Brave Search API or Tavily are reasonable defaults) — make this pluggable behind a `search_client.py` abstraction the same way `ollama_client.py` abstracts the model runtime, so it can be swapped without touching pipeline code.
- Search results outside the curated trusted-domain list should still be crawled and shown, but tagged with a **fourth trust tier: "Unverified — new source"** (striped red pin) rather than silently dropped. This keeps arbitrary queries useful while being honest about source quality (see §8).

### Depth tiers (fixes "there should be a limit, and autonomous, with more on request")

| Tier | Sources crawled | Output | When it's used |
|---|---|---|---|
| 1 — Quick | 3–5 | One-line + short summary, no timeline | Default for routine/low-complexity breaking items |
| 2 — Standard | 8–12 | Full summary, single timeline, key entities | Default for most stories under "Balanced" preference |
| 3 — Deep | 15–25 | Multiple timelines (see below), full mind map, glossary, controversy notes, deep analysis | Default under "Give me everything"; or reached via Dig Deeper; or auto-selected when the pipeline detects a story is part of an existing multi-story cluster (i.e. it's clearly an ongoing saga, not a one-off) |

The autonomous depth choice (which tier to start at) is decided at classification time using: story complexity signal (number of distinct sub-events found in initial search), whether it matches an existing cluster, and the user's depth preference as a bias, not a hard override.

### Multiple timelines + mind map (fixes "a bunch of timelines, then a mindmap")

For Tier 3 stories, generate:
- **Master timeline** — the full chronological arc.
- **Per-thread timelines** — 2–4 sub-timelines split by thread the synthesis step detects (e.g. for a tariff story: "Policy decisions", "Market reactions", "Diplomatic responses"). Each is a filtered view of the same event set, not independently generated data.
- **One mind map** tying the whole cluster together — this is the payoff screen after the timelines, exactly as described: read the threads, then see how everyone/everything connects.

Pipeline stage list (extends the existing 7-stage `research_pipeline.py`):

```
5%   Classify intent + depth
15%  Search (RSS + real web search)
30%  Crawl candidates (emits per-URL events to the console)
45%  Dedup + rank + assign trust tiers
55%  Cluster into story arc (new/existing StoryCluster)
70%  Synthesize summary + master timeline
80%  Synthesize per-thread timelines (Tier 3 only)
88%  Extract entities + build mind map
95%  Pin to board, draw connections
100% Done — persist AgentEvent log
```

---

## 8. Trust & Citation System

- Expand `TRUSTED_SOURCES` from the current list to a **100+ domain registry**, moved into an editable config file (JSON), organized by tier and category, so it can grow without a code change. Keep the existing 4-field shape (domain, trust rank, source type, category) and add a `verified_by: "curated" | "auto"` flag to distinguish the hand-picked list from domains discovered via web search that get provisionally tiered.
- Unknown domains found via real web search get **heuristically tiered** (checked against a blocklist of known low-quality/content-farm domains, defaulted to the new Tier 4 "Unverified" otherwise) rather than silently trusted or silently dropped.
- Every generated claim in a story (summary sentence, timeline event, entity fact) should be traceable to the specific source(s) it came from. UI: small superscript source markers inline in generated text, hover shows a source-preview card (domain, trust badge, publish date, the exact excerpt used). This is a citation *system*, not just a source list at the bottom.
- Case File keeps the existing "Sources" tab, but the trust badge becomes clickable → filters the board/timeline to only claims backed by that source.

---

## 9. Story Intelligence — Layered Disclosure

Carries forward the insight from your earlier conversation: summaries alone lose nuance, and news currently assumes prior knowledge it shouldn't. Structure every Case File as explicit, separately-revealed layers, not one long page:

1. **Explain like I just woke up** — 2–3 plain sentences, zero jargon, assumes nothing.
2. **Five-minute summary** — the existing "concise summary" field.
3. **Timeline(s)** — master + per-thread (Tier 3).
4. **Key people & organizations** — entity pills, click for a mini-bio card.
5. **Glossary** — every acronym/term used anywhere in the story, defined inline, chip-style, expandable.
6. **Deep analysis** — the existing "deep summary" field, collapsed by default.
7. **Sources & documents** — the citation system above, plus links to primary documents (filings, transcripts) when found.
8. **Controversy / what's disputed** — shown as a distinct section when it exists, never flattened into the main summary (existing amber-bordered pattern — keep it).

Each layer is collapsed by default except 1–3; the user expands what they want. This is the "progressive disclosure, like a game teaching mechanics" idea applied directly — don't dump everything, reveal on request.

Also carry forward **narrative threading**: when the clustering step (§7) detects that today's article is a continuation of an existing story (e.g. "China retaliates" following "Trump increases tariffs"), it must be added to the *existing* `StoryCluster` and its timeline, not created as a disconnected new card. This is the single most important fix from the earlier critique — connected events must visually and structurally stay connected.

---

## 10. Pages & Navigation V2

| Page | Route | Change from V1 |
|---|---|---|
| Onboarding | `/welcome` | **New.** First-run only, revisitable from Settings. |
| The Board | `/` | **Replaces** `Home.jsx`. Corkboard (or Clean view toggle), live-populating, never empty. |
| Category Board | `/category/:cat` | Same route, now a filtered view of the board rather than a separate RSS list. |
| Search | `/search` | Same route, now backed by real web search (§7), results pin onto the board. |
| Case File | `/story/:id` | **Replaces** `StoryDetail.jsx`. Layered disclosure (§9), adds "How this was researched" tab showing the persisted agent log. |
| Topic Research | `/research` | Kept, now exposes depth-tier selection explicitly instead of vague "modes". |
| Timeline (full page) | `/timeline/:id` | Kept, extended to tab between master timeline and per-thread timelines. |
| Mind Map (full page) | `/mindmap/:id` | Kept, now represents the whole `StoryCluster`, not a single story. |
| Chat | `/chat` | Kept as-is. |
| Pinned (was Saved) | `/saved` | Renamed to match board metaphor; same function. |
| Settings | `/settings` | Extended: onboarding preferences, source registry browser/editor, search provider config, default depth. |

---

## 11. Data Model V2

New/changed tables on top of the existing `stories`, `sources`, `conversations`, `saved_stories`, `jobs`:

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
  id, job_id, story_id, timestamp, stage, message, data (json)   -- persisted, not just streamed
```

`Source.trust_rank` gains a 4th tier ("unverified"); `Source` gains `verified_by`.

---

## 12. Backend Architecture — Keep / Replace / Add

**Keep as-is:**
- `database.py` async pattern (`get_db()` context manager).
- `extraction.py` — trafilatura + BeautifulSoup crawler, rate limiting, retry/backoff. Solid, no reason to touch it.
- `ranking.py` — dedup + trust scoring logic. Extend for the 4th tier, don't rewrite.
- The model abstraction pattern in `ollama_client.py` (rename to `model_client.py` for clarity since it's LM Studio today — keep it swappable).
- CORS/lifespan setup in `main.py`.

**Replace:**
- The trigger model: on-demand-only research → pre-warm scheduler (new `scheduler.py`, simple asyncio interval loop is enough, no need for a heavy task queue at this scale) + manual triggers as a secondary path.
- The job status shape: flat `{progress, message}` → structured `AgentEvent` stream over SSE.
- `synthesis.py`: extend to produce `StoryCluster` assignment, `TimelineThread`s (Tier 3), and mind-map data for the whole cluster, not just one story.
- Search: RSS-keyword-match-only → add a `search_client.py` wrapping SearXNG (or fallback API) as a first-class step before crawling.

**Add:**
- `services/clustering.py` — decides whether a new story joins an existing `StoryCluster` (based on shared entities + topic overlap) or starts a new one.
- `services/board_layout.py` — runs the D3-equivalent force layout server-side (or feeds raw graph data to the frontend to run it client-side with D3, which is simpler — recommended) and persists `BoardPin` positions.
- Router: `routers/board.py` — serves board state (pins + connections + clusters) and `routers/preferences.py` for onboarding data.
- Config: move `TRUSTED_SOURCES` into an external editable JSON, load at startup.

---

## 13. Frontend Architecture — Keep / Replace / Add

**Keep as-is:**
- D3 dependency and the existing rendering approach in `TimelineViz.jsx` / `MindMapViz.jsx` (run-to-convergence, no continuous animation) — reuse the same technique for the board's force layout.
- `ChatPanel.jsx`, `useChat.js`, the SSE-reading pattern in `api.js`'s `streamChat()` — copy this exact pattern for the new agent event stream.
- Design tokens (colors, fonts) as the base layer — extend, don't replace (§14).

**Replace:**
- `Home.jsx` → `Board.jsx` (corkboard / clean-view toggle, §4).
- `StoryDetail.jsx` → `CaseFile.jsx` (layered disclosure, §9, plus "How this was researched" tab).
- `LoadingState.jsx`'s `ResearchProgress` → the live agent console (§6), reusable across Board and Case File.

**Add:**
- `pages/Onboarding.jsx` + `hooks/usePreferences.js`.
- `components/board/BoardCanvas.jsx`, `BoardCard.jsx`, `ConnectionString.jsx`.
- `components/agent/AgentConsole.jsx` (the log panel), `hooks/useAgentEvents.js`.
- `components/story/LayerSection.jsx` (generic collapsible layer used throughout the Case File).

---

## 14. Design Tokens V2

Base palette stays (it was never the problem):

```css
--bg:        #0d0d0f
--surface:   #1e1e23
--border:    #2a2a32
--text-1:    #f0f0ee
--text-2:    #a8a8b3
--text-3:    #6e6e7a
--accent:    #4f7ef7
--green:     #2ecc71
--amber:     #f59e0b
--red:       #ef4444
--font-sans: Inter
--font-serif: Playfair Display
```

New, additive tokens for the board:

```css
--cork:           #3d3229   /* board background, subtle texture, not busy */
--card-paper:     #f4f1e8   /* card face — warm off-white, not pure white */
--card-paper-alt: #ece7d8   /* alternate card shade for visual rhythm */
--pin-tier1:      #2ecc71   /* green — highly trusted */
--pin-tier2:      #f59e0b   /* amber — trusted */
--pin-tier3:      #9aa0a6   /* grey — standard */
--pin-unverified: #ef4444   /* red/striped — unverified, new source */
--string-red:     #c0392b   /* connection string */
```

Rules: cork texture stays subtle (low-contrast noise, not a loud pattern), card rotation capped at ±5°, no shadow blur beyond a tight, realistic drop shadow. The board should read as "confident and a little playful," never as "cluttered."

---

## 15. Definition of Done — Fix Map

| Your complaint | Requirement it maps to | Acceptance test |
|---|---|---|
| "feed is still empty" | §5 Onboarding + §7 Pre-warm scheduler | Fresh install → onboarding → board has 15+ pinned stories within ~90s, with zero manual search. |
| "it is not researching" / "just loads, nothing comes" | §6 Live Agent Console | Every research trigger (pre-warm, search, dig deeper) streams visible, real, per-step events — never a bare spinner. |
| "search should actually search the web" | §7 Real web search | A query for a topic with no matching RSS entry still returns real, freshly crawled results. |
| "should be autonomous... give more if user wants more" | §7 Depth tiers + Dig Deeper | Every story shows its current depth tier and a working "Dig Deeper" that adds sources/events without duplicating the card. |
| "100s of trusted websites... which data to take" | §8 Trust & Citation | Source registry has 100+ curated domains, visible and browsable in Settings; every generated claim has a hover-able citation. |
| "bunch of timelines, then a mindmap" | §7 Multi-timeline + §9 | Tier 3 stories show 2–4 thread timelines plus one cluster-level mind map. |
| "should be able to see everything... live in AI" | §6 | Console is not cosmetic — it reflects real pipeline events and is persisted/replayable per story. |
| "like a college of 5-10 news... with pins, like a criminal board" | §4 Investigation Board | Board renders pinned, tilted, trust-colored cards connected by string, with a Clean-view fallback. |
| "should be fun to read, not timepass" | §4 + §9 | Board interactions (pin-drop, string-draw) are the primary "fun" surface; Case File content stays calm/editorial, no fun added at the cost of readability. |
| "let it take time but everything visible" | §5 Step 3 + §6 | First-load wait is explicitly framed up front and fully visible, never a silent wait. |

---

## 16. Build Order

Suggested sequence so the agent has something working at each checkpoint rather than a big-bang rewrite:

1. Backend: `AgentEvent` model + SSE stream + retrofit existing pipeline stages to emit structured events (unblocks visibility immediately, even before the board exists).
2. Frontend: `AgentConsole.jsx` wired to the new stream, dropped into the existing V1 `StoryDetail` page as a sanity check.
3. Backend: pre-warm scheduler + `UserPreferences` + onboarding endpoints.
4. Frontend: `Onboarding.jsx`.
5. Backend: `search_client.py` (SearXNG/fallback) wired into the pipeline as a pre-crawl step.
6. Backend: `StoryCluster`, `TimelineThread`, `Connection`, `BoardPin` models + `clustering.py` + `board_layout.py`.
7. Frontend: `BoardCanvas.jsx` + card/string components, replacing `Home.jsx`.
8. Backend: depth tiers + Dig Deeper endpoint.
9. Frontend: `CaseFile.jsx` layered disclosure, citation hover cards.
10. Trust registry expansion + Settings page updates.
11. Polish pass: pin-drop/string-draw animations, Clean-view toggle, mobile responsive board fallback.

---

*End of specification. This document is meant to be handed to the build agent in full — do not summarize or truncate sections before pasting.*
