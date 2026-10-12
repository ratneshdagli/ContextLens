"""Story detail, feedback, saved, search and grounded chat."""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend import db
from backend.routers.feed import CARD_SQL, FACT_DOMAINS, card, signal_of
from backend.services import interests, kb, llm, pipeline, runs
from backend.services.llm import LLMError
from backend.settings_store import get_settings

router = APIRouter(prefix="/api", tags=["stories"])


@router.get("/stories/{story_id}")
async def story_detail(story_id: str):
    s = await db.fetch_one("SELECT * FROM stories WHERE id=?", (story_id,))
    if not s:
        raise HTTPException(404, "Story not found")
    s = db.story_from_row(s)
    ch = await db.fetch_one("SELECT name FROM channels WHERE id=?", (s.get("channel_id") or "",))
    s["channel_name"] = ch["name"] if ch else ""                 # the reader's own category, shown instead of the generic topic
    sources = await db.fetch_all("SELECT n,url,outlet,domain,title,trust_rank,published_at,excerpt,has_full_text "
                                 "FROM story_sources WHERE story_id=? ORDER BY n", (story_id,))
    if not sources and s.get("cluster_id"):                       # instant (wire) story: list the outlets that covered it
        arts = await db.fetch_all("SELECT url,outlet,domain,title,trust_rank,published_at,snippet FROM articles "
                                  "WHERE cluster_id=? ORDER BY trust_rank, length(snippet) DESC LIMIT 14", (s["cluster_id"],))
        sources = [{"n": i, "url": a["url"], "outlet": a["outlet"], "domain": a["domain"], "title": a["title"],
                    "trust_rank": a["trust_rank"], "published_at": a["published_at"], "excerpt": (a["snippet"] or "")[:320],
                    "has_full_text": 0} for i, a in enumerate(arts, 1)]
    for x in sources:
        x["factcheck"] = any((x.get("domain") or "").endswith(d) for d in FACT_DOMAINS)
    developments = []
    if s.get("cluster_id"):
        crow = await db.fetch_one("SELECT merged FROM clusters WHERE id=?", (s["cluster_id"],))
        seen_dev = {s["headline_original"]}
        for u in sorted(db.jload(crow["merged"], []) if crow else [], key=lambda x: x.get("at") or ""):
            if u.get("title") and u["title"] not in seen_dev:      # other angles of the same story that were folded into it
                seen_dev.add(u["title"])
                developments.append({k: u.get(k) for k in ("title", "outlet", "url", "at")})
    events = await db.fetch_all("SELECT date,precision,text,quote,source_url,source_outlet,kind FROM timeline_events "
                                "WHERE story_id=? ORDER BY date", (story_id,))
    ents = await db.fetch_all("SELECT name,type,mentions FROM entities WHERE story_id=? ORDER BY mentions DESC", (story_id,))
    rels = await db.fetch_all("SELECT subject,relation,object,evidence,source_url FROM relations WHERE story_id=?", (story_id,))
    connected = {r["subject"] for r in rels} | {r["object"] for r in rels}
    graph = {"nodes": [e for e in ents if e["name"] in connected],
             "edges": [{"source": r["subject"], "target": r["object"], "label": r["relation"],
                        "evidence": r["evidence"], "url": r["source_url"]} for r in rels]} if rels else None
    keys = {kb.entity_key(e["name"]): e for e in ents}
    kb_rows = []
    if keys:
        ph = ",".join("?" * len(keys))
        kb_rows = await db.fetch_all(f"SELECT id,key,story_count FROM kb_entities WHERE key IN ({ph})", tuple(keys))
    kb_by_key = {r["key"]: r for r in kb_rows}
    people = []
    for k, e in keys.items():
        row = kb_by_key.get(k)
        people.append({**e, "kb_id": row["id"] if row else None, "story_count": row["story_count"] if row else 0})
    players = [e for e in people if e["type"] in ("person", "organization", "place") and e["kb_id"]][:8]
    thread = await kb.thread_history(story_id)
    kb_related = await kb.related_stories(story_id)
    react = await db.fetch_one("SELECT action FROM feedback WHERE story_id=? AND action IN ('like','dislike') ORDER BY id DESC LIMIT 1", (story_id,))
    saved = await db.fetch_one("SELECT 1 FROM saved WHERE story_id=?", (story_id,))
    following = await db.fetch_one("SELECT 1 FROM follows WHERE story_id=?", (story_id,))
    gate = s["gate"] or {}
    extras = s["extras"] or {}
    built = s["tier"] in ("primary", "more") and bool(s.get("built_at"))
    return {**{k: s[k] for k in ("id", "kind", "query", "headline", "headline_original", "dek", "what_happened", "why_it_matters",
                                 "why_for_you", "key_points", "report", "topic", "tags", "trust_score", "outlet_count",
                                 "reading_minutes", "image_url", "tier", "status", "error", "published_at", "created_at")},
            "channel_name": s["channel_name"], "built": built, "building": story_id in pipeline._building, "build_run": pipeline._build_runs.get(story_id),
            "build_error": (gate.get("build_error") if not built else None),
            "verification": s["verification"], "claims": extras.get("claims", []),
            "lens": extras.get("lens"), "background": extras.get("background", []), "explain": extras.get("explain"),
            "simple": extras.get("simple"), "signal": signal_of(gate), "sources": sources, "timeline": events, "graph": graph,
            "players": players, "entities": people[:14], "thread": thread, "kb_related": kb_related, "developments": developments,
            "saved": bool(saved), "following": bool(following), "reaction": react["action"] if react else None,
            "gate": {"publish": (gate.get("publish") or {}).get("reasons"), "triage": (gate.get("triage") or {}).get("reasons"),
                     "timeline": (gate.get("timeline") or {}).get("reason"), "graph": (gate.get("graph") or {}).get("reason"),
                     "explain": gate.get("explain")}}


