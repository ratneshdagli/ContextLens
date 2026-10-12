"""Source auditor: find news sites the live globe fetchers keep meeting, probe their feeds, and keep only the ones that pass.
Fail-closed: a site is added only with a working, fresh, English feed; everything else is reported with its reason."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import httpx

from backend.config import BLOCKLIST_DOMAINS, FEEDS, SEARXNG_URL, TRUSTED_DOMAIN_MAP, USER_AGENT
from backend.services import channels, extract, feeds as F

log = logging.getLogger("audit")
DIR = pathlib.Path(__file__).resolve().parent.parent
AGGREGATORS = {"msn.com", "yahoo.com", "news.google.com", "google.com", "bing.com", "dailyhunt.in", "newsbreak.com", "flipboard.com",
               "smartnews.com", "ground.news", "facebook.com", "x.com", "twitter.com", "youtube.com", "instagram.com", "reddit.com",
               "wikipedia.org", "linkedin.com", "whatsapp.com", "t.me"}
TLD_CC = {"in": "IN", "uk": "GB", "au": "AU", "ca": "CA", "za": "ZA", "ng": "NG", "pk": "PK", "bd": "BD", "lk": "LK", "ke": "KE",
          "nz": "NZ", "ie": "IE", "sg": "SG", "ph": "PH", "my": "MY", "gh": "GH", "np": "NP"}


def base(domain: str) -> str:
    return F.domain_of("https://" + domain) if domain else ""


def known() -> set[str]:
    return {F.domain_of(u) for u, _, _ in FEEDS} | set(TRUSTED_DOMAIN_MAP) | BLOCKLIST_DOMAINS | AGGREGATORS


def country_of(domain: str) -> str:
    return TLD_CC.get(domain.rsplit(".", 1)[-1], "")


async def _probe_stealth(client: httpx.AsyncClient, domain: str) -> dict | None:
    """The plain probe found nothing (bot wall, JS-built page): read the home page's feed links through the stealth browser."""
    out = await extract.stealth_eval(f"https://{domain}/", "JSON.stringify(Array.from(document.querySelectorAll('link[rel=alternate]')).map(l=>[l.type,l.href]))", key="audit")
    try:
        links = [h for t, h in json.loads(out or "[]") if h and ("rss" in t or "atom" in t or "xml" in t) and "comment" not in h.lower()]
    except ValueError:
        return None
    for u in links[:4]:
        hit = await channels.validate_feed(client, u)
        if hit:
            return hit
    return None


async def audit_domain(client: httpx.AsyncClient, sem: asyncio.Semaphore, domain: str) -> dict:
    """{domain, verdict: accept|local-language|reject, reason, url?, outlet?, n?, newest_days?}"""
    out = {"domain": domain, "verdict": "reject", "reason": ""}
    hit = await channels.probe_feed(client, domain, sem) or await _probe_stealth(client, domain)
    if not hit:
        return dict(out, reason="no working RSS/Atom feed")
    try:
        r = await client.get(hit["url"])
        items = [e for e in feedparser.parse(r.content).entries if e.get("title") and e.get("link")]
    except Exception as e:  # noqa: BLE001
        return dict(out, reason=f"feed fetch failed: {str(e)[:50]}")
    times = [t for t in (F._entry_time(e) for e in items) if t]
    age = (datetime.now(timezone.utc) - max(times)).days if times else None
    titles = [F.clean_text(e["title"]) for e in items]
    ascii_share = sum(sum(c.isascii() for c in t) / max(1, len(t)) for t in titles) / max(1, len(titles))
    out.update(url=hit["url"], outlet=hit["outlet"], n=len(items), newest_days=age, ascii=round(ascii_share, 2))
    if len(items) < 5:
        return dict(out, reason=f"only {len(items)} items")
    if age is None or age > 7:
        return dict(out, reason="stale: nothing in the last 7 days")
    if ascii_share < 0.8:
        return dict(out, verdict="local-language", reason="feed is not English (kept in the report, not added to the English feed)")
    return dict(out, verdict="accept", reason="ok")


async def candidates(api: str = "", search: str = "", extra: tuple = ()) -> list[str]:
    """Domains to audit: what the live fetchers saw (via the running API), a SearXNG news search, and any given by hand."""
    found: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
        if api:
            try:
                for o in (await c.get(api.rstrip("/") + "/api/globe/outlets")).json():
                    found[base(o["domain"])] = found.get(base(o["domain"]), 0) + o["n"]
            except Exception as e:  # noqa: BLE001
                log.warning("could not read live outlets: %s", str(e)[:60])
        if search:
            try:
                r = await c.get(SEARXNG_URL.rstrip("/") + "/search", params={"q": search, "format": "json", "categories": "news"})
                for x in r.json().get("results", []):
                    d = base(urlparse(x.get("url") or "").netloc.removeprefix("www."))
                    found[d] = found.get(d, 0) + 1
            except Exception as e:  # noqa: BLE001
                log.warning("searxng discovery failed: %s", str(e)[:60])
    for d in extra:
        found[base(d)] = found.get(base(d), 0) + 1
    skip = known()
    return [d for d, _ in sorted(found.items(), key=lambda kv: -kv[1])
            if d and "." in d and not any(d == k or d.endswith("." + k) for k in skip)]       # subdomains of aggregators and blocked sites too


async def run(api: str = "", search: str = "", extra: tuple = (), limit: int = 40) -> list[dict]:
    doms = (await candidates(api, search, extra))[:limit]
    sem = asyncio.Semaphore(6)
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
        return list(await asyncio.gather(*(audit_domain(c, sem, d) for d in doms)))


def _write(path: pathlib.Path, data: list, indent: int) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def apply(results: list[dict], only: tuple = ()) -> int:
    """Add the accepted sites to feeds_extra.json (read at the next start) and trusted_sources.json (trust rank 3, 'auditor')."""
    ok = [r for r in results if r["verdict"] == "accept" and (not only or r["domain"] in only)]
    if not ok:
        return 0
    fp, tp = DIR / "feeds_extra.json", DIR / "trusted_sources.json"
    feeds = json.loads(fp.read_text(encoding="utf-8"))
    trusted = json.loads(tp.read_text(encoding="utf-8"))
    have_u, have_d = {f["url"] for f in feeds}, {t["domain"] for t in trusted}
    added = 0
    for r in ok:
        cc = country_of(r["domain"])
        if r["url"] not in have_u:
            feeds.append({"url": r["url"], "outlet": r["outlet"], "topic": "india" if cc == "IN" else "world"})
            have_u.add(r["url"])
            added += 1
        if r["domain"] not in have_d:
            trusted.append({"domain": r["domain"], "name": r["outlet"], "trust_rank": 3, "type": "newspaper", "verified_by": "auditor", "country": cc})
            have_d.add(r["domain"])
    _write(fp, feeds, 0)
    _write(tp, trusted, 1)
    return added
