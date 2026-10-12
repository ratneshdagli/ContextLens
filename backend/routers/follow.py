"""Follow a story: ContextLens watches new coverage and tells you when it develops.

A follow stores the story's semantic centroid. Any newer article (in any outlet) whose embedding is close to it
counts as an update — no keyword rules to maintain.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import APIRouter, HTTPException

from backend import db
from backend.services import llm

router = APIRouter(prefix="/api", tags=["follow"])
SIM = 0.80


@router.post("/stories/{story_id}/follow")
async def follow(story_id: str):
    s = await db.fetch_one("SELECT id,headline,cluster_id FROM stories WHERE id=?", (story_id,))
    if not s:
        raise HTTPException(404, "Story not found")
    if await db.fetch_one("SELECT 1 FROM follows WHERE story_id=?", (story_id,)):
        return {"following": True}
    cen = None
    if s["cluster_id"]:
        row = await db.fetch_one("SELECT centroid FROM clusters WHERE id=?", (s["cluster_id"],))
        cen = row["centroid"] if row else None
    if not cen:
        try:
            cen = llm.to_blob((await llm.embed([s["headline"]], kind="document"))[0])
        except llm.LLMError as e:
            raise HTTPException(503, f"Can't follow right now — the embedding model is unavailable ({str(e)[:100]})")
    now = db.now_iso()
    await db.execute("INSERT INTO follows(id,story_id,title,centroid,created_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                     (db.new_id(), story_id, s["headline"], cen, now, now))
    return {"following": True}


@router.delete("/stories/{story_id}/follow")
async def unfollow(story_id: str):
    await db.execute("DELETE FROM follows WHERE story_id=?", (story_id,))
    return {"following": False}


@router.get("/following")
async def following():
    """Followed stories with newer related coverage (grouped by event, newest first)."""
    rows = await db.fetch_all("SELECT id,story_id,title,centroid,created_at,last_seen_at FROM follows ORDER BY created_at DESC")
    if not rows:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    arts = await db.fetch_all(
        "SELECT a.title,a.outlet,a.url,a.published_at,a.embedding,a.cluster_id,c.story_id AS story FROM articles a "
        "LEFT JOIN clusters c ON c.id=a.cluster_id WHERE a.embedding IS NOT NULL AND a.published_at >= ?", (cutoff,))
    vecs = [(a, llm.from_blob(a["embedding"])) for a in arts]
    out = []
    for f in rows:
        c = llm.from_blob(f["centroid"])
        updates, seen_titles = [], set()
        for a, v in vecs:
            if v is None or c is None or v.shape != c.shape or (a["published_at"] or "") <= f["created_at"]:
                continue
            sim = float(v @ c)
            key = a["title"].lower()[:50]
            if sim >= SIM and key not in seen_titles:
                seen_titles.add(key)
                updates.append({"title": a["title"], "outlet": a["outlet"], "url": a["url"], "published_at": a["published_at"],
                                "story_id": a["story"], "is_new": (a["published_at"] or "") > f["last_seen_at"], "sim": round(sim, 2)})
        updates.sort(key=lambda u: u["published_at"] or "", reverse=True)
        out.append({"id": f["id"], "story_id": f["story_id"], "title": f["title"], "since": f["created_at"],
                    "new_count": sum(1 for u in updates if u["is_new"]), "updates": updates[:6]})
    return out


@router.post("/following/{follow_id}/seen")
async def mark_seen(follow_id: str):
    await db.execute("UPDATE follows SET last_seen_at=? WHERE id=?", (db.now_iso(), follow_id))
    return {"ok": True}
