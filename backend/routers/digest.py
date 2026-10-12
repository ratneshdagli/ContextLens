"""ContextLens v6 — GET/POST /api/digest: track GitHub repos and topics from your own documents,
alongside the news feed's own /api/following (stories you're already tracking)."""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from backend.services import watch

router = APIRouter(prefix="/api/digest", tags=["digest"])


class RepoIn(BaseModel):
    repo: str


class TopicIn(BaseModel):
    topic: str


async def _with_digest(w: dict) -> dict:
    return {**w, "digest": await watch.refresh(await watch.get_watch(w["id"]))}


@router.get("")
async def get_digest():
    return {"items": await watch.list_watches()}


@router.post("/repo")
async def add_repo(body: RepoIn):
    try:
        w = await watch.add_repo(body.repo)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await _with_digest(w)


@router.post("/topic")
async def add_topic(body: TopicIn):
    try:
        w = await watch.add_topic(body.topic)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await _with_digest(w)


@router.post("/doc")
async def add_doc(file: UploadFile = File(...)):
    data = await file.read()
    try:
        w = await watch.add_doc(file.filename or "document", data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await _with_digest(w)


@router.post("/{watch_id}/refresh")
async def refresh_one(watch_id: str):
    row = await watch.get_watch(watch_id)
    if not row:
        raise HTTPException(404, "Not tracking that")
    return await watch.refresh(row)


@router.delete("/{watch_id}")
async def delete_watch(watch_id: str):
    await watch.remove_watch(watch_id)
    return {"ok": True}
