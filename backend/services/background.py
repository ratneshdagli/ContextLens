"""ContextLens v4 — history retrieval: "how did we get here?"

v3 only read dated sentences inside today's articles, which gave *zero* timelines on real data (daily news rarely
spells out its own history). History now comes from earlier coverage of the same story, each item dated by its own
publish date, so nothing depends on the model guessing a date:

  sources  →  our own knowledge base (earlier stories in the same thread + archived articles)
              SearXNG news search (dated), GDELT (dated), Wikipedia lead text (primer, not a timeline entry)
  filter   →  must be >=8 h older than this story, semantically close to it (embedding) and share a specific anchor
  render   →  one plain caption per milestone; captions are the outlet's own headline (or a writer rewrite that must
              stay grounded in that headline+snippet); every entry links to its source

The result feeds (a) the "How it unfolded" timeline (gated: >=2 earlier dated events on >=2 dates + today) and
(b) the explainer's BACKGROUND block, so the "why did this happen" chain can use facts that today's article omits.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import dateparser
import httpx
import numpy as np

from backend import db
from backend.config import SEARXNG_URL, USER_AGENT
from backend.services import feeds, llm
from backend.services.clustering import anchors
from backend.services.llm import LLMError
from backend.services.verify import _content

log = logging.getLogger("background")

MIN_AGE_H = 8
SIM_FLOOR = 0.66

_STOPQ = {"the", "and", "for", "with", "after", "amid", "over", "into", "from", "that", "this", "says", "said",
          "new", "why", "how", "what", "will", "has", "have", "are", "its", "his", "her", "their", "than", "more"}


def _parse_dt(v) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        d = v
    else:
        v = str(v).strip()
        m = re.match(r"^(\d{4})(\d{2})(\d{2})T?(\d{2})?(\d{2})?(\d{2})?Z?$", v)          # GDELT seendate
        if m:
            g = [int(x) if x else 0 for x in m.groups()]
            d = datetime(g[0], g[1], g[2], g[3], g[4], g[5])
        else:
            d = dateparser.parse(v, settings={"RETURN_AS_TIMEZONE_AWARE": False, "PREFER_DATES_FROM": "past"})
    if not d:
        return None
    return d.replace(tzinfo=None) if d.tzinfo is None else d.astimezone(timezone.utc).replace(tzinfo=None)


def build_queries(headline: str, entity_names: list[str]) -> list[str]:
    """Two short queries: entity-led and headline-keyword-led (no LLM, deterministic)."""
    ents = [e for e in entity_names if 3 < len(e) < 40][:3]
    kw = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", headline) if w.lower() not in _STOPQ]
    lowered_ents = " ".join(ents).lower()
    kw = [w for w in kw if w.lower() not in lowered_ents][:4]
    qs = []
    if ents:
        qs.append(" ".join(ents[:2] + kw[:2]))
    qs.append(" ".join(kw[:6]) if len(kw) >= 3 else headline[:80])
    seen, out = set(), []
    for q in qs:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


async def _searx(q: str, time_range: str = "year") -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=14) as c:
            r = await c.get(SEARXNG_URL.rstrip("/") + "/search",
                            params={"q": q, "format": "json", "categories": "news", "language": "en", "time_range": time_range})
            r.raise_for_status()
            return [{"url": i["url"], "title": i.get("title", ""), "snippet": i.get("content", ""),
                     "dt": _parse_dt(i.get("publishedDate")), "src": "search"} for i in r.json().get("results", [])[:20]]
    except Exception as e:  # noqa: BLE001
        log.info("searxng history failed for %r: %s", q, str(e)[:80])
        return []


_gdelt_lock = asyncio.Lock()
_gdelt_last = 0.0


async def _gdelt(q: str) -> list[dict]:
    """GDELT allows one request per 5 s; serialise + retry once on 429."""
    global _gdelt_last
    import time
    try:
        async with _gdelt_lock:
            for attempt in range(2):
                wait = 5.6 - (time.monotonic() - _gdelt_last)
                if wait > 0:
                    await asyncio.sleep(wait)
                _gdelt_last = time.monotonic()
                async with httpx.AsyncClient(timeout=16, headers={"User-Agent": USER_AGENT}) as c:
                    r = await c.get("https://api.gdeltproject.org/api/v2/doc/doc",
                                    params={"query": f"{q} sourcelang:english", "mode": "artlist", "format": "json",
                                            "maxrecords": 25, "sort": "hybridrel", "timespan": "3months"})
                if r.status_code != 429:
                    break
            if r.status_code != 200 or "json" not in r.headers.get("content-type", "json"):
                return []
            return [{"url": i["url"], "title": i.get("title", ""), "snippet": "", "dt": _parse_dt(i.get("seendate")),
                     "src": "gdelt"} for i in r.json().get("articles", [])]
    except Exception as e:  # noqa: BLE001
        log.info("gdelt history failed: %s", str(e)[:80])
        return []


# ── dates: search engines often omit them, so recover them from the URL, snippet or the page's own metadata ──
_URL_DATE = re.compile(r"/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:[/-]|$)|[/_-](20\d{2})(\d{2})(\d{2})(?:\D|$)")
_META_DATE = re.compile(
    r"""(?:article:published_time|og:published_time|datePublished|name=.pubdate.|name=.date.|uploadDate)"""
    r"""[^>]{0,80}?(?:content=|:)\s*["']?(20\d{2}-\d{2}-\d{2}(?:[T ][\d:]{4,8})?)""", re.I)
