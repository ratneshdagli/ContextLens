"""ContextLens v3 — RSS ingestion.

Fixes vs v2: feeds are parsed from raw *bytes* (feedparser handles the declared encoding, which
removes the mojibake), summaries are HTML-stripped, junk/opinion/live items are dropped before
anything is embedded or scored, and there is no per-headline "search" step at all.
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from backend import db
from backend.config import (BLOCKLIST_DOMAINS, FEED_TIMEOUT, FEEDS, TRUSTED_DOMAIN_MAP, USER_AGENT)

log = logging.getLogger("feeds")

JUNK_TITLE_RE = re.compile(
    r"(^|\b)(live(?: blog| updates?)?:|watch:|watch live|podcast|newsletter|crossword|quiz|horoscope|wordle|"
    r"connections hint|photos?:|in pictures|gallery|sponsored|advertis|how to watch|deals?:|best .* deals|"
    r"today'?s? (?:top )?(?:picks|papers)|what to stream|obituary quiz)",
    re.IGNORECASE,
)
OPINION_URL_RE = re.compile(r"/(opinion|opinions|commentisfree|comment|editorial|voices|blogs?|podcasts?|video|videos|live)/", re.I)
TRACKING_PARAMS = re.compile(r"^(utm_|ocid|cmpid|ito|xtor|gaa_|fbclid|gclid|ref|ncid|smid|sref|source)", re.I)


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    if "<" in s and ">" in s:
        s = BeautifulSoup(s, "html.parser").get_text(" ", strip=True)
    s = re.sub(r"\s+", " ", s).strip()
    # Repair the classic UTF-8-decoded-as-latin1 mojibake if any slipped through
    if "Ã" in s or "â€" in s:
        try:
            s = s.encode("cp1252", errors="ignore").decode("utf-8", errors="ignore") or s
        except Exception:
            pass
    return s


def normalise_url(url: str) -> str:
    p = urlparse(url.strip())
    q = "&".join(x for x in p.query.split("&") if x and not TRACKING_PARAMS.match(x.split("=")[0]))
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", q, ""))


def url_hash(url: str) -> str:
    return hashlib.sha1(normalise_url(url).encode("utf-8")).hexdigest()


def domain_of(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def lookup_source(domain: str) -> Optional[dict]:
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        hit = TRUSTED_DOMAIN_MAP.get(".".join(parts[i:]))
        if hit:
            return hit
    return None


def trust_rank_for(domain: str, default: int = 3) -> int:
    hit = lookup_source(domain)
    if hit:
        return int(hit["trust_rank"])
    if domain in BLOCKLIST_DOMAINS:
        return 5
    return default


def _entry_image(entry) -> str:
    for attr in ("media_content", "media_thumbnail"):
        v = getattr(entry, attr, None)
        if v:
            for m in v:
                if m.get("url"):
                    return m["url"]
    for enc in getattr(entry, "enclosures", []) or []:
        if "image" in (enc.get("type") or "") and enc.get("href"):
            return enc["href"]
    raw = (entry.get("summary") or "") + " ".join(c.get("value", "") for c in entry.get("content", []) or [])
    m = re.search(r'<img[^>]+src="([^"]+)"', raw)
    return m.group(1) if m else ""


def _entry_time(entry) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
            except Exception:
                continue
    return None


async def _fetch_one(client: httpx.AsyncClient, url: str, outlet: str, topic: str, sem: asyncio.Semaphore) -> list[dict]:
    async with sem:
        try:
            r = await client.get(url)
            r.raise_for_status()
            content = r.content  # bytes on purpose
        except Exception as e:
            log.info("feed failed %s: %s", url, str(e)[:80])
            return []
    loop = asyncio.get_running_loop()
    parsed = await loop.run_in_executor(None, feedparser.parse, content)
    return await loop.run_in_executor(None, _items_from, parsed, outlet, topic)      # HTML cleaning is CPU work: not on the event loop


def _items_from(parsed, outlet: str, topic: str) -> list[dict]:
    out = []
    for e in parsed.entries[:40]:
        link = (e.get("link") or "").strip()
        title = clean_text(e.get("title", ""))
        if not link or not title or len(title) < 15:
            continue
        if JUNK_TITLE_RE.search(title) or OPINION_URL_RE.search(link):
            continue
        dom = domain_of(link)
        if dom in BLOCKLIST_DOMAINS:
            continue
        src = lookup_source(dom)
        published = _entry_time(e)
        out.append({
            "url": link,
            "title": title,
            "snippet": clean_text(e.get("summary", ""))[:600],
            "outlet": (src or {}).get("name") or outlet,
            "domain": dom,
            "trust_rank": trust_rank_for(dom, 3),
            "topic": topic,
            "published_at": published.isoformat() if published else None,
            "image_url": _entry_image(e),
        })
    return out


async def ingest(max_age_hours: int = 48, feeds: Optional[list] = None, extra_items: Optional[list] = None,
                 windows: Optional[dict] = None) -> dict:
    """Fetch all feeds, store new articles, return stats.
    `feeds=[]` means no general feeds (a channel-only refresh); `extra_items` are pre-fetched items (channel searches);
    `windows` maps a topic to its own max age in hours (channels publish slowly, so they keep a longer window)."""
    feeds = FEEDS if feeds is None else feeds
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(timeout=FEED_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"}) as client:
        results = await asyncio.gather(*[_fetch_one(client, u, o, t, sem) for (u, o, t) in feeds])
    ok_feeds = sum(1 for r in results if r)
    items = [it for r in results for it in r] + list(extra_items or [])

    now_utc = datetime.now(timezone.utc)
    fresh, seen = [], set()
    for it in items:
        h = url_hash(it["url"])
        if h in seen:
            continue
        seen.add(h)
        cutoff = now_utc - timedelta(hours=max((windows or {}).get(it["topic"], 0), max_age_hours))
        if it["published_at"] and datetime.fromisoformat(it["published_at"]) < cutoff:
            continue
        it["url_hash"] = h
        fresh.append(it)

    now = db.now_iso()
    async with db.get_db() as conn:
        new = 0
        for it in fresh:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO articles(id,url,url_hash,title,snippet,outlet,domain,trust_rank,topic,published_at,fetched_at,image_url)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (db.new_id(), it["url"], it["url_hash"], it["title"], it["snippet"], it["outlet"], it["domain"],
                 it["trust_rank"], it["topic"], it["published_at"] or now, now, it["image_url"]),
            )
            new += cur.rowcount or 0
        await conn.commit()
    return {"feeds_total": len(feeds), "feeds_ok": ok_feeds, "items": len(items), "fresh": len(fresh), "new": new}
