"""Ask your whole briefing: retrieval-augmented Q&A over the stories you already have.

Refuses (without calling the LLM) when nothing in the briefing is relevant, so it cannot bluff.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend import db
from backend.services import llm
from backend.services.llm import LLMError

router = APIRouter(prefix="/api", tags=["ask"])
MIN_SIM = 0.60
TOP_K = 5


class AskIn(BaseModel):
    question: str


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@router.post("/ask")
async def ask(body: AskIn):
    q = re.sub(r"\s+", " ", body.question).strip()[:400]

    async def gen():
        if len(q) < 4:
            yield _sse({"t": "chunk", "c": "Ask a question about today’s news."})
            yield _sse({"t": "done"})
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        rows = await db.fetch_all(
            "SELECT id,headline,dek,what_happened,why_it_matters,key_points,published_at FROM stories "
            "WHERE kind IN ('feed','research') AND status='complete' AND tier IN ('primary','more') AND published_at >= ? "
            "ORDER BY published_at DESC LIMIT 120", (cutoff,))
        if not rows:
            yield _sse({"t": "meta", "sources": []})
            yield _sse({"t": "chunk", "c": "Your briefing is empty right now. Refresh it first, or use Research to look into a topic."})
            yield _sse({"t": "done"})
            return
        try:
            docs = [f"{r['headline']}. {r['dek']} {r['what_happened'][:500]}" for r in rows]
            dv = await llm.embed(docs, kind="document")
            qv = (await llm.embed([q], kind="query"))[0]
        except LLMError as e:
            yield _sse({"t": "error", "c": str(e)[:200]})
            yield _sse({"t": "done"})
            return
        sims = dv @ qv
        qwords = {w for w in re.findall(r"[a-z]{4,}", q.lower())}
        scored = []
        for i, r in enumerate(rows):
            boost = 0.03 * sum(1 for w in qwords if w in (r["headline"] + " " + r["dek"]).lower())
            scored.append((float(sims[i]) + min(boost, 0.09), r))
        scored.sort(key=lambda x: -x[0])
        top = [r for s, r in scored[:TOP_K] if s >= MIN_SIM]
        if not top:
            yield _sse({"t": "meta", "sources": []})
            yield _sse({"t": "chunk", "c": "Your briefing doesn’t cover that yet. Try **Research** to have ContextLens read the sources and write a cited briefing on it."})
            yield _sse({"t": "done"})
            return
        yield _sse({"t": "meta", "sources": [{"i": i, "id": r["id"], "headline": r["headline"]} for i, r in enumerate(top, 1)]})
        ctx = []
        for i, r in enumerate(top, 1):
            pts = " | ".join(db.jload(r["key_points"], []) or [])
            ctx.append(f"[S{i}] {r['headline']}\n{r['dek']}\n{r['what_happened']}\nKey points: {pts}\nWhy it matters: {r['why_it_matters']}")
        system = ("You are ContextLens. Answer the reader's question using ONLY the numbered stories below. "
                  "Cite the stories you use like [S1]. If the stories don't answer the question, say so plainly. "
                  "Be concise (max 7 sentences); no speculation.\n\n" + "\n\n".join(ctx)[:7500])
        messages = [{"role": "system", "content": system}, {"role": "user", "content": q}]
        try:
            async for piece in llm.stream_chat("writer", messages, max_tokens=520):
                yield _sse({"t": "chunk", "c": piece})
        except LLMError as e:
            yield _sse({"t": "error", "c": str(e)[:200]})
        yield _sse({"t": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")
