"""Knowledge base: entity pages, the Atlas graph, story threads."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend import db
from backend.routers.feed import CARD_SQL, card
from backend.services import interests, kb

router = APIRouter(prefix="/api", tags=["knowledge"])


@router.get("/atlas")
async def atlas(days: int = 21, limit: int = 60):
    return await kb.atlas(max(1, min(days, 90)), max(10, min(limit, 120)))


@router.get("/entities/search")
async def entity_search(q: str):
    return await kb.search_entities(q)


@router.get("/entities/{entity_id}")
async def entity(entity_id: int):
    await kb.ensure_summary(entity_id)
    page = await kb.entity_page(entity_id)
    if not page:
        raise HTTPException(404, "Not found")
    p = await interests.get_profile()
    page["following"] = page["name"].lower() in [f.lower() for f in p["followed"]]
    if page["summary"] == "—":
        page["summary"] = ""
    return page


@router.post("/entities/{entity_id}/follow")
async def follow_entity(entity_id: int):
    e = await db.fetch_one("SELECT name FROM kb_entities WHERE id=?", (entity_id,))
    if not e:
        raise HTTPException(404, "Not found")
    p = await interests.get_profile()
    if e["name"].lower() not in [f.lower() for f in p["followed"]]:
        await interests.save_profile({"followed": p["followed"] + [e["name"]]})
    return {"following": True}


@router.delete("/entities/{entity_id}/follow")
async def unfollow_entity(entity_id: int):
    e = await db.fetch_one("SELECT name FROM kb_entities WHERE id=?", (entity_id,))
    if not e:
        raise HTTPException(404, "Not found")
    p = await interests.get_profile()
    await interests.save_profile({"followed": [f for f in p["followed"] if f.lower() != e["name"].lower()]})
    return {"following": False}


@router.get("/threads/{thread_id}")
async def thread(thread_id: str):
    t = await db.fetch_one("SELECT id,title,first_at,last_at,story_count FROM threads WHERE id=?", (thread_id,))
    if not t:
        raise HTTPException(404, "Not found")
    rows = await db.fetch_all(CARD_SQL + " WHERE s.id IN (SELECT story_id FROM thread_stories WHERE thread_id=?) "
                              "AND s.tier IN ('primary','more','wire') ORDER BY COALESCE(s.published_at,s.created_at)", (thread_id,))
    return {**t, "stories": [card(r) for r in rows]}