_SNIP_DATE = re.compile(r"^\s*((?:\d{1,2}\s+[A-Z][a-z]{2,8}\s+20\d{2})|(?:[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+20\d{2}))")


def date_hint(c: dict) -> Optional[datetime]:
    m = _URL_DATE.search(c["url"])
    if m:
        g = [x for x in m.groups() if x]
        try:
            return datetime(int(g[0]), int(g[1]), int(g[2]))
        except ValueError:
            pass
    m = _SNIP_DATE.match(c.get("snippet", "") or "")
    return _parse_dt(m.group(1)) if m else None


async def _page_date(client: httpx.AsyncClient, url: str) -> Optional[datetime]:
    try:
        r = await client.get(url, headers={"Range": "bytes=0-150000"})
        if r.status_code >= 400:
            return None
        m = _META_DATE.search(r.text[:160000])
        return _parse_dt(m.group(1)) if m else None
    except Exception:
        return None


async def resolve_dates(cands: list[dict], max_fetch: int = 14) -> None:
    """Fill c['dt'] in place for candidates that lack one."""
    todo = []
    for c in cands:
        if not c.get("dt"):
            c["dt"] = date_hint(c)
        if not c.get("dt"):
            todo.append(c)
    if not todo:
        return
    sem = asyncio.Semaphore(6)
    async with httpx.AsyncClient(timeout=9, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        async def one(c):
            async with sem:
                c["dt"] = await _page_date(client, c["url"])
        await asyncio.gather(*[one(c) for c in todo[:max_fetch]])


async def _kb_candidates(story_id: str, thread_rows: list[dict], vec: np.ndarray, before: datetime) -> list[dict]:
    """Earlier stories in the same thread (already explained by us) + archived articles close to this story."""
    out = []
    for t in thread_rows:
        dt = _parse_dt(t["at"])
        if dt and dt < before - timedelta(hours=MIN_AGE_H):
            out.append({"url": f"/story/{t['story_id']}", "title": t["headline"], "snippet": t.get("dek") or "",
                        "dt": dt, "src": "thread", "story_id": t["story_id"], "outlet": "ContextLens"})
    cutoff = (before - timedelta(days=45)).isoformat()
    rows = await db.fetch_all(
        "SELECT url,title,snippet,outlet,published_at,embedding FROM articles WHERE embedding IS NOT NULL "
        "AND published_at >= ? AND published_at <= ?", (cutoff, (before - timedelta(hours=MIN_AGE_H)).isoformat()))
    for r in rows:
        v = llm.from_blob(r["embedding"])
        if v is None or v.shape != vec.shape:
            continue
        if float(v @ vec) >= 0.74:
            out.append({"url": r["url"], "title": r["title"], "snippet": r["snippet"], "dt": _parse_dt(r["published_at"]),
                        "src": "archive", "outlet": r["outlet"]})
    return out


def _outlet_of(c: dict) -> str:
    if c.get("outlet"):
        return c["outlet"]
    dom = feeds.domain_of(c["url"])
    return (feeds.lookup_source(dom) or {}).get("name") or dom


def _clean_title(t: str) -> str:
    t = feeds.clean_text(t)
    t = re.sub(r"\s+[-–—|]\s+[^-–—|]{2,40}$", "", t).strip()      # trailing " - The Hindu"
    return t[:200]


async def find_history(story: dict, entity_names: list[str], own_urls: set[str], max_items: int = 14) -> tuple[list[dict], list[dict]]:
    """Return (history, related): dated, relevant, de-duplicated earlier coverage oldest first, and every relevant
    candidate (any date) for recap fetching.
    story: {id, headline, dek, first_seen(iso), vec(np.ndarray|None)}"""
    before = _parse_dt(story["first_seen"]) or datetime.utcnow()
    text = f"{story['headline']}. {story.get('dek', '')}"
    vec = story.get("vec")
    if vec is None:
        vec = (await llm.embed([text]))[0]
    thread_rows = story.get("thread_rows") or []
    queries = build_queries(story["headline"], entity_names)
    cands = await _kb_candidates(story["id"], thread_rows, vec, before)
    web: list[dict] = []
    for q in queries[:2]:
        web.extend(await _searx(q))
    web.extend(await _gdelt(queries[0]) if queries else [])
    cands.extend(web)

    # de-duplicate by URL and drop the story's own sources
    seen, pool = set(), []
    for c in cands:
        u = c["url"]
        if u in seen or u in own_urls or not c.get("title"):
            continue
        seen.add(u)
        pool.append(c)
    if not pool:
        return [], []

    # relevance first (cheap): embedding similarity + a shared specific anchor with the story
    story_anchors = anchors(text) | {n.lower() for e in entity_names for n in e.split() if len(n) > 3}
    embs = await llm.embed([f"{_clean_title(c['title'])}. {c.get('snippet', '')[:200]}" for c in pool])
    rel = []
    for c, v in zip(pool, embs):
        sim = float(v @ vec)
        shared = (anchors(c["title"]) | {w for w in _content(c["title"])}) & (story_anchors | set(_content(text)))
        if c["src"] == "thread" or (sim >= SIM_FLOOR and len(shared) >= 1) or sim >= 0.80:
            rel.append({**c, "sim": sim, "_vec": v})
    rel.sort(key=lambda c: -c["sim"])
    rel = rel[:24]

    # then dates (may need a page fetch), then the age window
    await resolve_dates(rel)
    related = [dict(c) for c in rel]
    for c in related:
        c.pop("_vec", None)
    kept = [c for c in rel if c.get("dt") and before - timedelta(days=120) <= c["dt"] <= before - timedelta(hours=MIN_AGE_H)]

    # drop near-duplicates (same day and near-identical, or > 0.93 similar)
    uniq: list[dict] = []
    for c in kept:
        if any(float(c["_vec"] @ u["_vec"]) >= 0.93 or
               (abs((c["dt"] - u["dt"]).days) == 0 and float(c["_vec"] @ u["_vec"]) >= 0.86) for u in uniq):
            continue
        uniq.append(c)
    top = sorted(uniq[:max_items], key=lambda c: c["dt"])
    for c in top:
        c.pop("_vec", None)
        c["title_clean"] = _clean_title(c["title"])
        c["outlet"] = _outlet_of(c)
    for c in related:
        c["title_clean"] = _clean_title(c["title"])
        c["outlet"] = _outlet_of(c)
    return top, related


# ── timeline rendering ────────────────────────────────────────────────────────────────

CAPTION_SCHEMA = {
    "type": "object",
    "properties": {"events": {"type": "array", "maxItems": 8, "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "milestone": {"type": "boolean"}, "caption": {"type": "string"}},
        "required": ["i", "milestone", "caption"], "additionalProperties": False}}},
    "required": ["events"], "additionalProperties": False,
}