@router.post("/stories/{story_id}/build")
async def build_story(story_id: str, force: bool = False):
    """Explain a story now (interactive priority). Returns {built} if nothing to do, else {run_id} to stream progress."""
    row = await db.fetch_one("SELECT tier,built_at,cluster_id,extras FROM stories WHERE id=?", (story_id,))
    if not row:
        raise HTTPException(404, "Story not found")
    has_explain = bool((db.jload(row["extras"], {}) or {}).get("explain"))
    if row["tier"] in ("primary", "more") and row["built_at"] and has_explain and not force:
        return {"built": True}
    if not row["cluster_id"]:
        raise HTTPException(409, "This story cannot be rebuilt (its source articles have expired).")
    if story_id in pipeline._building:
        return {"built": False, "run_id": pipeline._build_runs.get(story_id) or runs.active("feed"), "already_running": True}
    asyncio.create_task(pipeline.build_now(story_id))
    for _ in range(60):
        await asyncio.sleep(0.05)
        if story_id in pipeline._build_runs:
            break
    return {"built": False, "run_id": pipeline._build_runs.get(story_id), "already_running": False}


class FeedbackIn(BaseModel):
    action: str          # like | dislike | open | dismiss
    dwell_ms: int | None = None


@router.post("/stories/{story_id}/feedback")
async def feedback(story_id: str, body: FeedbackIn):
    if body.action not in ("like", "dislike", "open", "dismiss"):
        raise HTTPException(400, "bad action")
    await db.execute("INSERT INTO feedback(story_id,action,created_at,dwell_ms) VALUES(?,?,?,?)",
                     (story_id, body.action, db.now_iso(), body.dwell_ms))
    if body.action in ("like", "dislike", "dismiss"):
        await interests.apply_feedback(story_id, body.action)
    if body.action == "dismiss":
        await db.execute("UPDATE stories SET tier='more' WHERE id=? AND tier='primary'", (story_id,))
    return {"ok": True}


@router.post("/stories/{story_id}/save")
async def save_story(story_id: str):
    await db.execute("INSERT OR IGNORE INTO saved(story_id,saved_at) VALUES(?,?)", (story_id, db.now_iso()))
    await db.execute("INSERT INTO feedback(story_id,action,created_at) VALUES(?,?,?)", (story_id, "save", db.now_iso()))
    await interests.apply_feedback(story_id, "save")
    return {"saved": True}


@router.delete("/stories/{story_id}/save")
async def unsave_story(story_id: str):
    await db.execute("DELETE FROM saved WHERE story_id=?", (story_id,))
    return {"saved": False}


@router.get("/saved")
async def list_saved():
    rows = await db.fetch_all(CARD_SQL + " WHERE s.id IN (SELECT story_id FROM saved) ORDER BY "
                              "(SELECT saved_at FROM saved v WHERE v.story_id=s.id) DESC")
    return [card(r) for r in rows]


@router.get("/search")
async def search(q: str, limit: int = 30):
    words = [w for w in re.split(r"[\s\-–]+", q.strip()) if len(w) >= 2][:6]
    if not words:
        return []
    # every word must appear somewhere in the story (any order), so "IIT professor" finds "IIT-Bombay … suspended professor"
    one = "(s.headline LIKE ? OR s.dek LIKE ? OR s.what_happened LIKE ? OR s.headline_original LIKE ? OR s.tags LIKE ?)"
    args: list = []
    for w in words:
        args += [f"%{w}%"] * 5
    rows = await db.fetch_all(
        CARD_SQL + f" WHERE s.status='complete' AND s.tier IN ('primary','more','wire') AND {' AND '.join([one] * len(words))} "
                   "ORDER BY s.outlet_count DESC, s.published_at DESC LIMIT ?", (*args, limit))
    return [card(r) for r in rows]


