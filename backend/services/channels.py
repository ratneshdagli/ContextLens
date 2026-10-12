"""ContextLens v6.2 — reader-defined channels ("Classical music", "Indian startups", anything).

A channel is a name + short description. It brings its own sources, so a niche topic is not limited to the fixed feed list:

  discover   queries (LLM, with a rule-based fallback) → Google News RSS + SearXNG → the domains that cover the topic
             → probe each domain for a live RSS/Atom feed. Nothing is hard-coded; the reader can add, disable or remove feeds.
  ingest     the channel's live feeds + a fresh SearXNG news search per query feed the normal pipeline (topic "ch:<id>").
  match      a story belongs to a channel when name + description (embedding) and the channel's key terms agree. Items
             from general feeds can match too, so a BBC story about an orchestra lands in "Classical music".
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urljoin

import feedparser
import httpx
import numpy as np
from bs4 import BeautifulSoup

from backend import db
from backend.config import BLOCKLIST_DOMAINS, FEEDS, SEARXNG_URL, USER_AGENT
from backend.services import feeds as F
from backend.services import interests as I
from backend.services import llm

log = logging.getLogger("channels")

PREFIX = "ch:"
# a story must look like the channel: items from the channel's own sources get a lower bar than items from general feeds
MEMBER_MIN_OWN, MEMBER_MIN_GENERAL = 0.38, 0.60

_STOP = set("""about after again also and are because been before being between both but can could does each from have into just
like made make more most much news other over some such than that their them then there these they this those through very
want was were what when where which while who will with would your latest recent new stories story coverage""".split())
_NOT_A_SOURCE = ("google.", "youtube.", "youtu.be", "facebook.", "twitter.", "x.com", "instagram.", "reddit.", "linkedin.",
                 "tiktok.", "pinterest.", "wikipedia.", "msn.com", "yahoo.com", "flipboard.", "newsbreak.", "dailyhunt.",
                 "apple.news", "bing.com", "quora.", "medium.com", "substack.com", "wordpress.com", "blogspot.", "tumblr.",
                 "amazon.", "ebay.", "spotify.", "apple.com", "soundcloud.", "archive.org", "github.com")
_FEED_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml", "/feeds/all.rss", "/?feed=rss2")
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml", "application/rdf+xml")
_GENERAL_DOMAINS = {F.domain_of(u) for u, _, _ in FEEDS}        # already ingested for everyone; the vector match picks their relevant items


def topic_of(cid: str) -> str:
    return PREFIX + cid


def is_channel_topic(t: str) -> bool:
    return bool(t) and t.startswith(PREFIX)


# ── CRUD ─────────────────────────────────────────────────────────────────────

_COLS = "id,name,description,weight,depth,size,days,position,muted,queries,feeds,vec,discovered_at,created_at"


def _row(r: dict, vec: bool = False) -> dict:
    out = {k: r[k] for k in ("id", "name", "description", "weight", "depth", "size", "days", "position", "discovered_at", "created_at")}
    out["muted"] = db.jload(r["muted"], []) or []
    out["queries"] = db.jload(r["queries"], []) or []
    out["feeds"] = db.jload(r["feeds"], []) or []
    if vec:
        out["vec"] = llm.from_blob(r["vec"])
    return out


async def list_channels(vec: bool = False) -> list[dict]:
    rows = await db.fetch_all(f"SELECT {_COLS} FROM channels ORDER BY position, created_at")
    return [_row(r, vec) for r in rows]


async def get(cid: str, vec: bool = False) -> Optional[dict]:
    r = await db.fetch_one(f"SELECT {_COLS} FROM channels WHERE id=?", (cid,))
    return _row(r, vec) if r else None


def _int(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _clean(patch: dict) -> dict:
    """Validated column values for whichever fields the patch carries."""
    out: dict = {}
    if "name" in patch:
        out["name"] = re.sub(r"\s+", " ", str(patch["name"] or "")).strip()[:60]
    if "description" in patch:
        out["description"] = re.sub(r"\s+", " ", str(patch["description"] or "")).strip()[:400]
    if "weight" in patch:
        out["weight"] = _int(patch["weight"], 0, 3, 2)
    if "depth" in patch:
        out["depth"] = _int(patch["depth"], 1, 3, 2)
    if "size" in patch:
        out["size"] = _int(patch["size"], 3, 30, 12)
    if "days" in patch:
        out["days"] = _int(patch["days"], 1, 7, 5)
    if "muted" in patch:
        out["muted"] = db.jdump(I._clean_list(patch["muted"], limit=30))
    if "queries" in patch:
        out["queries"] = db.jdump(I._clean_list(patch["queries"], limit=8, maxlen=120))
    return out


async def create(data: dict) -> dict:
    v = _clean(data)
    if not v.get("name"):
        raise ValueError("a channel needs a name")
    cid = db.new_id()
    pos = (await db.fetch_one("SELECT COALESCE(MAX(position),-1)+1 AS p FROM channels"))["p"]
    v.setdefault("description", "")
    cols = {"id": cid, "position": pos, "created_at": db.now_iso(), **v}
    await db.execute(f"INSERT INTO channels({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values()))
    return await get(cid)


async def update(cid: str, patch: dict) -> Optional[dict]:
    old = await get(cid)
    if not old:
        return None
    v = _clean(patch)
    if "name" in v and not v["name"]:
        raise ValueError("a channel needs a name")
    if ("name" in v and v["name"] != old["name"]) or ("description" in v and v["description"] != old["description"]):
        v["vec"] = None                                          # re-embed on the next refresh
    if v:
        await db.execute(f"UPDATE channels SET {','.join(f'{k}=?' for k in v)} WHERE id=?", (*v.values(), cid))
    return await get(cid)


async def delete(cid: str) -> bool:
    if not await get(cid):
        return False
    t = topic_of(cid)
    await db.execute("DELETE FROM channels WHERE id=?", (cid,))
    await db.execute("UPDATE stories SET channel_id='' WHERE channel_id=?", (cid,))
    for tbl in ("articles", "clusters", "stories"):
        await db.execute(f"UPDATE {tbl} SET topic='world' WHERE topic=?", (t,))      # its stories stay, as general news
    return True


async def reorder(ids: list[str]) -> None:
    await db.executemany("UPDATE channels SET position=? WHERE id=?", [(i, cid) for i, cid in enumerate(ids)])


async def counts() -> dict[str, dict]:
    rows = await db.fetch_all(
        "SELECT channel_id, COUNT(*) n, SUM(CASE WHEN built_at IS NOT NULL THEN 1 ELSE 0 END) built, MAX(COALESCE(published_at,created_at)) last "
        "FROM stories WHERE channel_id!='' AND kind='feed' AND tier IN ('primary','more','wire') GROUP BY channel_id")
    return {r["channel_id"]: {"stories": r["n"], "built": r["built"] or 0, "last": r["last"]} for r in rows}


# ── matching ─────────────────────────────────────────────────────────────────

def text_of(ch: dict) -> str:
    return f"{ch['name']}. {ch['description']}".strip()


def _terms(ch: dict) -> list[str]:
    seen, out = set(), []
    for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", f"{ch['name']} {ch['name']} {ch['description']}"):
        w = w.lower().rstrip("s") if len(w) > 5 else w.lower()
        if w not in _STOP and w not in seen:
            seen.add(w)
            out.append(w)
    return out[:16]


def prepare(ch: dict) -> dict:
    """Attach the compiled key-term regex used by `affinity`. Idempotent."""
    t = _terms(ch)
    ch["_re"] = re.compile("|".join(r"(?<!\w)" + re.escape(x) for x in t), re.I) if t else None
    return ch


def affinity(centroid: np.ndarray, text: str, ch: dict) -> float:
    """0..1: how much a story belongs to the channel. Embedding similarity to "name. description" (calibrated like interests)
    plus evidence from the channel's key terms in the story text — neither alone decides."""
    kw = 0.0
    if ch.get("_re") is not None:
        kw = min(1.0, len({m.group(0).lower() for m in ch["_re"].finditer(text)}) / 3)
    v = ch.get("vec")
    if v is None or v.shape != centroid.shape:
        # no embedding (model offline): two distinct key terms are enough for the channel's OWN sources (bar 0.38), never for general feeds (0.60)
        return float(0.45 if kw >= 0.67 else 0.3 * kw)
    return float(min(1.0, 0.85 * I.calibrate(float(v @ centroid)) + 0.15 * kw))


