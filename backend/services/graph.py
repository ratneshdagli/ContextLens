"""ContextLens v3 — story map (entities + evidenced relations). Rendered, never generated.

Entities come from GLiNER (fallback: capitalised phrases). Relations come from a schema-constrained
call whose `subject`/`object` are *enums of the extracted entities*, so the model cannot invent
nodes, and each relation must carry an `evidence` sentence that really occurs in the article.
Gate: >=4 connected nodes and >=3 evidenced edges, otherwise the UI shows "Key players" instead.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from typing import Optional

from backend.services import llm, ner_client
from backend.services.llm import LLMError
from backend.services.timeline import quote_in_text
from backend.settings_store import get_settings

log = logging.getLogger("graph")

LABELS = ["person", "organization", "country", "city", "government agency", "company", "political party", "event", "law"]
TYPE_MAP = {"person": "person", "organization": "organization", "government agency": "organization",
            "company": "organization", "political party": "organization", "country": "place", "city": "place",
            "event": "event", "law": "other"}
_TITLES = re.compile(r"^(president|prime minister|pm|vice president|vice premier|secretary|foreign minister|minister|"
                     r"chancellor|senator|governor|mayor|chief minister|cm|justice|judge|mr\.?|mrs\.?|ms\.?|dr\.?|"
                     r"professor|prof\.?|general|gen\.?|king|queen|prince|sir)\s+", re.I)
NEWS_ORGS = {"reuters", "associated press", "ap", "afp", "bloomberg", "bbc", "cnn", "npr", "the guardian", "guardian",
             "new york times", "nyt", "washington post", "al jazeera", "pti", "ani", "ians", "xinhua", "sky news",
             "cbs news", "abc news", "nbc news", "fox news", "the hindu", "times of india", "ndtv", "mint"}
GENERIC_PLACES = {"us", "u.s.", "u.s", "usa", "united states", "america", "the united states", "u.k.", "uk"}


def _canon(name: str) -> str:
    n = re.sub(r"\s+", " ", name.replace("’s", "").replace("'s", "")).strip()
    for _ in range(2):
        n = _TITLES.sub("", n)
    return n.strip(" ,.")


_CAP = re.compile(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+){0,2})\b")


def entities_from_preds(texts: list[str], preds_per_text: list, headline: str = "") -> list[dict]:
    """Merge entities across texts; rank by mentions (headline mentions count double).
    `preds_per_text[i]` is the NER worker output for texts[i], or None -> regex fallback for that text."""
    counts: Counter = Counter()
    types: dict[str, str] = {}
    for t, preds in zip(texts, preds_per_text):
        if preds is not None:
            for e in preds:
                n = _canon(e["text"])
                if len(n) < 3 or n.lower() in NEWS_ORGS or n.lower() in GENERIC_PLACES:
                    continue
                counts[n] += 1
                types.setdefault(n, TYPE_MAP.get(e["label"], "other"))
        else:
            for m in _CAP.findall(t[:4500]):
                counts[m] += 1
                types.setdefault(m, "other")
    # merge surname-only mentions into full names ("Trump" -> "Donald Trump")
    names = list(counts)
    for short in [n for n in names if " " not in n]:
        fulls = [n for n in names if " " in n and n.split()[-1].lower() == short.lower()]
        if fulls:
            best = max(fulls, key=lambda n: counts[n])
            counts[best] += counts.pop(short)
    hl = headline.lower()
    ranked = sorted(counts, key=lambda n: -(counts[n] + (2 if n.lower() in hl else 0)))
    out = []
    for n in ranked:
        if counts[n] >= 2 or n.lower() in hl:
            out.append({"name": n, "type": types.get(n, "other"), "mentions": counts[n]})
    return out[:12]


async def extract_entities(texts: list[str], headline: str = "", use_model: bool = True) -> list[dict]:
    """Entities via the isolated GLiNER worker; regex fallback if the worker is off, crashed or out of memory."""
    texts = [t[:4500] for t in texts]
    preds = await ner_client.predict(texts, LABELS) if use_model and texts else None
    return entities_from_preds(texts, preds if preds is not None else [None] * len(texts), headline)


def _relation_schema(names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {"relations": {
            "type": "array", "maxItems": 8,
            "items": {"type": "object", "properties": {
                "subject": {"type": "string", "enum": names},
                "relation": {"type": "string"},
                "object": {"type": "string", "enum": names},
                "evidence": {"type": "string"},
            }, "required": ["subject", "relation", "object", "evidence"], "additionalProperties": False},
        }},
        "required": ["relations"], "additionalProperties": False,
    }


REL_SYSTEM = """You extract relationships between entities from a news article for a relationship map.
- Each relation is (subject, relation, object): subject and object MUST be picked from the allowed entity list.
- relation: a short verb phrase of 1-4 words (e.g. "sanctioned", "met with", "acquired", "criticised", "leads").
- evidence: the exact sentence copied verbatim from the article that states this relationship.
- Only include relationships explicitly stated in the article. If none are stated, return an empty list."""

_BAD_REL = re.compile(r"^(is|are|was|were|and|of|the|associated|related|mentioned)( with| to)?$", re.I)


def _entity_in_evidence(name: str, ev: str) -> bool:
    last = name.split()[-1].lower()
    return name.lower() in ev.lower() or last in ev.lower()


async def extract_relations(src: dict, entities: list[dict]) -> list[dict]:
    names = [e["name"] for e in entities]
    if len(names) < 2:
        return []
    user = (f"ALLOWED ENTITIES: {', '.join(names)}\n\nARTICLE ({src['outlet']}):\n{src['text'][:5000]}")
    try:
        out = await llm.chat_json("writer", REL_SYSTEM, user, _relation_schema(names),
                                  schema_name="relations", max_tokens=700, retries=0)
    except LLMError:
        return []
    return filter_relations(out.get("relations", []), src["text"], src["url"], names)


def filter_relations(raw: list[dict], text: str, url: str, names: list[str]) -> list[dict]:
    """Deterministic acceptance rules. The model is asked to stay inside `names`, but small models don't always
    obey the schema enum, so it is enforced here: a node we did not extract can never appear on the map."""
    allowed = {n.lower(): n for n in names}
    rels = []
    for r in raw:
        s, o = allowed.get(str(r.get("subject", "")).strip().lower()), allowed.get(str(r.get("object", "")).strip().lower())
        rel, ev = str(r.get("relation", "")).strip().lower(), str(r.get("evidence", "")).strip()
        if not s or not o or s == o:
            continue                                   # endpoint not in the extracted entity list
        if not rel or len(rel.split()) > 5 or _BAD_REL.match(rel) or llm.looks_like_garbage(rel):
            continue
        if not quote_in_text(ev, text):
            continue
        if not (_entity_in_evidence(s, ev) and _entity_in_evidence(o, ev)):
            continue                                   # evidence must actually mention both ends
        rels.append({"subject": s, "relation": rel, "object": o, "evidence": ev[:280], "source_url": url})
    return rels


def dedupe_relations(rels: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rels:
        key = (frozenset((r["subject"].lower(), r["object"].lower())), r["relation"])
        pair = frozenset((r["subject"].lower(), r["object"].lower()))
        if key in seen or (pair, "*") in seen:
            continue
        seen.add(key)
        seen.add((pair, "*"))                          # one edge per entity pair keeps the map readable
        out.append(r)
    return out


def gate(entities: list[dict], rels: list[dict]) -> tuple[bool, str]:
    connected = {r["subject"] for r in rels} | {r["object"] for r in rels}
    if len(connected) < 4:
        return False, f"only {len(connected)} connected entities (need ≥4)"
    if len(rels) < 3:
        return False, f"only {len(rels)} evidenced relations (need ≥3)"
    return True, f"{len(connected)} entities, {len(rels)} evidenced relations"


async def build_graph(sources: list[dict], headline: str, relations: bool = True) -> dict:
    """sources: [{url,outlet,text}] full text only. Entities are always extracted (cheap); the LLM relation
    pass runs only when `relations` is true."""
    use_ner = (await get_settings()).get("ner_enabled", True)
    entities = await extract_entities([s["text"] for s in sources], headline, use_ner)
    if not relations:
        return {"entities": entities, "relations": [], "shown": False, "reason": "not enough coverage for a relationship map"}
    rels: list[dict] = []
    for s in sorted(sources, key=lambda x: -len(x["text"]))[:2]:
        rels.extend(await extract_relations(s, entities))
        if len(dedupe_relations(rels)) >= 5:
            break
    rels = dedupe_relations(rels)
    ok, reason = gate(entities, rels)
    return {"entities": entities, "relations": rels if ok else [], "shown": ok, "reason": reason}
