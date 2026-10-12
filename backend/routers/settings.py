"""Settings, models, sources, data management, health."""

from __future__ import annotations

import time

from fastapi import APIRouter
from pydantic import BaseModel

from backend import db, settings_store
from backend.config import APP_VERSION, LLM_BASE_URL, SEARXNG_URL, STRICTNESS, TRUSTED_SOURCES
from backend.services import lms, llm, runs
from backend.services.llm import LLMError

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/health")
async def health():
    h = await llm.health()
    s = await settings_store.get_settings()
    return {"status": "ok", "version": APP_VERSION,
            "llm": {"available": h["available"], "local": h["local"], "base_url": LLM_BASE_URL, "error": h.get("error"),
                    "cloud": h["fallback"]},   # {provider: ready|cooling}; cloud answers first, LM Studio is the last resort
            "models": h["models"], "feed_running": runs.active("feed"), "settings": s}


@router.get("/models/status")
async def models_status():
    """What LM Studio currently has loaded (there should never be more than one chat model)."""
    ps = await lms.loaded()
    return {"managed": ps is not None, "loaded": [{"id": m.get("identifier"), "type": m.get("type"), "status": m.get("status"),
                                                   "size_gb": round((m.get("sizeBytes") or 0) / 1e9, 1)} for m in (ps or [])],
            **lms.status()}


@router.post("/models/unload")
async def models_unload():
    n = await lms.unload_all_chat()
    return {"unloaded": n}


@router.get("/settings")
async def get_settings():
    s = await settings_store.get_settings()
    h = await llm.health()
    return {"settings": s, "available_models": h["models"], "llm_available": h["available"], "llm_url": LLM_BASE_URL,
            "searxng_url": SEARXNG_URL, "strictness_levels": list(STRICTNESS), "version": APP_VERSION}


@router.patch("/settings")
async def patch_settings(body: dict):
    return await settings_store.update_settings(body)


class TestIn(BaseModel):
    role: str = "writer"


@router.post("/settings/test")
async def test_model(body: TestIn):
    """Round-trip a tiny structured call to verify the model for a role works, and measure latency."""
    t = time.time()
    try:
        if body.role == "embed":
            v = await llm.embed(["connectivity test"])
            return {"ok": True, "seconds": round(time.time() - t, 1), "detail": f"embedding dimension {v.shape[1]}"}
        model = await llm.resolve_model(body.role)
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
                  "required": ["ok", "word"], "additionalProperties": False}
        out = await llm.chat_json(body.role, "Reply with JSON only.", 'Return {"ok": true, "word": "ready"}',
                                  schema, max_tokens=40, retries=0)
        return {"ok": bool(out.get("ok")), "seconds": round(time.time() - t, 1), "detail": f"{model} responded"}
    except LLMError as e:
        return {"ok": False, "seconds": round(time.time() - t, 1), "detail": str(e)[:240]}


@router.get("/sources")
async def sources():
    return sorted(TRUSTED_SOURCES, key=lambda s: (s["trust_rank"], s["name"]))


@router.post("/data/clear-stories")
async def clear_stories():
    for t in ("story_sources", "timeline_events", "entities", "relations", "messages", "feedback", "saved",
              "stories", "clusters", "articles", "run_events", "runs", "kb_entities", "kb_mentions", "threads",
              "thread_stories", "story_links", "dives", "papers", "paper_articles"):
        await db.execute(f"DELETE FROM {t}")
    await db.kv_set("last_feed_run", {})
    return {"ok": True}


@router.get("/data/export")
async def export_data():
    return {"profile": await db.kv_get("profile"), "settings": await db.kv_get("settings"),
            "saved": await db.fetch_all("SELECT s.headline, s.dek, v.saved_at, v.note FROM saved v JOIN stories s ON s.id=v.story_id"),
            "feedback": await db.fetch_all("SELECT story_id, action, created_at FROM feedback")}
