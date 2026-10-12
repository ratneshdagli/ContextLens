"""ContextLens v4 — LM Studio model manager: exactly one chat LLM resident at a time.

On a 6 GB GPU two 4–8B models cannot be loaded together, and LM Studio's "JIT" loading will happily keep the
previous model resident next to the new one. Before any chat request we therefore make sure that the wanted model
is the *only* LLM loaded: everything else (except embedding models, ~80 MB) is unloaded via the `lms` CLI.

If the CLI cannot be found the manager degrades to a no-op and LM Studio's own JIT behaviour applies.
Everything here is best-effort and never raises into the caller; the pipeline is phase-batched per model so swaps
are rare (one per phase, not one per story).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("lms")

_lock = asyncio.Lock()
_current: Optional[str] = None
_last_check = 0.0
_swaps = 0
_last_error = ""


def find_cli() -> Optional[str]:
    if os.getenv("DISABLE_LMS") == "1":              # the test suite must never unload the developer's real models
        return None
    env = os.getenv("LMS_PATH")
    if env and Path(env).exists():
        return env
    for cand in (shutil.which("lms"), str(Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms"))):
        if cand and Path(cand).exists():
            return cand
    return None


async def _run(*args: str, timeout: float = 240) -> tuple[int, str]:
    cli = find_cli()
    if not cli:
        return 127, "lms CLI not found"
    proc = await asyncio.create_subprocess_exec(
        cli, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        creationflags=0x08000000 if os.name == "nt" else 0)   # CREATE_NO_WINDOW
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def loaded() -> Optional[list[dict]]:
    """Currently loaded models [{identifier,type,...}] or None if the CLI is unavailable."""
    code, out = await _run("ps", "--json", timeout=30)
    if code != 0:
        return None
    try:
        data = json.loads(out[out.index("["):])
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _matches(wanted: str, m: dict) -> bool:
    w = wanted.lower()
    return w in {str(m.get("identifier", "")).lower(), str(m.get("modelKey", "")).lower()}


async def ensure(model_id: str, ttl_seconds: int = 0) -> bool:
    """Make `model_id` the only resident chat LLM. Returns True when it is loaded (or the CLI is unavailable)."""
    global _current, _last_check, _swaps, _last_error
    if _current == model_id and time.time() - _last_check < 20:
        return True
    async with _lock:
        ps = await loaded()
        if ps is None:
            _last_error = "lms CLI unavailable — relying on LM Studio's own model loading"
            _current = model_id
            _last_check = time.time()
            return True
        llms = [m for m in ps if m.get("type") == "llm"]
        have = next((m for m in llms if _matches(model_id, m)), None)
        for m in llms:
            if m is have:
                continue
            ident = m.get("identifier") or m.get("modelKey")
            log.info("unloading %s (only one chat model may be resident)", ident)
            await _run("unload", str(ident), timeout=60)
        if not have:
            # one request at a time and an 8k window keep the KV cache small enough for a 6 GB GPU
            args = ["load", model_id, "--identifier", model_id, "--gpu", "max", "--context-length", "8192", "--parallel", "1", "-y"]
            if ttl_seconds > 0:
                args += ["--ttl", str(int(ttl_seconds))]
            log.info("loading %s", model_id)
            code, out = await _run(*args, timeout=300)
            if code != 0:
                _last_error = f"could not load {model_id}: {out[-200:]}"
                log.warning(_last_error)
                _current = None
                return False
            _swaps += 1
        _last_error = ""
        _current = model_id
        _last_check = time.time()
        return True


async def ensure_embed(model_id: str) -> None:
    """Keep the (tiny) embedding model resident; never touches chat models."""
    ps = await loaded()
    if ps is None:
        return
    if not any(m.get("type") == "embedding" and _matches(model_id, m) for m in ps):
        await _run("load", model_id, "--identifier", model_id, "-y", timeout=120)


async def unload_all_chat() -> int:
    """Free the GPU (used after long background work / on shutdown). Returns how many models were unloaded."""
    global _current
    ps = await loaded() or []
    n = 0
    for m in ps:
        if m.get("type") == "llm":
            await _run("unload", str(m.get("identifier") or m.get("modelKey")), timeout=60)
            n += 1
    _current = None
    return n


def invalidate() -> None:
    """Forget the cached 'current' model (e.g. after the user loads/unloads models by hand)."""
    global _current, _last_check
    _current, _last_check = None, 0.0


def status() -> dict:
    return {"cli": find_cli(), "current": _current, "swaps": _swaps, "error": _last_error}
