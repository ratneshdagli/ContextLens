"""ContextLens v5 — live voice: a fast *talker* plus a parallel *reasoner*.

Talker  answers immediately from what the app already knows (explained stories, timelines, the knowledge base). The
        common turns — "give me the highlights", "why did it happen", "how did we get here", "what next", "next" —
        need no language model at all: they are read out of the stored explanations, so speech starts at once.
        Free questions use fast retrieval over the story centroids and one streamed LLM answer; every sentence is
        grounding-checked *before* it is emitted, so the assistant does not bluff out loud.
Reasoner after the talker has answered, a background step searches the open web, reads the best sources and — only
        if it finds something new that the sources support — adds "I dug a little deeper…". A new user turn cancels it.

The server is stateless: the client sends the recent history and the current focus (story being discussed + the queue
of stories being read). Everything is yielded as small events for the client to speak/show in order.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

import numpy as np

from backend import db
from backend.services import explainer, extract, feeds, kb, llm, lms, pipeline
from backend.services.llm import LLMError
from backend.services.research import HARD_PAYWALL, _searx
from backend.services.verify import _content, check_claim, split_sentences

log = logging.getLogger("voice")

MIN_SIM = 0.62          # story-centroid cosine needed before the talker answers from the knowledge base
TOP_K = 3
CONTEXT_CHARS = 5200

# ── understanding what was asked (rules, not a model: instant and predictable) ─────────────────────────────────
_RX = {
    "stop": re.compile(r"^\W*(stop|pause|be quiet|quiet|shut up|enough|that'?s enough|cancel|hold on)\W*$", re.I),
    "hello": re.compile(r"^\W*(hi|hello|hey|hey there|hi there|good (morning|afternoon|evening)|namaste|yo)\W*$", re.I),
    "brief": re.compile(r"\b(highlights?|headlines?|top (news|stories)|what'?s (happening|going on|new|up)|brief me|catch me up|"
                        r"news (for )?today|today'?s news|give me the news|any news|latest news|what did i miss|read (me )?the news)\b", re.I),
    "next": re.compile(r"^\W*(next( one| story)?|skip( it| this| that)?( one| story)?|another( one| story)?|go on|continue|keep going|"
                       r"what else|move on|more news|next please)\W*$", re.I),
    "deeper": re.compile(r"\b(tell me more|more (detail|details)|more about (this|that|it)|go deeper|dig (deeper|in|into (this|that|it))|"
                         r"elaborate|explain (this|that|it|more|further)|deep dive|explain (this|that|it) (to me )?(simply|properly))\b", re.I),
    "history": re.compile(r"\b(timeline|how did (it|this|that) (unfold|develop|progress)|what happened (before|earlier)|"
                          r"how did we get here|what led (up )?to (this|that|it)|the story so far|previously)\b", re.I),
    "why": re.compile(r"^\W*(why|what caused|how did (this|it|that) (happen|start|come)|what'?s the (background|context|reason))\b|"
                      r"\bwhy (did|is|was|are|were|do|does|has|have)\b", re.I),
    "future": re.compile(r"\b(what happens next|what'?s next|what to expect|what will happen|what comes next|what should i watch)\b", re.I),
    "who": re.compile(r"^\W*who (is|are|was|were)\b", re.I),
}
# "what happened with X", "tell me about X", "any update on X": when X is a story we have already explained, the answer
# is stored — read it out instead of paying for a model call.
_TELL = re.compile(r"^\W*(so,? )?(what('?s| is| was| happened| has happened)( (going on|the (latest|story|news|update)))?( with| on| about| in)|"
                   r"tell me (more )?about|any (news|updates?) (on|about|regarding)|update me on|catch me up on|explain)\b", re.I)
FAST_SIM = 0.72
_FOLLOWUP = re.compile(r"\b(this|that|it|they|them|he|she|his|her|these|those|the (story|report|news))\b", re.I)
_WEB_HINT = re.compile(r"\b(latest|right now|just now|breaking|live|search|look (it )?up|google|on the web|update|today)\b", re.I)


def classify(text: str, has_focus: bool = False) -> str:
    """One of: stop hello brief next deeper history why future who question."""
    t = (text or "").strip()
    for name in ("stop", "hello", "next", "deeper", "history", "future", "brief"):
        if _RX[name].search(t):
            return name
    if has_focus and _RX["why"].search(t) and len(t.split()) <= 12:
        return "why"
    if _RX["who"].search(t):
        return "who"
    return "question"


# ── speech-friendly text ───────────────────────────────────────────────────────────────────────────────────────
_URL = re.compile(r"https?://\S+")
_REFS = re.compile(r"\[(?:S|F|H)?\d+(?:[,\s]+(?:S|F|H)?\d+)*\]|\((?:S|F|H)\d+\)")
_MD = re.compile(r"[*_`#>]+")
_RUPEE = re.compile(r"(?:₹|\bRs\.?)\s?(\d[\d,.]*\d|\d)(\s*(?:crore|lakh|thousand|million|billion|trillion))?", re.I)
_NO_SPLIT = {"mr", "mrs", "ms", "dr", "prof", "rs", "no", "st", "vs", "u.s", "u.k", "e.g", "i.e", "p.m", "a.m", "inc", "co", "ltd",
             "jr", "sr", "gen", "sen", "rep", "hon", "sh", "smt", "cr", "lt", "col", "capt", "mt", "dept", "govt", "approx"}


def clean_for_speech(text: str) -> str:
    t = _URL.sub("", text or "")
    t = _REFS.sub("", t)
    t = _MD.sub("", t)
    t = _RUPEE.sub(lambda m: f"{m.group(1)}{m.group(2) or ''} rupees", t)          # "₹1,200 crore" -> "1,200 crore rupees"
    t = t.replace("&", " and ").replace("—", ", ").replace("–", ", ").replace("…", "...")
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def split_ready(buf: str) -> tuple[list[str], str]:
    """Cut every complete sentence off the front of a growing text buffer; return (sentences, remainder)."""
    out, start = [], 0
    for m in re.finditer(r"[.!?]+[\"'”’)]*\s+(?=[A-Z0-9\"'“‘(])", buf):
        chunk = buf[start:m.end()].strip()
        last = re.findall(r"([A-Za-z.]+)[.!?]+[\"'”’)]*\s*$", chunk)
        if last and last[-1].rstrip(".").lower() in _NO_SPLIT:
            continue                                        # "Dr. Rao", "Rs. 500", "U.S. officials" are not sentence ends
        if chunk:
            out.append(chunk)
        start = m.end()
    return out, buf[start:]


def speakable(text: str, max_len: int = 240) -> list[str]:
    """A block of text as sentences a TTS engine handles well (long ones are cut at a comma or 'and')."""
    sents, rest = split_ready(clean_for_speech(text) + " X")
    sents = [s for s in sents if s]
    tail = rest[:-1].strip() if rest.endswith("X") else rest.strip()
    if tail:
        sents.append(tail)
    out = []
    for s in sents:
        while len(s) > max_len:
            cut = max(s.rfind(", ", 0, max_len), s.rfind(" and ", 0, max_len), s.rfind("; ", 0, max_len))
            if cut < 60:
                cut = s.rfind(" ", 0, max_len)
            out.append(s[:cut + 1].strip().rstrip(",;") + ".")
            s = s[cut + 1:].strip()
        if s:
            out.append(s)
    return out


def shorten(text: str, words: int) -> str:
    """The first sentences of `text` that fit into `words` words."""
    out, n = [], 0
    for s in split_sentences(text or ""):
        w = len(s.split())
        if out and n + w > words:
            break
        out.append(s)
        n += w
    return " ".join(out)


def _say(text: str) -> list[dict]:
    return [{"t": "say", "c": s} for s in speakable(text)]


def _date_words(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso[:10])
        return f"{d.day} {d.strftime('%B')}"
    except Exception:  # noqa: BLE001
        return ""


# ── reading what the app already knows ─────────────────────────────────────────────────────────────────────────

async def load_story(story_id: str) -> Optional[dict]:
    row = await db.fetch_one(
        "SELECT id,headline,dek,what_happened,topic,tier,built_at,outlet_count,extras,cluster_id,image_url FROM stories WHERE id=?", (story_id,))
    if not row:
        return None
    extras = db.jload(row["extras"], {}) or {}
    events = await db.fetch_all("SELECT date,text FROM timeline_events WHERE story_id=? ORDER BY date", (story_id,))
    outlets = []
    if row["cluster_id"]:
        outlets = [r["outlet"] for r in await db.fetch_all(
            "SELECT outlet FROM articles WHERE cluster_id=? GROUP BY outlet ORDER BY MIN(trust_rank), COUNT(*) DESC LIMIT 6", (row["cluster_id"],))]
    merged = []
    if row["cluster_id"]:
        crow = await db.fetch_one("SELECT merged FROM clusters WHERE id=?", (row["cluster_id"],))
        merged = [m["title"] for m in (db.jload(crow["merged"], []) if crow else []) if m.get("title")][:3]
    return {**row, "explain": extras.get("explain"), "events": events, "outlets": outlets, "developments": merged,
            "built": row["tier"] in ("primary", "more") and bool(row["built_at"]) and bool(extras.get("explain"))}


def card_event(s: dict) -> dict:
    ex = s.get("explain") or {}
    return {"t": "card", "story": {"id": s["id"], "headline": s["headline"], "topic": s["topic"], "built": s["built"],
                                   "outlet_count": s["outlet_count"], "gist": ex.get("gist") or shorten(s.get("dek") or "", 30),
                                   "image_url": s.get("image_url") or ""}}


def _list_outlets(names: list[str]) -> str:
    names = names[:4]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def brief_line(s: dict, words: int = 46) -> str:
    ex = s.get("explain") or {}
    body = ex.get("gist") or shorten(s.get("dek") or s.get("what_happened") or "", words)
    head = s["headline"].strip()
    return f"{head}{'' if head[-1:] in '.?!' else '.'} {shorten(body, words)}"


def why_lines(s: dict) -> list[str]:
    ex = s.get("explain") or {}
    steps = [w for w in ex.get("why") or [] if isinstance(w, dict) and w.get("cause") and w.get("link")]
    if not steps:
        return []
    out = [f"Here's why it happened. {ex.get('gist', '')}".strip()]
    ords = ["First", "Second", "Third", "Fourth", "And finally"]
    for i, w in enumerate(steps[:5]):
        out.append(f"{ords[min(i, len(ords) - 1)]}, {w['cause'].rstrip('.')}. {w['link']}")
    if ex.get("so_what"):
        out.append(f"What it means for people: {shorten(ex['so_what'], 40)}")
    return out


def history_lines(s: dict, earlier: list[str]) -> list[str]:
    ev = [e for e in s["events"] if e.get("text")][:5]
    out = []
    if earlier:
        out.append(f"This connects to earlier coverage. {earlier[0]}.")
    if ev:
        out.append("Here's how it unfolded.")
        out += [f"{(_date_words(e['date']) + ': ') if _date_words(e['date']) else ''}{shorten(e['text'], 32)}" for e in ev]
    return out


# ── the turn ────────────────────────────────────────────────────────────────────────────────────────────────────

async def _top_stories(exclude: set[str], n: int) -> list[dict]:
    from backend.routers.feed import get_feed          # same ordering as the front page (relevance × recency, built first)
    data = await get_feed(limit=80)
    cards = ([data["lead"]] if data.get("lead") else []) + data["stories"]
    picks = [c for c in cards if c["id"] not in exclude][:n]
    out = []
    for c in picks:
        s = await load_story(c["id"])
        if s:
            out.append(s)
    return out


async def _brief(exclude: list[str], n: int = 5) -> AsyncIterator[dict]:
    stories = await _top_stories(set(exclude), n)
    if not stories:
        for e in _say("I don't have any stories yet. Refresh the news from the Today page and I'll be ready in a minute."):
            yield e
        return
    first = not exclude
    for e in _say("Here are today's top stories." if first else "Here are the next stories."):
        yield e
    queue = list(exclude)
    for i, s in enumerate(stories, len(exclude) + 1):
        queue.append(s["id"])
        yield card_event(s)
        yield {"t": "focus", "story_id": s["id"], "queue": queue, "pos": len(queue) - 1, "headline": s["headline"]}
        for e in _say(f"Number {i}. {brief_line(s)}"):
            yield e
    for e in _say("Say tell me more about any of them, next to keep going, or just ask me anything."):
        yield e


async def _wait_for_build(story_id: str, patience_s: int = 900) -> AsyncIterator[dict]:
    """Start (or join) the full explanation of a story and yield keep-alives until it is written."""
    if story_id not in pipeline._building:
        asyncio.create_task(pipeline.build_now(story_id))          # detached: it must finish even if this turn is cancelled
    waited = 0
    while waited < patience_s:
        await asyncio.sleep(4)
        waited += 4
        if story_id not in pipeline._building:
            return
        run = pipeline._build_runs.get(story_id)
        yield {"t": "status", "c": "Reading the coverage and working out why it happened…", "phase": "build", "run": run}


async def _explain_focus(kind: str, story_id: str, web: bool) -> AsyncIterator[dict]:
    s = await load_story(story_id)
    if not s:
        for e in _say("I lost track of which story we were on. Ask me about it again."):
            yield e
        return
    yield card_event(s)
    if not s["built"] and kind in ("deeper", "why", "history", "future"):
        talk = [f"I've only skimmed this one so far, so let me do it properly."]
        if s["outlets"]:
            talk.append(f"Meanwhile, {s['outlet_count']} outlets are reporting it, including {_list_outlets(s['outlets'])}.")
        elif s["outlet_count"] > 1:
            talk.append(f"Meanwhile, {s['outlet_count']} outlets are reporting it.")
        if s.get("dek"):
            talk.append(shorten(s["dek"], 40))
        for e in _say(" ".join(talk)):
            yield e
        yield {"t": "turn_done"}
        async for e in _wait_for_build(story_id):
            yield e
        s = await load_story(story_id)
        if not s or not s["built"]:
            for e in _say("I couldn't finish explaining that one. You can open it on the Today page and try again."):
                yield e
            return
        yield card_event(s)
        yield {"t": "turn_open"}                                   # the deferred answer starts: the client should stop listening

    ex = s.get("explain") or {}
    if kind in ("deeper", "why"):
        lines = why_lines(s) or [brief_line(s)]
        for e in _say(" ".join(lines)):
            yield e
        if s["developments"]:
            for e in _say(f"There are also newer angles: {shorten('. '.join(s['developments']), 40)}"):
                yield e
    elif kind == "history":
        th = await kb.thread_history(story_id)
        lines = history_lines(s, [x["headline"] for x in (th or {}).get("earlier", [])[-2:]])
        for e in _say(" ".join(lines) if lines else "I don't have a dated timeline for this one, only today's coverage."):
            yield e
    elif kind == "future":
        nxt = [x for x in ex.get("next") or []][:3]
        for e in _say(" ".join(["Here's what the reporting says is coming."] + nxt) if nxt else
                      "The coverage doesn't mention anything scheduled yet. I'll keep an eye on it."):
            yield e


STRONG_SIM = 0.80   # embedding similarity alone is trusted only above this bar (see the content-word gate below)


async def _retrieve(q: str, focus_id: Optional[str]) -> tuple[list[dict], float]:
    """Top stories for a question: cosine to the story centroids (already stored) + a small headline-word boost.

    A pure embedding match can be fooled by two stories that share only generic vocabulary ("FIR", "case",
    "actor", "probe") — e.g. a question about a comedian's FIR once matched an unrelated actor's-death story at
    0.6+ cosine, and the talker spoke it as fact. So unless a candidate shares at least one real content word
    with the query, it is only trusted when the raw cosine is very high (STRONG_SIM); otherwise it is dropped and
    the caller falls back to "I don't have that" + a live web search, which is exactly what should happen for a
    story outside this app's own aggregation window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    rows = await db.fetch_all(
        "SELECT id,headline,dek,centroid FROM stories WHERE kind IN ('feed','paper','link') AND tier IN ('primary','more','wire') "
        "AND centroid IS NOT NULL AND COALESCE(published_at,created_at) >= ?", (cutoff,))
    if not rows:
        return [], 0.0
    qv = (await llm.embed([q], kind="query"))[0]
    words = set(_content(q))
    scored = []
    for r in rows:
        v = llm.from_blob(r["centroid"])
        if v is None or v.shape != qv.shape:
            continue
        cos = float(v @ qv)
        shared = words & set(_content(r["headline"] + " " + (r["dek"] or "")))
        if r["id"] != focus_id and len(words) >= 2 and not shared and cos < STRONG_SIM:
            continue                                                 # no shared topic word and not an overwhelming match: distrust it
        boost = min(0.09, 0.03 * len(shared))
        scored.append((cos + boost + (0.05 if r["id"] == focus_id else 0.0), r["id"]))
    scored.sort(reverse=True)
    picked = [sid for s, sid in scored[:TOP_K] if s >= MIN_SIM]
    if focus_id and focus_id not in picked and (len(q.split()) <= 8 and _FOLLOWUP.search(q)):
        picked.insert(0, focus_id)                                  # "why did he say that?" is about the story we are on
        picked = picked[:TOP_K]
    stories = [s for s in [await load_story(i) for i in picked] if s]
    return stories, (scored[0][0] if scored else 0.0)


