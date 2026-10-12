"""Live voice: one streamed turn per user utterance, plus prompt suggestions built from today's stories."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.services import lms, voice
from backend.services.llm import LLMError

router = APIRouter(prefix="/api/voice", tags=["voice"])
log = logging.getLogger("voice")


class TurnIn(BaseModel):
    text: str = Field(..., max_length=600)
    history: list[dict] = Field(default_factory=list, max_length=12)
    focus: dict = Field(default_factory=dict)
    web: bool = True                     # let the reasoner search the open web after the quick answer


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


@router.post("/turn")
async def voice_turn(body: TurnIn):
    async def gen():
        try:
            async for ev in voice.turn(body.text, body.history, body.focus, body.web):
                yield _sse(ev)
        except LLMError as e:
            yield _sse({"t": "say", "c": "Sorry, my language model isn't responding."})
            yield _sse({"t": "error", "c": str(e)[:200]})
            yield _sse({"t": "done"})
        except Exception as e:  # noqa: BLE001 — a voice turn must always end cleanly for the client
            log.exception("voice turn failed")
            yield _sse({"t": "say", "c": "Sorry, something went wrong on my side."})
            yield _sse({"t": "error", "c": str(e)[:200]})
            yield _sse({"t": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/suggest")
async def suggest():
    from backend.routers.feed import get_feed
    data = await get_feed(limit=12)
    cards = ([data["lead"]] if data.get("lead") else []) + data["stories"]
    prompts = ["Give me today's highlights"]
    for c in cards[:3]:
        words = c["headline"].split()
        prompts.append("Tell me about " + " ".join(words[:9]).rstrip(",.:;") + ("…" if len(words) > 9 else ""))
    prompts += ["What's the most important story for India today?", "Explain the biggest story like I'm new to it"]
    return {"prompts": prompts[:6], "top": [{"id": c["id"], "headline": c["headline"]} for c in cards[:5]],
            "model": lms.status().get("current"), "built": data.get("built", 0), "total": data.get("total", 0)}
