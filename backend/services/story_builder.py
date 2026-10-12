"""ContextLens v3 — story card builder (writer model, grounded + validated).

Input: one cluster (same event across outlets). Output: an edited headline, dek, plain-language
summary, key points and "why it matters", all constrained to the fetched source text, plus the
attributed source list. Original outlet headlines are always preserved and shown in Coverage.
"""

from __future__ import annotations

import re
from typing import Optional

import numpy as np

from backend import db
from backend.config import DEPTH_ARTICLES
from backend.services import extract, llm
from backend.services.editor import validate_headline
from backend.services.llm import LLMError, looks_like_garbage
from backend.services.verify import ground, provenance, split_sentences

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "dek": {"type": "string"},
        "what_happened": {"type": "string"},
        "key_points": {"type": "array", "minItems": 3, "maxItems": 5, "items": {"type": "string"}},
        "why_it_matters": {"type": "string"},
        "tags": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
    },
    "required": ["headline", "dek", "what_happened", "key_points", "why_it_matters", "tags"],
    "additionalProperties": False,
}

LEVEL_NOTE = {
    "simple": "Write in very simple language (short sentences, no jargon, explain any technical term).",
    "general": "Write for an intelligent general reader; explain jargon briefly.",
    "expert": "Write for a well-informed reader; be dense and precise, no hand-holding.",
}

SYSTEM = """You are a senior wire-service editor writing a story card from several outlets' reporting.
HARD RULES
1. Use ONLY facts stated in the SOURCES. Never invent or infer names, numbers, dates, causes or quotes.
2. The headline must state concretely WHO did WHAT (max 16 words), understandable by a stranger with no background. It must not be a vague label ("Iran Tensions Rise") and not clickbait. Do not copy an outlet's headline word for word if a clearer sentence is possible.
3. dek: ONE sentence (max 30 words) with the most important extra detail, not repeating the headline.
4. what_happened: 2-4 plain sentences in past/present tense, chronological where relevant.
5. key_points: 3-5 specific facts (numbers, names, decisions), each max 25 words, each directly in the sources.
6. why_it_matters: 1-2 sentences on the significance, only what the sources support; no speculation.
7. tags: 2-4 short topical tags (e.g. "Semiconductors", "RBI policy").
GOOD headline: "Iran says the US is preparing to resume strikes as Gulf states brace for escalation"
BAD headline: "Middle East tensions escalate" / "You won't believe what Iran said"
"""


def _member_sim_filter(members: list[dict], lead: dict, floor: float = 0.78) -> list[dict]:
    lv = llm.from_blob(lead.get("embedding"))
    if lv is None:
        return members
    out = []
    for m in members:
        v = llm.from_blob(m.get("embedding"))
        if m["id"] == lead["id"] or v is None or v.shape != lv.shape or float(v @ lv) >= floor:
            out.append(m)
    return out


async def load_cluster_members(cluster_id: str) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT id,url,title,snippet,outlet,domain,trust_rank,published_at,image_url,embedding,full_text,is_paywall,topic "
        "FROM articles WHERE cluster_id=? ORDER BY trust_rank ASC, length(snippet) DESC", (cluster_id,))
    return rows


async def gather(cluster_id: str, depth: int) -> dict:
    members = await load_cluster_members(cluster_id)
    if not members:
        raise LLMError("cluster has no articles")
    lead = members[0]
    focus = _member_sim_filter(members, lead)
    # fetch full text: best-trust, one per domain, up to the depth budget
    want, doms = [], set()
    for m in focus:
        if m["domain"] not in doms and m["trust_rank"] <= 3:
            want.append(m)
            doms.add(m["domain"])
    want = want[: DEPTH_ARTICLES.get(depth, 3) + 2]
    fetched = await extract.fetch_many(want)
    texts: list[dict] = []
    for m, f in zip(want, fetched):
        m["_text"] = f["text"]
        m["_paywall"] = f["paywall"]
        if f["text"] and not f["paywall"]:
            texts.append({"url": m["url"], "outlet": m["outlet"], "published_at": m["published_at"],
                          "title": m["title"], "text": f["text"], "id": m["id"]})
    if len(texts) < 2:                              # paywalled / unreadable: try other outlets before settling for snippets
        tried = {m["id"] for m in want}
        more = [m for m in focus if m["id"] not in tried and m["domain"] not in doms][:4]
        for m, f in zip(more, await extract.fetch_many(more)):
            if f["text"] and not f["paywall"]:
                texts.append({"url": m["url"], "outlet": m["outlet"], "published_at": m["published_at"],
                              "title": m["title"], "text": f["text"], "id": m["id"]})
    texts = texts[: DEPTH_ARTICLES.get(depth, 3)]
    return {"members": focus, "all_members": members, "texts": texts, "lead": lead}


