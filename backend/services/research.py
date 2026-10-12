"""ContextLens v3 — Loop B: on-demand research.

query → LLM query expansion → parallel retrieval (own article corpus by embedding, SearXNG news, GDELT)
→ relevance filter → full-text fetch → cited report (each section cites sources by number)
→ per-section grounding check (unsupported sections are dropped) → optional timeline / story map (same gates).
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote_plus

import httpx
import numpy as np

from backend import db
from backend.config import DEPTH_ARTICLES, SEARXNG_URL, USER_AGENT
from backend.services import extract, feeds, graph, llm, runs, timeline
from backend.services.editor import validate_headline
from backend.services.llm import LLMError, looks_like_garbage
from backend.services.verify import ground
from backend.settings_store import get_settings

log = logging.getLogger("research")

# Outlets that almost always refuse scraping / hide the article body: usable for headlines, rarely for full text.
HARD_PAYWALL = {"reuters.com", "wsj.com", "ft.com", "bloomberg.com", "economist.com", "nytimes.com", "washingtonpost.com",
                "thetimes.co.uk", "telegraph.co.uk", "barrons.com", "businessinsider.com"}

EXPAND_SCHEMA = {"type": "object", "properties": {"queries": {"type": "array", "minItems": 2, "maxItems": 4,
                 "items": {"type": "string"}}}, "required": ["queries"], "additionalProperties": False}

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "dek": {"type": "string"},
        "summary": {"type": "string"},
        "sections": {"type": "array", "minItems": 2, "maxItems": 5, "items": {"type": "object", "properties": {
            "heading": {"type": "string"}, "text": {"type": "string"},
            "sources": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "integer"}}},
            "required": ["heading", "text", "sources"], "additionalProperties": False}},
        "key_points": {"type": "array", "minItems": 3, "maxItems": 6, "items": {"type": "string"}},
        "disagreements": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        "open_questions": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
    },
    "required": ["headline", "dek", "summary", "sections", "key_points", "disagreements", "open_questions"],
    "additionalProperties": False,
}

REPORT_SYSTEM = """You are a research editor. Write a sourced briefing about the user's topic using ONLY the numbered SOURCES.
- headline: a clear, concrete title for the briefing (max 16 words).  dek: one sentence (max 30 words).
- summary: 3-5 sentences giving the state of things.
- sections: 2-5 sections (e.g. what happened, background, reactions, what to watch). Each section text is 2-4 sentences and must only state facts found in the sources it lists in "sources" (source numbers).
- key_points: 3-6 specific facts (numbers, names, decisions).
- disagreements: places where sources conflict or differ in emphasis (empty list if none).
- open_questions: at most 3 things the sources do not answer.
Never invent facts. If the sources do not cover an aspect, leave it out."""


CLAIM_SCHEMA = {
    **REPORT_SCHEMA,
    "properties": {**REPORT_SCHEMA["properties"],
                   "verdict": {"type": "string", "enum": ["supported", "partly_supported", "contradicted", "not_enough_evidence"]},
                   "verdict_reason": {"type": "string"}},
    "required": REPORT_SCHEMA["required"] + ["verdict", "verdict_reason"],
}

CLAIM_SYSTEM = """You are a fact-checking editor. The user gives a CLAIM. Judge it using ONLY the numbered SOURCES.
- verdict: supported (the sources clearly state it) | partly_supported (some parts confirmed, others unverified or nuanced) | contradicted (the sources state the opposite) | not_enough_evidence (the sources do not settle it).
- verdict_reason: 1-2 sentences saying exactly what the sources do and do not establish.
- headline: the claim restated neutrally, max 16 words.  dek: one sentence with the bottom line.  summary: 3-4 sentences.
- sections: use headings like "Evidence for", "Evidence against", "Context". Each section text is 2-4 sentences using only facts in the sources it lists in "sources".
- key_points: 3-6 specific facts.  disagreements: where sources conflict.  open_questions: what remains unverified.
Be strict: never call a claim supported or contradicted from a single source. Never invent facts."""

VERDICT_LABEL = {"supported": "Supported", "partly_supported": "Partly supported", "contradicted": "Contradicted",
                 "not_enough_evidence": "Not enough evidence"}


def settle_verdict(verdict: str, sections: list[dict], texts: list[dict]) -> tuple[str, str]:
    """Deterministic guard: a decisive verdict needs >=2 independent outlets actually cited."""
    cited = {i for sec in sections for i in sec["cites"]}
    outlets = {texts[i - 1]["domain"] for i in cited if 1 <= i <= len(texts)}
    if verdict in ("supported", "contradicted") and len(outlets) < 2:
        return "not_enough_evidence", f"Downgraded: only {len(outlets)} independent outlet was cited, and a decisive verdict needs at least two."
    return verdict, ""


async def _expand(query: str) -> list[str]:
    try:
        out = await llm.chat_json(
            "writer", "You generate web/news search queries. Return 2-4 short, distinct queries (max 8 words each) covering "
            "the topic from different angles (latest developments, background, key actors, impact). No quotes.",
            f"Topic: {query}", EXPAND_SCHEMA, schema_name="queries", max_tokens=160, retries=0)
        qs = [q.strip() for q in out["queries"] if 3 < len(q.strip()) < 90]
    except LLMError:
        qs = []
    return [query] + [q for q in qs if q.lower() != query.lower()][:3]


async def _searx(q: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(SEARXNG_URL.rstrip("/") + "/search",
                            params={"q": q, "format": "json", "categories": "news", "language": "en"})
            r.raise_for_status()
            return [{"url": i["url"], "title": i.get("title", ""), "snippet": i.get("content", ""),
                     "published_at": i.get("publishedDate")} for i in r.json().get("results", [])[:15]]
    except Exception as e:
        log.info("searxng failed for %r: %s", q, str(e)[:80])
        return []


async def _gdelt(q: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get("https://api.gdeltproject.org/api/v2/doc/doc",
                            params={"query": f"{q} sourcelang:english", "mode": "artlist", "format": "json",
                                    "maxrecords": 20, "sort": "hybridrel", "timespan": "1month"})
            r.raise_for_status()
            return [{"url": i["url"], "title": i.get("title", ""), "snippet": "", "published_at": None}
                    for i in r.json().get("articles", [])]
    except Exception as e:
        log.info("gdelt failed: %s", str(e)[:80])
        return []


async def _local(query: str, days: int = 6, k: int = 12) -> list[dict]:
    """Search the already-ingested newspaper corpus by embedding similarity."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await db.fetch_all("SELECT id,url,title,snippet,outlet,domain,trust_rank,published_at,embedding FROM articles "
                              "WHERE embedding IS NOT NULL AND published_at >= ?", (cutoff,))
    if not rows:
        return []
    q = (await llm.embed([query], kind="query"))[0]
    scored = []
    for r in rows:
        v = llm.from_blob(r["embedding"])
        if v is not None and v.shape == q.shape:
            scored.append((float(v @ q), r))
    scored.sort(key=lambda x: -x[0])
    return [{**{k_: r[k_] for k_ in ("url", "title", "snippet", "published_at")}, "_sim": s} for s, r in scored[:k] if s >= 0.58]


