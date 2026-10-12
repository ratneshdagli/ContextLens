"""ContextLens v3 — the Editor (independent second model).

Small local models are unreliable as free-form judges (in testing, both Llama-3-8B and Phi-4-mini
answered keep=false for "Nvidia unveils AI accelerator" against a reader who follows AI chips).
So the editor is a set of *decomposed classifiers*: the model answers narrow enum/boolean
questions with a short restatement first (forces comprehension), and the actual decision is made
by deterministic code from those answers. Every decision is stored with its reasons.
"""

from __future__ import annotations

import re
from typing import Optional

from backend.config import STRICTNESS
from backend.services import llm
from backend.services.llm import LLMError, looks_like_garbage

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "what_happened": {"type": "string"},
        "story_type": {"type": "string", "enum": [
            "breaking_event", "development", "analysis_or_feature", "routine_update", "service_or_how_to",
            "promotion_or_deals", "stock_tips_or_market_chatter", "political_sniping", "celebrity_gossip",
            "sports_result", "obituary", "other"]},
        "has_concrete_new_fact": {"type": "boolean"},
        "impact": {"type": "string", "enum": ["local_minor", "notable", "major", "historic"]},
        "matches_reader": {"type": "string", "enum": ["strong", "partial", "none"]},
        "reader_value": {"type": "integer", "minimum": 0, "maximum": 10},
    },
    "required": ["what_happened", "story_type", "has_concrete_new_fact", "impact", "matches_reader", "reader_value"],
    "additionalProperties": False,
}

TRIAGE_SYSTEM = """You are the news editor of a personalised newspaper. For each candidate story you answer a fixed questionnaire.
Be literal and DISCRIMINATING: most stories are NOT front-page material. Use the full range of every scale.
- what_happened: restate the actual event in one plain sentence (max 25 words). If you cannot say what happened, it is probably not news.
- story_type: breaking_event (something significant just happened) | development (a concrete new step in an ongoing story) | analysis_or_feature (in-depth reporting/explainer) | routine_update (scheduled or recurring update) | service_or_how_to (guides, tips, 'how to check', explainers of procedures) | promotion_or_deals (shopping, deals, sponsored) | stock_tips_or_market_chatter (ONLY: analyst price targets, 'stocks in focus' lists, one company's share-price move, IPO subscription/GMP/allotment, buy/sell calls — NOT central-bank, macro, market-wide or industry news, which are developments) | political_sniping (parties or politicians trading barbs, rhetoric, jibes, without a decision or event) | celebrity_gossip | sports_result | obituary | other.
- has_concrete_new_fact: true only if there is a specific new event, decision, number or finding.
- impact: local_minor = affects one company, person, city or small group, or is commentary; notable = affects an industry, a state or a large group; major = affects a whole country, global markets, or millions of people; historic = a once-in-a-decade event.
- matches_reader: strong ONLY if the story's main subject is literally one of the reader's listed interests or followed topics; partial if it is in the same broad area; none otherwise.
- reader_value: 0-3 = skip, 4-6 = maybe, 7-10 = must-read for THIS reader. Most stories should be 0-5."""

REJECT_TYPES = {"service_or_how_to", "promotion_or_deals"}                 # objective junk: always vetoed
SOFT_REJECT_TYPES = {"stock_tips_or_market_chatter", "political_sniping"}    # subjective labels: vetoed only when deterministic evidence is weak
SOFT_VETO_RANK = 0.60
IMPACT_W = {"local_minor": 0.15, "notable": 0.5, "major": 0.85, "historic": 1.0}
MATCH_W = {"strong": 1.0, "partial": 0.55, "none": 0.1}


def _triage_prompt(brief: str, c: dict) -> str:
    heads = []
    seen = set()
    for m in sorted(c["members"], key=lambda m: m["trust_rank"])[:4]:
        key = m["title"].lower()[:40]
        if key not in seen:
            seen.add(key)
            heads.append(f"- ({m['outlet']}) {m['title']}")
    snippet = next((m["snippet"] for m in sorted(c["members"], key=lambda m: -len(m.get("snippet") or "")) if m.get("snippet")), "")
    return (
        f"READER PROFILE\n{brief}\n\nCANDIDATE STORY (covered by {c['outlet_count']} outlet(s))\n"
        + "\n".join(heads) + (f"\nSnippet: {snippet[:380]}" if snippet else "")
    )


