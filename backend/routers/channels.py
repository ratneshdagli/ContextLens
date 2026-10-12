"""Reader-defined channels: create / edit / reorder, discover their sources, refresh one on its own."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services import channels, pipeline, runs

router = APIRouter(prefix="/api", tags=["channels"])
_tasks: set = set()


class ChannelIn(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    weight: Optional[int] = None          # 0 off .. 3 high
    depth: Optional[int] = None           # 1..3 articles read per story
    size: Optional[int] = None            # stories kept on the channel page
    days: Optional[int] = None            # freshness window
    muted: Optional[list[str]] = None
    queries: Optional[list[str]] = None


class FeedIn(BaseModel):
    url: str
    off: Optional[bool] = None


def _public(ch: dict, counts: dict) -> dict:
    out = {k: v for k, v in ch.items() if k not in ("vec", "_re")}
    out["stats"] = counts.get(ch["id"], {"stories": 0, "built": 0, "last": None})
    out["feeds_live"] = sum(1 for f in ch["feeds"] if f.get("ok", True) and not f.get("off"))
    return out


async def _one(cid: str) -> dict:
    ch = await channels.get(cid)
    if not ch:
        raise HTTPException(404, "channel not found")
    return _public(ch, await channels.counts())


@router.get("/channels")
async def list_channels():
    counts = await channels.counts()
    return [_public(c, counts) for c in await channels.list_channels()]


@router.post("/channels")
async def create_channel(body: ChannelIn):
    try:
        ch = await channels.create(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _public(ch, {})


class ReorderIn(BaseModel):
    ids: list[str]


@router.post("/channels/reorder")
async def reorder_channels(body: ReorderIn):
    await channels.reorder(body.ids)
    return {"ok": True}


@router.patch("/channels/{cid}")
async def patch_channel(cid: str, body: ChannelIn):
    try:
        ch = await channels.update(cid, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ch:
        raise HTTPException(404, "channel not found")
    return await _one(cid)


@router.delete("/channels/{cid}")
async def delete_channel(cid: str):
    if not await channels.delete(cid):
        raise HTTPException(404, "channel not found")
    return {"ok": True}


@router.post("/channels/{cid}/discover")
async def discover(cid: str):
    """Find sources for the topic (takes 20-60 s: search + probing sites for working feeds)."""
    try:
        ch = await channels.discover(cid)
    except KeyError:
        raise HTTPException(404, "channel not found")
    return {**_public(ch, await channels.counts()), "discovery": ch["discovery"]}


@router.post("/channels/{cid}/feeds")
async def add_feed(cid: str, body: FeedIn):
    try:
        await channels.add_feed(cid, body.url)
    except KeyError:
        raise HTTPException(404, "channel not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await _one(cid)


@router.patch("/channels/{cid}/feeds")
async def toggle_feed(cid: str, body: FeedIn):
    if not await channels.set_feed(cid, body.url, off=body.off):
        raise HTTPException(404, "channel not found")
    return await _one(cid)


@router.delete("/channels/{cid}/feeds")
async def remove_feed(cid: str, url: str):
    if not await channels.set_feed(cid, url, remove=True):
        raise HTTPException(404, "channel not found")
    return await _one(cid)


@router.post("/channels/{cid}/refresh")
async def refresh_channel(cid: str):
    """Fetch only this channel's sources and build its stories (a fast first fill, without a full refresh)."""
    if not await channels.get(cid):
        raise HTTPException(404, "channel not found")
    rid = runs.active("feed")
    if rid:
        return {"run_id": rid, "already_running": True}
    task = asyncio.create_task(pipeline.run_feed_cycle("channel", cid))
    _tasks.add(task)                                       # keep a reference: a task nobody holds can be collected mid-run
    task.add_done_callback(_tasks.discard)
    for _ in range(100):
        await asyncio.sleep(0.05)
        rid = runs.active("feed")
        if rid:
            break
    return {"run_id": rid, "already_running": False}
