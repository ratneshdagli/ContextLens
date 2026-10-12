"""ContextLens v3 — grounded timeline.

v2 asked an LLM to "extract chronological events" from raw text and got invented dates.
Now:
  1. per source article, a schema-constrained call extracts ONLY events that are explicitly dated in the text,
     each with the verbatim sentence (`quote`) that contains the date expression;
  2. the quote must really occur in the article and contain the date expression (else dropped);
  3. dates are resolved with `dateparser` relative to the article's publish date (else dropped);
  4. events from all sources are merged, sorted, and every entry keeps its source link;
  5. a deterministic gate decides if a timeline exists at all: >=3 distinct events on >=2 distinct
     dates spanning >=1 day. Otherwise the module is absent (not empty).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import dateparser
from dateparser.date import DateDataParser

from backend.services import llm
from backend.services.llm import LLMError
from backend.services.verify import _content

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array", "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "event": {"type": "string"},
                    "date_text": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["event", "date_text", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}

SYSTEM = """You extract dated events from a news article for a timeline. Rules:
- Only include events (things that happened or were decided) whose date or time is EXPLICITLY written in the text, e.g. "on March 3", "in 2021", "last Tuesday", "on 12 September".
- date_text: the exact date expression as written in the article.
- quote: the exact sentence copied verbatim from the article that contains the date expression.
- event: one plain sentence (max 25 words) saying what happened. Do not add facts that are not in the quote.
- Do NOT include the article's own publication, scheduled or planned or expected future events, or undated background. Only things that already happened.
- If there are no explicitly dated events, return an empty list."""


_FUTURE_RE = re.compile(r"\b(is|are|was|were)?\s*(scheduled|expected|set|due|slated|planned|poised|likely)\s+(to|for)\b|"
                        r"\b(will|would|plans? to|aims? to|upcoming|to be held|later this)\b", re.I)


def is_future_or_plan(text: str) -> bool:
    """Timelines record what HAPPENED; scheduled/planned/expected items are not history."""
    return bool(_FUTURE_RE.search(text))


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in_text(quote: str, text: str) -> bool:
    q, t = _norm(quote), _norm(text)
    if len(q) < 20:
        return False
    if q in t:
        return True
    # tolerate small edits: 85% of the quote's word 4-grams must occur in the text
    words = q.split()
    if len(words) < 6:
        return False
    grams = [" ".join(words[i:i + 4]) for i in range(len(words) - 3)]
    return sum(g in t for g in grams) / len(grams) >= 0.85


def resolve_date(date_text: str, published: Optional[datetime]) -> Optional[tuple[date, str]]:
    base = (published or datetime.now(timezone.utc)).replace(tzinfo=None)
    settings = {"RELATIVE_BASE": base, "PREFER_DATES_FROM": "current_period", "PREFER_DAY_OF_MONTH": "first",
                "RETURN_AS_TIMEZONE_AWARE": False, "STRICT_PARSING": False}
    try:
        data = DateDataParser(languages=["en"], settings=settings).get_date_data(date_text.strip())
    except Exception:
        return None
    if not data or not data.date_obj:
        return None
    d = data.date_obj.date()
    period = getattr(data, "period", "day") or "day"
    if d > (base + timedelta(days=1)).date():
        return None                                    # future-dated → plan/forecast, not history (never guess another year)
    if d < date(1900, 1, 1):
        return None
    return d, period if period in ("day", "month", "year") else "day"


def _parse_pub(s: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s) if s else None
    except Exception:
        return None


def relevant_to(event_text: str, quote: str, story_text: str) -> bool:
    """Event must belong to THIS story: share a specific anchor with the headline/summary and some content words."""
    from backend.services.clustering import anchors
    shared = (anchors(event_text) | anchors(quote)) & anchors(story_text)
    ew, sw = set(_content(event_text)), set(_content(story_text))
    overlap = len(ew & sw) / len(ew) if ew else 0.0
    return len(shared) >= 2 or (len(shared) >= 1 and overlap >= 0.2)


async def extract_from_source(src: dict, story_text: str = "") -> list[dict]:
    text = (src.get("text") or "")[:5500]
    if len(text) < 500:
        return []
    user = f"ARTICLE (published {src.get('published_at','unknown')}, from {src['outlet']}):\n{text}"
    try:
        out = await llm.chat_json("writer", SYSTEM, user, EVENT_SCHEMA, schema_name="timeline_events", max_tokens=700, retries=0)
    except LLMError:
        return []
    pub = _parse_pub(src.get("published_at"))
    events = []
    for ev in out.get("events", []):
        quote, dtext, etext = ev["quote"].strip(), ev["date_text"].strip(), ev["event"].strip()
        if llm.looks_like_garbage(etext) or len(etext) < 12 or is_future_or_plan(etext):
            continue
        if len(set(_content(etext)) - set(_content(dtext))) < 4:
            continue                                   # too thin to be a milestone ("The incident occurred on Sept 18")
        if not quote_in_text(quote, src["text"]):
            continue                                   # not really in the article → invented
        if _norm(dtext) not in _norm(quote):
            continue                                   # date expression not in the quote
        if story_text and not relevant_to(etext, quote, story_text):
            continue                                   # dated, real, but about something else
        res = resolve_date(dtext, pub)
        if not res:
            continue
        d, precision = res
        events.append({"date": d.isoformat(), "precision": precision, "text": etext, "quote": quote[:260],
                       "source_url": src["url"], "source_outlet": src["outlet"]})
    return events


def merge_events(events: list[dict]) -> list[dict]:
    """Drop near-duplicate events (same date + similar wording); prefer more precise / earlier source."""
    out: list[dict] = []
    for ev in sorted(events, key=lambda e: (e["date"], {"day": 0, "month": 1, "year": 2}[e["precision"]])):
        cw = set(_content(ev["text"]))
        dup = False
        for kept in out:
            if kept["date"] != ev["date"]:
                continue
            kw = set(_content(kept["text"]))
            if cw and kw and len(cw & kw) / min(len(cw), len(kw)) >= 0.5:
                dup = True
                break
        if not dup:
            out.append(ev)
    return out


def gate(events: list[dict], fast_breaking: bool = False) -> tuple[bool, str]:
    """Deterministic: does this story deserve a timeline?"""
    if len(events) < 3:
        return False, f"only {len(events)} grounded dated event(s) (need ≥3)"
    dates = sorted({e["date"] for e in events})
    if len(dates) < 2:
        return False, "all events on a single date"
    span = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days
    if span < 1 and not fast_breaking:
        return False, "events span less than a day"
    return True, f"{len(events)} grounded events across {len(dates)} dates ({span} days)"


_DATE_SIGNAL = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2}\b|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|"
    r"\b(?:in|since|until|by)\s+(?:19|20)\d{2}\b|\blast\s+(?:week|month|year|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b|\bon\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)

def date_signals(text: str) -> int:
    """How many explicit date expressions an article contains (a cheap test for 'this narrates events')."""
    return len({m.group(0).lower() for m in _DATE_SIGNAL.finditer(text)})


async def build_timeline(sources: list[dict], story_text: str = "", max_sources: int = 2, apply_gate: bool = True) -> dict:
    """sources: [{url,outlet,published_at,text}] (full text only). Reads the sources with the most explicit dates first.
    Returns {events, shown, reason}; with apply_gate=False the ungated events are returned (caller merges + gates)."""
    raw: list[dict] = []
    ranked = sorted((s for s in sources if date_signals(s["text"]) >= 3), key=lambda x: -date_signals(x["text"]))
    for s in ranked[:max_sources]:                                    # sequential: one LLM at a time
        raw.extend(await extract_from_source(s, story_text))
        if len(merge_events(raw)) >= 6:
            break
    events = merge_events(raw)
    if not apply_gate:
        return {"events": events, "shown": bool(events), "reason": "ungated", "raw_count": len(raw)}
    ok, reason = gate(events)
    return {"events": events if ok else [], "shown": ok, "reason": reason, "raw_count": len(raw)}
