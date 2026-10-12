# ContextLens — Content Quality Patch

> Read together with `contextlens-final-architecture.md`. This document amends Sections 6, 7, 9, 10, and 12 of that spec. It does not repeat the rest of the architecture — hand both files to the build agent.

---

## 0. The one bug behind all four symptoms

Irrelevant stories on the board, timelines that don't make sense, and a mind map that's either garbage or doesn't render — these are not four separate bugs. They're one mistake happening in four places: **an open-ended generative task was handed to a small local model where a constrained classification, a deterministic rule, or a pure data rendering should have been used instead.**

Every AI-touching step in this pipeline must be one of exactly three kinds:

| Kind | What it means | Example |
|---|---|---|
| **Classify** | Narrow question, strict rubric, structured output, one of a fixed set of answers | "Is this worth showing? true/false" |
| **Render** | No LLM at all — draw already-extracted structured data as-is | Mind map from graph query results |
| **Constrained generate** | Short, bounded writing task with fixed inputs and a tight brief | Rewrite a headline in one sentence from given facts |

If a step doesn't fit one of these three, it's the next bug waiting to happen. Below, each of your four symptoms is traced to which kind it was probably implemented as (wrong) versus what it must be (fixed).

---

## 1. Irrelevant news on the dashboard

**Wrong shape:** either no filtering at all, or filtering left implicitly to whatever the synthesis prompt happens to produce.

**Fix — two separate gates, run at two different points, doing two different jobs. Don't merge them into one step.**

### Gate A — Relevance (pre-crawl, cheap, no LLM call)

Before spending any crawl/synthesis budget on a candidate article, check it against the user's onboarding interests using embeddings only:

1. At startup, embed a short static description for each category the user can pick (e.g. "Business & Markets: company earnings, IPOs, monetary policy, trade, corporate leadership changes").
2. For each candidate article (title + RSS snippet — no need to crawl the full page yet), embed it and compute cosine similarity against the user's selected category embeddings.
3. Only articles above a threshold proceed to crawl. Everything else is discarded before it costs anything.

This is pure math — the same `nomic-embed-text` + cosine-similarity approach already specified for dedup (§6.4 of the main doc). No LLM judgment involved, and it directly stops the "why is this here" complaint before a single token of generation is spent on it.

### Gate B — Worth showing (post-synthesis, one constrained classification call)

This is a different question from relevance: a story can be perfectly on-topic and still be noise (a routine restated press release, a thin rehash, a near-duplicate of something already on the board). This one genuinely needs judgment, so it gets exactly one narrow, structured LLM call — never a free-form one:

```
You are a news editor. You are given a story's extracted key facts, not the raw article.
Respond ONLY with JSON in this exact shape:
{"worth_showing": true|false, "category": "significant"|"developing"|"routine"|"thin", "reason": "<one sentence>"}

Rules:
- worth_showing = false if this is a routine announcement with no concrete new development
  (e.g. "company reiterates quarterly guidance", "official reaffirms prior statement")
- worth_showing = false if fewer than 2 independent sources cover it
- worth_showing = false if the extracted facts contain no clear "what happened" —
  only background, opinion, or speculation
- worth_showing = true otherwise

Key facts:
{facts}
```

Enforce JSON-mode / grammar-constrained decoding if your local runtime supports it (LM Studio can constrain output via a JSON schema) — this is exactly the kind of narrow, structured task small models handle reliably, unlike open-ended writing.

Stories that fail Gate B don't vanish permanently — route them to a collapsed "more stories" tier rather than the primary board, and keep them reachable via direct search. Silently disappearing content feels broken; a clearly lower-priority tier doesn't.

---

## 2. Headlines that don't "make any sense" / feel boring

**Clarifying what the earlier spec already said, and tightening it:** the *default* headline shown on every board card and at the top of the Case File was always meant to be an edited version, not the raw wire headline. If the current build is showing raw publisher headlines as the primary text, that's the bug — fix by making the rewrite the default and the original the fallback, not the other way around.

