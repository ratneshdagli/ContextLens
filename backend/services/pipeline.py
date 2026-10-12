"""ContextLens v4 — the feed pipeline.

  ingest → embed+cluster → score (deterministic, vs. the reader's interests)
       → WIRE tier: the top `wire_size` events appear on the front page immediately (no LLM; cleaned outlet headline)
       → BUILD queue (background, best first): editor triage → writer card → entities → history timeline
         → explainer (why / what led to it) → editor publish gate → persist (tier primary | more | rejected)
       → slow extras (evidenced story map) for primary stories afterwards.

Any story a reader opens while it is still "wire" is built immediately at interactive priority (`build_now`).
Work is phase-batched per model so LM Studio swaps models a few times per cycle, and lms.py guarantees that only
one chat model is resident. Every decision is stored with its reasons in `stories.gate`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from typing import Optional

import numpy as np

from backend import db
from backend.config import FEEDS, STRICTNESS
from backend.services import (background, channels, clustering, context, editor, explainer, feeds, graph, interests, kb, lens,
                              llm, ranking, runs, story_builder, timeline)
from backend.services.llm import LLMError
from backend.settings_store import get_settings

log = logging.getLogger("pipeline")
_lock = asyncio.Lock()
_building: set[str] = set()
_build_runs: dict[str, str] = {}          # story id -> run id for reader-triggered builds
BATCH = 4
MAX_BUILD_FAILS = 2
BUILD_V = 3       # bump when the build path improves (JSON repair, snippet fallback): stories that gave up earlier retry

date_signals = timeline.date_signals


# ── candidate loading ─────────────────────────────────────────────────────────

async def load_candidates(window_hours: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    clusters = await db.fetch_all(
        "SELECT id,title,topic,first_seen,last_seen,outlet_count,article_count,centroid,story_id,triage "
        "FROM clusters WHERE last_seen >= ?", (cutoff,))
    arts = await db.fetch_all(
        "SELECT cluster_id,url,title,snippet,outlet,domain,trust_rank,image_url,published_at FROM articles "
        "WHERE cluster_id IS NOT NULL AND published_at >= ?", (cutoff,))
    by: dict[str, list[dict]] = {}
    for a in arts:
        by.setdefault(a["cluster_id"], []).append(a)
    out = []
    for c in clusters:
        v = llm.from_blob(c["centroid"])
        if v is None or c["id"] not in by:
            continue
        out.append({**c, "centroid": v, "members": by[c["id"]], "triage": db.jload(c["triage"])})
    return out


async def load_cluster(cluster_id: str) -> Optional[dict]:
    c = await db.fetch_one("SELECT id,title,topic,first_seen,last_seen,outlet_count,article_count,centroid,story_id,triage "
                           "FROM clusters WHERE id=?", (cluster_id,))
    if not c:
        return None
    arts = await db.fetch_all("SELECT cluster_id,url,title,snippet,outlet,domain,trust_rank,image_url,published_at "
                              "FROM articles WHERE cluster_id=?", (cluster_id,))
    return {**c, "centroid": llm.from_blob(c["centroid"]), "members": arts, "triage": db.jload(c["triage"])}


def _profile_hash(profile: dict, strictness: str) -> str:
    import hashlib
    keep = {k: profile[k] for k in ("interests", "topics", "muted_keywords", "followed")}
    return hashlib.sha1((db.jdump(keep) + strictness).encode()).hexdigest()[:10]


def _honest_why(rank: dict, decision: dict, cluster: dict) -> str:
    """Only claim an interest match when the editor model agrees that the story is on-topic for the reader."""
    ans = (decision or {}).get("answers") or {}
    why = rank.get("why", "")
    if rank.get("channel"):                                     # the reader made this channel: membership is the honest reason
        return why
    if rank.get("matched_interest") and ans.get("matches_reader") == "none":
        return f"covered by {cluster['outlet_count']} outlets" if cluster["outlet_count"] >= 3 else f"top {cluster['topic']} story"
    return why


# ── wire tier (no LLM) ────────────────────────────────────────────────────────

_OUTLET_TAIL = re.compile(r"\s+[-–—|]\s+[^-–—|]{2,40}$")


def clean_headline(t: str) -> str:
    t = feeds.clean_text(t or "")
    t = _OUTLET_TAIL.sub("", t).strip()
    return t[:180]


def _wire_fields(c: dict, rank: dict) -> dict:
    mem = sorted(c["members"], key=lambda m: (m["trust_rank"], -len(m.get("snippet") or "")))
    lead = mem[0]
    dek = max((m.get("snippet") or "" for m in mem), key=len)
    dek = feeds.clean_text(dek)
    if dek.lower().startswith(clean_headline(lead["title"]).lower()[:40]):
        dek = ""
    image = next((m["image_url"] for m in mem if m.get("image_url")), "")
    return dict(headline=clean_headline(lead["title"]), headline_original=lead["title"], dek=dek[:260], topic=c["topic"],
                outlet_count=c["outlet_count"], image_url=image)


async def persist_wire(c: dict, rank: dict, existing: Optional[dict]) -> Optional[str]:
    """Insert (or refresh) the instant, LLM-free version of a story. Built stories are never downgraded."""
    now = db.now_iso()
    f = _wire_fields(c, rank)
    gate = {"rank": {k: rank.get(k) for k in ("score", "components", "why", "matched_interest", "channel")}, "wire": True}
    if existing:
        if existing["tier"] == "wire":
            await db.execute("UPDATE stories SET outlet_count=?, gate=?, why_for_you=?, matched_interest=?, interest_match=?, "
                             "published_at=?, channel_id=? WHERE id=?",
                             (c["outlet_count"], db.jdump({**(db.jload(existing["gate"], {}) or {}), **gate}), rank.get("why", ""),
                              rank.get("matched_interest", ""), rank["components"]["interest"], c["last_seen"],
                              rank.get("channel") or "", existing["id"]))
        return existing["id"]
    sid = db.new_id()
    await db.execute(
        "INSERT INTO stories(id,kind,cluster_id,created_at,updated_at,headline,headline_original,dek,topic,outlet_count,image_url,"
        "tier,gate,status,published_at,why_for_you,matched_interest,interest_match,centroid,reading_minutes,trust_score) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "feed", c["id"], now, now, f["headline"], f["headline_original"], f["dek"], f["topic"], f["outlet_count"],
         f["image_url"], "wire", db.jdump(gate), "complete", c["last_seen"], rank.get("why", ""), rank.get("matched_interest", ""),
         rank["components"]["interest"], llm.to_blob(c["centroid"]), 1,
         round(float(np.mean([(5 - min(m["trust_rank"], 4)) / 4 for m in c["members"]])), 3)))
    await db.execute("UPDATE clusters SET story_id=? WHERE id=?", (sid, c["id"]))
    await db.execute("UPDATE stories SET channel_id=? WHERE id=?", (rank.get("channel") or "", sid))
    try:
        await kb.assign_thread(sid)
    except Exception as e:  # noqa: BLE001 — threads are an enhancement, never block the feed
        log.info("thread assign failed: %s", str(e)[:80])
    return sid


# ── the expensive part: one story, end to end ─────────────────────────────────

async def write_phase(c: dict, story_id: str, s: dict, emit=None) -> dict:
    """Writer-model work for one event: full text → card → entities → history timeline → explainer.
    `emit(msg)` is an optional async progress callback."""
    async def say(msg: str):
        if emit:
            await emit(msg)

    await say(f"Reading {c['outlet_count']} outlet(s)…")
    g = await story_builder.gather(c["id"], s["depth"])
    await say("Writing the story card…")
    card = await story_builder.write_card(g, s["reading_level"])
    texts = g["texts"]
    ents: list[dict] = []
    if texts:
        try:
            await say("Finding the people, places and organisations…")
            ents = await graph.extract_entities([t["text"] for t in texts], card["headline"], s.get("ner_enabled", True))
        except Exception as e:  # noqa: BLE001
            log.info("entity extraction failed: %s", str(e)[:80])
    names = [e["name"] for e in ents]

    hist = await kb.thread_history(story_id)
    thread_rows = [{"story_id": r["story_id"], "headline": r["headline"], "dek": r["dek"], "at": r["at"]} for r in hist["earlier"]]
    tl = {"events": [], "shown": False, "reason": "no earlier coverage found", "history": [], "raw_count": 0}
    try:
        await say("Tracing how the story developed…")
        tl = await background.build_story_timeline(
            {"id": story_id, "headline": card["headline"], "dek": card["dek"], "what_happened": card["what_happened"],
             "first_seen": c["first_seen"], "vec": c["centroid"], "thread_rows": thread_rows},
            texts, names, {m["url"] for m in g["all_members"]})
    except Exception as e:  # noqa: BLE001
        log.warning("timeline failed: %s", traceback.format_exc()[-300:])
        tl["reason"] = f"timeline error: {str(e)[:80]}"

    primer_items: list[dict] = []
    try:
        primer_items = await context.background_for(ents) if ents else []
    except Exception:  # noqa: BLE001 — network optional
        primer_items = []
    primer = " ".join(f"{p['title']}: {p['extract']}" for p in primer_items)[:700]

    hist_events = tl["events"] or [
        {"date": h["dt"].date().isoformat(), "text": h["title_clean"], "source_url": h["url"], "source_outlet": h["outlet"]}
        for h in tl["history"]]
    explain = None
    for attempt in range(2):                             # one retry: a model swap or a busy server can fail a single call
        try:
            await say("Working out why it happened…")
            explain = await explainer.write_explainer(card, texts, g["members"], hist_events, primer, s["reading_level"], names)
            break
        except LLMError as e:
            log.info("explainer failed for %s (attempt %d): %s", story_id, attempt + 1, str(e)[:300])
            await asyncio.sleep(4)
    return {"g": g, "card": card, "entities": ents, "timeline": tl, "explain": explain, "primer": primer_items}


async def persist_built(story_id: str, c: dict, d: dict, decision: dict, rank: dict, publish: dict) -> None:
    now = db.now_iso()
    g, card, tl = d["g"], d["card"], d["timeline"]
    members = g["members"]
    trust = float(np.mean([(5 - min(m["trust_rank"], 4)) / 4 for m in members]))
    longest = max((len(t["text"].split()) for t in g["texts"]), default=180)
    minutes = int(min(8, max(1, round(longest / 230))))
    lead = g["lead"]
    image = next((m["image_url"] for m in [lead] + members if m.get("image_url")), "")
    tier = publish["tier"]
    pending_map = tier == "primary" and len(g["texts"]) >= 2 and c["outlet_count"] >= 3
    gate = {"rank": {k: rank.get(k) for k in ("score", "components", "why", "matched_interest", "channel")},
            "triage": decision, "publish": publish,
            "timeline": {"shown": tl["shown"], "reason": tl["reason"], "raw_count": tl.get("raw_count", 0)},
            "graph": {"shown": False, "reason": "pending" if pending_map else "not applicable"},
            "explain": {"ok": bool(d["explain"]), "grounded_steps": (d["explain"] or {}).get("grounded_steps", 0),
                        "dropped": (d["explain"] or {}).get("dropped", [])},
            "built_articles": c["article_count"], "built_at": now, "cfg": c.get("_cfg", "")}
    texts_n = [dict(t, n=i) for i, t in enumerate(g["texts"], 1)]
    extras = {"claims": card.get("_claims", []),
              "lens": lens.build_lens(texts_n, g["members"], f"{card['headline']}. {card['dek']} {card['what_happened']}"),
              "explain": d["explain"], "background": d["primer"]}
    sid = story_id
    async with db.get_db() as conn:
        for tbl in ("story_sources", "timeline_events", "entities", "relations"):
            await conn.execute(f"DELETE FROM {tbl} WHERE story_id=?", (sid,))
        vals = dict(
            updated_at=now, headline=card["headline"], headline_original=lead["title"],
            dek=card["dek"], what_happened=card["what_happened"], why_it_matters=card["why_it_matters"],
            why_for_you=_honest_why(rank, decision, c), key_points=db.jdump(card["key_points"]), topic=c["topic"],
            tags=db.jdump(card["tags"]), trust_score=round(trust, 3), interest_match=rank["components"]["interest"],
            matched_interest=rank.get("matched_interest", ""), outlet_count=c["outlet_count"], channel_id=rank.get("channel") or "",
            reading_minutes=minutes, image_url=image, tier=tier, gate=db.jdump(gate),
            verification=db.jdump(card["_grounding"]), status="complete", error=None, built_at=now,
            published_at=c["last_seen"], extras=db.jdump(extras))
        sets = ",".join(f"{k}=?" for k in vals)
        await conn.execute(f"UPDATE stories SET {sets} WHERE id=?", (*vals.values(), sid))
        await conn.executemany(
            "INSERT INTO story_sources(story_id,n,url,outlet,domain,title,trust_rank,published_at,excerpt,has_full_text) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", story_builder.source_rows(g, sid))
        await conn.executemany(
            "INSERT INTO timeline_events(story_id,date,precision,text,quote,source_url,source_outlet,kind) VALUES(?,?,?,?,?,?,?,?)",
            [(sid, e["date"], e["precision"], e["text"], e["quote"], e["source_url"], e["source_outlet"], e.get("kind", "reported"))
             for e in tl["events"]])
        await conn.executemany(
            "INSERT INTO entities(story_id,name,type,mentions) VALUES(?,?,?,?)",
            [(sid, e["name"], e["type"], e["mentions"]) for e in d["entities"]])
        await conn.execute("UPDATE clusters SET story_id=? WHERE id=?", (sid, c["id"]))
        await conn.commit()
    if tier in ("primary", "more"):
        try:
            await kb.assign_thread(sid)
            await kb.register_entities(sid, d["entities"])
            await kb.link_related(sid)
        except Exception as e:  # noqa: BLE001
            log.info("kb linking failed: %s", str(e)[:100])


async def _mark_failed(story_id: str, err: str) -> None:
    row = await db.fetch_one("SELECT gate FROM stories WHERE id=?", (story_id,))
    gate = db.jload((row or {}).get("gate"), {}) or {}
    gate["build_fails"] = (int(gate.get("build_fails", 0)) if gate.get("build_v") == BUILD_V else 0) + 1
    gate["build_v"] = BUILD_V
    gate["build_error"] = err[:200]
    await db.execute("UPDATE stories SET gate=? WHERE id=?", (db.jdump(gate), story_id))


async def rank_cluster(c: dict, profile: dict, chans: Optional[list[dict]] = None) -> dict:
    items, ivecs = await interests.interest_vectors(profile)
    taste, _ = await interests.get_taste()
    chans = await channels.load_ready() if chans is None else chans
    r = ranking.score_cluster(c, profile, items, ivecs, taste, chans)
    return {**c, **r}


def _cfg(ch: dict) -> str:
    import hashlib
    return hashlib.sha1((channels.text_of(ch) + "|v3").encode()).hexdigest()[:6]     # "v3": the editor judges channel stories as that channel's follower


def _channel_ctx(c: dict, chmap: dict, brief: str, ph: str, strict: str, s: dict) -> tuple[str, str, str, dict]:
    """(brief, triage-cache key, strictness, settings) for one event. A channel is judged on its own terms (its description is the
    reader's interest), relaxes strictness (the reader asked for this topic) and sets how deep the story is written.
    Also stamps c["_cfg"]: stories the editor rejects are re-judged when the channel's definition changes."""
    ch = chmap.get(c.get("channel") or "")
    if not ch:
        return brief, ph, strict, s
    c["_cfg"] = _cfg(ch)
    return channels.brief_line(ch), ph + c["_cfg"], "lenient", {**s, "depth": ch["depth"]}


async def build_now(story_id: str, run: Optional["runs.Run"] = None) -> Optional[str]:
    """Build one wire story immediately (a reader opened it). Interactive priority; always ends visible (never 'rejected')."""
    if story_id in _building:
        return story_id
    row = await db.fetch_one("SELECT id,cluster_id,tier,channel_id FROM stories WHERE id=?", (story_id,))
    if not row or not row["cluster_id"]:
        return None
    _building.add(story_id)
    llm.interactive()
    own_run = run is None
    if own_run:
        run = await runs.Run.start("build", "Explaining story")
        await db.execute("UPDATE runs SET story_id=? WHERE id=?", (story_id, run.id))
    _build_runs[story_id] = run.id
    try:
        s = await get_settings()
        profile = await interests.get_profile()
        c = await load_cluster(row["cluster_id"])
        if not c or c["centroid"] is None:
            raise LLMError("story data is no longer available")
        chans = await channels.load_ready()
        c = await rank_cluster(c, profile, chans)
        if c.get("excluded"):                           # a mute (or a missing embedding) must not stop a story the reader opened, or drop its channel
            c = {**c, "components": {"interest": 0.0, "topic": 0, "coverage": 0, "trust": 0, "recency": 0, "taste": 0},
                 "why": "opened by the reader", "matched_interest": "", "channel": row["channel_id"] or None}
        c["decision"] = {"keep": True, "score": 0.0, "reasons": ["opened by the reader"], "answers": {}}

        async def say(msg: str):
            await run.emit("build", msg)

        brief, _, strict_c, sx = _channel_ctx(c, {x["id"]: x for x in chans}, interests.profile_brief(profile), "", s["strictness"], s)
        d = await write_phase(c, story_id, sx, say)
        await say("Editor is checking the draft…")
        pub = await editor.publish_gate(d["card"], brief, [m["title"] for m in d["g"]["members"]], d["card"]["_plain"],
                                        d["card"]["_grounding"], strict_c, c["outlet_count"], c["components"]["interest"], None,
                                        niche=bool(c.get("channel")))
        if pub["tier"] == "rejected":
            pub = {**pub, "tier": "more", "reasons": pub["reasons"] + ["shown because you opened it"]}
        await persist_built(story_id, c, d, c["decision"], c, pub)
        if own_run:
            await run.finish("complete")
        asyncio.create_task(enrich_modules(only=story_id))
        return story_id
    except Exception as e:  # noqa: BLE001
        log.warning("build_now failed: %s", traceback.format_exc()[-400:])
        await _mark_failed(story_id, str(e))
        if own_run:
            await run.emit("error", f"Could not build this story: {str(e)[:160]}", "error")
            await run.finish("failed", error=str(e)[:200])
        return None
    finally:
        _building.discard(story_id)
        _build_runs.pop(story_id, None)


# ── the feed cycle ────────────────────────────────────────────────────────────

async def run_feed_cycle(trigger: str = "manual", only_channel: Optional[str] = None) -> Optional[str]:
    """One refresh. `only_channel` limits it to a single channel's sources and stories (a fast first fill after creating one)."""
    if _lock.locked():
        return runs.active("feed")
    llm.background()                                   # a refresh started from a request must not outrank readers
    async with _lock:
        run = await runs.Run.start("feed", trigger)
        try:
            await _cycle(run, only_channel)
            await run.finish("complete")
            asyncio.create_task(enrich_modules())
        except Exception as e:  # noqa: BLE001
            log.error("feed cycle failed:\n%s", traceback.format_exc())
            await run.emit("error", f"Run failed: {str(e)[:200]}", "error")
            await run.finish("failed", error=str(e)[:300])
        return run.id


async def _cycle(run: "runs.Run", only_channel: Optional[str] = None):
    s = await get_settings()
    profile = await interests.get_profile()
    strict = s["strictness"]
    thr = STRICTNESS[strict]
    st = run.stats

    # 0 ── health
    await run.emit("start", "Checking language-model server…", progress=0.02)
    h = await llm.health()
    if not h.get("local"):  # clustering/ranking need the local embedding model; chat alone could fall back to the cloud
        raise LLMError(h.get("error", "LLM server unreachable"))
    await run.emit("start", f"Model server online. Writer: {s['model_writer']} · Editor: "
                            f"{s['model_writer'] if s['single_model_mode'] else s['model_editor']} (one model loaded at a time)", "success")

    # 1 ── ingest (the fixed newspaper list + every channel's own feeds and searches)
    chans = await channels.load_ready()
    chmap = {c["id"]: c for c in chans}
    if only_channel and only_channel not in chmap:
        raise LLMError("that channel no longer exists")
    ch_run = [c for c in chans if c["weight"] > 0 and (not only_channel or c["id"] == only_channel)]
    window = max([s["max_age_hours"]] + [c["days"] * 24 for c in ch_run])      # niche channels publish slowly: they keep a longer window
    await run.emit("ingest", "Fetching newspaper & wire feeds…" if not only_channel else f"Fetching sources for “{chmap[only_channel]['name']}”…", progress=0.05)
    extra = await channels.search_items(ch_run)
    ing = await feeds.ingest(s["max_age_hours"], feeds=channels.feed_list(ch_run) if only_channel else FEEDS + channels.feed_list(ch_run),
                             extra_items=extra, windows={channels.topic_of(c["id"]): c["days"] * 24 for c in ch_run})
    st["ingest"] = ing
    ch_note = f" · {len(ch_run)} channel(s), {len(extra)} search hits" if ch_run else ""
    await run.emit("ingest", f"{ing['feeds_ok']}/{ing['feeds_total']} feeds · {ing['new']} new articles ({ing['fresh']} fresh){ch_note}", "success", progress=0.15)

    # 2 ── embed + cluster
    n_emb = await clustering.embed_new_articles(window)
    cl = await clustering.run_clustering(window)
    st["cluster"] = cl
    merged_txt = f", {cl['merged']} duplicate stories merged" if cl.get("merged") else ""
    await run.emit("cluster", f"Embedded {n_emb} articles → {cl['clusters']} distinct events ({cl['new_clusters']} new{merged_txt})", "success", progress=0.25)

    # 3 ── deterministic scoring against the interest profile
    cands = await load_candidates(window)
    items, ivecs = await interests.interest_vectors(profile)
    taste, _ = await interests.get_taste()
    n_wire = int(s["wire_size"] * thr["topk_extra"])

    def score_all():                                      # thousands of events: CPU-bound, so off the event loop
        out, skipped = [], 0
        for c in cands:
            r = ranking.score_cluster(c, profile, items, ivecs, taste, chans)
            if r["excluded"]:
                skipped += 1
                continue
            if not r.get("channel") and ranking._age_hours(c["last_seen"]) > s["max_age_hours"]:
                continue                                  # general news keeps the normal window; only channels reach further back
            if only_channel and r.get("channel") != only_channel:
                continue
            out.append({**c, **r})
        general = [] if only_channel else ranking.mmr_select([x for x in out if not x.get("channel")], max(n_wire, s["candidate_pool"]))
        picks: list[dict] = []                            # every channel gets its own quota: niche topics must not compete with world news
        for ch in chans:
            if only_channel and ch["id"] != only_channel:
                continue
            picks += ranking.mmr_select([x for x in out if x.get("channel") == ch["id"]], ch["size"])
        return general, picks, skipped, len(out)
    general, ch_picks, excluded, _ = await asyncio.to_thread(score_all)
    pool = general + ch_picks
    st["scored"] = {"events": len(cands), "excluded": excluded, "pool": len(pool), "channel_events": len(ch_picks)}
    await run.emit("score", f"Scored {len(cands)} events against your interests ({excluded} excluded by mutes/topics)"
                            + (f"; {len(ch_picks)} fit your channels" if chans else ""), "success", progress=0.3)

    # 4 ── WIRE tier: instant front page, no LLM
    have = await db.fetch_all("SELECT id,cluster_id,tier,gate,published_at,json_extract(extras,'$.explain.v') AS ev "
                              "FROM stories WHERE kind='feed' AND cluster_id IS NOT NULL")
    by_cluster = {r["cluster_id"]: r for r in have}
    wired = 0
    for c in general[:n_wire] + ch_picks:
        ex = by_cluster.get(c["id"])
        if ex and ex["tier"] == "rejected":
            ch = chmap.get(c.get("channel") or "")
            if not ch or (db.jload(ex["gate"], {}) or {}).get("cfg") == _cfg(ch):
                continue
            # rejected before this channel existed in its current form (or under an older editor): judge it again
            await db.execute("UPDATE stories SET tier='wire' WHERE id=?", (ex["id"],))
            await db.execute("UPDATE clusters SET triage=NULL WHERE id=?", (c["id"],))
            ex = by_cluster[c["id"]] = {**ex, "tier": "wire"}
        if not ex and c["outlet_count"] < 2 and c["components"]["interest"] < 0.5 and not c.get("channel"):
            continue                                        # single-outlet items must at least match the reader's interests (niche channels are mostly single-outlet)
        sid = await persist_wire(c, c, ex)
        if sid:
            c["_story_id"] = sid
            wired += 1
            if ex and ex["tier"] != "wire":                 # an already-built story can join (or leave) a channel without a rebuild
                chid = c.get("channel") or ""
                await db.execute("UPDATE stories SET channel_id=?, why_for_you=CASE WHEN ?!='' THEN ? ELSE why_for_you END WHERE id=?",
                                 (chid, chid, c.get("why", ""), sid))
    st["wire"] = {"shown": wired}
    await run.emit("wire", f"Front page ready: {wired} stories are live now — the writers are upgrading them one by one", "success", progress=0.35)

    # 5 ── BUILD queue: triage → build → gate, best first, in small batches
    ph = _profile_hash(profile, strict)
    brief = interests.profile_brief(profile)
    fresh_cut = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
    queue = []
    for c in pool:
        sid = c.get("_story_id") or (by_cluster.get(c["id"]) or {}).get("id")
        ex = by_cluster.get(c["id"])
        if not sid:
            continue
        if ex and ex["tier"] in ("primary", "more", "rejected"):
            g0 = db.jload(ex["gate"], {}) or {}
            built = g0.get("built_articles", 0)
            stale = ex["tier"] != "rejected" and ((ex["ev"] or 0) < explainer.EXPLAIN_VERSION     # built before the current explainer
                                                  or bool(g0.get("rebuild")))                    # or merged with another cluster since
            if c["article_count"] < built + 3 and not stale:
                continue
        gate0 = db.jload(ex["gate"], {}) if ex else {}
        if (gate0 or {}).get("build_fails", 0) >= MAX_BUILD_FAILS and (gate0 or {}).get("build_v") == BUILD_V:
            continue                                        # gave up under *this* build logic; older failures get a fresh try
        c["_story_id"] = sid
        queue.append(c)
    per_ch: dict[str, int] = {}
    ch_queue = []
    for c in queue:                                         # channels have their own build budget (their size, at most 10 per refresh)
        cid = c.get("channel")
        if cid and per_ch.get(cid, 0) < min(chmap[cid]["size"], 10):
            per_ch[cid] = per_ch.get(cid, 0) + 1
            ch_queue.append(c)
    gen_queue = [c for c in queue if not c.get("channel")][: s["build_budget"]]
    queue = [c for pair in zip_longest(ch_queue, gen_queue) for c in pair if c is not None]     # alternate: a channel's stories must not wait behind the whole front page
    await run.emit("plan", f"{len(queue)} stories queued for full explanation (best first)", progress=0.38)

    built_n = {"primary": 0, "more": 0, "rejected": 0, "failed": 0}
    st["triage"] = {"kept": 0, "rejected": 0}
    step = 0.6 / max(len(queue), 1)
    for b in range(0, len(queue), BATCH):
        chunk = queue[b:b + BATCH]
        # 5a editor triage (cached per profile)
        keep = []
        for c in chunk:
            prev = c["triage"] if isinstance(c["triage"], dict) else None
            brief_c, ph_c, strict_c, _s = _channel_ctx(c, chmap, brief, ph, strict, s)
            if prev and prev.get("ph") == ph_c and prev.get("ac", 0) >= c["article_count"] - 1 and \
                    prev.get("at", "") > (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat():
                dec = prev["decision"]
            else:
                dec = await editor.triage(c, brief_c, c["score"], c["components"]["interest"], strict_c, niche=bool(c.get("channel")))
                await db.execute("UPDATE clusters SET triage=? WHERE id=?",
                                 (db.jdump({"ph": ph_c, "ac": c["article_count"], "at": db.now_iso(), "decision": dec}), c["id"]))
            c["decision"] = dec
            tag = "KEEP" if dec["keep"] else "skip"
            await run.emit("triage", f"[{tag}] {c['title'][:90]} — {'; '.join(dec['reasons'])[:110]}",
                           "success" if dec["keep"] else "info", {"cluster": c["id"], "score": dec["score"]})
            if dec["keep"]:
                keep.append(c)
                st["triage"]["kept"] += 1
            else:
                st["triage"]["rejected"] += 1
                built_n["rejected"] += 1
                await db.execute("UPDATE stories SET tier='rejected', updated_at=?, gate=json_set(COALESCE(gate,'{}'),'$.cfg',?) WHERE id=? AND tier='wire'",
                                 (db.now_iso(), c.get("_cfg", ""), c["_story_id"]))
        # 5b+5c per story: writer builds everything the reader needs → the other model audits it → persist immediately
        # (one model swap pair per story, so finished stories appear on the front page one at a time)
        for k, c in enumerate(keep):
            sid = c["_story_id"]
            if sid in _building:
                continue                                    # a reader already opened it and it is being built
            _building.add(sid)
            try:
                await run.emit("build", f"Explaining: {c['title'][:80]}", progress=0.38 + step * (b + k))

                async def say(msg):
                    await run.emit("build", f"  · {msg}")
                brief_c, _ph, strict_c, s_c = _channel_ctx(c, chmap, brief, ph, strict, s)
                d = await write_phase(c, sid, s_c, say)
                await run.emit("build", f"Wrote: “{d['card']['headline']}” (grounding {d['card']['_grounding']['score']:.0%}; "
                                        f"timeline: {d['timeline']['reason']}; explainer: {'ok' if d['explain'] else 'skipped'})", "success")
                pub = await editor.publish_gate(d["card"], brief_c, [m["title"] for m in d["g"]["members"]], d["card"]["_plain"],
                                                d["card"]["_grounding"], strict_c, c["outlet_count"], c["components"]["interest"],
                                                ((c["decision"].get("answers") or {}).get("story_type")), niche=bool(c.get("channel")))
                lvl = "success" if pub["tier"] == "primary" else ("info" if pub["tier"] == "more" else "warning")
                await run.emit("publish", f"[{pub['tier'].upper()}] {d['card']['headline'][:90]} — {'; '.join(pub['reasons'])[:130]}", lvl)
                await persist_built(sid, c, d, c["decision"], c, pub)
                built_n[pub["tier"]] = built_n.get(pub["tier"], 0) + 1
            except Exception as e:  # noqa: BLE001 — one bad story must not kill the cycle
                built_n["failed"] += 1
                await _mark_failed(sid, str(e))
                await run.emit("build", f"✗ {c['title'][:70]}: {str(e)[:120]}", "warning")
                log.warning("build failed: %s", traceback.format_exc()[-400:])
            finally:
                _building.discard(sid)
        await run.emit("publish", f"Batch complete: {built_n}", progress=0.38 + step * min(b + BATCH, len(queue)))

    st["built"] = built_n

    # 6 ── housekeeping: old instant stories nobody built or saved disappear after a week
    old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    await db.execute(
        "DELETE FROM stories WHERE tier IN ('wire','rejected') AND kind='feed' AND COALESCE(published_at,created_at) < ? "
        "AND id NOT IN (SELECT story_id FROM saved) AND id NOT IN (SELECT story_id FROM follows)", (old,))
    await db.execute("UPDATE clusters SET story_id=NULL WHERE story_id IS NOT NULL AND story_id NOT IN (SELECT id FROM stories)")
    await db.execute("DELETE FROM articles WHERE published_at < ? AND cluster_id IN (SELECT id FROM clusters WHERE story_id IS NULL)", (old,))
    await db.execute("DELETE FROM clusters WHERE last_seen < ? AND story_id IS NULL", (old,))
    if not only_channel:                                    # a one-channel refresh must not overwrite the front-page funnel numbers
        await db.kv_set("last_feed_run", {"at": db.now_iso(), "stats": st})
    await run.emit("done", f"Feed updated: {built_n['primary']} front-page, {built_n['more']} more, {built_n['rejected']} filtered out, "
                           f"{built_n['failed']} failed; {wired} live", "success", progress=1.0)


# ── slow extras: evidenced story map for primary stories ──────────────────────
_enrich_lock = asyncio.Lock()


async def enrich_modules(limit: int = 8, only: Optional[str] = None) -> Optional[str]:
    """Relationship map (entities are already stored by the build). Runs in the background; stories are already visible."""
    if _enrich_lock.locked():
        return None
    if not only:
        llm.background()
    async with _enrich_lock:
        if only:
            rows = await db.fetch_all("SELECT id,headline,outlet_count,gate FROM stories WHERE id=?", (only,))
        else:
            rows = await db.fetch_all(
                "SELECT id,headline,outlet_count,gate FROM stories WHERE kind='feed' AND tier='primary' "
                "AND gate LIKE '%\"reason\":\"pending\"%' ORDER BY published_at DESC LIMIT ?", (limit,))
        rows = [r for r in rows if '"reason":"pending"' in (r["gate"] or "")]
        if not rows:
            return None
        run = await runs.Run.start("enrich", "story maps")
        try:
            for i, r in enumerate(rows):
                sid = r["id"]
                texts = await db.fetch_all(
                    "SELECT ss.url,ss.outlet,ss.published_at,a.full_text AS text FROM story_sources ss JOIN articles a ON a.url=ss.url "
                    "WHERE ss.story_id=? AND a.full_text IS NOT NULL AND a.is_paywall=0 ORDER BY ss.n", (sid,))
                ents = await db.fetch_all("SELECT name,type,mentions FROM entities WHERE story_id=? ORDER BY mentions DESC", (sid,))
                gate = db.jload(r["gate"], {}) or {}
                gr = {"entities": ents, "relations": [], "shown": False, "reason": "not enough coverage for a relationship map"}
                try:
                    if len(texts) >= 2 and r["outlet_count"] >= 3 and len(ents) >= 4:
                        rels: list[dict] = []
                        for t in sorted(texts, key=lambda x: -len(x["text"]))[:2]:
                            rels.extend(await graph.extract_relations(t, ents))
                            if len(graph.dedupe_relations(rels)) >= 5:
                                break
                        rels = graph.dedupe_relations(rels)
                        ok, reason = graph.gate(ents, rels)
                        gr = {"entities": ents, "relations": rels if ok else [], "shown": ok, "reason": reason}
                except Exception as e:  # noqa: BLE001
                    await run.emit("modules", f"module error: {str(e)[:100]}", "warning")
                async with db.get_db() as conn:
                    await conn.execute("DELETE FROM relations WHERE story_id=?", (sid,))
                    if gr["shown"]:
                        await conn.executemany(
                            "INSERT INTO relations(story_id,subject,relation,object,evidence,source_url) VALUES(?,?,?,?,?,?)",
                            [(sid, x["subject"], x["relation"], x["object"], x["evidence"], x["source_url"]) for x in gr["relations"]])
                    gate["graph"] = {"shown": gr["shown"], "reason": gr["reason"]}
                    await conn.execute("UPDATE stories SET gate=? WHERE id=?", (db.jdump(gate), sid))
                    await conn.commit()
                await run.emit("modules", f"“{r['headline'][:60]}…” — map: {gr['reason']}",
                               "success" if gr["shown"] else "info", progress=(i + 1) / len(rows))
            await run.finish("complete")
        except Exception as e:  # noqa: BLE001
            await run.emit("error", str(e)[:200], "error")
            await run.finish("failed", error=str(e)[:200])
        return run.id
