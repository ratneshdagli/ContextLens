"""Background scheduler: refresh the feed on an interval (configurable / can be disabled in Settings)."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from backend import db
from backend.settings_store import get_settings

log = logging.getLogger("scheduler")
_task: Optional[asyncio.Task] = None


async def _loop():
    await asyncio.sleep(8)
    from backend.services import llm, pipeline, watch
    while True:
        try:
            s = await get_settings()
            prof = await db.kv_get("profile") or {}
            last = await db.kv_get("last_feed_run") or {}
            due = True
            if last.get("at"):
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["at"])).total_seconds() / 60
                due = age >= s["refresh_minutes"]
            if s["auto_refresh"] and prof.get("onboarded") and due:
                log.info("scheduled feed refresh")
                await pipeline.run_feed_cycle("scheduled")
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            log.warning("scheduler tick failed: %s", e)
        try:
            llm.background()                            # digest refreshes must never outrank a reader
            n = await watch.refresh_stale()
            if n:
                log.info("refreshed %d stale digest watch(es)", n)
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            log.warning("digest refresh tick failed: %s", e)
        await asyncio.sleep(120)


def start():
    global _task
    if os.getenv("DISABLE_SCHEDULER") == "1":
        return
    _task = asyncio.create_task(_loop())


def stop():
    if _task:
        _task.cancel()