**Constrained generation prompt** (bounded, one sentence, fixed inputs — this is a legitimate small-model task because it's short and tightly scoped, unlike "write a timeline"):

```
Rewrite this headline for a reader with no prior context on this topic.
One sentence. Plain, factual language. No clickbait. State concretely what happened —
don't assume the reader already knows the background. Do not add any fact not present
in the key facts below.

Original headline: "{raw_headline}"
Key facts: {facts}
```

The original publisher headline is preserved and always visible — on the source card in the Sources tab, clearly attributed — it's just no longer the first thing the reader sees. This keeps the attribution guarantee from the main spec intact while fixing the actual complaint (boring, jargon-laden, assumes-you-already-know-this headlines).

---

## 3. Timelines that "don't make sense"

**Wrong shape:** almost certainly a timeline is being generated unconditionally for every story, by asking the LLM to freely narrate "a timeline" from raw article text — which means the model is inventing structure, not reporting it.

**Fix — two changes, one a rule (no LLM), one a hard constraint on what the LLM is given.**

### 3a. Gate the timeline with a deterministic rule, not a model decision

Before generating anything, check the *extracted, dated* events already pulled during entity extraction (§6.5 of the main doc — this data exists before synthesis ever runs):

```
if len(distinct_dated_events) < 3:
    skip timeline, show summary + sources only
elif time_span(distinct_dated_events) < 1 hour and not tagged "fast-breaking":
    skip timeline
else:
    generate timeline
```

This is plain code, not a model call. It directly encodes the Story Forest finding cited in the main doc: most real stories are a single event, not a saga, and forcing timeline structure onto them is exactly what produces nonsense. Skipping is the correct behavior for most stories, not a fallback.

### 3b. When a timeline is generated, the model only orders and lightly narrates events that already exist — it never invents them

The synthesis step must receive the pre-extracted, dated event list as structured input and be explicitly instructed it cannot add events not in that list. If the current implementation feeds raw article text and asks the model to "extract and build a timeline" in one step, split it: extraction (already GLiNER's job, §6.5) happens first and separately, timeline generation only orders and captions what extraction already found.

### 3c. Verify before display

Run the same MiniCheck-style check (§6.7 of the main doc) against each timeline entry's description, not just prose summary sentences — each event description must be traceable to the source text that produced it.

---

## 4. The mind map "isn't making sense" or isn't rendering at all

**This is the clearest case of the underlying bug, and the most important fix.**

If any part of the current build asks the LLM to output a diagram directly — Mermaid syntax, DOT, or freehand graph JSON — that is the bug, full stop. Small local models are unreliable at producing syntactically valid diagram markup, and even when the syntax happens to parse, the graph structure itself is invented, because the model is being asked to hallucinate a graph rather than report one that already exists.

**Hard rule: the mind map is never generated. It is always rendered.**

- Nodes = entities that GLiNER already extracted and that are already stored in the knowledge graph for this story cluster (§6.2, §6.5).
- Edges = relationships that are already stored — from Graphiti's temporal edges and from the Story Forest-style clustering connections (§6.2, §6.3). These already exist as structured records before the mind map is ever drawn.
- The frontend rendering code (D3, per §14 of the main doc) takes this as a plain JSON query result — `{nodes: [...], edges: [...]}` — and draws it. No LLM is in this path at all.
- The **only** legitimate LLM touchpoint for the mind map is optionally writing a short human-readable caption for an edge (e.g. turning a raw relation record into "resigned amid pressure from") — a bounded one-line rewrite of data that already exists, the same category as the headline fix in §2, never structure invention.

If the current implementation has no graph query to render from yet (i.e. the knowledge graph pieces from §6.2–§6.5 of the main doc aren't wired up yet), that's the actual root cause of "it is not being created only" — there's nothing real to render, so whatever's showing is either empty or a model's best guess at filling the gap. The fix isn't a better diagram prompt, it's finishing the graph pipeline that should be feeding it.

---

## 5. Updated pipeline (supersedes §7 of the main doc)

```
5%   Classify intent + depth tier
12%  Relevance gate (embedding similarity vs user interests — no LLM, §1 Gate A)
20%  Search (RSS + SearXNG for on-demand queries)
32%  Crawl candidates (trafilatura + BS4 fallback)
40%  Deduplicate (SimHash pass, then embedding pass)
48%  Extract entities + dated events (GLiNER)
55%  Update knowledge graph (Graphiti + FalkorDB, JSON episodes)
62%  Cluster/thread into story arc (Story Forest-style)
68%  Rewrite headline (constrained generation, §2)
72%  Synthesize summary (LLM, from structured facts only)
78%  Timeline gate check (deterministic rule, §3a) → generate or skip
84%  Worth-showing gate (constrained classification, §1 Gate B)
90%  Verify generated claims + timeline entries (MiniCheck)
95%  Render mind map from graph query (no LLM, §4), pin to board
100% Done — persist AgentEvent log
```

Note where the LLM appears versus where it doesn't: three constrained generation calls (headline, summary, optional edge captions), one classification call (worth-showing), everything else is embeddings, deterministic rules, or pure rendering. That ratio is the actual fix — if you count more free-form LLM calls than that in the current implementation, that's where to look next.

---

## 6. Data model additions (extends §12 of the main doc)

```
Story gains:
  relevance_score        float (0–1, from Gate A)
  worth_showing           bool
  worth_showing_category  "significant"|"developing"|"routine"|"thin"
  worth_showing_reason    text
  headline_original       text   -- verbatim, attributed
  headline_edited         text   -- what's shown by default
  timeline_generated      bool   -- false means the gate in §3a skipped it
```

---

## 7. Definition of done — additions

| Symptom | Fix | Acceptance test |
|---|---|---|
| Dashboard news feels irrelevant | Relevance gate (embeddings, pre-crawl) | A story with low similarity to any selected interest never reaches the board |
| Stories feel like noise/filler | Worth-showing gate (constrained classification) | Every board story has a `worth_showing_reason` on record; low-value stories are in the secondary tier, not the primary board |
| Headlines feel raw/boring | Edited headline as default, original preserved | Board cards show `headline_edited`; `headline_original` is visible and attributed in the Sources tab |
| Timelines don't make sense | Deterministic gate + extraction-grounded generation | A story with fewer than 3 distinct dated events never gets a timeline section; every timeline entry traces to a real extracted event |
| Mind map is garbage or missing | Pure rendering from graph query, no LLM-generated diagram code | Mind map data is `{nodes, edges}` from a graph query, never a string the LLM was asked to compose as a diagram |
