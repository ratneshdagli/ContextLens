"""Live local headlines for a place: Google News RSS location searches, cached 15 minutes. Shown on the globe as
"live headlines" next to the reader's own stories; never stored, never fed to a model."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, urlparse

import feedparser
import httpx

from backend import db
from backend.config import SEARXNG_URL, USER_AGENT

log = logging.getLogger("geo_live")
TTL = 900
LIVE_TTL = 240                                            # places in the reader's current view refresh this often
_cache: dict[tuple, tuple[float, list[dict]]] = {}
_sem = asyncio.Semaphore(6)
_inflight: set[tuple] = set()                              # places being fetched right now (the area collector reports these as pending)
_native: set[tuple] = set()                              # places whose own-script name has been searched (a plain fetch earlier must not hide that)
_outlets: dict[str, list] = {}                             # domain -> [outlet name, headlines seen]: the source auditor's candidate list
_SUFFIX = re.compile(r"\s+-\s+([^-]{2,60})$")


def _seen_outlet(domain: str, outlet: str) -> None:
    row = _outlets.setdefault(domain, [outlet, 0])
    row[1] += 1


def outlets() -> list[dict]:
    return sorted(({"domain": d, "outlet": v[0], "n": v[1]} for d, v in _outlets.items()), key=lambda r: -r["n"])


def _rss_entries(content: bytes) -> list[tuple[str, str, str, str]]:
    out = []
    for e in feedparser.parse(content).entries[:100]:
        title = (e.get("title") or "").strip()
        outlet = ((e.get("source") or {}).get("title") or "").strip()
        m = _SUFFIX.search(title)
        if m:
            outlet = outlet or m.group(1)
            title = title[:m.start()].strip()
        link = e.get("link") or ""
        src = urlparse((e.get("source") or {}).get("href") or "").netloc.removeprefix("www.")
        if src and outlet:
            _seen_outlet(src, outlet)
        if "bing.com/news/apiclick" in link:
            link = (parse_qs(urlparse(link).query).get("url") or [link])[0]
        out.append((title, outlet, link, e.get("published", "")))
    return out


_BING_PAGE = ("JSON.stringify(Array.from(document.querySelectorAll('a.title')).map(a=>[a.innerText,a.href,"
              "a.closest('.news-card-body')?.querySelector('span[aria-label$=ago]')?.getAttribute('aria-label')||'']))")
_AGO = re.compile(r"(\d+)\s*(minute|hour|day|week)", re.I)


def _ago(s: str) -> str:
    m = _AGO.search(s or "")
    if not m:
        return ""
    secs = int(m.group(1)) * {"minute": 60, "hour": 3600, "day": 86400, "week": 604800}[m.group(2).lower()]
    return (datetime.now(timezone.utc) - timedelta(seconds=secs)).isoformat()


_LANGS = {"DEVANAGARI": ("mr", "hi"), "BENGALI": ("bn",), "TAMIL": ("ta",), "TELUGU": ("te",), "GUJARATI": ("gu",), "KANNADA": ("kn",),
          "MALAYALAM": ("ml",), "GURMUKHI": ("pa",), "ARABIC": ("ar",), "CYRILLIC": ("ru", "uk"), "GREEK": ("el",), "HEBREW": ("he",),
          "THAI": ("th",), "HANGUL": ("ko",), "CJK": ("zh-CN",), "HIRAGANA": ("ja",), "KATAKANA": ("ja",)}


def _langs(native: str) -> tuple[str, ...]:
    """Google News editions worth asking for a place's name written in its own script."""
    try:
        return _LANGS.get(unicodedata.name(native[0]).split()[0], ())
    except (ValueError, IndexError):
        return ()


async def _fetch_stealth(place: str, gl: str, page: int) -> list[tuple[str, str, str, str]]:
    """Bing News result pages through the stealth browser: real article links and 2-3x what the RSS endpoint returns."""
    from backend.services import extract
    out = await extract.stealth_eval(f"https://www.bing.com/news/search?q={quote(chr(34) + place + chr(34))}&setmkt=en-{gl}&first={1 + 10 * page}",
                                     _BING_PAGE, key="geo")
    try:
        return [(t, urlparse(u).netloc.removeprefix("www."), u, _ago(ago)) for t, u, ago in (json.loads(out) if out else []) if u.startswith("http")]
    except ValueError:
        return []


