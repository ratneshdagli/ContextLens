"""Two shelves learned from the competitor study: Local (Google News Local / NewsBreak / SmartNews) and Blind spots (Ground News).
Both are deterministic: no model writes anything here."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend import db
from backend.routers import globe
from backend.routers.feed import CARD_SQL, card
from backend.services import geo, geo_live, llm

router = APIRouter(prefix="/api", tags=["discover"])
HOME = "home_place"


class Home(BaseModel):
    name: str = Field(..., min_length=2, max_length=60)
    country: str = Field("", max_length=60)
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


@router.get("/local/search")
async def local_search(q: str = Query("", max_length=60)):
    return geo.search(q, 6)


@router.put("/local")
async def set_home(h: Home):
    await db.kv_set(HOME, h.model_dump())
    return h


@router.delete("/local")
async def clear_home():
    await db.kv_set(HOME, None)
    return {"ok": True}


@router.get("/local")
async def local(limit: int = Query(6, ge=1, le=20)):
    """News near the reader's home place: stories and headlines placed there, plus the live local headlines (fetched in the background)."""
    home = await db.kv_get(HOME)
    if not home:
        return {"place": None, "items": []}
    asyncio.create_task(geo_live.collect_places([(home["name"], home["lat"], home["lon"], "")]))      # keep the home place fresh
    r = await globe.near(lat=home["lat"], lon=home["lon"], km=40, hours=72, limit=limit)
    return {"place": home, "items": r["items"]}


@router.get("/blindspots")
async def blindspots(limit: int = Query(4, ge=1, le=12)):
    """Stories several outlets covered, all from ONE country's press: what the rest of the world's papers did not pick up."""
    cut = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    rows = await db.fetch_all(CARD_SQL + " WHERE s.kind='feed' AND s.tier IN ('primary','more') AND s.channel_id='' AND s.outlet_count>=3 "
                              "AND json_array_length(COALESCE(json_extract(s.extras,'$.lens.countries'),'[]'))=1 "
                              "AND COALESCE(s.published_at,s.created_at) > ? ORDER BY s.outlet_count DESC, COALESCE(s.published_at,s.created_at) DESC LIMIT ?",
                              (cut, limit))
    out = []
    for r in rows:
        cc = (db.jload((await db.fetch_one("SELECT json_extract(extras,'$.lens.countries') AS c FROM stories WHERE id=?", (r["id"],)))["c"], [""]) or [""])[0]
        out.append(dict(card(r), only_cc=cc, only_country=geo.G.country_names.get(cc, cc)))
    return out


_SENT = re.compile(r"(?<=[.!?])\s+")


def _sixty(text: str) -> str:
    """Whole sentences, 20-60 words in all, or nothing: a card is never cut mid-sentence and never padded."""
    out, words = [], 0
    for s in _SENT.split(re.sub(r"\s+", " ", text or "").strip()):
        n = len(s.split())
        if words + n > 60:
            break
        out.append(s)
        words += n
    return " ".join(out) if words >= 20 else ""


@router.get("/shorts")
async def shorts(topic: str = "", limit: int = Query(30, ge=1, le=60)):
    """Inshorts-style deck: every story in under 60 words, built only from the story's own validated text."""
    cut = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    sql = (CARD_SQL + " WHERE s.kind='feed' AND s.tier IN ('primary','more') AND s.channel_id='' AND s.built_at IS NOT NULL "
           "AND COALESCE(s.published_at,s.created_at) > ?")
    args: list = [cut]
    if topic:
        sql += " AND s.topic=?"
        args.append(topic)
    rows = await db.fetch_all(sql + " ORDER BY COALESCE(s.published_at,s.created_at) DESC LIMIT 200", args)
    cards = sorted((card(r) for r in rows), key=lambda c: -c["signal"])[:limit * 2]
    body = {r["id"]: r["what_happened"] for r in await db.fetch_all(
        f"SELECT id,what_happened FROM stories WHERE id IN ({','.join('?' * len(cards))})", [c["id"] for c in cards])} if cards else {}
    out = []
    for c in cards:
        text = _sixty(body.get(c["id"], "")) or _sixty(c["dek"])
        if text:
            out.append({k: c[k] for k in ("id", "headline", "topic", "outlet_count", "published_at", "signal", "image_url")} | {"body": text, "words": len(text.split())})
    return out[:limit]


