"""Runtime-editable settings persisted in the kv table, merged over DEFAULT_SETTINGS."""

from __future__ import annotations

from backend.config import DEFAULT_SETTINGS
from backend import db

_cache: dict | None = None
_REV = 5                       # bump when a default changes meaning; keys in _RESET_ON_UPGRADE then fall back to the new default
_RESET_ON_UPGRADE = ("wire_size",)


async def get_settings() -> dict:
    global _cache
    if _cache is None:
        stored = await db.kv_get("settings", {}) or {}
        if stored.get("_rev", 0) < _REV:
            for k in _RESET_ON_UPGRADE:
                stored.pop(k, None)
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
        _cache = merged
    return dict(_cache)


def _coerce(key: str, value):
    default = DEFAULT_SETTINGS[key]
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return str(value)


async def update_settings(patch: dict) -> dict:
    global _cache
    current = await get_settings()
    for k, v in (patch or {}).items():
        if k in DEFAULT_SETTINGS:
            try:
                current[k] = _coerce(k, v)
            except (TypeError, ValueError):
                continue
    if current.get("strictness") not in ("lenient", "balanced", "strict"):
        current["strictness"] = "balanced"
    current["feed_size"] = max(4, min(40, current["feed_size"]))
    current["candidate_pool"] = max(8, min(120, current["candidate_pool"]))
    current["wire_size"] = max(10, min(400, current["wire_size"]))
    current["build_budget"] = max(4, min(120, current["build_budget"]))
    current["idle_unload_minutes"] = max(0, min(240, current["idle_unload_minutes"]))
    if current.get("reading_level") not in ("simple", "general", "expert"):
        current["reading_level"] = "simple"
    current["depth"] = max(1, min(3, current["depth"]))
    current["min_signal"] = max(0, min(9, current["min_signal"]))
    current["refresh_minutes"] = max(15, min(720, current["refresh_minutes"]))
    _cache = current
    await db.kv_set("settings", {**current, "_rev": _REV})
    return dict(current)


def invalidate() -> None:
    global _cache
    _cache = None
