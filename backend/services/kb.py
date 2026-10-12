"""ContextLens v4 — the living knowledge base: entities, story threads and links between stories.

Every story that enters the app is connected to what we already know:
  * threads   – the same evolving story across days ("IIT Bombay student death" on Mon, Tue, Wed…). A story joins the
                thread whose centroid it is close to (cosine >= THREAD_SIM), else it starts a new one.
  * entities  – people / organisations / places extracted from full text, canonicalised and counted across stories.
  * links     – stories that share >=2 specific entities (with the reason spelled out: "Both involve Tata Sons, Noel Tata").

Nothing here calls an LLM; it is pure embeddings + counting, so it is fast, deterministic and explainable.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from backend import db
from backend.services import clustering, llm
from backend.services.graph import GENERIC_PLACES, NEWS_ORGS, _canon

log = logging.getLogger("kb")

THREAD_SIM = 0.82
THREAD_SIM_ENTITY = 0.76      # lower bar when the story shares >=2 entities with the thread
THREAD_WINDOW_DAYS = 21
MAX_ENTITY_STORIES = 60       # entities appearing in more stories than this are too generic to link on ("India")

_JUNK = {"professor", "police", "government", "minister", "court", "school", "university", "institute", "company",
         "people", "man", "woman", "student", "students", "official", "officials", "reuters", "report", "sources"}


def entity_key(name: str) -> str:
    n = _canon(name).lower()
    n = re.sub(r"^(the|mr|mrs|ms|dr)\s+", "", n)
    return re.sub(r"[^a-z0-9 ]", "", n).strip()


def _ok_entity(e: dict) -> bool:
    n = e["name"].strip()
    k = entity_key(n)
    if len(k) < 3 or k in _JUNK or n.lower() in NEWS_ORGS or n.lower() in GENERIC_PLACES:
        return False
    if e.get("type") == "other" and " " not in n:
        return False
    return not n.islower()


def _norm(v: np.ndarray) -> np.ndarray:
    return v / max(float(np.linalg.norm(v)), 1e-9)


def _dt(iso: Optional[str]) -> datetime:
    try:
        d = datetime.fromisoformat(iso) if iso else datetime.now(timezone.utc)
    except Exception:
        d = datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── threads ──────────────────────────────────────────────────────────────────

async def assign_thread(story_id: str) -> Optional[str]:
    """Attach the story to a thread (creating one if needed). Returns the thread id."""
    s = await db.fetch_one("SELECT id,headline,dek,topic,centroid,published_at,created_at FROM stories WHERE id=?", (story_id,))
    if not s or not s["headline"]:
        return None
    existing = await db.fetch_one("SELECT thread_id FROM thread_stories WHERE story_id=?", (story_id,))
    if existing:
        return existing["thread_id"]
    v = llm.from_blob(s["centroid"])
    if v is None:
        v = (await llm.embed([f"{s['headline']}. {s['dek']}"]))[0]
        await db.execute("UPDATE stories SET centroid=? WHERE id=?", (llm.to_blob(v), story_id))
    at = s["published_at"] or s["created_at"]
    cutoff = (_dt(at) - timedelta(days=THREAD_WINDOW_DAYS)).isoformat()
    # match against individual member stories (a running-mean centroid drifts until everything "matches" it)
    rows = await db.fetch_all(
        "SELECT ts.thread_id, s.headline, s.centroid FROM thread_stories ts JOIN stories s ON s.id=ts.story_id "
        "JOIN threads t ON t.id=ts.thread_id WHERE t.last_at >= ? AND s.id != ?", (cutoff, story_id))
    recent = await db.fetch_all("SELECT headline FROM stories WHERE COALESCE(published_at,created_at) >= ? AND headline!=''", (cutoff,))
    generic = clustering.generic_anchors([r["headline"] for r in recent])
    best_tid, best_sim = None, 0.0
    for r in rows:
        tv = llm.from_blob(r["centroid"])
        if tv is None or tv.shape != v.shape:
            continue
        sim = float(tv @ v)
        if sim > best_sim and clustering.should_merge(sim, s["headline"], r["headline"], generic):
            best_tid, best_sim = r["thread_id"], sim
    best = await db.fetch_one("SELECT id,title,centroid,story_count FROM threads WHERE id=?", (best_tid,)) if best_tid else None
    async with db.get_db() as conn:
        if best:
            n = best["story_count"]
            tv = llm.from_blob(best["centroid"])
            new_c = _norm((tv * n + v) / (n + 1))
            await conn.execute("UPDATE threads SET centroid=?, story_count=story_count+1, last_at=MAX(last_at,?), title=? WHERE id=?",
                               (llm.to_blob(new_c), at, s["headline"], best["id"]))
            tid = best["id"]
        else:
            tid = db.new_id()
            await conn.execute("INSERT INTO threads(id,title,topic,centroid,first_at,last_at,story_count) VALUES(?,?,?,?,?,?,1)",
                               (tid, s["headline"], s["topic"], llm.to_blob(v), at, at))
        await conn.execute("INSERT OR REPLACE INTO thread_stories(story_id,thread_id,at) VALUES(?,?,?)", (story_id, tid, at))
        await conn.commit()
    return tid


async def _story_entity_keys(story_id: str) -> list[dict]:
    return await db.fetch_all("SELECT e.key AS name_key FROM kb_mentions m JOIN kb_entities e ON e.id=m.entity_id WHERE m.story_id=?", (story_id,))


async def _thread_entity_keys(thread_id: str) -> set[str]:
    rows = await db.fetch_all(
        "SELECT DISTINCT e.key FROM thread_stories ts JOIN kb_mentions m ON m.story_id=ts.story_id JOIN kb_entities e ON e.id=m.entity_id "
        "WHERE ts.thread_id=?", (thread_id,))
    return {r["key"] for r in rows}


async def thread_history(story_id: str) -> dict:
    """Earlier and later stories in the same thread. {thread_id,title,earlier:[...],later:[...]}"""
    ts = await db.fetch_one("SELECT thread_id,at FROM thread_stories WHERE story_id=?", (story_id,))
    if not ts:
        return {"thread_id": None, "title": "", "earlier": [], "later": []}
    rows = await db.fetch_all(
        "SELECT s.id AS story_id,s.headline,s.dek,s.tier,s.built_at,ts.at FROM thread_stories ts JOIN stories s ON s.id=ts.story_id "
        "WHERE ts.thread_id=? AND ts.story_id!=? AND s.tier IN ('primary','more','wire') AND s.headline!='' ORDER BY ts.at", (ts["thread_id"], story_id))
    title = (await db.fetch_one("SELECT title FROM threads WHERE id=?", (ts["thread_id"],)) or {}).get("title", "")
    earlier = [r for r in rows if r["at"] < ts["at"]]
    later = [r for r in rows if r["at"] >= ts["at"]]
    return {"thread_id": ts["thread_id"], "title": title, "earlier": earlier[-8:], "later": later[:6]}


# ── entities + links ─────────────────────────────────────────────────────────

async def register_entities(story_id: str, entities: list[dict]) -> int:
    """entities: [{name,type,mentions}] from graph.extract_entities. Upserts kb_entities/kb_mentions. Returns count kept."""
    s = await db.fetch_one("SELECT published_at,created_at FROM stories WHERE id=?", (story_id,))
    at = (s or {}).get("published_at") or (s or {}).get("created_at") or db.now_iso()
    kept = [e for e in entities if _ok_entity(e)]
    async with db.get_db() as conn:
        await conn.execute("DELETE FROM kb_mentions WHERE story_id=?", (story_id,))
        for e in kept:
            key = entity_key(e["name"])
            row = await (await conn.execute("SELECT id,type FROM kb_entities WHERE key=?", (key,))).fetchone()
            if row:
                eid = row["id"]
                if row["type"] == "other" and e["type"] != "other":
                    await conn.execute("UPDATE kb_entities SET type=? WHERE id=?", (e["type"], eid))
            else:
                cur = await conn.execute("INSERT INTO kb_entities(key,name,type,first_seen,last_seen) VALUES(?,?,?,?,?)",
                                         (key, _canon(e["name"]), e["type"], at, at))
                eid = cur.lastrowid
            await conn.execute("INSERT OR REPLACE INTO kb_mentions(entity_id,story_id,mentions) VALUES(?,?,?)", (eid, story_id, e["mentions"]))
            await conn.execute(
                "UPDATE kb_entities SET story_count=(SELECT COUNT(*) FROM kb_mentions WHERE entity_id=?), "
                "first_seen=MIN(COALESCE(first_seen,?),?), last_seen=MAX(COALESCE(last_seen,?),?) WHERE id=?", (eid, at, at, at, at, eid))
        await conn.commit()
    return len(kept)


async def link_related(story_id: str, limit: int = 8) -> int:
    """Create story_links for stories sharing >=2 specific entities (and, when available, a similar topic vector)."""
    rows = await db.fetch_all(
        "SELECT m2.story_id AS other, COUNT(*) AS shared, GROUP_CONCAT(e.name, '|') AS names "
        "FROM kb_mentions m1 JOIN kb_mentions m2 ON m1.entity_id=m2.entity_id AND m2.story_id!=m1.story_id "
        "JOIN kb_entities e ON e.id=m1.entity_id JOIN stories s ON s.id=m2.story_id "
        "WHERE m1.story_id=? AND e.story_count BETWEEN 2 AND ? AND s.tier IN ('primary','more') "
        "GROUP BY m2.story_id HAVING shared>=2 ORDER BY shared DESC LIMIT ?", (story_id, MAX_ENTITY_STORIES, limit))
    mine = await db.fetch_one("SELECT centroid FROM stories WHERE id=?", (story_id,))
    mv = llm.from_blob((mine or {}).get("centroid"))
    thread = await db.fetch_one("SELECT thread_id FROM thread_stories WHERE story_id=?", (story_id,))
    n = 0
    async with db.get_db() as conn:
        for r in rows:
            names = [x for x in (r["names"] or "").split("|") if x][:3]
            other = await (await conn.execute("SELECT centroid FROM stories WHERE id=?", (r["other"],))).fetchone()
            sim = 0.0
            ov = llm.from_blob(other["centroid"]) if other else None
            if mv is not None and ov is not None and mv.shape == ov.shape:
                sim = float(mv @ ov)
            same = False
            if thread:
                t2 = await (await conn.execute("SELECT thread_id FROM thread_stories WHERE story_id=?", (r["other"],))).fetchone()
                same = bool(t2 and t2["thread_id"] == thread["thread_id"])
            kind = "thread" if same else "related"
            score = round(min(1.0, 0.15 * r["shared"] + 0.6 * max(sim, 0)), 3)
            reason = "Both involve " + ", ".join(names)
            for a, b in ((story_id, r["other"]), (r["other"], story_id)):
                await conn.execute("INSERT OR REPLACE INTO story_links(a,b,kind,score,reason) VALUES(?,?,?,?,?)", (a, b, kind, score, reason))
            n += 1
        await conn.commit()
    return n


async def related_stories(story_id: str, limit: int = 6) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT s.id,s.headline,s.dek,s.topic,s.tier,s.published_at,s.created_at,l.kind,l.reason,l.score FROM story_links l "
        "JOIN stories s ON s.id=l.b WHERE l.a=? AND s.tier IN ('primary','more') ORDER BY (l.kind='thread') DESC, l.score DESC LIMIT ?",
        (story_id, limit))
    return rows


# ── entity pages + atlas ─────────────────────────────────────────────────────

async def entity_page(entity_id: int) -> Optional[dict]:
    e = await db.fetch_one("SELECT * FROM kb_entities WHERE id=?", (entity_id,))
    if not e:
        return None
    stories = await db.fetch_all(
        "SELECT s.id,s.headline,s.dek,s.topic,s.tier,s.image_url,COALESCE(s.published_at,s.created_at) AS at,m.mentions "
        "FROM kb_mentions m JOIN stories s ON s.id=m.story_id WHERE m.entity_id=? AND s.tier IN ('primary','more','wire') "
        "ORDER BY at DESC LIMIT 40", (entity_id,))
    co = await db.fetch_all(
        "SELECT e2.id,e2.name,e2.type,COUNT(*) AS together FROM kb_mentions a JOIN kb_mentions b ON a.story_id=b.story_id AND a.entity_id!=b.entity_id "
        "JOIN kb_entities e2 ON e2.id=b.entity_id WHERE a.entity_id=? GROUP BY e2.id ORDER BY together DESC LIMIT 12", (entity_id,))
    return {**e, "stories": stories, "related": co}


async def ensure_summary(entity_id: int) -> Optional[dict]:
    """Lazy Wikipedia lead text for an entity page (cached in kb_entities)."""
    e = await db.fetch_one("SELECT id,name,type,summary,wiki_url FROM kb_entities WHERE id=?", (entity_id,))
    if not e or e["summary"]:
        return e
    from backend.services import context
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8, headers=context.UA, follow_redirects=True) as c:
            res = await context._lookup(c, e["name"])
    except Exception:
        res = None
    if res:
        await db.execute("UPDATE kb_entities SET summary=?, wiki_url=? WHERE id=?", (res["extract"], res["url"], entity_id))
        e["summary"], e["wiki_url"] = res["extract"], res["url"]
    else:
        await db.execute("UPDATE kb_entities SET summary=? WHERE id=?", ("—", entity_id))       # do not retry every visit
        e["summary"] = "—"
    return e


async def search_entities(q: str, limit: int = 10) -> list[dict]:
    q = re.sub(r"[^a-z0-9 ]", "", q.lower()).strip()
    if len(q) < 2:
        return []
    return await db.fetch_all("SELECT id,name,type,story_count FROM kb_entities WHERE key LIKE ? ORDER BY story_count DESC LIMIT ?",
                              (f"%{q}%", limit))


async def find_entity(name: str) -> Optional[dict]:
    return await db.fetch_one("SELECT * FROM kb_entities WHERE key=?", (entity_key(name),))


async def atlas(days: int = 21, limit: int = 60) -> dict:
    """Entity co-mention graph for the last `days`: nodes = entities in >=2 stories, edges = appeared together in >=2 stories."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    nodes = await db.fetch_all(
        "SELECT e.id,e.name,e.type,COUNT(DISTINCT m.story_id) AS stories FROM kb_entities e JOIN kb_mentions m ON m.entity_id=e.id "
        "JOIN stories s ON s.id=m.story_id WHERE COALESCE(s.published_at,s.created_at) >= ? AND s.tier IN ('primary','more') "
        "AND e.story_count <= ? GROUP BY e.id HAVING stories>=2 ORDER BY stories DESC LIMIT ?", (cutoff, MAX_ENTITY_STORIES, limit))
    ids = [n["id"] for n in nodes]
    if len(ids) < 2:
        return {"nodes": nodes, "edges": []}
    ph = ",".join("?" * len(ids))
    edges = await db.fetch_all(
        f"SELECT a.entity_id AS source,b.entity_id AS target,COUNT(*) AS weight FROM kb_mentions a JOIN kb_mentions b "
        f"ON a.story_id=b.story_id AND a.entity_id<b.entity_id WHERE a.entity_id IN ({ph}) AND b.entity_id IN ({ph}) "
        f"GROUP BY a.entity_id,b.entity_id HAVING weight>=2 ORDER BY weight DESC LIMIT 220", (*ids, *ids))
    return {"nodes": nodes, "edges": edges}


async def rebuild_threads() -> int:
    """Recompute every thread from scratch (used after changing the matching rules)."""
    await db.execute("DELETE FROM thread_stories")
    await db.execute("DELETE FROM threads")
    rows = await db.fetch_all("SELECT id FROM stories WHERE headline!='' ORDER BY COALESCE(published_at,created_at)")
    for r in rows:
        try:
            await assign_thread(r["id"])
        except Exception as e:  # noqa: BLE001
            log.info("thread rebuild skipped %s: %s", r["id"], str(e)[:60])
    return len(rows)