# Script ranges per language: the gate checks the output is really in that script.
LANGS = {"hi": ("Hindi", "ऀ-ॿ"), "mr": ("Marathi", "ऀ-ॿ"), "bn": ("Bengali", "ঀ-৿"),
         "ta": ("Tamil", "஀-௿"), "te": ("Telugu", "ఀ-౿"), "gu": ("Gujarati", "઀-૿"),
         "kn": ("Kannada", "ಀ-೿"), "ml": ("Malayalam", "ഀ-ൿ")}
_DIGITS = re.compile(r"\d+")


def _tr_ok(src: str, out: str, lang: str) -> str | None:
    """Fail-closed translation gate: None = pass, else the reason. Numbers must survive, the text must be in the target script."""
    letters = [c for c in out if c.isalpha()]
    if len(letters) < 10 or len(out) > len(src) * 3 or len(out) < len(src) * 0.4:
        return "bad length"
    if sum(bool(re.match(f"[{LANGS[lang][1]}]", c)) for c in letters) / len(letters) < 0.6:
        return "not in the target script"
    if sorted(_DIGITS.findall(src)) != sorted(n for n in _DIGITS.findall(out) if n.isascii()):
        return "numbers changed"
    return None


class TrIn(BaseModel):
    lang: str


@router.post("/translate/{story_id}")
async def translate(story_id: str, body: TrIn):
    """Headline + short summary in another Indian language. Cached per story; never shown unless the gate passes."""
    if body.lang not in LANGS:
        raise HTTPException(400, "Unsupported language")
    s = await db.fetch_one("SELECT headline,what_happened,dek,extras FROM stories WHERE id=?", (story_id,))
    if not s:
        raise HTTPException(404, "Story not found")
    extras = db.jload(s["extras"], {}) or {}
    if (hit := (extras.get("tr") or {}).get(body.lang)):
        return hit
    text = _sixty(s["what_happened"]) or _sixty(s["dek"])
    if not text:
        raise HTTPException(422, "Nothing to translate")
    name = LANGS[body.lang][0]
    schema = {"type": "object", "properties": {"headline": {"type": "string"}, "body": {"type": "string"}},
              "required": ["headline", "body"], "additionalProperties": False}
    llm.interactive()
    try:
        out = await llm.chat_json(
            "writer", f"You translate news into {name}. Translate faithfully and completely: add nothing, drop nothing, keep every number "
            f"exactly as written in digits 0-9, write names in the {name} script. Natural newspaper {name}.",
            f"HEADLINE: {s['headline']}\nTEXT: {text}", schema, schema_name="translate", temperature=0.1, max_tokens=700, retries=1,
            validate=lambda o: _tr_ok(s["headline"], o["headline"], body.lang) or _tr_ok(text, o["body"], body.lang))
    except llm.LLMError as e:
        raise HTTPException(503, f"Translation unavailable: {str(e)[:120]}")
    res = {"headline": out["headline"].strip(), "body": out["body"].strip(), "lang": body.lang}
    extras.setdefault("tr", {})[body.lang] = res
    await db.execute("UPDATE stories SET extras=? WHERE id=?", (db.jdump(extras), story_id))
    return res


@router.get("/alerts")
async def alerts(since: str = "", min_signal: int = Query(7, ge=1, le=10), limit: int = Query(5, ge=1, le=20)):
    """Breaking: built front-page stories newer than `since` whose Signal clears the reader's bar (client keeps its own topic/quiet-hours rules)."""
    try:
        t = datetime.fromisoformat(since.replace("Z", "+00:00"))
        t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        t = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = await db.fetch_all(CARD_SQL + " WHERE s.kind='feed' AND s.tier='primary' AND s.built_at IS NOT NULL AND s.channel_id='' "
                              "AND s.built_at > ? ORDER BY s.built_at DESC LIMIT 100", (t.astimezone(timezone.utc).isoformat(),))
    hot = sorted((card(r) for r in rows), key=lambda c: -c["signal"])
    return {"now": db.now_iso(), "items": [{k: c[k] for k in ("id", "headline", "dek", "topic", "signal", "outlet_count", "published_at")}
                                           for c in hot if c["signal"] >= min_signal][:limit]}
