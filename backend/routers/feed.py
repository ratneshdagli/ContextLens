"""Feed + run-progress endpoints (v4: instant wire tier + progressively built stories)."""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from backend import db
from backend.config import TRUSTED_SOURCES
from backend.services import channels, pipeline, runs
from backend.settings_store import get_settings

router = APIRouter(prefix="/api", tags=["feed"])
CHANNEL_TASTE = 2        # stories per channel on the main "All" feed; the channel's own tab shows everything

# Outlets whose job is testing claims. A story one of them covered gets a "Fact-check" tag and can be filtered on.
FACT_DOMAINS = sorted({s["domain"] for s in TRUSTED_SOURCES if s.get("type") == "fact-checker"})
FACT_COND = "(" + " OR ".join(f"a.domain LIKE '%{d}'" for d in FACT_DOMAINS) + ")" if FACT_DOMAINS else "(0)"

# Stories that are graphic or distressing. The reader can hide them ("Calm mode") without losing them: they stay
# in Everything. Words like "war"/"attack" only count for general news, not markets or sport ("trade war", "attack on goal").
_HEAVY = re.compile(r"\b(kill(?:ed|s|ing)?|dead|deaths?|dies|died|murder(?:ed|s)?|shot dead|shooting|massacre|bomb(?:ing|ed|s)?|suicide|"
                    r"rape[ds]?|assault(?:ed)?|terror(?:ist|ists|ism)?|hostages?|stabbed|lynch(?:ed|ing)?|tortur(?:e|ed)|casualt(?:y|ies)|"
                    r"beheaded|abducted|molest(?:ed|ation)?|harass(?:ed|ment)|airstrikes?)\b", re.I)
_HEAVY_NEWS = re.compile(r"\b(wars?|attacks?|attacked|crash(?:ed)?|explosions?|blasts?)\b", re.I)


def is_heavy(headline: str, dek: str, topic: str) -> bool:
    text = f"{headline}. {dek or ''}"
    if _HEAVY.search(text):
        return True
    return topic in ("world", "india", "politics") and bool(_HEAVY_NEWS.search(headline))


def _age_h(iso):
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 3600)
    except Exception:
        return 48.0


CARD_SQL = """
SELECT s.id,s.kind,s.headline,s.headline_original,s.dek,s.topic,s.tags,s.outlet_count,s.trust_score,s.reading_minutes,
       s.image_url,s.published_at,s.created_at,s.why_for_you,s.matched_interest,s.tier,s.gate,s.query,s.built_at,s.interest_match,
       s.channel_id, (SELECT name FROM channels WHERE id=s.channel_id) AS channel_name,
       (length(s.dek)+length(s.what_happened)+length(s.key_points)) AS brief_chars,
       json_extract(s.extras,'$.explain.gist') AS gist,
       json_extract(s.extras,'$.explain.why') AS why_json,
       (SELECT COUNT(*) FROM timeline_events t WHERE t.story_id=s.id) AS n_timeline,
       (SELECT COUNT(*) FROM relations r WHERE r.story_id=s.id) AS n_relations,
       (SELECT COUNT(*) FROM thread_stories ts2 WHERE ts2.thread_id=(SELECT thread_id FROM thread_stories WHERE story_id=s.id)) AS thread_size,
       (SELECT json_array_length(c.merged) FROM clusters c WHERE c.id=s.cluster_id) AS angles,
       (SELECT COUNT(*) FROM articles a WHERE a.cluster_id=s.cluster_id AND FACT_COND) AS n_fact,
       EXISTS(SELECT 1 FROM saved v WHERE v.story_id=s.id) AS saved,
       (SELECT action FROM feedback f WHERE f.story_id=s.id AND f.action IN ('like','dislike') ORDER BY f.id DESC LIMIT 1) AS reaction
FROM stories s
""".replace("FACT_COND", FACT_COND)


def signal_of(gate: dict) -> int:
    """Signal 1-10: how significant this story is, independent of the reader. Coverage breadth (how many independent
    outlets ran it) 50%, the editor model's impact read 35%, source quality 15%. Personal fit is shown separately
    ("matches your interest in ...")."""
    comp = (gate.get("rank") or {}).get("components") or {}
    impact = (((gate.get("triage") or {}).get("answers")) or {}).get("impact")
    imp = {"local_minor": 0.15, "notable": 0.5, "major": 0.85, "historic": 1.0}.get(impact, 0.5)
    x = 0.5 * comp.get("coverage", 0.0) + 0.35 * imp + 0.15 * comp.get("trust", 0.5)
    return int(max(1, min(10, round(10 * x))))