async def _ensure_articles(cands: list[dict]) -> list[dict]:
    """Make sure every candidate exists in `articles` (for text caching); return DB rows."""
    now = db.now_iso()
    async with db.get_db() as conn:
        for c in cands:
            dom = feeds.domain_of(c["url"])
            src = feeds.lookup_source(dom)
            await conn.execute(
                "INSERT OR IGNORE INTO articles(id,url,url_hash,title,snippet,outlet,domain,trust_rank,topic,published_at,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (db.new_id(), c["url"], feeds.url_hash(c["url"]), feeds.clean_text(c["title"])[:300],
                 feeds.clean_text(c.get("snippet", ""))[:500], (src or {}).get("name") or dom, dom,
                 feeds.trust_rank_for(dom, 4), "world", c.get("published_at") or now, now))
        await conn.commit()
    out = []
    for c in cands:
        row = await db.fetch_one("SELECT * FROM articles WHERE url_hash=?", (feeds.url_hash(c["url"]),))
        if row:
            row["_sim"] = c.get("_sim", 0.0)
            out.append(row)
    return out


def _validate_report(rep: dict, n_sources: int, plain: str) -> Optional[str]:
    if (err := validate_headline(rep["headline"], "")):
        return err
    for f in ("dek", "summary"):
        if looks_like_garbage(rep[f]) or len(rep[f]) < 30:
            return f"{f} invalid"
    for sec in rep["sections"]:
        if looks_like_garbage(sec["text"]) or len(sec["text"]) < 60:
            return "a section is too short or invalid"
        if any(not (1 <= i <= n_sources) for i in sec["sources"]):
            return f"section cites a source outside 1..{n_sources}"
    return None


async def run_research(query: str, mode: str = "research") -> tuple[str, str]:
    """Start a research run. Returns (run_id, story_id) immediately; work continues in the background."""
    query = re.sub(r"\s+", " ", query).strip()[:200]
    run = await runs.Run.start("research", query)
    sid = db.new_id()
    now = db.now_iso()
    await db.execute("INSERT INTO stories(id,kind,query,created_at,updated_at,headline,status,tier) VALUES(?,?,?,?,?,?,?,?)",
                     (sid, "research", query, now, now, query, "researching", "primary"))
    await db.execute("UPDATE runs SET story_id=? WHERE id=?", (sid, run.id))
    asyncio.create_task(_research_task(run, sid, query, mode))
    return run.id, sid