def context_for(stories: list[dict]) -> str:
    blocks = []
    for i, s in enumerate(stories, 1):
        ex = s.get("explain") or {}
        lines = [f"STORY {i}: {s['headline']}", s.get("dek") or ""]
        if ex.get("gist"):
            lines.append(f"Summary: {ex['gist']}")
        elif s.get("what_happened"):
            lines.append(f"What happened: {shorten(s['what_happened'], 90)}")
        for w in (ex.get("why") or [])[:5]:
            if isinstance(w, dict):
                lines.append(f"Because: {w.get('cause', '')}. {w.get('link', '')}")
        if ex.get("so_what"):
            lines.append(f"Who is affected: {ex['so_what']}")
        for e in s["events"][:4]:
            lines.append(f"On {_date_words(e['date'])}: {e['text']}")
        for d in s["developments"]:
            lines.append(f"Also reported: {d}")
        if s["outlets"]:
            lines.append(f"Reported by: {_list_outlets(s['outlets'])}")
        blocks.append("\n".join(x for x in lines if x))
    return "\n\n".join(blocks)[:CONTEXT_CHARS]


VOICE_SYSTEM = """You are Lens, the voice of ContextLens. You are TALKING to a reader, out loud, like a knowledgeable friend explaining the news.
Answer using ONLY the CONTEXT. Speak in short, plain sentences: 2 to 5 sentences, at most 90 words. Say what happened, then why.
No lists, no markdown, no brackets, no story numbers. If the CONTEXT only partly answers, say what you know in one sentence and say you will look it up.
Never invent numbers, names or reasons that are not in the CONTEXT.

CONTEXT:
{context}"""