def card(r: dict) -> dict:
    gate = db.jload(r.get("gate"), {}) or {}
    built = r["tier"] in ("primary", "more") and bool(r.get("built_at"))
    why = db.jload(r.get("why_json"), []) or []
    return {
        "id": r["id"], "kind": r["kind"], "headline": r["headline"], "headline_original": r["headline_original"],
        "dek": r["dek"], "topic": ("ch:" + r["channel_id"]) if r.get("channel_name") else ("world" if (r["topic"] or "").startswith("ch:") else r["topic"]),
        "channel": (r.get("channel_id") or "") if r.get("channel_name") else "", "channel_name": r.get("channel_name") or "",
        "tags": db.jload(r["tags"], []), "outlet_count": r["outlet_count"],
        "trust_score": r["trust_score"], "reading_minutes": r["reading_minutes"], "image_url": r["image_url"],
        "published_at": r["published_at"] or r["created_at"], "why_for_you": r["why_for_you"],
        "matched_interest": r["matched_interest"], "interest": r.get("interest_match") or 0, "tier": r["tier"],
        "built": built, "gist": r.get("gist") or "", "peek": [w.get("cause") for w in why[:3] if isinstance(w, dict)],
        "has_timeline": r["n_timeline"] > 0, "has_map": r["n_relations"] > 0, "thread_size": r.get("thread_size") or 0,
        "saved": bool(r["saved"]), "reaction": r["reaction"], "angles": r.get("angles") or 0,
        "factcheck": bool(r.get("n_fact")), "heavy": is_heavy(r["headline"], r["dek"], r["topic"]),
        "rank": (gate.get("rank") or {}).get("score") or 0.0, "signal": signal_of(gate),
        "brief_seconds": int((r.get("brief_chars") or 800) / 1000 * 60),
    }


@router.get("/feed")
async def get_feed(topic: str = "all", limit: int = 90, calm: bool = False):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = (now - timedelta(days=3)).isoformat()
    # a story whose channel is gone (deleted mid-refresh) is ordinary news; one in a switched-off channel is hidden with it
    rows = await db.fetch_all(CARD_SQL + " WHERE s.kind='feed' AND s.tier IN ('primary','more','wire') "
                              "AND (s.channel_id='' OR s.channel_id NOT IN (SELECT id FROM channels)) "
                              "AND COALESCE(s.published_at,s.created_at) > ? ORDER BY COALESCE(s.published_at,s.created_at) DESC LIMIT 400",
                              (cutoff,))
    # channel stories are fetched separately and reach further back: niche topics publish slowly, and 400 newer general stories must not crowd them out
    rows += await db.fetch_all(CARD_SQL + " WHERE s.kind='feed' AND s.tier IN ('primary','more','wire') "
                               "AND s.channel_id IN (SELECT id FROM channels WHERE weight>0) "
                               "AND COALESCE(s.published_at,s.created_at) > ? ORDER BY COALESCE(s.published_at,s.created_at) DESC LIMIT 300",
                               ((now - timedelta(days=7)).isoformat(),))
    cards = [card(r) for r in rows]
    min_sig = (await get_settings())["min_signal"]
    cards = [c for c in cards if c["tier"] == "more" or c["channel"] or c["signal"] >= min_sig]   # niche stories have few outlets: Signal can't judge them
    ch_counts: dict[str, int] = {}                                 # counted before the front page is cut to `limit`, so a niche channel never shows 0
    for c in cards:
        if c["channel"] and c["tier"] in ("primary", "wire") and not (calm and c["heavy"]):
            ch_counts[c["topic"]] = ch_counts.get(c["topic"], 0) + 1
    hidden = 0
    if calm:                                                       # "Calm mode": distressing stories move out of the front page
        n0 = len(cards)
        cards = [c for c in cards if not c["heavy"]]
        hidden = n0 - len(cards)
    if topic != "all":
        cards = [c for c in cards if c["topic"] == topic]          # a channel's stories carry topic "ch:<id>"
    tier_bonus = {"primary": 0.12, "wire": 0.0, "more": -0.15}
    in_channel = topic.startswith("ch:")
    for c in cards:      # a channel's own tab is its whole list (slow decay); on the main feed channels compete like any other news
        c["_d"] = (c["rank"] + tier_bonus.get(c["tier"], 0)) * math.exp(-_age_h(c["published_at"]) / (96.0 if (c["channel"] and in_channel) else 36.0))
    if topic == "all":
        for c in cards:
            if c["channel"]:
                c["_d"] *= 0.9                                  # a taste of a channel belongs after the day's biggest news, not on top of it
    front = sorted([c for c in cards if c["tier"] in ("primary", "wire")], key=lambda c: -c["_d"])
    if topic == "all":                                           # the main feed shows a taste of each channel, not all of it: the channel tab has the rest
        seen: dict[str, int] = {}
        kept = []
        for c in front:
            if c["channel"]:
                seen[c["channel"]] = seen.get(c["channel"], 0) + 1
                if seen[c["channel"]] > CHANNEL_TASTE:
                    continue
            kept.append(c)
        front = kept
    front = front[:limit]
    more = sorted([c for c in cards if c["tier"] == "more" and (in_channel or not c["channel"])], key=lambda c: -c["_d"])[:20]
    for c in front + more:
        c.pop("_d", None)
    lead = next((c for c in front if c["built"]), front[0] if front else None)
    rest = [c for c in front if not lead or c["id"] != lead["id"]]
    last = await db.kv_get("last_feed_run")
    st = (last or {}).get("stats") or {}
    funnel = None
    if st.get("ingest") and st.get("cluster"):
        funnel = [{"label": "articles read", "n": st["ingest"].get("fresh", 0)},
                  {"label": "distinct events", "n": st["cluster"].get("clusters", 0)},
                  {"label": "on your front page", "n": (st.get("wire") or {}).get("shown", len(front))},
                  {"label": "fully explained", "n": sum(1 for c in front if c["built"])}]
    topics: dict[str, int] = {}
    for c in front:
        topics[c["topic"]] = topics.get(c["topic"], 0) + 1
    chans = [{"id": ch["id"], "name": ch["name"], "description": ch["description"], "count": ch_counts.get("ch:" + ch["id"], 0)}
             for ch in await channels.list_channels() if ch["weight"] > 0]
    return {"funnel": funnel, "lead": lead, "stories": rest, "more": more, "updated_at": (last or {}).get("at"), "channels": chans,
            "running": runs.active("feed"), "topic_counts": topics, "total": len(front),
            "built": sum(1 for c in front if c["built"]), "building": sorted(pipeline._building), "hidden": hidden}