async def _fail(run: "runs.Run", sid: str, msg: str):
    await db.execute("UPDATE stories SET status='failed', error=? WHERE id=?", (msg[:300], sid))
    await run.emit("error", msg, "error")
    await run.finish("failed", error=msg[:300], story_id=sid)


async def _research_task(run: "runs.Run", sid: str, query: str, mode: str = "research"):
    try:
        s = await get_settings()
        depth = s["depth"]
        await run.emit("plan", f"Planning research on “{query}”", progress=0.04)
        queries = await _expand(query)
        await run.emit("plan", "Searching: " + " · ".join(queries), "success", progress=0.12)

        results = await asyncio.gather(*[_searx(q) for q in queries], *[_gdelt(queries[0])], _local(query))
        flat: list[dict] = []
        seen = set()
        for lst in results:
            for r in lst:
                h = feeds.url_hash(r["url"])
                if h not in seen and r["title"]:
                    seen.add(h)
                    flat.append(r)
        await run.emit("search", f"{len(flat)} unique candidate articles from {len(queries)} queries + your newspaper corpus", "success", progress=0.25)
        if not flat:
            return await _fail(run, sid, "No sources found for this topic. Try different wording.")

        rows = await _ensure_articles(flat[:60])
        # relevance filter (embeddings). nomic scores have a ~0.60 noise floor, so cut relative to the best match.
        qv = (await llm.embed([query], kind="query"))[0]
        tv = await llm.embed([f"{r['title']}. {r['snippet'][:200]}" for r in rows], kind="document")
        for r, v in zip(rows, tv):
            r["_rel"] = float(v @ qv) if v.shape == qv.shape else 0.0
        rows = [r for r in rows if r["domain"] not in feeds.BLOCKLIST_DOMAINS]
        best = max((r["_rel"] for r in rows), default=0.0)
        floor = max(0.58, best - 0.12)

        def rank(r):   # relevance + source quality, minus a penalty for outlets that usually block scraping
            return r["_rel"] + (5 - min(r["trust_rank"], 5)) * 0.02 - (0.10 if r["domain"] in HARD_PAYWALL else 0.0)

        rows.sort(key=lambda r: -rank(r))
        pool = [r for r in rows if r["_rel"] >= floor] or rows[:8]
        await run.emit("filter", f"{len(pool)} on-topic candidates (dropped {len(rows) - len(pool)} off-topic)", "success", progress=0.35)

        # fetch iteratively until enough readable sources (max 2 per domain, batches of 4)
        target = DEPTH_ARTICLES.get(depth, 3) + 1
        per_dom: dict[str, int] = {}
        queue = []
        for r in pool:
            if per_dom.get(r["domain"], 0) < 2:
                per_dom[r["domain"]] = per_dom.get(r["domain"], 0) + 1
                queue.append(r)
        texts, attempts = [], 0
        for i in range(0, min(len(queue), 16), 4):
            batch = queue[i:i + 4]
            attempts += len(batch)
            fetched = await extract.fetch_many(batch)
            for r, f in zip(batch, fetched):
                ok = bool(f["text"]) and not f["paywall"]
                await run.emit("crawl", f"{'✓' if ok else '·'} {r['outlet']} — {r['title'][:70]}" + ("" if ok else " (unreadable)"),
                               "success" if ok else "info")
                if ok and len({t["domain"] for t in texts} | {r["domain"]}) >= 1:
                    texts.append({"url": r["url"], "outlet": r["outlet"], "title": r["title"], "published_at": r["published_at"],
                                  "text": f["text"], "trust_rank": r["trust_rank"], "domain": r["domain"], "id": r["id"]})
            if len(texts) >= target:
                break
        if len(texts) < 2:
            return await _fail(run, sid, f"Only {len(texts)} readable source(s) among {attempts} tried (the rest were paywalled or blocked); not enough to write a reliable briefing. Try a more specific topic.")
        texts = texts[:DEPTH_ARTICLES.get(depth, 3) + 1]

        await run.emit("write", f"Writing a cited briefing from {len(texts)} full-text sources…", progress=0.5)
        per = max(1300, 6200 // len(texts))
        corpus = "\n\n".join(f"[{i}] {t['outlet']} — {t['title']}\n{t['text'][:per]}" for i, t in enumerate(texts, 1))
        plain = "\n".join(t["text"] for t in texts)
        claim = mode == "claim"
        rep = await llm.chat_json("writer", CLAIM_SYSTEM if claim else REPORT_SYSTEM,
                                  f"{'CLAIM' if claim else 'TOPIC'}: {query}\n\nSOURCES\n{corpus}",
                                  CLAIM_SCHEMA if claim else REPORT_SCHEMA,
                                  schema_name="report", temperature=0.2, max_tokens=1400,
                                  validate=lambda r: _validate_report(r, len(texts), plain), retries=2)

        # per-section grounding against the sources it cites
        sections, dropped = [], 0
        for sec in rep["sections"]:
            src_text = "\n".join(texts[i - 1]["text"] for i in sec["sources"])
            g = ground([sec["text"]], src_text)
            if g["score"] >= 0.5:
                sections.append({"heading": sec["heading"].strip(), "text": sec["text"].strip(), "cites": sorted(set(sec["sources"])), "grounding": g["score"]})
            else:
                dropped += 1
        if not sections:
            return await _fail(run, sid, "The model's briefing could not be verified against the sources.")
        g_all = ground([rep["dek"], rep["summary"]] + rep["key_points"], plain)
        await run.emit("verify", f"Verified {len(sections)} section(s) against cited sources" + (f"; dropped {dropped} unsupported" if dropped else ""),
                       "success" if not dropped else "warning", progress=0.8)

        tl = {"events": [], "shown": False, "reason": "skipped", "raw_count": 0}
        gr = {"entities": [], "relations": [], "shown": False, "reason": "skipped"}
        story_text = rep["headline"] + ". " + rep["summary"]
        if depth >= 2:
            tl = await timeline.build_timeline(texts, story_text)
            await run.emit("timeline", f"Timeline: {tl['reason']}", "success" if tl["shown"] else "info", progress=0.88)
            gr = await graph.build_graph(texts, rep["headline"])
            await run.emit("graph", f"Story map: {gr['reason']}", "success" if gr["shown"] else "info", progress=0.94)

        now = db.now_iso()
        report = {"sections": sections, "disagreements": rep["disagreements"], "open_questions": rep["open_questions"],
                  "queries": queries, "dropped_sections": dropped, "mode": mode}
        if claim:
            verdict, note = settle_verdict(rep["verdict"], sections, texts)
            report["verdict"] = verdict
            report["verdict_label"] = VERDICT_LABEL[verdict]
            report["verdict_reason"] = (note + " " if note else "") + rep["verdict_reason"].strip()
            await run.emit("verdict", f"Verdict: {VERDICT_LABEL[verdict]}" + (f" — {note}" if note else ""), "success" if not note else "warning")
        trust = float(np.mean([(5 - min(t["trust_rank"], 4)) / 4 for t in texts]))
        async with db.get_db() as conn:
            await conn.execute(
                "UPDATE stories SET headline=?,headline_original=?,dek=?,what_happened=?,key_points=?,report=?,trust_score=?,"
                "outlet_count=?,reading_minutes=?,status='complete',tier='primary',updated_at=?,published_at=?,verification=?,gate=?,tags=? WHERE id=?",
                (rep["headline"].strip(), query, rep["dek"].strip(), rep["summary"].strip(), db.jdump(rep["key_points"]),
                 db.jdump(report), round(trust, 3), len({t["domain"] for t in texts}), max(2, len(sections) + 1), now, now,
                 db.jdump(g_all), db.jdump({"timeline": {"reason": tl["reason"]}, "graph": {"reason": gr["reason"]}}),
                 db.jdump([q for q in queries[1:3]]), sid))
            await conn.executemany(
                "INSERT INTO story_sources(story_id,n,url,outlet,domain,title,trust_rank,published_at,excerpt,has_full_text) VALUES(?,?,?,?,?,?,?,?,?,1)",
                [(sid, i, t["url"], t["outlet"], t["domain"], t["title"], t["trust_rank"], t["published_at"], t["text"][:320]) for i, t in enumerate(texts, 1)])
            await conn.executemany("INSERT INTO timeline_events(story_id,date,precision,text,quote,source_url,source_outlet) VALUES(?,?,?,?,?,?,?)",
                                   [(sid, e["date"], e["precision"], e["text"], e["quote"], e["source_url"], e["source_outlet"]) for e in tl["events"]])
            await conn.executemany("INSERT INTO entities(story_id,name,type,mentions) VALUES(?,?,?,?)",
                                   [(sid, e["name"], e["type"], e["mentions"]) for e in gr["entities"]])
            await conn.executemany("INSERT INTO relations(story_id,subject,relation,object,evidence,source_url) VALUES(?,?,?,?,?,?)",
                                   [(sid, r["subject"], r["relation"], r["object"], r["evidence"], r["source_url"]) for r in gr["relations"]])
            await conn.commit()
        run.stats.update({"sources": len(texts), "sections": len(sections)})
        await run.emit("done", f"Briefing ready: “{rep['headline']}”", "success", progress=1.0)
        await run.finish("complete", story_id=sid)
    except Exception as e:  # noqa: BLE001
        log.error("research failed:\n%s", traceback.format_exc())
        await _fail(run, sid, f"Research failed: {str(e)[:200]}")