CAPTION_SYSTEM = """You turn earlier news items into timeline entries that explain how a story developed.
You get the CURRENT STORY and a numbered list of EARLIER ITEMS (date, outlet, headline, snippet).
For each item that is a real milestone of this same story (something that happened, was announced, decided or revealed),
return {i, milestone: true, caption}. caption: ONE plain sentence, max 22 words, present-tense-free (past tense), using only
what the item's headline/snippet says. Skip items that are opinion, live blogs, previews, or about a different story (milestone: false, caption: "")."""


def _caption_ok(caption: str, item: dict) -> bool:
    if not (12 <= len(caption) <= 220) or llm.looks_like_garbage(caption):
        return False
    base = f"{item['title']} {item.get('snippet', '')}"
    cw = set(_content(caption))
    bw = set(_content(base))
    return bool(cw) and len(cw & bw) / len(cw) >= 0.55


async def render_history_events(story: dict, items: list[dict]) -> list[dict]:
    """items: output of find_history. Returns publication-dated timeline events (ungated)."""
    if not items:
        return []
    listing = "\n".join(f"{i}. [{c['dt']:%Y-%m-%d}] {c['outlet']}: {c['title_clean']} — {c.get('snippet', '')[:160]}"
                        for i, c in enumerate(items))
    user = f"CURRENT STORY: {story['headline']}. {story.get('dek', '')}\n\nEARLIER ITEMS\n{listing}"
    picked: dict[int, str] = {}
    llm_ok = True
    try:
        out = await llm.chat_json("writer", CAPTION_SYSTEM, user, CAPTION_SCHEMA, schema_name="captions", max_tokens=700, retries=0)
        for e in out.get("events", []):
            i = e.get("i")
            if isinstance(i, int) and 0 <= i < len(items) and e.get("milestone") and _caption_ok(e["caption"].strip(), items[i]):
                picked[i] = e["caption"].strip().rstrip(".") + "."
    except LLMError as e:
        llm_ok = False
        log.info("caption pass failed (%s) — keeping only very close outlet headlines", str(e)[:80])
    events = []
    for i, c in enumerate(items):
        if llm_ok:
            if i not in picked and c["src"] != "thread":
                continue                                # the model judged it not a milestone (or could not caption it faithfully)
        elif c["src"] != "thread" and c.get("sim", 0) < 0.78:
            continue                                    # no judge available: only near-duplicates of this story qualify
        text = picked.get(i) or c["title_clean"]
        events.append({"date": c["dt"].date().isoformat(), "precision": "day", "text": text, "quote": c["title_clean"][:260],
                       "source_url": c["url"], "source_outlet": c["outlet"], "kind": "history"})
    return events