def brief_line(ch: dict) -> str:
    """The reader profile the editor judges a channel's story against: the channel IS the reader's interest (in the editor's own
    vocabulary), and nothing from their general news profile, so an orchestra story is not judged by AI-and-markets standards."""
    return (f"Reader's specific interests: {ch['name']}: {ch['description'] or ch['name']} (high priority)\n"
            f"Topics they follow closely: {ch['name']}\n"
            f"This story was collected for that interest. Judge it as a devoted follower of {ch['name']} would, not as general news: "
            f"routine news within this field (appointments, seasons, releases, reviews, interviews) is newsworthy to them.")


async def load_ready() -> list[dict]:
    """Channels with embeddings (computed on demand) and compiled terms, in the reader's order. Never raises."""
    chs = await list_channels(vec=True)
    need = [c for c in chs if c.get("vec") is None]
    if need:
        try:
            vecs = await llm.embed([text_of(c) for c in need], kind="query")
            for c, v in zip(need, vecs):
                c["vec"] = v
                await db.execute("UPDATE channels SET vec=? WHERE id=?", (llm.to_blob(v), c["id"]))
        except Exception as e:  # noqa: BLE001 — without the embedding model the key terms still work
            log.info("channel embedding unavailable: %s", str(e)[:100])
    return [prepare(c) for c in chs]