_gsem = asyncio.Semaphore(2)                               # Google News throttles bursts (503): two at a time, paced, and a cooldown when it pushes back
_g_cool = 0.0


async def _google(c: httpx.AsyncClient, url: str) -> bytes:
    global _g_cool
    if time.monotonic() < _g_cool:
        return b""
    async with _gsem:
        r = await c.get(url)
        await asyncio.sleep(0.7)
    if r.status_code in (429, 503):
        _g_cool = time.monotonic() + 120
        log.warning("Google News is throttling us; pausing it for 2 minutes (Bing, SearXNG and Camofox carry on)")
        return b""
    r.raise_for_status()
    return r.content


async def _fetch(c: httpx.AsyncClient, kind: str, place: str, gl: str) -> list[tuple[str, str, str, str]]:
    """One source's raw (title, outlet, link, published) rows; a source that fails just contributes nothing."""
    if kind.startswith("g:"):                                      # the place's own-script name in that language's Google News edition
        try:
            lang = kind[2:]
            body = await _google(c, f"https://news.google.com/rss/search?q={quote(chr(34) + place + chr(34) + ' when:7d')}&hl={lang}&gl={gl}&ceid={gl}:{lang}")
            return await asyncio.to_thread(_rss_entries, body) if body else []
        except Exception as e:  # noqa: BLE001
            log.info("live headlines (%s) failed for %r: %s", kind, place, str(e)[:60])
            return []
    if kind.startswith("b:"):                                      # Bing News RSS in that language's market
        try:
            r = await c.get(f"https://www.bing.com/news/search?q={quote(chr(34) + place + chr(34))}&format=rss&setmkt={kind[2:]}-{gl}")
            r.raise_for_status()
            return await asyncio.to_thread(_rss_entries, r.content)
        except Exception as e:  # noqa: BLE001
            log.info("live headlines (%s) failed for %r: %s", kind, place, str(e)[:60])
            return []
    if kind.startswith("stealth"):
        return await _fetch_stealth(place, gl, int(kind[7:] or 0))
    try:
        if kind in ("google", "google7"):
            body = await _google(c, f"https://news.google.com/rss/search?q={quote(chr(34) + place + chr(34) + (' when:7d' if kind == 'google7' else ' when:2d'))}&hl=en-{gl}&gl={gl}&ceid={gl}:en")
            return await asyncio.to_thread(_rss_entries, body) if body else []
        elif kind == "bing":
            r = await c.get(f"https://www.bing.com/news/search?q={quote(chr(34) + place + chr(34))}&format=rss&setmkt=en-{gl}")
        else:                                                      # our own SearXNG: news engines the RSS endpoints don't cover
            r = await c.get(SEARXNG_URL.rstrip("/") + "/search", params={"q": f'"{place}"', "format": "json", "categories": "news", "time_range": "day"})
            return [(x.get("title") or "", urlparse(x.get("url") or "").netloc.removeprefix("www."), x.get("url") or "", x.get("publishedDate") or "")
                    for x in r.json().get("results", [])[:30]]
        r.raise_for_status()
        return await asyncio.to_thread(_rss_entries, r.content)
    except Exception as e:  # noqa: BLE001 — live headlines are a bonus; a failure just means none
        log.info("live headlines (%s) failed for %r: %s", kind, place, str(e)[:60])
        return []


def _when(s: str) -> str:
    try:
        if s[:2] == "20" and "T" in s and "," not in s:
            return s
        d = parsedate_to_datetime(s) if "," in s else datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).isoformat()


