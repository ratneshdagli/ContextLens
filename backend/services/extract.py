"""ContextLens v3 — full-text article extraction (trafilatura + BeautifulSoup fallback)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Optional

import httpx
import trafilatura
from bs4 import BeautifulSoup
from charset_normalizer import from_bytes

from backend import db
from backend.config import CRAWL_TIMEOUT, USER_AGENT
from backend.services.feeds import clean_text

log = logging.getLogger("extract")

PAYWALL = ("subscribe to read", "subscription required", "sign in to read", "already a subscriber",
           "this article is for subscribers", "unlock this article", "create a free account to continue")
_last: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


def _decode(resp: httpx.Response) -> str:
    ct = resp.headers.get("content-type", "")
    if "charset=" in ct.lower():
        try:
            return resp.content.decode(ct.lower().split("charset=")[1].split(";")[0].strip(), errors="replace")
        except Exception:
            pass
    best = from_bytes(resp.content[:200000]).best()
    return str(best) if best else resp.content.decode("utf-8", errors="replace")


async def _fetch(url: str) -> Optional[str]:
    dom = re.sub(r"^https?://([^/]+).*", r"\1", url)
    lock = _locks.setdefault(dom, asyncio.Lock())
    async with lock:                       # per-domain politeness
        wait = 0.6 - (time.monotonic() - _last.get(dom, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        _last[dom] = time.monotonic()
    blocked = False
    try:
        async with httpx.AsyncClient(timeout=CRAWL_TIMEOUT, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}) as c:
            r = await c.get(url)
            if r.status_code in (401, 403, 429, 503):
                blocked = True                         # looks like a bot wall: worth a real browser
            elif r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
                return None
            else:
                return _decode(r)
    except Exception as e:
        log.info("fetch failed %s: %s", url, str(e)[:80])
        return None
    return await _fetch_stealth(url) if blocked else None


# Optional stealth browser (tools/camofox-browser, a Firefox build that passes bot checks). Used only after a plain fetch is
# refused, and only if its server is up; it does not open real paywalls. CAMOFOX_URL="" turns it off.
CAMOFOX = os.environ.get("CAMOFOX_URL", "http://127.0.0.1:9377").rstrip("/")
_cf_sem = asyncio.Semaphore(2)                       # RAM is tight: at most two tabs
_cf_down_until = 0.0


def _free_mb() -> float:
    """Free physical RAM in MB (Windows); infinity elsewhere or on error, so the guard never blocks by mistake."""
    try:
        import ctypes

        class M(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                        ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]
        m = M()
        m.len = ctypes.sizeof(M)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.avail / 1048576
    except Exception:  # noqa: BLE001
        return float("inf")


async def stealth_eval(url: str, expression: str, key: str = "articles") -> Optional[str]:
    """Open url in the stealth browser, return the string `expression` evaluates to (None if it is off, down or failed)."""
    global _cf_down_until
    if not CAMOFOX or time.monotonic() < _cf_down_until or _free_mb() < 3500:      # a Firefox tab is heavy: never when RAM is short
        return None
    tid = None
    try:
        async with _cf_sem, httpx.AsyncClient(timeout=45) as c:
            r = await c.post(f"{CAMOFOX}/tabs", json={"userId": "contextlens", "sessionKey": key, "url": url})
            tid = r.json().get("tabId") if r.status_code < 400 else None
            if not tid:
                return None
            e = await c.post(f"{CAMOFOX}/tabs/{tid}/evaluate", json={"userId": "contextlens", "expression": expression})
            out = (e.json().get("result") or "") if e.status_code < 400 else ""
            log.info("stealth fetch %s -> %d chars", url, len(out))
            return out or None
    except httpx.ConnectError:
        _cf_down_until = time.monotonic() + 120      # not running: stop trying for two minutes
        return None
    except Exception as e:  # noqa: BLE001
        log.info("stealth fetch failed %s: %s", url, str(e)[:80])
        return None
    finally:
        if tid:
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    await c.request("DELETE", f"{CAMOFOX}/tabs/{tid}", json={"userId": "contextlens"})
            except Exception:  # noqa: BLE001
                pass


async def _fetch_stealth(url: str) -> Optional[str]:
    return await stealth_eval(url, "document.documentElement.outerHTML")


def _bs4_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe", "figure"]):
        t.decompose()
    root = soup.find("article") or soup.find("main") or soup.body
    if not root:
        return ""
    return "\n".join(p.get_text(" ", strip=True) for p in root.find_all("p") if len(p.get_text(strip=True)) > 60)


def extract_text(html: str) -> tuple[str, bool]:
    text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True) or ""
    if len(text) < 400:
        text = _bs4_text(html) or text
    paywall = any(s in html.lower() for s in PAYWALL) and len(text) < 1500
    return text.strip(), paywall


async def fetch_full_text(article: dict) -> dict:
    """Fetch+extract one article, cache in DB. Returns {'text','paywall'}."""
    if article.get("full_text"):
        return {"text": article["full_text"], "paywall": bool(article.get("is_paywall"))}
    html = await _fetch(article["url"])
    if not html:
        return {"text": "", "paywall": False}
    loop = asyncio.get_running_loop()
    text, paywall = await loop.run_in_executor(None, extract_text, html)
    text = re.sub(r"\n{3,}", "\n\n", text)[:20000]
    if len(text) < 300:
        return {"text": "", "paywall": paywall}
    await db.execute("UPDATE articles SET full_text=?, is_paywall=? WHERE id=?",
                     (text, int(paywall), article["id"]))
    return {"text": text, "paywall": paywall}


async def fetch_many(articles: list[dict]) -> list[dict]:
    res = await asyncio.gather(*[fetch_full_text(a) for a in articles], return_exceptions=True)
    return [r if isinstance(r, dict) else {"text": "", "paywall": False} for r in res]
