"""Select-to-understand (inspector) and on-demand deep dives."""

from __future__ import annotations

import asyncio
import hashlib
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import db
from backend.services import dives, inspector, llm, runs
from backend.services.llm import LLMError

router = APIRouter(prefix="/api", tags=["inspect"])

ACTIONS = ("explain", "who", "why", "forensics", "history")


class InspectIn(BaseModel):
    story_id: str | None = None
    text: str
    action: str
    context: str = ""


@router.post("/inspect")
async def inspect(body: InspectIn):
    text = re.sub(r"\s+", " ", body.text).strip()
    if body.action not in ACTIONS:
        raise HTTPException(400, "unknown action")
    if len(text) < 2 or len(text) > 1500:
        raise HTTPException(400, "select between 2 and 1500 characters")
    context = re.sub(r"\s+", " ", body.context).strip()[:900]
    key = hashlib.sha1((text + "|" + context[:120]).encode()).hexdigest()[:24]
    kind = f"inspect:{body.action}"
    sid = body.story_id or "-"
    cached = await db.fetch_one("SELECT payload FROM dives WHERE story_id=? AND kind=? AND key=?", (sid, kind, key))
    if cached:
        return {**db.jload(cached["payload"], {}), "cached": True}
    llm.interactive()
    story = await inspector.load_story(body.story_id) if body.story_id else None
    try:
        if body.action == "explain":
            out = await inspector.explain_selection(text, context, story)
        elif body.action == "why":
            if not story:
                raise HTTPException(400, "open a story first")
            out = await inspector.why_selection(text, context, story)
        elif body.action == "who":
            out = await inspector.who_selection(text, story)
        elif body.action == "history":
            out = await inspector.history_selection(text, body.story_id)
        else:
            if not story:
                raise HTTPException(400, "open a story first")
            archive = await inspector.archive_matches(text, {s["url"] for s in story["sources"]})
            out = inspector.forensic_report(text, story, archive)
    except LLMError as e:
        raise HTTPException(503, f"The language model could not answer: {str(e)[:140]}")
    out = {"action": body.action, "text": text[:300], **out}
    await db.execute(
        "INSERT INTO dives(story_id,kind,key,payload,created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(story_id,kind,key) DO UPDATE SET payload=excluded.payload", (sid, kind, key, db.jdump(out), db.now_iso()))
    return {**out, "cached": False}


class DiveIn(BaseModel):
    kind: str
    arg: str = ""


@router.post("/stories/{story_id}/dive")
async def start_dive(story_id: str, body: DiveIn):
    if body.kind not in dives.KINDS:
        raise HTTPException(400, "unknown deep-dive")
    if not await db.fetch_one("SELECT 1 FROM stories WHERE id=?", (story_id,)):
        raise HTTPException(404, "Story not found")
    cached = await dives.get_cached(story_id, body.kind, body.arg)
    if cached:
        return {"cached": True, "result": cached}
    run = await runs.Run.start("dive", f"{body.kind}: {body.arg or 'story'}"[:80])
    await db.execute("UPDATE runs SET story_id=? WHERE id=?", (story_id, run.id))
    asyncio.create_task(dives.run_dive(run, story_id, body.kind, body.arg))
    return {"cached": False, "run_id": run.id}


@router.get("/stories/{story_id}/dive")
async def get_dive(story_id: str, kind: str, arg: str = ""):
    res = await dives.get_cached(story_id, kind, arg)
    if not res:
        raise HTTPException(404, "not ready")
    return res