def build_corpus(texts: list[dict], members: list[dict], budget: int = 6500) -> tuple[str, str]:
    """Numbered source block for the prompt + plain text used for grounding."""
    parts, plain = [], []
    if texts:
        per = max(1200, budget // len(texts))
        for i, t in enumerate(texts, 1):
            body = t["text"][:per]
            parts.append(f"[{i}] {t['outlet']} — {t['title']}\n{body}")
            plain.append(t["title"] + " " + t["text"])
    have = {t["url"] for t in texts}
    if sum(len(t["text"]) for t in texts) < 1500:   # thin or paywalled: add the other outlets' RSS blurbs (real source text), numbered like source_rows
        i = len(texts)
        for m in members:
            if m["url"] in have:
                continue
            i += 1                                  # keep the number even when skipped: it must match source_rows
            if not (m.get("snippet") or "").strip() or i > 5:
                continue
            parts.append(f"[{i}] {m['outlet']} — {m['title']}\n{m['snippet']}")
            plain.append(m["title"] + " " + m["snippet"])
    return "\n\n".join(parts), "\n".join(plain)


def _validate_card(card: dict, plain: str) -> Optional[str]:
    if (err := validate_headline(card["headline"], plain)):
        return err
    for f in ("dek", "what_happened", "why_it_matters"):
        if looks_like_garbage(card[f]):
            return f"{f} contains leaked reasoning or error text"
    if len(card["dek"]) < 30 or len(card["dek"]) > 260:
        return "dek must be one sentence of 30-260 characters"
    if len(card["what_happened"]) < 100:
        return "what_happened too short (need 2-4 sentences)"
    if any(len(k) < 20 or looks_like_garbage(k) for k in card["key_points"]):
        return "each key point must be a specific fact of at least 20 characters"
    if card["headline"].lower().strip(". ") == card["dek"].lower().strip(". "):
        return "dek repeats the headline"
    return None


async def write_card(g: dict, level: str = "general") -> dict:
    corpus, plain = build_corpus(g["texts"], g["members"])
    if len(plain) < 400:
        raise LLMError("not enough source text to write a grounded card")
    user = f"{LEVEL_NOTE.get(level, LEVEL_NOTE['general'])}\n\nSOURCES\n{corpus}"
    card = await llm.chat_json("writer", SYSTEM, user, CARD_SCHEMA, schema_name="story_card",
                               temperature=0.2, max_tokens=1300,
                               validate=lambda c: _validate_card(c, plain), retries=2)
    card = {k: (v.strip() if isinstance(v, str) else v) for k, v in card.items()}
    card["key_points"] = [k.strip() for k in card["key_points"]]
    card["tags"] = [re.sub(r"[^\w &/\-]", "", t).strip()[:28] for t in card["tags"] if t.strip()][:4]
    g_res = ground([card["headline"] + ".", card["dek"], card["what_happened"]] + card["key_points"], plain)
    card["_grounding"] = g_res
    card["_plain"] = plain
    # claim-level provenance: which source sentence supports each summary sentence / key point
    src = [{"n": i, "outlet": t["outlet"], "url": t["url"], "text": t["text"]} for i, t in enumerate(g["texts"], 1)]
    claims = [(x, j) for j, x in enumerate(split_sentences(card["what_happened"]))]
    claims_pts = [(x, j) for j, x in enumerate(card["key_points"])]
    prov = ([dict(c, kind="summary") for c in provenance(claims, src)] +
            [dict(c, kind="point") for c in provenance(claims_pts, src)])
    card["_claims"] = prov
    card["_has_full_text"] = bool(g["texts"])
    return card


def source_rows(g: dict, story_id: str) -> list[tuple]:
    """Full-text sources come first, in the same order used for the writer prompt and claim provenance, so a
    citation number n means the same thing everywhere."""
    rows, seen = [], set()
    text_by_url = {t["url"]: t for t in g["texts"]}
    ordered = [{"url": t["url"], "outlet": t["outlet"], "title": t["title"], "published_at": t["published_at"],
                "domain": next((m["domain"] for m in g["members"] if m["url"] == t["url"]), ""),
                "trust_rank": next((m["trust_rank"] for m in g["members"] if m["url"] == t["url"]), 3), "snippet": ""}
               for t in g["texts"]]
    ordered += [m for m in g["members"] if m["url"] not in text_by_url]
    for m in ordered:
        if m["url"] in seen or len(rows) >= 8:
            continue
        seen.add(m["url"])
        body = (text_by_url.get(m["url"]) or {}).get("text") or m.get("snippet", "")
        rows.append((story_id, len(rows) + 1, m["url"], m["outlet"], m["domain"], m["title"], m["trust_rank"],
                     m["published_at"], body[:320], int(m["url"] in text_by_url)))
    return rows