# ── discovery ────────────────────────────────────────────────────────────────

async def make_queries(name: str, description: str) -> list[str]:
    """Short news-search queries for the topic. One small LLM call; falls back to the name and description phrases."""
    qs: list[str] = []
    try:
        schema = {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 8}},
                  "required": ["queries"], "additionalProperties": False}
        out = await llm.chat_json(
            "writer", "You write short news-search queries. Reply with JSON only.",
            f"A reader wants a news channel called “{name}”: {description or name}\n"
            "Write 6 different short search queries (2-5 words) that find RECENT NEWS on this topic from different angles "
            "(people, institutions, events, releases, controversies). No site: operators, no quotes.",
            schema, max_tokens=300, retries=0)
        qs = [str(q) for q in out.get("queries", [])]
    except Exception as e:  # noqa: BLE001
        log.info("query generation failed (%s) — using the description", str(e)[:80])
    parts = [p for p in re.split(r"[,;:.\n]+|\band\b", description or "") if len(p.strip()) > 3]
    qs = [name] + qs + [f"{name} {p.strip()}" for p in parts[:5]]
    return I._clean_list([q for q in qs if 3 <= len(q) <= 90], limit=8, maxlen=90)


def _public_host(d: str) -> bool:
    """A plain public hostname. We fetch these addresses, so IPs, ports, user@ parts and local names are refused."""
    if not d or ":" in d or "@" in d or d == "localhost" or d.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".corp")):
        return False
    try:
        ipaddress.ip_address(d)
        return False
    except ValueError:
        return "." in d


def _is_source(domain: str) -> bool:
    return _public_host(domain) and not any(s in domain for s in _NOT_A_SOURCE) and domain not in BLOCKLIST_DOMAINS


async def _gnews(client: httpx.AsyncClient, q: str) -> list[str]:
    """Domains of the publishers Google News credits for this query (we never ingest Google's redirect links)."""
    try:
        r = await client.get(f"https://news.google.com/rss/search?q={quote(q)}&hl=en-US&gl=US&ceid=US:en")
        parsed = await asyncio.to_thread(feedparser.parse, r.content)
        return [F.domain_of((e.get("source") or {}).get("href", "")) for e in parsed.entries[:40]]
    except Exception as e:  # noqa: BLE001
        log.info("google news failed for %r: %s", q, str(e)[:60])
        return []


