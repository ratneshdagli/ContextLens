"""Research (Loop B) endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import db
from backend.routers.feed import CARD_SQL, card
from backend.services import research, runs

router = APIRouter(prefix="/api/research", tags=["research"])


class ResearchIn(BaseModel):
    query: str
    mode: str = "research"        # research | claim (fact-check)


@router.post("")
async def start(body: ResearchIn):
    if len(body.query.strip()) < 3:
        raise HTTPException(400, "Enter a topic (at least 3 characters).")
    if body.mode not in ("research", "claim"):
        raise HTTPException(400, "mode must be 'research' or 'claim'.")
    if runs.active("research"):
        raise HTTPException(409, "A research run is already in progress.")
    run_id, story_id = await research.run_research(body.query, body.mode)
    return {"run_id": run_id, "story_id": story_id}


@router.get("")
async def recent(limit: int = 12):
    rows = await db.fetch_all(CARD_SQL + " WHERE s.kind='research' ORDER BY s.created_at DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        st = await db.fetch_one("SELECT status,error,query FROM stories WHERE id=?", (r["id"],))
        out.append({**card(r), "status": st["status"], "error": st["error"], "query": st["query"]})
    return out