def decide_triage(ans: dict, rank_score: float, interest: float, strictness: str, niche: bool = False) -> dict:
    """Pure decision. The LLM is used as a *junk-type classifier* (hard vetoes) and a soft scorer; significance
    is carried mostly by the deterministic rank score (coverage breadth + interest embeddings), because small
    local models proved unreliable at judging significance on their own (see scripts/bench_editor.py).
    `niche` = collected for one of the reader's own channels: significance is judged by the channel's standards, not general
    news ("routine", "minor" and "no new fact" are what a niche beat is made of), so only off-topic and objective junk veto it."""
    thr = STRICTNESS[strictness]["triage_min"]
    if niche:
        thr = min(thr, 0.25)
        t_score = 0.5 * MATCH_W[ans["matches_reader"]] + 0.5 * (ans["reader_value"] / 10)
        combined = 0.6 * rank_score + 0.4 * t_score
        reasons = []
        if ans["story_type"] in REJECT_TYPES:
            reasons.append(f"type={ans['story_type']}")
        if ans["matches_reader"] == "none":
            reasons.append("off-topic for the channel")
        if not reasons and combined < thr:
            reasons.append(f"score {combined:.2f} < {thr:.2f}")
        return {"keep": not reasons, "score": round(combined, 3), "triage_score": round(t_score, 3),
                "reasons": reasons or ["passed"], "answers": ans}
    t_score = (0.40 * IMPACT_W[ans["impact"]] + 0.35 * MATCH_W[ans["matches_reader"]]
               + 0.25 * (ans["reader_value"] / 10))
    combined = 0.6 * rank_score + 0.4 * t_score
    reasons = []
    keep = True
    if ans["story_type"] in REJECT_TYPES:
        keep, _ = False, reasons.append(f"type={ans['story_type']}")
    if ans["story_type"] in SOFT_REJECT_TYPES and rank_score < SOFT_VETO_RANK:
        keep, _ = False, reasons.append(f"type={ans['story_type']} with weak interest/coverage")
    if not ans["has_concrete_new_fact"]:
        keep, _ = False, reasons.append("no concrete new fact")
    if ans["story_type"] == "routine_update" and ans["impact"] == "local_minor":
        keep, _ = False, reasons.append("routine + minor")
    if ans["story_type"] in ("celebrity_gossip", "obituary", "sports_result") and ans["matches_reader"] == "none":
        keep, _ = False, reasons.append(f"{ans['story_type']} outside interests")
    if ans["matches_reader"] == "none" and ans["impact"] == "local_minor":
        keep, _ = False, reasons.append("minor and off-topic")
    if combined < thr:
        keep, _ = False, reasons.append(f"score {combined:.2f} < {thr:.2f}")
    return {"keep": keep, "score": round(combined, 3), "triage_score": round(t_score, 3),
            "reasons": reasons or ["passed"], "answers": ans}


async def triage(c: dict, brief: str, rank_score: float, interest: float, strictness: str, niche: bool = False) -> dict:
    """Returns decision dict. On LLM failure the candidate is *not* auto-approved: it falls back to the
    deterministic score only, requires a high bar, and is marked `llm_failed`."""
    try:
        ans = await llm.chat_json("editor", TRIAGE_SYSTEM, _triage_prompt(brief, c), TRIAGE_SCHEMA,
                                  schema_name="triage", max_tokens=260)
        d = decide_triage(ans, rank_score, interest, strictness, niche)
        d["llm_failed"] = False
        return d
    except LLMError as e:
        ok = rank_score >= 0.62 and c["outlet_count"] >= 3
        return {"keep": ok, "score": round(rank_score, 3), "reasons": [f"editor unavailable ({str(e)[:60]}); deterministic fallback"],
                "llm_failed": True, "answers": {}}


# ── headline validation ──────────────────────────────────────────────────────

def validate_headline(h: str, source_text: str = "") -> Optional[str]:
    """Return an error string or None. Rejects leaked reasoning, error strings, clickbait shapes."""
    h = (h or "").strip()
    if looks_like_garbage(h):
        return "headline looks like leaked reasoning/error text"
    if not (25 <= len(h) <= 150):
        return f"headline length {len(h)} outside 25-150 chars"
    if h.endswith((":", "…", "...")) or h.startswith(('"', "'", "Headline", "Title")):
        return "malformed headline"
    if re.search(r"\b(you won'?t believe|shocking|this is why|here'?s why|what happened next)\b", h, re.I):
        return "clickbait phrasing"
    if h.isupper():
        return "all caps"
    if source_text:
        nums = set(re.findall(r"\d[\d,.]*\d|\d", h))
        low_src = source_text.replace(",", "")
        for n in nums:
            if n.replace(",", "") not in low_src and n not in ("1", "2", "3"):
                return f"number '{n}' not found in sources"
    return None


# ── publish gate ─────────────────────────────────────────────────────────────

PUBLISH_SCHEMA = {
    "type": "object",
    "properties": {
        "headline_matches_facts": {"type": "boolean"},
        "headline_is_clickbait": {"type": "boolean"},
        "summary_support": {"type": "string", "enum": ["fully", "mostly", "partly", "no"]},
        "understandable_standalone": {"type": "boolean"},
        "newsworthy_for_reader": {"type": "string", "enum": ["primary", "secondary", "reject"]},
        "issue": {"type": "string"},
    },
    "required": ["headline_matches_facts", "headline_is_clickbait", "summary_support",
                 "understandable_standalone", "newsworthy_for_reader", "issue"],
    "additionalProperties": False,
}