async def _searx(client: httpx.AsyncClient, q: str, categories: str = "news", time_range: str = "month") -> list[dict]:
    try:
        r = await client.get(SEARXNG_URL.rstrip("/") + "/search",
                             params={"q": q, "format": "json", "categories": categories, "language": "en", "time_range": time_range})
        r.raise_for_status()
        return r.json().get("results", [])[:25]
    except Exception as e:  # noqa: BLE001 — a dead search server means channels find nothing: say so in the log
        log.warning("searxng failed for %r: %s", q, str(e)[:60])
        return []


async def validate_feed(client: httpx.AsyncClient, url: str) -> Optional[dict]:
    """A feed counts only if it parses, has several real items and posted something in the last 4 months."""
    try:
        r = await client.get(url)
        if r.status_code != 200 or len(r.content) < 200:
            return None
        parsed = await asyncio.to_thread(feedparser.parse, r.content)
    except Exception:  # noqa: BLE001
        return None
    items = [e for e in parsed.entries if e.get("title") and e.get("link")]
    if len(items) < 3:
        return None
    newest = max((t for t in (F._entry_time(e) for e in items) if t), default=None)
    if newest is not None and newest < datetime.now(timezone.utc) - timedelta(days=120):
        return None
    title = F.clean_text(parsed.feed.get("title", ""))
    title = re.split(r"\s+[-–—|:]\s+", title)[0][:60] if title else ""
    return {"url": str(r.url), "outlet": title or F.domain_of(str(r.url)), "ok": True, "n": len(items),
            "domain": F.domain_of(str(r.url)), "checked_at": db.now_iso()}


async def probe_feed(client: httpx.AsyncClient, domain: str, sem: asyncio.Semaphore) -> Optional[dict]:
    """Find a working feed for a site: the <link rel=alternate> tags on its home page first, then the usual paths."""
    async with sem:
        base = "https://" + domain
        cands: list[str] = []
        try:
            r = await client.get(base + "/")
            if r.status_code == 200 and "html" in r.headers.get("content-type", "html"):
                soup = BeautifulSoup(r.text[:300_000], "html.parser")
                for l in soup.find_all("link", attrs={"rel": "alternate"}):
                    if (l.get("type") or "").lower() in _FEED_TYPES and l.get("href") and "comment" not in (l.get("href") + (l.get("title") or "")).lower():
                        cands.append(urljoin(str(r.url), l["href"]))
        except Exception:  # noqa: BLE001
            pass
        cands += [base + p for p in _FEED_PATHS]
        seen: set[str] = set()
        for u in cands[:9]:
            if u in seen:
                continue
            seen.add(u)
            hit = await validate_feed(client, u)
            if hit:
                return hit
    return None


async def discover(cid: str, max_feeds: int = 10) -> dict:
    """Find the sources that cover this channel's topic. Saves queries + live feeds; keeps what the reader added by hand."""
    ch = await get(cid)
    if not ch:
        raise KeyError(cid)
    queries = ch["queries"] or await make_queries(ch["name"], ch["description"])
    score: Counter = Counter()
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        jobs = [_gnews(client, q) for q in queries[:6]]
        jobs += [_searx(client, q, "news", "month") for q in queries[:6]]
        jobs += [_searx(client, q, "general", "year") for q in queries[:3]]
        res = await asyncio.gather(*jobs)
        n_g = min(6, len(queries))
        for i, r in enumerate(res):
            if i < n_g:
                score.update(d for d in r if _is_source(d))                           # Google News: publisher domains
            else:
                score.update(d for d in (F.domain_of(x.get("url", "")) for x in r) if _is_source(d))
        known = {f["domain"] for f in ch["feeds"] if f.get("domain")}
        ranked = [d for d, _ in score.most_common(40) if d not in _GENERAL_DOMAINS and d not in known][:24]
        probed = await asyncio.gather(*[probe_feed(client, d, sem) for d in ranked])
    live = [p for p in probed if p][:max_feeds]
    fresh = await get(cid)                                       # discovery takes a minute: merge into what is stored NOW, not what we read at the start
    if not fresh:
        raise KeyError(cid)
    have = {f["url"] for f in fresh["feeds"]}
    feeds = fresh["feeds"] + [p for p in live if p["url"] not in have]
    await db.execute("UPDATE channels SET queries=?, feeds=?, discovered_at=? WHERE id=?",
                     (db.jdump(fresh["queries"] or queries), db.jdump(feeds), db.now_iso(), cid))
    out = await get(cid)
    out["discovery"] = {"queries": out["queries"], "domains_seen": len(score), "domains_tried": len(ranked), "feeds_found": len(live),
                        "search_hits": sum(len(r) for r in res[n_g:]),
                        "covered_by_general_feeds": sorted(d for d, _ in score.most_common(40) if d in _GENERAL_DOMAINS)[:8]}
    return out