_RECAP_HINT = re.compile(r"\b(timeline|what we know|explained|so far|key events|everything|how (?:it|we|the)|what happened|"
                         r"days? (?:of|after|since)|hours? (?:of|after)|a look back|recap|behind the)\b", re.I)


async def fetch_recaps(related: list[dict], own_urls: set[str], limit: int = 4) -> list[dict]:
    """Fetch full text of the most recap-like related articles (they usually narrate dated events)."""
    from backend.services import extract
    from backend.services.research import HARD_PAYWALL, _ensure_articles
    cands = [c for c in related if c["url"] not in own_urls and feeds.domain_of(c["url"]) not in HARD_PAYWALL
             and not c["url"].startswith("/") and "msn.com" not in c["url"]]
    cands.sort(key=lambda c: -(c["sim"] + (0.15 if _RECAP_HINT.search(c["title"]) else 0.0)))
    rows = await _ensure_articles(cands[:limit + 2])
    fetched = await extract.fetch_many(rows)
    out = []
    for r, f in zip(rows, fetched):
        if f["text"] and not f["paywall"] and len(f["text"]) > 900:
            out.append({"url": r["url"], "outlet": r["outlet"], "title": r["title"], "text": f["text"],
                        "published_at": next((c["dt"].isoformat() for c in cands if c["url"] == r["url"] and c.get("dt")), r["published_at"])})
    return out[:limit]


async def build_story_timeline(story: dict, own_texts: list[dict], entity_names: list[str], own_urls: set[str]) -> dict:
    """The full "How it unfolded" pipeline. Returns {events, shown, reason, history, raw_count}.
    story: {id, headline, dek, what_happened, first_seen, vec?, thread_rows?}"""
    from backend.services import timeline
    history, related = await find_history(story, entity_names, own_urls)
    story_text = f"{story['headline']}. {story.get('dek', '')} {story.get('what_happened', '')}"
    events: list[dict] = []
    try:
        recaps = await fetch_recaps(related, own_urls)
    except Exception as e:  # noqa: BLE001 — network is optional
        log.info("recap fetch failed: %s", str(e)[:80])
        recaps = []
    sources = [dict(t) for t in own_texts] + recaps
    if sources:
        tl = await timeline.build_timeline(sources, story_text, max_sources=3, apply_gate=False)
        events.extend(dict(e, kind="reported") for e in tl["events"])
    events.extend(await render_history_events(story, history))
    events = _thin(timeline.merge_events(events))
    ok, reason = timeline.gate(events)
    return {"events": events if ok else [], "shown": ok, "reason": reason, "history": history,
            "raw_count": len(history) + len(recaps)}


def _thin(events: list[dict], keep: int = 7) -> list[dict]:
    """At most ~7 entries, at most 2 per day, oldest→newest."""
    per_day: dict[str, int] = {}
    out = []
    for e in sorted(events, key=lambda e: e["date"]):
        if per_day.get(e["date"], 0) >= 2:
            continue
        per_day[e["date"]] = per_day.get(e["date"], 0) + 1
        out.append(e)
    if len(out) > keep:
        step = len(out) / keep
        out = [out[int(i * step)] for i in range(keep - 1)] + [out[-1]]
    return out


def background_block(items: list[dict], limit: int = 8) -> str:
    """Compact text for the explainer prompt."""
    lines = [f"- {c['dt']:%d %b %Y} ({c['outlet']}): {c['title_clean']}" + (f" — {c['snippet'][:180]}" if c.get("snippet") else "")
             for c in items[-limit:]]
    return "\n".join(lines)