@router.get("/feed/all")
async def all_stories(topic: str = "all", q: str = "", source: str = "", factcheck: bool = False, days: int = 3, offset: int = 0, limit: int = 60):
    """Everything that was reported, not just what ranks: compact rows, filterable by topic, outlet and text."""
    cutoff = (datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=max(1, min(days, 7)))).isoformat()
    where = ["s.kind='feed'", "s.tier IN ('primary','more','wire')", "COALESCE(s.published_at,s.created_at) > ?"]
    args: list = [cutoff]
    for w in [w for w in re.split(r"[\s\-–]+", q.strip()) if len(w) >= 2][:6]:      # all words, any order
        where.append("(s.headline LIKE ? OR s.dek LIKE ?)")
        args += [f"%{w}%"] * 2
    if source.strip():
        where.append("s.cluster_id IN (SELECT cluster_id FROM articles WHERE outlet=? AND cluster_id IS NOT NULL)")
        args.append(source.strip())
    if factcheck:
        where.append(f"s.cluster_id IN (SELECT a.cluster_id FROM articles a WHERE a.cluster_id IS NOT NULL AND {FACT_COND})")
    base = " AND ".join(where)
    counts = await db.fetch_all(f"SELECT s.topic, COUNT(*) n FROM stories s WHERE {base} GROUP BY s.topic", tuple(args))     # ignores the topic filter
    if topic.startswith("ch:"):                                    # a reader channel
        cond, cargs = base + " AND s.channel_id=?", args + [topic[3:]]
    else:
        cond, cargs = (base + " AND s.topic=?", args + [topic]) if topic != "all" else (base, args)
    total = (await db.fetch_one(f"SELECT COUNT(*) n FROM stories s WHERE {cond}", tuple(cargs)))["n"]
    rows = await db.fetch_all(CARD_SQL + f" WHERE {cond} ORDER BY s.outlet_count DESC, COALESCE(s.published_at,s.created_at) DESC LIMIT ? OFFSET ?",
                              tuple(cargs) + (max(1, min(limit, 120)), max(0, offset)))
    items = []
    for r in rows:
        c = card(r)
        items.append({k: c[k] for k in ("id", "headline", "dek", "topic", "outlet_count", "published_at", "tier", "built", "angles", "signal", "gist", "factcheck", "heavy")})
    return {"total": total, "items": items, "topic_counts": {c["topic"]: c["n"] for c in counts}, "offset": offset}


@router.post("/feed/refresh")
async def refresh_feed():
    rid = runs.active("feed")
    if rid:
        return {"run_id": rid, "already_running": True}
    asyncio.create_task(pipeline.run_feed_cycle("manual"))
    for _ in range(50):                      # wait until the run id exists
        await asyncio.sleep(0.05)
        rid = runs.active("feed")
        if rid:
            break
    return {"run_id": rid, "already_running": False}


@router.get("/runs")
async def list_runs(limit: int = 15):
    rows = await db.fetch_all("SELECT id,kind,label,started_at,finished_at,status,progress,stage,stats,error,story_id "
                              "FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
    return [{**r, "stats": db.jload(r["stats"], {})} for r in rows]


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    r = await db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not r:
        raise HTTPException(404, "run not found")
    r["stats"] = db.jload(r["stats"], {})
    r["events"] = await runs.load_events(run_id)
    return r


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str):
    async def gen():
        for ev in runs.history(run_id):
            yield {"data": json.dumps(ev)}
        row = await db.fetch_one("SELECT status FROM runs WHERE id=?", (run_id,))
        if row and row["status"] != "running":
            yield {"data": json.dumps({"__done__": True, "status": row["status"]})}
            return
        q = runs.subscribe(run_id)
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}
                    continue
                yield {"data": json.dumps(ev)}
                if ev.get("__done__"):
                    break
        finally:
            runs.unsubscribe(run_id, q)
    return EventSourceResponse(gen())
