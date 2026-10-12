"""ContextLens v3 — background context ("what you need to know first").

Pulls a short, quotable primer for the story's main people/organisations from Wikipedia. Deterministic (no LLM),
so nothing here can be hallucinated: it is Wikipedia's own lead text with a link back.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import quote

import httpx

from backend.services.graph import NEWS_ORGS, GENERIC_PLACES

log = logging.getLogger("context")
UA = {"User-Agent": "ContextLens/3.0 (personal news reader; local-first)"}
API = "https://en.wikipedia.org/w/api.php"
SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\(.*?\)", "", s.lower())).strip()


def _first_sentences(text: str, n: int = 2, limit: int = 380) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())
    out = " ".join(parts[:n])
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + "…"


async def _lookup(client: httpx.AsyncClient, name: str) -> Optional[dict]:
    try:
        r = await client.get(API, params={"action": "query", "list": "search", "srsearch": name, "srlimit": 3,
                                          "format": "json", "srprop": ""})
        r.raise_for_status()
        hits = r.json().get("query", {}).get("search", [])
    except Exception as e:
        log.info("wikipedia search failed for %r: %s", name, str(e)[:60])
        return None
    want = _norm(name)
    for h in hits:
        title = h["title"]
        t = _norm(title)
        if not (t == want or want in t or (len(want.split()) >= 2 and t in want)):
            continue                                              # avoid wrong-entity matches
        try:
            s = await client.get(SUMMARY + quote(title.replace(" ", "_"), safe=""))
            if s.status_code != 200:
                continue
            d = s.json()
        except Exception:
            continue
        if d.get("type") != "standard" or not d.get("extract"):
            continue
        return {"title": d.get("title", title), "extract": _first_sentences(d["extract"]),
                "url": d.get("content_urls", {}).get("desktop", {}).get("page", ""),
                "thumb": (d.get("thumbnail") or {}).get("source", "")}
    return None


async def background_for(entities: list[dict], limit: int = 2) -> list[dict]:
    """entities: [{name,type,mentions}] ordered by prominence."""
    cands = []
    for e in entities:
        n = e["name"]
        if e["type"] in ("person", "organization", "event") and e["mentions"] >= 2 \
                and n.lower() not in NEWS_ORGS and n.lower() not in GENERIC_PLACES and len(n) > 3:
            cands.append(n)
    out, seen = [], set()
    async with httpx.AsyncClient(timeout=8, headers=UA, follow_redirects=True) as client:
        for name in cands[:4]:
            res = await _lookup(client, name)
            if res and res["title"] not in seen:
                seen.add(res["title"])
                out.append(res)
            if len(out) >= limit:
                break
    return out