PUBLISH_SYSTEM = """You are the standards editor. Another writer drafted a story card from source excerpts. Audit it.
- headline_matches_facts: true only if EVERY claim in the new headline is supported by the source excerpts.
- headline_is_clickbait: true if it teases, exaggerates or hides the actual subject.
- summary_support: fully = every sentence and key point is stated in the excerpts; mostly = nearly all are; partly = several claims are NOT in the excerpts; no = contradicts or is unrelated to the excerpts. Paraphrasing is fine — judge the facts, not the wording.
- understandable_standalone: true if a reader with no background could understand the headline and summary.
- newsworthy_for_reader: primary (should lead the reader's front page), secondary (fine but minor), reject (routine, trivial, or not worth the reader's time).
- issue: at most 20 words naming the biggest problem, or "none"."""


def _publish_prompt(brief: str, card: dict, originals: list[str], excerpts: str) -> str:
    kp = "\n".join(f"- {k}" for k in card["key_points"])
    return (
        f"READER PROFILE\n{brief}\n\nORIGINAL HEADLINES FROM OUTLETS\n" + "\n".join(f"- {o}" for o in originals[:4])
        + f"\n\nSOURCE EXCERPTS\n{excerpts[:6000]}\n\nDRAFT CARD\nHeadline: {card['headline']}\nDek: {card['dek']}\n"
        f"Summary: {card['what_happened']}\nKey points:\n{kp}"
    )


def decide_publish(ans: dict, ground: dict, strictness: str, outlet_count: int, interest: float,
                   story_type: str | None = None, niche: bool = False) -> dict:
    """The LLM's answers are advisory for factual support: the deterministic grounding score decides
    together with them, because a small judge is noisy on 'partly' vs 'mostly'.
    `niche` = a story collected for one of the reader's own channels: niche beats are mostly single-outlet and "minor" by
    general-news standards, so outlet count and 'secondary' do not demote it, and a trivial verdict only demotes it."""
    tier, reasons = "primary", []
    g = ground["score"]
    if not ans["headline_matches_facts"]:
        tier = "rejected"; reasons.append("headline not supported by sources")
    if ans["summary_support"] == "no":
        if g < 0.7:
            tier = "rejected"; reasons.append("summary unsupported")
        else:
            tier = "more"; reasons.append("editor doubts summary (grounding ok)")
    if tier != "rejected":
        if ans["headline_is_clickbait"]:
            tier = "more"; reasons.append("clickbait")
        if ans["summary_support"] == "partly" and g < 0.85 and tier == "primary":
            tier = "more"; reasons.append(f"summary only partly supported (grounding {g:.0%})")
        if not ans["understandable_standalone"] and tier == "primary":
            tier = "more"; reasons.append("not understandable standalone")
        if ans["newsworthy_for_reader"] == "reject":
            if niche and interest >= 0.5:
                tier = "more"; reasons.append("editor: minor, kept for your channel")
            else:
                tier = "rejected"; reasons.append("editor: not worth reader's time")
        elif ans["newsworthy_for_reader"] == "secondary" and tier == "primary" and interest < 0.6 and not niche:
            tier = "more"; reasons.append("editor: secondary")
        if g < STRICTNESS[strictness]["publish_min"] and tier == "primary":
            tier = "more"; reasons.append(f"grounding {g:.0%} below bar")
        if tier == "primary" and outlet_count < 2 and interest < 0.75 and not niche:
            tier = "more"; reasons.append("single outlet")
        if tier == "primary" and story_type in SOFT_REJECT_TYPES and outlet_count < 3 and not niche:
            tier = "more"; reasons.append(f"typed as {story_type.replace('_', ' ')} with narrow coverage")
    if ans.get("issue") and tier != "primary" and ans["issue"].lower().strip(". ") not in ("none", "n/a", ""):
        reasons.append(f"editor note: {ans['issue'][:100]}")
    return {"tier": tier, "reasons": reasons or ["passed all checks"], "answers": ans, "grounding": ground}


async def publish_gate(card: dict, brief: str, originals: list[str], excerpts: str, ground: dict,
                       strictness: str, outlet_count: int, interest: float, story_type: str | None = None,
                       niche: bool = False) -> dict:
    """Fail-closed: if the editor model is unavailable the story is demoted to 'more', never promoted."""
    try:
        ans = await llm.chat_json("editor", PUBLISH_SYSTEM, _publish_prompt(brief, card, originals, excerpts),
                                  PUBLISH_SCHEMA, schema_name="publish", max_tokens=260)
        return decide_publish(ans, ground, strictness, outlet_count, interest, story_type, niche)
    except LLMError as e:
        tier = "more" if ground["score"] >= 0.6 else "rejected"
        return {"tier": tier, "reasons": [f"editor unavailable ({str(e)[:60]}); demoted"], "answers": {}, "grounding": ground}
