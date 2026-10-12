"""ContextLens v4 — "go deeper": on-demand deep dives that pull in fresh reporting beyond the story's own sources.

  why      the causes, in plain words          (searches for explainers + analysis of the same story)
  history  the background / how it started     (searches for backgrounders and earlier coverage)
  views    who sees this differently and why   (searches for reactions, analysis and opinion — each view attributed)
  next     what is expected to happen next     (upcoming dates / decisions stated by the reporting; scenarios flagged as uncertain)
  who      who this person/organisation is     (knowledge base + Wikipedia, no LLM)
  term     what a term means                   (curated glossary, else the writer's general definition)

Every paragraph must cite numbered facts (from the story's own text and the newly fetched articles) and is checked
against them; paragraphs that are not supported are dropped. Results are cached per (story, kind, arg).
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Optional

from backend import db
from backend.services import background, explainer, extract, glossary, inspector, llm, runs
from backend.services.llm import LLMError, looks_like_garbage
from backend.services.verify import _numbers, check_claim

log = logging.getLogger("dives")

KINDS = ("why", "history", "views", "next", "who", "term")

SUFFIX = {"why": "why explained analysis", "history": "background history how it started", "views": "reaction analysis opinion",
          "next": "what next expected"}

TASK = {
    "why": "Explain WHY this happened: the causes and how they connect, oldest cause first. Plain words, cause and effect.",
    "history": "Explain the BACKGROUND: how this situation started and the key earlier developments that led to it, oldest first.",
    "views": "Explain the DIFFERENT VIEWS: who sees this differently and why. Attribute every view to who holds it (a named person, party or outlet). Stay neutral; do not pick a side.",
    "next": "Explain WHAT HAPPENS NEXT: only scheduled events, deadlines and decisions the FACTS mention, then clearly-flagged possible scenarios (say 'may' / 'could'). Do not predict.",
}

SCHEMA = {"type": "object", "properties": {
    "paragraphs": {"type": "array", "minItems": 2, "maxItems": 5, "items": {
        "type": "object", "properties": {"text": {"type": "string"}, "facts": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"}}},
        "required": ["text", "facts"], "additionalProperties": False}}},
    "required": ["paragraphs"], "additionalProperties": False}

SYSTEM = """You are a patient explainer journalist writing a short deep-dive for a reader with no background.
{task}
Write 3-5 short paragraphs (each 1-3 plain sentences, max 60 words). Use ONLY the numbered FACTS; each paragraph lists the ids of the 1-3 facts it relies on. If the FACTS do not cover something, leave it out — never fill gaps. No filler ("significant implications", "highlights", "underscores"). Do not confuse who did what."""


async def get_cached(story_id: str, kind: str, arg: str = "") -> Optional[dict]:
    row = await db.fetch_one("SELECT payload FROM dives WHERE story_id=? AND kind=? AND key=?", (story_id, kind, arg.lower()[:120]))
    return db.jload(row["payload"]) if row else None


async def _store(story_id: str, kind: str, arg: str, payload: dict) -> None:
    await db.execute(
        "INSERT INTO dives(story_id,kind,key,payload,created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(story_id,kind,key) DO UPDATE SET payload=excluded.payload, created_at=excluded.created_at",
        (story_id, kind, arg.lower()[:120], db.jdump(payload), db.now_iso()))


async def _extra_texts(story: dict, kind: str, names: list[str], run: runs.Run) -> list[dict]:
    from backend.services.research import HARD_PAYWALL, _ensure_articles
    from backend.services import feeds
    qs = background.build_queries(story["headline"], names)
    cands = []
    for q in qs[:2]:
        await run.emit("search", f"Searching: {q} {SUFFIX[kind]}")
        cands.extend(await background._searx(f"{q} {SUFFIX[kind]}", "month"))
    own = {s["url"] for s in story["sources"]}
    seen, pool = set(), []
    for c in cands:
        if c["url"] in seen or c["url"] in own or feeds.domain_of(c["url"]) in HARD_PAYWALL or "msn.com" in c["url"]:
            continue
        seen.add(c["url"])
        pool.append(c)
    if not pool:
        return []
    vec = (await llm.embed([f"{story['headline']}. {story['dek']}"]))[0]
    embs = await llm.embed([f"{c['title']}. {c.get('snippet', '')[:160]}" for c in pool])
    ranked = sorted(zip(pool, embs), key=lambda x: -float(x[1] @ vec))
    picks = [c for c, v in ranked[:8] if float(v @ vec) >= 0.6][:4]
    await run.emit("fetch", f"Reading {len(picks)} more article(s)…")
    rows = await _ensure_articles(picks)
    fetched = await extract.fetch_many(rows)
    return [{"url": r["url"], "outlet": r["outlet"], "text": f["text"]} for r, f in zip(rows, fetched)
            if f["text"] and not f["paywall"] and len(f["text"]) > 800]


async def run_dive(run: runs.Run, story_id: str, kind: str, arg: str = "") -> None:
    llm.interactive()
    try:
        story = await inspector.load_story(story_id)
        if not story:
            raise LLMError("story not found")
        payload = await _compute(run, story, kind, arg)
        await _store(story_id, kind, arg, payload)
        await run.finish("complete")
    except Exception as e:  # noqa: BLE001
        log.warning("dive failed: %s", traceback.format_exc()[-400:])
        await run.emit("error", f"Could not finish: {str(e)[:160]}", "error")
        await run.finish("failed", error=str(e)[:200])


async def _compute(run: runs.Run, story: dict, kind: str, arg: str) -> dict:
    if kind == "term":
        await run.emit("term", f"Looking up “{arg}”")
        hit = glossary.find_terms(arg, 1)
        if hit:
            return {"kind": kind, "title": arg, "paragraphs": [{"text": hit[0]["meaning"], "sources": [], "basis": "curated"}]}
        out = await inspector.explain_selection(arg, "", story)
        return {"kind": kind, "title": arg, "paragraphs": [{"text": out["answer"], "sources": [], "basis": "general"}]}
    if kind == "who":
        await run.emit("who", f"Looking up {arg}")
        w = await inspector.who_selection(arg, story)
        paras = []
        if w["role_in_story"]:
            paras.append({"text": f"In this story: {w['role_in_story']}.", "sources": [], "basis": "reported"})
        if w["summary"]:
            paras.append({"text": w["summary"], "sources": [{"outlet": "Wikipedia", "url": w["wiki_url"]}] if w["wiki_url"] else [], "basis": "encyclopedia"})
        if not paras:
            paras.append({"text": f"We have no reliable background on {arg} yet.", "sources": [], "basis": "none"})
        return {"kind": kind, "title": w["name"], "paragraphs": paras, "stories": w["stories"], "kb_id": w["kb_id"]}
    if kind not in TASK:
        raise LLMError("unknown deep-dive")

    names = [e["name"] for e in await db.fetch_all("SELECT name FROM entities WHERE story_id=? ORDER BY mentions DESC LIMIT 4", (story["id"],))]
    own = [{"url": s["url"], "outlet": s["outlet"], "text": s["text"]} for s in story["sources"] if s.get("text")]
    extra = []
    try:
        extra = await _extra_texts(story, kind, names, run)
    except Exception as e:  # noqa: BLE001 — network is optional; fall back to the story's own sources
        log.info("dive retrieval failed: %s", str(e)[:100])
    texts = own[:3] + extra
    focus = f"{story['headline']}. {SUFFIX[kind]}"
    facts = explainer.fact_sheet(texts, focus, k=22) if texts else []
    if len(facts) < 4:
        raise LLMError("not enough reporting to go deeper on this")
    by_id = {f["id"]: f for f in facts}
    await run.emit("write", "Writing the explanation…")
    user = f"STORY: {story['headline']}\n\nFACTS\n" + "\n".join(f"{f['id']} [{f['outlet']}] {f['text']}" for f in facts)
    allowed = _numbers(" ".join(f["text"] for f in facts))

    def valid(o: dict):
        for p in o["paragraphs"]:
            if looks_like_garbage(p["text"]) or len(p["text"]) < 30:
                return "each paragraph needs 1-3 plain sentences"
            if any(i.upper() not in by_id for i in p["facts"]):
                return "cite only fact ids that exist"
        return None
    out = await llm.chat_json("writer", SYSTEM.format(task=TASK[kind]), user, SCHEMA, schema_name="dive",
                              temperature=0.2, max_tokens=900, validate=valid, retries=1)
    paras, used = [], {}
    for p in out["paragraphs"]:
        cited = [by_id[i.upper()] for i in p["facts"] if i.upper() in by_id]
        if check_claim(p["text"], " ".join(f["text"] for f in cited), 0.2) or \
                any(n not in allowed and n not in ("1", "2") for n in _numbers(p["text"])):
            continue                                        # not supported by what it cites
        srcs = []
        for f in cited:
            key = f["url"]
            if key not in used:
                used[key] = {"outlet": f["outlet"], "url": f["url"]}
            srcs.append(used[key])
        paras.append({"text": p["text"].strip(), "sources": srcs, "basis": "reported"})
    if not paras:
        raise LLMError("the reporting did not support a reliable answer")
    return {"kind": kind, "title": {"why": "Why this happened", "history": "How it started", "views": "Different views",
                                     "next": "What happens next"}[kind],
            "paragraphs": paras, "extra_sources": len(extra)}
