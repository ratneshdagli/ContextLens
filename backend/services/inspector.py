"""ContextLens v4 — select-to-understand: what you can do with any highlighted text in a story.

  explain    plain-language explanation of the selected term / sentence / number (curated glossary first, no LLM)
  who        who or what is this (knowledge base + Wikipedia lead; role in this story from the reporting)
  why        why was this said / done / decided — only from the reporting and earlier events; says so when it isn't known
  forensics  a deterministic audit of the passage: who asserts it, hedging, unnamed sources, charged language, number
             conflicts across outlets, how many outlets corroborate it, first sighting. No LLM, so it is fast and explainable.
  history    earlier stories in the knowledge base about the same thing

The LLM actions (explain, why) are grounded in the story's own fact sheet and validated; forensics never uses a model.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import numpy as np

from backend import db
from backend.services import explainer, glossary, kb, llm
from backend.services.llm import LLMError, looks_like_garbage
from backend.services.verify import _content, _numbers, check_claim, split_sentences

log = logging.getLogger("inspector")

# ── deterministic language analysis ──────────────────────────────────────────

HEDGE_RE = re.compile(r"\b(reportedly|allegedly|alleged(?:ly)?|purportedly|apparently|appears? to|seems? to|may|might|could|"
                      r"possibly|probably|likely|unlikely|suggests?|unconfirmed|rumou?rs?|speculat\w+|believed to|said to be|"
                      r"thought to|understood to)\b", re.I)
UNNAMED_RE = re.compile(r"\b(people familiar with|persons? familiar with|sources? (?:said|say|told|close|familiar|aware)|"
                        r"a source|an? (?:unnamed |senior |top )?(?:official|insider|person|source)|officials? (?:said|say|told)|"
                        r"on condition of anonymity|who asked not to be named|unnamed)\b", re.I)
LOADED = {"slams", "blasts", "shocking", "devastating", "massive", "sweeping", "explosive", "bombshell", "crisis", "chaos",
          "brutal", "terrifying", "unprecedented", "historic", "outrage", "outrageous", "furious", "stunning", "disastrous",
          "catastrophic", "scandal", "witch-hunt", "propaganda", "regime", "radical", "extremist", "fiasco", "meltdown",
          "crackdown", "plunge", "skyrocket", "soar", "slump", "tirade", "rant", "fumes", "lashes", "eviscerates", "destroys",
          "humiliating", "shameful", "reckless", "draconian", "sinister", "alarming", "bombshell"}
ATTRIB_VERB = r"(?:said|says|told|claimed|claims|alleged|alleges|stated|states|added|warned|announced|denied|admitted|argued|insisted|noted|wrote|tweeted|posted)"
ATTRIB_RE = re.compile(rf"(?:according to (?P<a>[A-Z][\w.\-’' ]{{2,45}}?)(?:,|\.|\s(?:said|says))|"
                       rf"(?P<b>(?:[A-Z][\w.\-’']+\s?){{1,5}})\s+{ATTRIB_VERB}\b)")
QUOTE_RE = re.compile(r"[\"“”][^\"“”]{12,}[\"“”]")
NUM_RE = re.compile(r"\d[\d,]*\.?\d*\s?(?:%|per ?cent|crore|lakh|million|billion|trillion|thousand|points?|dollars?|rupees?)?", re.I)


def language_flags(text: str) -> dict:
    hedges = sorted({m.group(0).lower() for m in HEDGE_RE.finditer(text)})
    unnamed = sorted({m.group(0).lower() for m in UNNAMED_RE.finditer(text)})
    loaded = sorted({w for w in re.findall(r"[a-z\-]+", text.lower()) if w in LOADED})
    attrib = []
    for m in ATTRIB_RE.finditer(text):
        who = (m.group("a") or m.group("b") or "").strip(" ,.")
        if who and who.lower() not in ("he", "she", "they", "it", "we", "the") and who not in attrib:
            attrib.append(who)
    return {"hedges": hedges, "unnamed": unnamed, "loaded": loaded, "attributed_to": attrib[:3],
            "has_quote": bool(QUOTE_RE.search(text)), "numbers": [n.strip() for n in NUM_RE.findall(text) if re.search(r"\d", n)][:6]}


_UNIT = re.compile(r"%|per ?cent|crore|lakh|million|billion|trillion|thousand|points?|dollars?|rupees?|rs\.?|km|kg|mw|gw", re.I)


def quantities(text: str) -> set[str]:
    """Numbers that measure something (a unit follows, or >=100 and not a year). Dates, day numbers and list markers are ignored,
    so 'numbers differ' can only fire on real quantities."""
    out: set[str] = set()
    for m in re.finditer(r"(\d[\d,]*\.?\d*)\s?([A-Za-z%.]{0,9})", text):
        raw = m.group(1).replace(",", "").rstrip(".")
        try:
            val = float(raw)
        except ValueError:
            continue
        unit = bool(_UNIT.match(m.group(2) or ""))
        if unit or (val >= 100 and not 1900 <= val <= 2100):
            out.add(raw)
    return out


def _evidence_type(f: dict, text: str) -> str:
    if f["has_quote"] and f["attributed_to"]:
        return "direct quote"
    if f["unnamed"]:
        return "anonymous sourcing"
    if f["numbers"] and re.search(r"%|per ?cent|crore|lakh|million|billion|points?", text, re.I):
        return "statistic"
    if f["attributed_to"]:
        return "attributed statement"
    return "unattributed statement"


async def load_story(story_id: str) -> Optional[dict]:
    s = await db.fetch_one("SELECT id,headline,dek,what_happened,key_points,extras,cluster_id,published_at FROM stories WHERE id=?", (story_id,))
    if not s:
        return None
    srcs = await db.fetch_all(
        "SELECT ss.n,ss.url,ss.outlet,ss.domain,ss.published_at,ss.excerpt,a.full_text AS text FROM story_sources ss "
        "LEFT JOIN articles a ON a.url=ss.url WHERE ss.story_id=? ORDER BY ss.n", (story_id,))
    if not srcs and s["cluster_id"]:
        arts = await db.fetch_all("SELECT url,outlet,domain,published_at,snippet AS excerpt,full_text AS text FROM articles WHERE cluster_id=?", (s["cluster_id"],))
        srcs = [dict(a, n=i) for i, a in enumerate(arts, 1)]
    return {**s, "extras": db.jload(s["extras"], {}) or {}, "sources": srcs}


def forensic_report(text: str, story: dict, archive: list[dict]) -> dict:
    sents = [x for x in split_sentences(text) if len(x) > 20] or [text.strip()]
    flags = language_flags(text)
    sources = [x for x in story["sources"] if (x.get("text") or x.get("excerpt"))]
    main = max(sents, key=len)[:400]                 # a multi-sentence selection is matched on its main sentence
    core = re.sub(r"\baccording to [^,.;]+[,;]?", " ", main, flags=re.I)         # the attribution / hedging wrapper is not the claim
    core = HEDGE_RE.sub(" ", UNNAMED_RE.sub(" ", core))
    claim_words = set(_content(core)) or set(_content(main))
    claim_nums = quantities(main)
    support, conflicts = [], []
    for src in sources:
        body = src.get("text") or src.get("excerpt") or ""
        best = None
        for sent in split_sentences(body)[:200]:
            sw = set(_content(sent))
            if not claim_words or not sw:
                continue
            recall = len(claim_words & sw) / len(claim_words)
            if best is None or recall > best[0]:
                best = (recall, sent)
        if not best:
            continue
        recall, sent = best
        sn = quantities(sent)
        if recall >= 0.55 and (not claim_nums or claim_nums <= sn):
            support.append({"outlet": src["outlet"], "url": src["url"], "quote": sent[:300], "published_at": src.get("published_at")})
        elif recall >= 0.42 and len(claim_words & set(_content(sent)) - set(re.findall(r"\d+", " ".join(claim_words)))) >= 3                 and claim_nums and sn and not (claim_nums & sn):
            conflicts.append({"outlet": src["outlet"], "url": src["url"], "quote": sent[:300],
                              "numbers": sorted(sn)[:4]})
    seen_out = {x["outlet"] for x in support}
    also = [a for a in archive if a["outlet"] not in seen_out][:4]
    dates = sorted(x["published_at"] for x in support if x.get("published_at"))
    n_out = len({x["outlet"] for x in support}) + len({a["outlet"] for a in also})
    out_total = len({x["outlet"] for x in sources})

    notes = []                                   # (severity, text): warn | info | good
    if conflicts:
        c = conflicts[0]
        notes.append(("warn", f"Numbers differ: this passage says {', '.join(sorted(claim_nums)[:3])}, but {c['outlet']} reports {', '.join(c['numbers'][:3])}."))
    if flags["unnamed"]:
        notes.append(("warn", f"Relies on an unnamed source (“{flags['unnamed'][0]}”) — nobody can be held to it."))
    if flags["hedges"]:
        notes.append(("info", "Framed as unconfirmed, not established fact: “" + "”, “".join(flags["hedges"][:3]) + "”."))
    if flags["loaded"]:
        notes.append(("info", "Uses charged wording (" + ", ".join(flags["loaded"][:4]) + ") — the same facts could be written more neutrally."))
    if len(support) <= 1 and out_total >= 3 and not conflicts:
        notes.append(("warn", f"None of the {out_total} outlets in this story clearly states this in the same terms." if not support
                      else f"Only 1 of the {out_total} outlets in this story clearly states this."))
    if len(support) >= 2:
        notes.append(("good", f"{len({x['outlet'] for x in support})} outlets report this independently."))
    if flags["attributed_to"]:
        notes.append(("info", "Attributed to " + ", ".join(flags["attributed_to"][:2]) + " — treat it as their statement."))
    elif not flags["unnamed"] and not flags["hedges"]:
        notes.append(("info", "Stated as fact in the article, with no attribution in the passage itself."))
    order = {"warn": 0, "info": 1, "good": 2}
    notes.sort(key=lambda n: order[n[0]])
    return {"claim": text.strip()[:500], "evidence_type": _evidence_type(flags, text), "attributed_to": flags["attributed_to"],
            "hedges": flags["hedges"], "unnamed_sources": flags["unnamed"], "loaded_words": flags["loaded"],
            "numbers": flags["numbers"], "support": support[:6], "conflicts": conflicts[:3], "also_reported": also,
            "outlets_supporting": len({x["outlet"] for x in support}), "outlets_total": out_total, "corroboration": n_out,
            "first_seen": dates[0] if dates else None, "notes": [{"level": lv, "text": t} for lv, t in notes]}


async def archive_matches(text: str, exclude_urls: set[str], days: int = 7, k: int = 4) -> list[dict]:
    """Other outlets in the recent article archive that say something semantically equivalent."""
    rows = await db.fetch_all(
        "SELECT url,title,outlet,published_at,embedding FROM articles WHERE embedding IS NOT NULL AND published_at >= date('now', ?)",
        (f"-{days} day",))
    if not rows:
        return []
    try:
        q = (await llm.embed([text[:500]], kind="query"))[0]
    except LLMError:
        return []
    scored = []
    for r in rows:
        if r["url"] in exclude_urls:
            continue
        v = llm.from_blob(r["embedding"])
        if v is not None and v.shape == q.shape:
            scored.append((float(v @ q), r))
    scored.sort(key=lambda x: -x[0])
    return [{"outlet": r["outlet"], "title": r["title"], "url": r["url"], "published_at": r["published_at"], "sim": round(s, 2)}
            for s, r in scored[:k] if s >= 0.76]


# ── LLM actions ───────────────────────────────────────────────────────────────

_FACT_REF = re.compile(r"\b[FH]\d{1,2}\b|\[[A-Z][^\]]{2,40}\]")
ANSWER_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}, "known": {"type": "boolean"}},
                 "required": ["answer", "known"], "additionalProperties": False}

EXPLAIN_SYSTEM = """You explain a highlighted piece of a news story to a reader who has NO background.
- If it is a specialist term or acronym: first give a general one-sentence definition, then one sentence on what it means in this story.
- If it is a sentence or claim: say in plain words what it means and why it matters here.
- If it is a name: say who or what it is, and its role in this story.
- If it is a number: say what it measures and whether it is big or small for the context.
Use short sentences and everyday words. Max 90 words. Use the STORY FACTS for anything specific; you may use general knowledge only for definitions. known=false if you cannot explain it from the facts and general knowledge."""

WHY_SYSTEM = """A reader highlighted something in a news story and asks: WHY? Explain why this was said, done or decided, using ONLY the STORY FACTS and EARLIER EVENTS.
Plain words, max 110 words, cause first ("Because... so..."). Never guess motives. If the facts do not say why, set known=false and write: what the reporting DOES say about it, then "The reporting does not say why." """


async def _fact_block(story: dict, k: int = 12) -> tuple[str, str]:
    texts = [{"url": s["url"], "outlet": s["outlet"], "text": s["text"]} for s in story["sources"] if s.get("text")]
    story_text = f"{story['headline']}. {story['dek']} {story['what_happened']}"
    facts = explainer.fact_sheet(texts, story_text, k=k) if texts else []
    if not facts:
        facts = [{"id": f"F{i}", "outlet": s["outlet"], "text": s["excerpt"]} for i, s in enumerate(story["sources"][:6], 1) if s.get("excerpt")]
    block = "\n".join(f"{f['id']} [{f['outlet']}] {f['text']}" for f in facts)
    return block, " ".join(f["text"] for f in facts)


async def explain_selection(text: str, context: str, story: Optional[dict]) -> dict:
    hits = glossary.find_terms(text, 1) if len(text) <= 70 else []
    if hits:
        return {"answer": hits[0]["meaning"], "kind": "term", "curated": True, "known": True}
    facts, known_text = ("", "")
    head = ""
    if story:
        facts, known_text = await _fact_block(story)
        head = f"STORY: {story['headline']}\nGIST: {(story['extras'].get('explain') or {}).get('gist', story['dek'])}\n"
    user = f"{head}STORY FACTS\n{facts}\n\nSURROUNDING TEXT: {context[:700]}\n\nHIGHLIGHTED: {text[:400]}"
    allowed = _numbers(known_text + " " + context + " " + text)

    def valid(o: dict):
        a = o["answer"]
        if looks_like_garbage(a) or not (20 <= len(a) <= 800):
            return "answer must be 20-800 characters of plain text"
        if _FACT_REF.search(a):
            return "write plain prose: never mention fact ids (F1, F2) or bracketed source names"
        bad = [n for n in _numbers(a) if n not in allowed and n not in ("1", "2")]
        return f"numbers not in the story: {bad[:2]}" if bad else None
    out = await llm.chat_json("writer", EXPLAIN_SYSTEM, user, ANSWER_SCHEMA, schema_name="inspect_explain",
                              max_tokens=320, validate=valid, retries=1)
    return {"answer": out["answer"].strip(), "kind": "passage", "curated": False, "known": bool(out["known"])}


async def why_selection(text: str, context: str, story: dict) -> dict:
    facts, known_text = await _fact_block(story)
    events = await db.fetch_all("SELECT date,text FROM timeline_events WHERE story_id=? ORDER BY date", (story["id"],))
    earlier = "\n".join(f"- {e['date']}: {e['text']}" for e in events) or "(none)"
    user = (f"STORY: {story['headline']}\n\nSTORY FACTS\n{facts}\n\nEARLIER EVENTS\n{earlier}\n\n"
            f"SURROUNDING TEXT: {context[:600]}\n\nHIGHLIGHTED: {text[:400]}")
    base = known_text + " " + " ".join(e["text"] for e in events)

    def valid(o: dict):
        a = o["answer"]
        if looks_like_garbage(a) or not (20 <= len(a) <= 900):
            return "answer must be 20-900 characters of plain text"
        if _FACT_REF.search(a):
            return "write plain prose: never mention fact ids (F1, F2, H1) or bracketed source names"
        probs = check_claim(a, base + " " + context + " " + text, 0.15)
        return f"not supported by the facts: {probs}" if probs else None
    out = await llm.chat_json("writer", WHY_SYSTEM, user, ANSWER_SCHEMA, schema_name="inspect_why",
                              max_tokens=380, validate=valid, retries=1)
    return {"answer": out["answer"].strip(), "known": bool(out["known"]), "earlier_events": len(events)}


async def who_selection(text: str, story: Optional[dict]) -> dict:
    name = re.sub(r"\s+", " ", text).strip(" .,;:\"'“”")[:80]
    ent = await kb.find_entity(name)
    if not ent:                                       # try the longest capitalised phrase / last word match
        cand = re.findall(r"[A-Z][\w.\-’']+(?:\s+[A-Z][\w.\-’']+){0,3}", name)
        for c in sorted(cand, key=len, reverse=True):
            ent = await kb.find_entity(c)
            if ent:
                break
    summary, url = "", ""
    role = ""
    stories: list[dict] = []
    if ent:
        e = await kb.ensure_summary(ent["id"])
        summary, url = ((e or {}).get("summary") or ""), ((e or {}).get("wiki_url") or "")
        summary = "" if summary == "—" else summary
        page = await kb.entity_page(ent["id"])
        stories = [{"id": s["id"], "headline": s["headline"], "at": s["at"]} for s in (page or {}).get("stories", [])[:5]]
    else:
        from backend.services import context
        import httpx
        try:
            async with httpx.AsyncClient(timeout=8, headers=context.UA, follow_redirects=True) as c:
                res = await context._lookup(c, name)
        except Exception:
            res = None
        if res:
            summary, url = res["extract"], res["url"]
    if story:
        w = next((x for x in (story["extras"].get("explain") or {}).get("who", []) if x["name"].lower().split()[-1] in name.lower()), None)
        role = w["role"] if w else ""
    return {"name": ent["name"] if ent else name, "kb_id": ent["id"] if ent else None, "type": ent["type"] if ent else "unknown",
            "role_in_story": role, "summary": summary, "wiki_url": url, "stories": stories,
            "known": bool(summary or role or stories)}


async def history_selection(text: str, story_id: Optional[str]) -> dict:
    """Earlier stories about the same subject: by entity, else by embedding."""
    out: list[dict] = []
    ent = await kb.find_entity(text)
    if ent:
        page = await kb.entity_page(ent["id"])
        out = [{"id": s["id"], "headline": s["headline"], "at": s["at"], "why": f"mentions {ent['name']}"}
               for s in (page or {}).get("stories", []) if s["id"] != story_id][:6]
    if len(out) < 3:
        try:
            q = (await llm.embed([text[:400]], kind="query"))[0]
        except LLMError:
            q = None
        if q is not None:
            rows = await db.fetch_all("SELECT id,headline,centroid,COALESCE(published_at,created_at) AS at FROM stories "
                                      "WHERE centroid IS NOT NULL AND tier IN ('primary','more','wire') AND headline!=''")
            sc = []
            for r in rows:
                v = llm.from_blob(r["centroid"])
                if v is not None and v.shape == q.shape and r["id"] != story_id:
                    sc.append((float(v @ q), r))
            sc.sort(key=lambda x: -x[0])
            have = {o["id"] for o in out}
            for s, r in sc[:6]:
                if s >= 0.66 and r["id"] not in have:
                    out.append({"id": r["id"], "headline": r["headline"], "at": r["at"], "why": f"similar topic ({s:.2f})"})
    return {"stories": out[:6], "known": bool(out)}
