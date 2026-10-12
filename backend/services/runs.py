"""Run tracking: persisted pipeline runs + in-memory pub/sub for live SSE progress."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from backend import db

_history: dict[str, list[dict]] = {}
_subs: dict[str, list[asyncio.Queue]] = {}
_active: dict[str, str] = {}          # kind -> run_id (single-flight per kind)


class Run:
    def __init__(self, run_id: str, kind: str):
        self.id, self.kind = run_id, kind
        self.stats: dict = {}
        _history[run_id] = []

    @classmethod
    async def start(cls, kind: str, label: str = "") -> "Run":
        rid = db.new_id()
        await db.execute("INSERT INTO runs(id,kind,label,started_at,status) VALUES(?,?,?,?, 'running')",
                         (rid, kind, label, db.now_iso()))
        _active[kind] = rid
        return cls(rid, kind)

    async def emit(self, stage: str, message: str, level: str = "info", data: Optional[dict] = None,
                   progress: Optional[float] = None):
        ev = {"run_id": self.id, "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"), "stage": stage,
              "level": level, "message": message, "data": data or {}, "progress": progress}
        _history[self.id].append(ev)
        if len(_history[self.id]) > 600:
            del _history[self.id][:100]
        for q in list(_subs.get(self.id, [])):
            q.put_nowait(ev)
        await db.execute("INSERT INTO run_events(run_id,ts,stage,level,message,data) VALUES(?,?,?,?,?,?)",
                         (self.id, db.now_iso(), stage, level, message[:500], db.jdump(data or {})))
        if progress is not None:
            await db.execute("UPDATE runs SET progress=?, stage=? WHERE id=?", (progress, stage, self.id))

    async def finish(self, status: str = "complete", error: Optional[str] = None, story_id: Optional[str] = None):
        await db.execute("UPDATE runs SET status=?, finished_at=?, stats=?, error=?, story_id=?, progress=? WHERE id=?",
                         (status, db.now_iso(), db.jdump(self.stats), error, story_id,
                          1.0 if status == "complete" else 0.0, self.id))
        if _active.get(self.kind) == self.id:
            _active.pop(self.kind, None)
        for q in list(_subs.get(self.id, [])):
            q.put_nowait({"__done__": True, "status": status})


def active(kind: str) -> Optional[str]:
    return _active.get(kind)


def subscribe(run_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subs.setdefault(run_id, []).append(q)
    return q


def unsubscribe(run_id: str, q: asyncio.Queue) -> None:
    if q in _subs.get(run_id, []):
        _subs[run_id].remove(q)


def history(run_id: str) -> list[dict]:
    return list(_history.get(run_id, []))


async def load_events(run_id: str) -> list[dict]:
    rows = await db.fetch_all("SELECT ts,stage,level,message,data FROM run_events WHERE run_id=? ORDER BY id", (run_id,))
    return [{**r, "data": db.jload(r["data"], {})} for r in rows]