async def _stream_answer(q: str, ctx: str, history: list[dict]) -> AsyncIterator[str]:
    """Grounding-checked sentences of a spoken answer (a sentence the context does not support is never spoken)."""
    msgs = [{"role": "system", "content": VOICE_SYSTEM.format(context=ctx)}]
    for h in history[-6:]:
        if h.get("text") and h.get("role") in ("user", "assistant"):
            msgs.append({"role": h["role"], "content": h["text"][:320]})
    msgs.append({"role": "user", "content": q})
    buf = ""
    async for piece in llm.stream_chat("writer", msgs, temperature=0.3, max_tokens=260):
        buf += piece
        ready, buf = split_ready(buf)
        for s in ready:
            s = clean_for_speech(s)
            if len(s) > 12 and not check_claim(s, ctx, 0.25, name_limit=1):
                yield s
    tail = clean_for_speech(buf)
    if len(tail) > 12 and not check_claim(tail, ctx, 0.25, name_limit=1):
        yield tail


DIG_SCHEMA = {"type": "object", "properties": {"sentences": {"type": "array", "maxItems": 3, "items": {
    "type": "object", "properties": {"text": {"type": "string"}, "facts": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"}}},
    "required": ["text", "facts"], "additionalProperties": False}}}, "required": ["sentences"], "additionalProperties": False}

DIG_SYSTEM = """You are the research half of a two-person news desk. Your colleague has just told the reader ALREADY_SAID in answer to the QUESTION.
Using ONLY the numbered FACTS from fresh web sources, write up to 3 short, plain sentences of NEW information that helps answer the QUESTION.
Do not repeat ALREADY_SAID. Each sentence lists the ids of the facts that support it. If the facts add nothing new, return an empty list."""


def _rank_web(results: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in results:
        dom = feeds.domain_of(r["url"])
        if not dom or dom in seen or dom in feeds.BLOCKLIST_DOMAINS:
            continue
        seen.add(dom)
        src = feeds.lookup_source(dom)
        out.append({**r, "domain": dom, "outlet": (src or {}).get("name") or dom, "rank": (src or {}).get("trust_rank", 4)})
    out.sort(key=lambda r: (r["rank"], r["domain"] in HARD_PAYWALL))
    return out


async def _dig(q: str, headline: str, said: list[str]) -> AsyncIterator[dict]:
    """The reasoner: search the open web, read the best sources, add only what is new *and* supported."""
    llm.background()                                                # a new spoken question always goes first
    yield {"t": "dig", "state": "start"}
    query = q if len(q.split()) >= 4 else f"{headline} {q}".strip()
    yield {"t": "status", "c": "Searching the web for more…", "phase": "dig"}
    web = _rank_web(await _searx(query))[:5]
    if not web:
        yield {"t": "dig", "state": "empty"}
        return
    fetched = await asyncio.gather(*[extract.fetch_full_text({"url": w["url"], "id": None}) for w in web[:4]], return_exceptions=True)
    texts = []
    for w, f in zip(web, fetched):
        body = f["text"] if isinstance(f, dict) and f.get("text") else ""
        if len(body) >= 400:
            texts.append({"url": w["url"], "outlet": w["outlet"], "text": body, "title": w.get("title", "")})
        elif w.get("snippet") and len(w["snippet"]) > 120:
            texts.append({"url": w["url"], "outlet": w["outlet"], "text": w["snippet"], "title": w.get("title", "")})
    if not texts:
        yield {"t": "dig", "state": "empty"}
        return
    yield {"t": "status", "c": f"Reading {len(texts)} sources…", "phase": "dig"}
    facts = explainer.fact_sheet(texts, f"{headline} {q}", k=10)
    if not facts:
        yield {"t": "dig", "state": "empty"}
        return
    sheet = "\n".join(f"{f['id']} [{f['outlet']}]: {f['text']}" for f in facts)
    try:
        out = await llm.chat_json("writer", DIG_SYSTEM, f"QUESTION: {q}\nALREADY_SAID: {' '.join(said)[:700]}\n\nFACTS:\n{sheet}",
                                  DIG_SCHEMA, schema_name="dig", max_tokens=380, retries=0)
    except LLMError:
        yield {"t": "dig", "state": "empty"}
        return
    by_id = {f["id"]: f for f in facts}
    good, used = [], []
    said_words = [set(_content(s)) for s in said]
    for item in out.get("sentences", []):
        cited = [by_id[i] for i in item["facts"] if i in by_id]
        text = clean_for_speech(item["text"])
        if not cited or len(text) < 25 or check_claim(text, " ".join(c["text"] for c in cited), 0.3, name_limit=1):
            continue
        w = set(_content(text))
        if any(w and len(w & sw) / len(w) >= 0.7 for sw in said_words):
            continue                                                # already said
        good.append(text)
        used += cited
    if not good:
        yield {"t": "dig", "state": "empty"}
        return
    seen, srcs = set(), []
    for f in used:
        if f["url"] not in seen:
            seen.add(f["url"])
            srcs.append({"outlet": f["outlet"], "url": f["url"], "title": next((t["title"] for t in texts if t["url"] == f["url"]), "")})
    yield {"t": "sources", "items": srcs}
    for e in _say("I dug a little deeper. " + " ".join(good)):
        yield e
    yield {"t": "dig", "state": "done"}


async def _answer_question(q: str, history: list[dict], focus: dict, web: bool) -> AsyncIterator[dict]:
    yield {"t": "say", "c": random.choice(["Let me check.", "One moment.", "Good question, let me look.", "Let me see what I've got."])}
    yield {"t": "status", "c": "Looking through today's stories…", "phase": "think"}
    try:
        stories, best = await _retrieve(q, focus.get("story_id"))
    except LLMError as e:
        for x in _say("I can't reach my language model right now, so I can't look that up."):
            yield x
        yield {"t": "error", "c": str(e)[:160]}
        return
    said: list[str] = []
    headline = stories[0]["headline"] if stories else ""
    if stories and stories[0]["built"] and best >= FAST_SIM and _TELL.search(q):
        s = stories[0]                                             # already explained: read it out, no model call
        yield card_event(s)
        yield {"t": "focus", "story_id": s["id"], "queue": focus.get("queue") or [], "pos": focus.get("pos") or 0, "headline": s["headline"]}
        said = why_lines(s)[:5] or [brief_line(s)]
        for e in _say(" ".join(said)):
            yield e
        yield {"t": "turn_done"}
        if web:
            async for e in _dig(q, headline, said):
                yield e
        return
    if not lms.status().get("current"):
        yield {"t": "status", "c": "Waking up my language model…", "phase": "think"}
    elif llm.gate_busy():
        yield {"t": "status", "c": "The newsroom is mid-refresh — I'll jump in as soon as the current story is done…", "phase": "queued"}
    if stories:
        ctx = context_for(stories)
        # Cards/focus are held back until the first grounded sentence actually arrives — the retrieval can surface
        # a candidate that shares a word with the question ("FIR", "case") without truly answering it, and every
        # sentence about it then gets rejected by the grounding check below. Showing its card anyway, right next
        # to "I don't have a reliable answer", was confusing — better to show nothing until there is something real.
        pending = [card_event(s) for s in stories[:2]] + [
            {"t": "focus", "story_id": stories[0]["id"], "queue": focus.get("queue") or [], "pos": focus.get("pos") or 0,
             "headline": stories[0]["headline"]}]
        try:
            first = True
            async for sent in _stream_answer(q, ctx, history):
                if first:
                    for e in pending:
                        yield e
                    yield {"t": "status", "c": "", "phase": "answering"}   # clears the queued/thinking pill once words start
                    first = False
                said.append(sent)
                yield {"t": "say", "c": sent}
        except LLMError as e:
            yield {"t": "error", "c": str(e)[:160]}
    if not said:
        msg = ("I couldn't find that in the stories I've read." if not stories else
               "I don't have a reliable answer to that in what I've read.") + (" Let me search the web." if web else "")
        for e in _say(msg):
            yield e
    yield {"t": "turn_done"}
    if web:
        async for e in _dig(q, headline, said):
            yield e


async def turn(text: str, history: Optional[list[dict]] = None, focus: Optional[dict] = None, web: bool = True) -> AsyncIterator[dict]:
    """Everything the voice client needs for one user utterance, in speaking order."""
    llm.interactive()                     # a person is on the line right now: jump ahead of any background pipeline work
    history, focus = history or [], focus or {}
    text = re.sub(r"\s+", " ", text or "").strip()[:500]
    sid = focus.get("story_id")
    kind = classify(text, bool(sid))
    yield {"t": "intent", "kind": kind, "text": text}
    if kind == "stop":
        yield {"t": "say", "c": "Okay."}
        yield {"t": "turn_done"}
    elif kind == "hello":
        for e in _say("Hi, I'm Lens. I can walk you through today's news, explain why things happened, or look something up. Want the highlights?"):
            yield e
        yield {"t": "turn_done"}
    elif kind == "brief":
        async for e in _brief([]):
            yield e
        yield {"t": "turn_done"}
    elif kind == "next":
        queue = focus.get("queue") or []
        pos = focus.get("pos")
        if not queue or pos is None:
            async for e in _brief([]):
                yield e
        elif pos + 1 < len(queue):
            s = await load_story(queue[pos + 1])
            if s:
                yield card_event(s)
                yield {"t": "focus", "story_id": s["id"], "queue": queue, "pos": pos + 1, "headline": s["headline"]}
                for e in _say(f"Number {pos + 2}. {brief_line(s)}"):
                    yield e
        else:
            async for e in _brief(queue):
                yield e
        yield {"t": "turn_done"}
    elif kind in ("deeper", "why", "history", "future") and sid:
        async for e in _explain_focus(kind, sid, web):
            yield e
        yield {"t": "turn_done"}
        if kind == "deeper" and web:
            s = await load_story(sid)
            if s:
                async for e in _dig(f"{s['headline']} latest developments", s["headline"], []):
                    yield e
    else:
        async for e in _answer_question(text, history, focus, web):
            yield e
    yield {"t": "done"}