async def add_feed(cid: str, url: str) -> dict:
    """Add a feed by hand. Accepts a feed URL or a site address (we look for its feed)."""
    ch = await get(cid)
    if not ch:
        raise KeyError(cid)
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    if not _public_host(F.domain_of(url)):
        raise ValueError("That is not a public website address.")
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        hit = await validate_feed(client, url) or await probe_feed(client, F.domain_of(url), asyncio.Semaphore(2))
    if not hit:
        raise ValueError("No working feed found at that address (it needs several recent items).")
    if any(f["url"] == hit["url"] for f in ch["feeds"]):
        raise ValueError("That feed is already in this channel.")
    hit["manual"] = True
    await db.execute("UPDATE channels SET feeds=? WHERE id=?", (db.jdump(ch["feeds"] + [hit]), cid))
    return await get(cid)


async def set_feed(cid: str, url: str, off: Optional[bool] = None, remove: bool = False) -> Optional[dict]:
    ch = await get(cid)
    if not ch:
        return None
    feeds = []
    for f in ch["feeds"]:
        if f["url"] == url:
            if remove:
                continue
            if off is not None:
                f = {**f, "off": bool(off)}
        feeds.append(f)
    await db.execute("UPDATE channels SET feeds=? WHERE id=?", (db.jdump(feeds), cid))
    return await get(cid)


# ── ingest side ──────────────────────────────────────────────────────────────

def feed_list(chs: list[dict]) -> list[tuple[str, str, str]]:
    """(url, outlet, topic) for every live, enabled channel feed — the shape feeds.ingest already takes."""
    return [(f["url"], f.get("outlet") or f.get("domain") or "", topic_of(c["id"]))
            for c in chs if c["weight"] > 0 for f in c["feeds"] if f.get("ok", True) and not f.get("off")]


def _when(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()
    except ValueError:
        return None


async def search_items(chs: list[dict]) -> list[dict]:
    """Fresh news-search hits for each channel's queries, shaped like feed items (real publisher URLs, so full text works)."""
    chs = [c for c in chs if c["weight"] > 0]
    if not chs:
        return []
    jobs_for: list[dict] = []
    async with httpx.AsyncClient(timeout=14, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        for c in chs:
            jobs_for += [c] * len((c["queries"] or [c["name"]])[:4])
        res = await asyncio.gather(*[_searx(client, q, "news", "week") for c in chs for q in (c["queries"] or [c["name"]])[:4]])
    out: list[dict] = []
    for c, hits in zip(jobs_for, res):
        for h in hits:
            url, title = (h.get("url") or "").strip(), F.clean_text(h.get("title", ""))
            dom = F.domain_of(url)
            if not url or len(title) < 15 or not _is_source(dom) or F.JUNK_TITLE_RE.search(title) or F.OPINION_URL_RE.search(url):
                continue
            src = F.lookup_source(dom)
            out.append({"url": url, "title": title, "snippet": F.clean_text(h.get("content", ""))[:600],
                        "outlet": (src or {}).get("name") or dom, "domain": dom, "trust_rank": F.trust_rank_for(dom, 3),
                        "topic": topic_of(c["id"]), "published_at": _when(h.get("publishedDate")), "image_url": h.get("thumbnail") or ""})
    return out