# ── grounded chat ────────────────────────────────────────────────────────────

class ChatIn(BaseModel):
    message: str


@router.get("/stories/{story_id}/chat")
async def chat_history(story_id: str):
    return await db.fetch_all("SELECT role,content,created_at FROM messages WHERE story_id=? ORDER BY id", (story_id,))


@router.post("/stories/{story_id}/chat")
async def chat(story_id: str, body: ChatIn):
    s = await db.fetch_one("SELECT * FROM stories WHERE id=?", (story_id,))
    if not s:
        raise HTTPException(404, "Story not found")
    s = db.story_from_row(s)
    srcs = await db.fetch_all(
        "SELECT ss.outlet,ss.title,ss.url,ss.excerpt,a.full_text FROM story_sources ss LEFT JOIN articles a ON a.url=ss.url "
        "WHERE ss.story_id=? ORDER BY ss.n LIMIT 4", (story_id,))
    ctx = [f"HEADLINE: {s['headline']}", f"SUMMARY: {s['what_happened']}", "KEY POINTS: " + " | ".join(s["key_points"] or [])]
    if s.get("report"):
        ctx.append("REPORT: " + " ".join(sec.get("text", "") for sec in (s["report"] or {}).get("sections", []))[:2500])
    for i, r in enumerate(srcs, 1):
        ctx.append(f"[{i}] {r['outlet']}: {(r['full_text'] or r['excerpt'] or '')[:1500]}")
    system = ("You are ContextLens, a careful news assistant. Answer the reader's question using ONLY the story context below. "
              "Cite sources like [1] when you use them. If the context does not contain the answer, say so plainly — never guess. "
              "Be concise (max 6 sentences).\n\n" + "\n".join(ctx)[:7000])
    hist = await db.fetch_all("SELECT role,content FROM messages WHERE story_id=? ORDER BY id DESC LIMIT 6", (story_id,))
    messages = [{"role": "system", "content": system}] + list(reversed(hist)) + [{"role": "user", "content": body.message[:600]}]
    await db.execute("INSERT INTO messages(story_id,role,content,created_at) VALUES(?,?,?,?)", (story_id, "user", body.message[:600], db.now_iso()))

    async def gen():
        full = ""
        try:
            async for piece in llm.stream_chat("writer", messages, max_tokens=520):
                full += piece
                yield f"data: {json.dumps({'t': 'chunk', 'c': piece})}\n\n"
        except LLMError as e:
            yield f"data: {json.dumps({'t': 'error', 'c': str(e)[:200]})}\n\n"
        if full.strip():
            await db.execute("INSERT INTO messages(story_id,role,content,created_at) VALUES(?,?,?,?)",
                             (story_id, "assistant", full.strip(), db.now_iso()))
        yield f"data: {json.dumps({'t': 'done'})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# ── explain it simply (on demand, grounded in the story's own text, cached) ───────────────

class ExplainIn(BaseModel):
    level: str = "simple"


@router.post("/stories/{story_id}/explain")
async def explain(story_id: str, body: ExplainIn):
    s = await db.fetch_one("SELECT headline,dek,what_happened,why_it_matters,key_points,extras FROM stories WHERE id=?", (story_id,))
    if not s:
        raise HTTPException(404, "Story not found")
    extras = db.jload(s["extras"], {}) or {}
    if extras.get("simple"):
        return {"text": extras["simple"], "cached": True}
    pts = "; ".join(db.jload(s["key_points"], []) or [])
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
    try:
        out = await llm.chat_json(
            "writer",
            "You explain news to someone with NO background. Use only the facts given. Short sentences, plain words, "
            "define any technical term in a few words, no jargon, no opinions. Max 110 words. Start with what happened.",
            "\n".join([f"HEADLINE: {s['headline']}", f"SUMMARY: {s['what_happened']}",
                       f"KEY POINTS: {pts}", f"WHY IT MATTERS: {s['why_it_matters']}"]),
            schema, schema_name="simple", max_tokens=320, retries=1,
            validate=lambda o: None if 40 <= len(o["text"]) <= 900 and not llm.looks_like_garbage(o["text"]) else "bad length or content")
    except LLMError as e:
        raise HTTPException(503, f"The language model is unavailable: {str(e)[:120]}")
    extras["simple"] = out["text"].strip()
    await db.execute("UPDATE stories SET extras=? WHERE id=?", (db.jdump(extras), story_id))
    return {"text": extras["simple"], "cached": False}