async def headlines(place: str, cc: str = "", limit: int = 25, at: tuple = (), deep: bool = False, ttl: int = TTL, native: str = "") -> tuple[list[dict], bool]:
    """(items, from_cache). Items: {title, outlet, url, published_at}. Google News + Bing News + SearXNG, only items that mention the place."""
    key = (place.lower(), cc)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl and (not native or key in _native):
        return hit[1][:limit], True
    gl = cc if cc and len(cc) == 2 else "US"
    kinds = ("google", "bing", "searx") + (("google7", "stealth0", "stealth1") if deep else ())    # deep = also the week's news + Camofox pages
    _inflight.add(key)
    try:
        async with _sem, httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
            jobs = [(k, place) for k in kinds] + [(f"{src}:{lang}", native) for lang in _langs(native) for src in ("g", "b")]
            rows = [r for part in await asyncio.gather(*(_fetch(c, k, nm, gl) for k, nm in jobs)) for r in part]
    finally:
        _inflight.discard(key)
    items: list[dict] = []
    seen: set[str] = set()
    for title, outlet, link, pub in rows:
        if len(title) < 15 or len(title) > 180 or title.lower() in seen or not (place.lower() in title.lower() or (native and native in title)):
            continue
        if not native and sum(ord(ch) > 127 for ch in title) > 0.2 * len(title):
            continue                  # English-named places: skip headlines written mostly in another script (own-script places keep theirs)
        seen.add(title.lower())
        if outlet and link.startswith("http") and "news.google.com" not in link:
            _seen_outlet(urlparse(link).netloc.removeprefix("www."), outlet)
        items.append({"title": title, "outlet": outlet, "url": link, "published_at": _when(pub)})
    items.sort(key=lambda i: i["published_at"], reverse=True)
    _cache[key] = (time.time() - (TTL - 120 if not rows else 0), items)
    if native:
        _native.add(key)       # every source failed: retry in 2 min, not 15
    await store(place, cc, at, items)
    if len(_cache) > 900:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:150]:
            _cache.pop(k, None)
    return items[:limit], False


async def warm(n: int = 260) -> int:
    """Keep local headlines fresh: the cities the feed talks about most first, then every country's capital and the biggest cities,
    so the globe has live dots on every continent (they become dots on the globe)."""
    from backend.services import geo
    rows = await db.fetch_all("SELECT place,cc,country,AVG(lat) AS lat,AVG(lon) AS lon,COUNT(*) AS c FROM geo_tags "
                              "WHERE precision='city' AND lat IS NOT NULL AND tagged_at > datetime('now','-3 day') "
                              "GROUP BY place,cc ORDER BY c DESC LIMIT 60")
    todo = [(r["place"], r["cc"], r["lat"], r["lon"], r["country"]) for r in rows]
    if geo.G.ok:
        todo += [(p.name, p.cc, p.lat, p.lon, geo.G.country_names.get(p.cc, p.cc)) for p in geo.G.countries.values()]
        top = sorted(geo.G.cities, key=lambda p: -p.pop)[:80]
        todo += [(p.name, p.cc, p.lat, p.lon, geo.G.country_names.get(p.cc, p.cc)) for p in top]
    fresh = {(r["place"].lower(), r["cc"]) for r in await db.fetch_all(
        "SELECT DISTINCT place,cc FROM live_news WHERE seen_at > ?", ((datetime.now(timezone.utc) - timedelta(seconds=TTL)).isoformat(),))}
    fetched, seen = 0, set()
    for place, cc, lat, lon, country in todo:
        k = (place.lower(), cc)
        if k in seen:
            continue
        seen.add(k)
        if len(seen) > n:
            break
        if k in fresh or (k in _cache and time.time() - _cache[k][0] < TTL):
            continue
        await headlines(place, cc, at=(lat, lon, country, place), deep=False)                # the background refresh stays light: Camofox pages are for what the reader is looking at
        fetched += 1
        await asyncio.sleep(1.0)                   # be polite to the free endpoints
    return fetched


def _id(url: str) -> str:
    return "L" + hashlib.md5(url.encode()).hexdigest()[:12]


