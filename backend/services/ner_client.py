"""Client for the isolated NER worker (see ner_worker.py). Never raises: returns None when NER is unavailable."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import sys
import time
from typing import Optional

from backend.config import ROOT

log = logging.getLogger("ner")

_proc: Optional[asyncio.subprocess.Process] = None
_lock = asyncio.Lock()
_ids = itertools.count(1)
_failures = 0
_disabled_until = 0.0


async def _kill() -> None:
    global _proc
    if _proc and _proc.returncode is None:
        try:
            _proc.kill()
        except ProcessLookupError:
            pass
    _proc = None


async def _ensure() -> bool:
    global _proc
    if _proc and _proc.returncode is None:
        return True
    _proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "backend.services.ner_worker", cwd=str(ROOT),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        line = await asyncio.wait_for(_proc.stdout.readline(), timeout=180)
        return bool(line) and json.loads(line).get("ready") is True
    except Exception:
        await _kill()
        return False


async def predict(texts: list[str], labels: list[str], threshold: float = 0.55, timeout: float = 120) -> Optional[list[list[dict]]]:
    """Returns predictions per text, or None if the worker is unavailable/crashed (caller falls back to regex)."""
    global _failures, _disabled_until
    if time.time() < _disabled_until:
        return None
    async with _lock:
        try:
            if not await _ensure():
                raise RuntimeError("worker failed to start")
            rid = next(_ids)
            _proc.stdin.write((json.dumps({"id": rid, "texts": texts, "labels": labels, "threshold": threshold}) + "\n").encode())
            await _proc.stdin.drain()
            line = await asyncio.wait_for(_proc.stdout.readline(), timeout=timeout)
            if not line:
                raise RuntimeError("worker exited")
            resp = json.loads(line)
            _failures = 0
            return resp["preds"]
        except Exception as e:  # noqa: BLE001
            _failures += 1
            log.warning("NER worker unavailable (%s); falling back to regex", str(e)[:80])
            await _kill()
            if _failures >= 2:
                _disabled_until = time.time() + 600          # back off for 10 minutes
                _failures = 0
            return None


async def shutdown() -> None:
    await _kill()