async def store(place: str, cc: str, at: tuple, items: list[dict]) -> None:
    """Keep fetched headlines: the map reads them back from here, so they survive restarts and panning away and back."""
    if not items or not at:
        return
    now = db.now_iso()
    await db.executemany("INSERT OR REPLACE INTO live_news(id,place,cc,country,lat,lon,title,outlet,url,published_at,seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         [(_id(i["url"]), at[3], cc, at[2], at[0], at[1], i["title"], i["outlet"], i["url"], i["published_at"], now) for i in items if i["url"]])


async def points(hours: int = 48, box: tuple = (-180.0, -90.0, 180.0, 90.0)) -> list[dict]:
    """Stored live headlines as pin-able items {id,title,outlet,url,t,place,country,lat,lon}; quiet places are topped up from the week."""
    w, s_, e, n = box
    lon = "lon BETWEEN ? AND ?" if w <= e else "(lon >= ? OR lon <= ?)"
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = await db.fetch_all(f"SELECT * FROM live_news WHERE published_at >= ? AND lat BETWEEN ? AND ? AND {lon} ORDER BY published_at DESC LIMIT 8000",
                              (week, max(-90, s_), min(90, n), w, e))
    cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    by: dict[tuple, list[dict]] = {}
    for r in rows:
        by.setdefault((r["place"], r["cc"]), []).append(r)
    out = []
    for rs in by.values():
        recent = [r for r in rs if r["published_at"] >= cut][:20]
        if len(recent) < 5:
            recent += [r for r in rs if r["published_at"] < cut][:5 - len(recent)]
        out += [{"id": r["id"], "title": r["title"], "outlet": r["outlet"], "url": r["url"], "t": r["published_at"], "place": r["place"],
                 "country": r["country"], "lat": r["lat"], "lon": r["lon"]} for r in recent]
    return out


async def prune() -> None:
    await db.execute("DELETE FROM live_news WHERE published_at < ?", ((datetime.now(timezone.utc) - timedelta(days=14)).isoformat(),))


async def collect_area(w: float, s: float, e: float, n: float, zoom: float) -> dict:
    """The reader zoomed into a region: fetch live headlines for its biggest cities, in parallel, in the background.
    Returns at once with {places, queued, pending}; poll again to see pending drop as the dots become available."""
    from backend.services import geo
    cap = 14 if zoom < 6 else 28 if zoom < 8 else 48
    todo = geo.cities_in(w, s, e, n, cap) if zoom >= 4 else []
    fresh = lambda k: k in _cache and time.time() - _cache[k][0] < LIVE_TTL   # noqa: E731
    queued = 0
    for p in todo:
        k = (p.name.lower(), p.cc)
        if fresh(k) or k in _inflight:
            continue
        _inflight.add(k)                                  # claimed now so a second poll doesn't queue it again
        queued += 1
        asyncio.create_task(_collect_one(p))
    keys = {(p.name.lower(), p.cc) for p in todo}
    return {"places": len(todo), "queued": queued, "pending": len(keys & _inflight)}


async def _collect_one(p) -> None:
    from backend.services import geo
    k = (p.name.lower(), p.cc)
    _inflight.discard(k)                                  # headlines() re-adds it for the duration of the fetch
    try:
        await headlines(p.name, p.cc, at=(p.lat, p.lon, geo.G.country_names.get(p.cc, p.cc), p.name), deep=True, ttl=LIVE_TTL)
    except Exception as e:  # noqa: BLE001
        log.info("area fetch failed for %r: %s", p.name, str(e)[:60])


async def collect_places(places: list[tuple[str, float, float, str]]) -> dict:
    """Street-level zoom: the map's own neighbourhood/suburb labels (name, lat, lon). Same contract as collect_area."""
    from backend.services import geo
    queued, keys = 0, set()
    for name, lat, lon, native in places[:40]:
        near = geo.nearest_city(lat, lon, 300.0)
        cc = near.cc if near else ""
        k = (name.lower(), cc)
        keys.add(k)
        if (k in _cache and time.time() - _cache[k][0] < LIVE_TTL and (not native or k in _native)) or k in _inflight:
            continue
        _inflight.add(k)
        queued += 1
        asyncio.create_task(_collect_local(name, cc, lat, lon, geo.G.country_names.get(cc, cc), native))
    return {"places": len(keys), "queued": queued, "pending": len(keys & _inflight)}


async def _collect_local(name: str, cc: str, lat: float, lon: float, country: str, native: str = "") -> None:
    _inflight.discard((name.lower(), cc))
    try:
        await headlines(name, cc, at=(lat, lon, country, name), deep=True, ttl=LIVE_TTL, native=native)
    except Exception as e:  # noqa: BLE001
        log.info("local fetch failed for %r: %s", name, str(e)[:60])
