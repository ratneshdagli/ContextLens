"""ContextLens v4 — the explainer pass ("why did this happen, and what led to it?").

The v3 card restated the facts ("Sensex jumped 564 points, driven by oil prices cooling"). Readers who do not follow
markets/politics need the *mechanism*. This second writer pass produces, in plain language:

  gist       what happened + the one reason it matters (<= 40 words)
  why        a causal chain of 2-5 steps, oldest cause first. Each step = a short label + one *evidence sentence copied
             verbatim from the sources (or from earlier coverage)* + a one-sentence link explaining how it led on.
             The model only chooses which numbered facts to use and writes the connective explanation, so it cannot
             mis-state who did what (a failure seen with 8B models when they paraphrase). Steps that have no evidence
             are labelled "general" (how the world works), are capped at 2, and may not introduce numbers or names.
  so_what    who is affected and how, concretely
  terms      jargon a newcomer would not know: curated definitions first (glossary.py), model-suggested only for the rest
  next       what is expected next — only things the reporting itself says are coming
  who        the main people/organisations and their role in one line

"Questions to ask next" are deterministic (see `suggested_questions`) because small models bake false premises into them.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from backend.services import glossary, llm
from backend.services.llm import LLMError, looks_like_garbage
from backend.services.timeline import is_future_or_plan, relevant_to
from backend.services.verify import _content, _names, _numbers, acronyms, check_claim, split_sentences

log = logging.getLogger("explainer")

EXPLAIN_VERSION = 2          # bump when the explainer prompt/validation improves: older explanations are rebuilt in the queue
BASIS = ["reported", "history", "general"]

EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "gist": {"type": "string"},
        "why": {"type": "array", "minItems": 2, "maxItems": 5, "items": {
            "type": "object",
            "properties": {"cause": {"type": "string"},
                           "facts": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
                           "link": {"type": "string"},
                           "basis": {"type": "string", "enum": BASIS}},
            "required": ["cause", "facts", "link", "basis"], "additionalProperties": False}},
        "so_what": {"type": "string"},
        "terms": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "properties": {"term": {"type": "string"}, "meaning": {"type": "string"}},
            "required": ["term", "meaning"], "additionalProperties": False}},
        "next": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        "who": {"type": "array", "maxItems": 5, "items": {
            "type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}},
            "required": ["name", "role"], "additionalProperties": False}},
    },
    "required": ["gist", "why", "so_what", "terms", "next", "who"],
    "additionalProperties": False,
}

LEVEL = {
    "simple": "Use everyday words and short sentences (like explaining to a curious 15-year-old). Never leave a technical word unexplained.",
    "general": "Write for an intelligent adult who does not follow this topic. Briefly explain specialist words.",
    "expert": "Write for a well-informed reader: dense, precise, skip the basics, but still say WHY.",
}

SYSTEM = """You are a patient explainer journalist. The reader is smart but has NO background in this topic and finds ordinary news confusing. Using the numbered FACTS (F = today's reporting, H = earlier events), explain what is going on, WHY it happened and what led up to it.

Fields:
- gist: 1-2 plain sentences (max 40 words) of the form "<what happened>, because <the main reason>." Do NOT copy the headline and do NOT add anything that is not in the FACTS.
- why: the causal chain, 2-5 steps, OLDEST cause first, ending with what happened today. Every step must be a DIFFERENT event or fact (never restate a step, never explain a step with itself). For each step:
    cause = a short plain label (max 12 words);
    facts = the id(s) of the 1-2 FACTS that show it (e.g. ["F3"] or ["H1","F7"]); use an empty list only for basis "general";
    link = ONE plain sentence saying how this step led to the next ("So...", "Because of this...", "That meant...");
    basis = reported (facts are F ids) | history (facts include an H id) | general (how the world works, no facts; e.g. why cheaper oil helps an oil-importing country; use at most twice and NEVER include a number, date or name that is not in the FACTS).
- so_what: 1-2 sentences: which people or groups named in the FACTS are affected, and how. Use only groups the FACTS mention; if they name nobody, say what changes for the parties they do name. Not "significant implications".
- terms: up to 4 specialist words from the story that are not everyday English. meaning = a GENERAL dictionary-style definition (max 22 words), NOT what it refers to in this story.
- next: up to 3 things the reporting says are coming (hearings, deadlines, decisions), each one plain sentence taken from UPCOMING. Empty list if none.
- who: up to 5 key people/organisations with a one-line role (max 15 words) that the FACTS support.

RULES: Only use the FACTS. Do not invent causes: if the FACTS do not say why, use fewer steps and mark the mechanism as "general". Never write filler like "significant implications", "highlights", "underscores", "raises concerns", "amid", "remains to be seen". Be specific: names, numbers, places. Do not confuse who did what."""

BANNED = re.compile(
    r"\b(significant implications?|highlights? the|underscores?|raises? (?:concerns?|questions)|remains to be seen|"
    r"a (?:stark )?reminder|paints? a picture|sends? a (?:strong )?(?:message|signal)|the situation|complex landscape|"
    r"only time will tell|sheds? light on)\b", re.I)

_STOCK_GROUPS = re.compile(r"\b(home loans?|students of (?:that|the|this) institute|first-time buyers?|ordinary citizens?)\b", re.I)
_CUES = re.compile(r"\b(because|after|following|due to|led to|amid|despite|since|as a result|triggered|sparked|said|alleged|"
                   r"accused|announced|decided|ordered|demanded|filed|denied|rejected|apolog\w+|resign\w*|suspend\w*|"
                   r"warned|approved|raised|cut|fell|rose|jumped|dropped)\b", re.I)
_BOILER = re.compile(r"^(read more|also read|subscribe|sign up|copyright|advertisement|follow us|listen to|watch|click here|"
                     r"share this|photo:|image:|file photo|\(with inputs)", re.I)


def fact_sheet(texts: list[dict], story_text: str, k: int = 24) -> list[dict]:
    """The k most informative sentences across all full-text sources, in reading order.
    [{id:'F1', n:<source number>, outlet, url, text}]"""
    story_words = set(_content(story_text))
    cands = []
    for n, t in enumerate(texts, 1):
        sents = split_sentences(t["text"])[:90]
        for pos, s in enumerate(sents):
            if not (45 <= len(s) <= 330) or _BOILER.match(s):
                continue
            w = set(_content(s))
            if not w:
                continue
            overlap = len(w & story_words) / max(len(w), 1)
            score = 0.55 * overlap + (0.2 if _CUES.search(s) else 0) + (0.1 if re.search(r"\d", s) else 0) + \
                    (0.15 if pos < 6 else 0.0)
            cands.append({"score": score, "n": n, "pos": pos, "outlet": t["outlet"], "url": t["url"], "text": s, "w": w})
    cands.sort(key=lambda c: -c["score"])
    chosen: list[dict] = []
    for c in cands:
        if len(chosen) >= k:
            break
        if any(len(c["w"] & x["w"]) / max(len(c["w"] | x["w"]), 1) >= 0.6 for x in chosen):
            continue                                        # near-duplicate of a sentence we already picked
        chosen.append(c)
    chosen.sort(key=lambda c: (c["n"], c["pos"]))
    return [{"id": f"F{i}", "n": c["n"], "outlet": c["outlet"], "url": c["url"], "text": c["text"]}
            for i, c in enumerate(chosen, 1)]


def future_sentences(texts: list[dict], story_text: str, limit: int = 6) -> list[str]:
    """Sentences in the sources that announce something upcoming and are about this story."""
    out = []
    story_words = set(_content(story_text))
    for t in texts:
        for s in split_sentences(t["text"])[:90]:
            if not (35 <= len(s) <= 300 and is_future_or_plan(s)):
                continue
            w = set(_content(s))
            shared = len(w & story_words)
            if shared >= 2 and shared / max(len(w), 1) >= 0.12 or relevant_to(s, s, story_text):
                out.append(s)
    seen, uniq = set(), []
    for s in out:
        key = s[:60].lower()
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq[:limit]


def _overlap(a: str, b: str) -> float:
    wa, wb = set(_content(a)), set(_content(b))
    return len(wa & wb) / max(min(len(wa), len(wb)), 1)


def _validator(headline: str, known: str):
    def validate(o: dict) -> Optional[str]:
        blob = " ".join([o["gist"], o["so_what"]] + [w["link"] + " " + w["cause"] for w in o["why"]])
        if looks_like_garbage(blob):
            return "leaked reasoning or error text"
        if BANNED.search(blob):
            return f"filler phrase '{BANNED.search(blob).group(0)}' — be specific instead"
        if not (30 <= len(o["gist"]) <= 340):
            return "gist must be 1-2 plain sentences (30-340 characters)"
        if len(set(_content(o["gist"])) - set(_content(headline))) < 4:
            return "gist adds too little beyond the headline — say what happened AND the reason (because...)"
        for field in ("gist", "so_what"):
            bad = check_claim(o[field], known, 0.25, name_limit=1)
            if bad:
                return f"{field} states things the facts do not: {bad} — use only the FACTS"
        for sent in split_sentences(o["so_what"]):          # every sentence must be grounded, not just the average
            if len(sent) > 25 and check_claim(sent, known, 0.4, name_limit=1):
                return f"so_what sentence '{sent[:60]}' is not supported by the FACTS — name only groups the FACTS mention"
        stock = _STOCK_GROUPS.search(o["so_what"])
        if stock and not _STOCK_GROUPS.search(known):
            return f"so_what mentions '{stock.group(0)}' which the FACTS never mention"
        if len(o["so_what"]) < 30:
            return "so_what must say concretely who is affected"
        for i, a in enumerate(o["why"]):
            for b in o["why"][i + 1:]:
                if _overlap(a["cause"], b["cause"]) >= 0.7:
                    return f"steps repeat each other ('{a['cause'][:40]}' / '{b['cause'][:40]}') — each step must be a different event"
        for w in o["why"]:
            if len(w["link"]) < 25 or len(w["cause"]) < 8:
                return "each 'why' step needs a short label and a one-sentence link"
            if w["basis"] != "general" and not w["facts"]:
                return "steps with basis reported/history must list fact ids"
        return None
    return validate


def _prompt(card: dict, facts: list[dict], hist_lines: list[str], primer: str, upcoming: list[str], level: str) -> str:
    parts = [LEVEL.get(level, LEVEL["simple"]), f"\nSTORY HEADLINE: {card['headline']}"]
    parts.append("\nFACTS (today's reporting)\n" + "\n".join(f"{f['id']} [{f['outlet']}] {f['text']}" for f in facts))
    if hist_lines:
        parts.append("\nEARLIER EVENTS (oldest first)\n" + "\n".join(hist_lines))
    if primer:
        parts.append(f"\nBACKGROUND (encyclopedia)\n{primer}")
    parts.append("\nUPCOMING (sentences from the reporting that mention future events)\n" +
                 ("\n".join(f"- {u}" for u in upcoming) if upcoming else "(none)"))
    return "\n".join(parts)


def suggested_questions(explain: dict, entities: list[str]) -> list[dict]:
    """Deterministic 'go deeper' prompts. kind maps to a backend deep-dive; q is the button label."""
    qs = [{"kind": "why", "q": "Why did this happen?"},
          {"kind": "history", "q": "What led up to this?"}]
    if explain.get("who") or entities:
        name = (explain.get("who") or [{"name": entities[0]}])[0]["name"] if (explain.get("who") or entities) else ""
        if name:
            qs.append({"kind": "who", "q": f"Who is {name}?", "arg": name})
    for t in (explain.get("terms") or [])[:1]:
        qs.append({"kind": "term", "q": f"What does “{t['term']}” mean?", "arg": t["term"]})
    qs.append({"kind": "views", "q": "What are the different views?"})
    qs.append({"kind": "next", "q": "What happens next?"})
    return qs[:6]


def _terms(model_terms: list[dict], visible: str) -> list[dict]:
    curated = glossary.find_terms(visible, limit=5)
    have = {c["term"].lower() for c in curated}
    out = list(curated)
    low = visible.lower()
    for t in model_terms:
        term, meaning = t["term"].strip(), t["meaning"].strip()
        k = term.lower()
        if len(out) >= 6 or not (2 < len(term) <= 40) or k in have or k not in low or glossary.find_terms(term, 1):
            continue                                        # already covered by a curated definition
        if not (14 <= len(meaning) <= 200) or looks_like_garbage(meaning) or _overlap(meaning, visible) > 0.85:
            continue                                        # a definition that just re-quotes the story is not a definition
        have.add(k)
        out.append({"term": term, "meaning": meaning.rstrip(".") + ".", "curated": False})
    return out[:6]


async def write_explainer(card: dict, texts: list[dict], members: list[dict], history: Optional[list[dict]] = None,
                          primer: str = "", level: str = "simple", entities: Optional[list[str]] = None) -> dict:
    """card: output of story_builder.write_card. texts: [{url,outlet,text,...}] full-text sources (may be empty →
    snippet facts). history: earlier events [{date,text,source_url,source_outlet}] oldest first.
    Returns the `explain` dict (see module doc). Raises LLMError if the model cannot produce a valid one."""
    story_text = f"{card['headline']}. {card['dek']} {card['what_happened']}"
    facts = fact_sheet(texts, story_text) if texts else []
    if len(facts) < 4:                                       # snippet-only stories: use each outlet's snippet as a fact
        for i, m in enumerate(members[:8], len(facts) + 1):
            if m.get("snippet") and len(m["snippet"]) > 40:
                facts.append({"id": f"F{i}", "n": i, "outlet": m["outlet"], "url": m["url"], "text": m["snippet"][:300]})
    if len(facts) < 3:
        raise LLMError("not enough source facts to explain this story")
    hist = [h for h in (history or []) if h.get("text")][-6:]
    hist_facts = {f"H{i}": h for i, h in enumerate(hist, 1)}
    hist_lines = [f"H{i} [{h['date']}] {h['text']}" for i, h in hist_facts.items()]
    upcoming = future_sentences(texts, story_text) if texts else []

    known = " ".join(f["text"] for f in facts) + " " + card["what_happened"] + " " + " ".join(card["key_points"])
    known += " " + " ".join(h["text"] for h in hist)
    out = await llm.chat_json("writer", SYSTEM, _prompt(card, facts, hist_lines, primer, upcoming, level), EXPLAIN_SCHEMA,
                              schema_name="explainer", temperature=0.15, max_tokens=1200,
                              validate=_validator(card["headline"], known), retries=2)

    by_id = {f["id"]: f for f in facts}
    allowed_nums, allowed_low = _numbers(known), known.lower().replace("’", "'") + " " + " ".join(acronyms(known))

    def _general_ok(text: str) -> bool:
        new_nums = [n for n in _numbers(text) if n not in allowed_nums and n not in ("1", "2")]
        return not new_nums and len([n for n in _names(text) if n not in allowed_low]) < 2

    steps, dropped = [], []
    for w in out["why"]:
        cause, link, basis = w["cause"].strip().rstrip("."), w["link"].strip(), w["basis"]
        ids = [i.strip().upper() for i in w["facts"] if isinstance(i, str)]
        ev = [by_id[i] for i in ids if i in by_id]
        hv = [hist_facts[i] for i in ids if i in hist_facts]
        step: dict = {"cause": cause, "link": link, "basis": "general"}
        problems = check_claim(f"{cause}. {link}", known, 0.0)         # numbers/names must exist; wording is free
        if problems:
            dropped.append({"step": cause[:80], "why": problems})
            continue
        if ev:
            e = ev[0]
            step.update(basis="reported", evidence=e["text"], n=e["n"], outlet=e["outlet"], url=e["url"])
        elif hv:
            h = hv[0]
            step.update(basis="history", evidence=h["text"], outlet=h.get("source_outlet", ""), url=h.get("source_url", ""),
                        date=h["date"])
        elif not _general_ok(cause + " " + link):
            dropped.append({"step": cause[:80], "why": ["general step introduced facts"]})
            continue
        steps.append(step)
    kept_g = 0
    final = []
    for s in steps:
        if s["basis"] == "general":
            kept_g += 1
            if kept_g > 2:
                continue
        final.append(s)
    if len(final) < 2:
        raise LLMError(f"explainer kept only {len(final)} valid 'why' step(s); dropped: {dropped}")

    visible = story_text + " " + " ".join(f["text"] for f in facts)
    nxt = []
    up_blob = "\n".join(upcoming)
    for n in out["next"]:
        n = n.strip()
        if 30 <= len(n) <= 260 and not looks_like_garbage(n) and upcoming and not check_claim(n, up_blob, 0.45):
            nxt.append(n)                                   # must be grounded in a sentence that itself announces something upcoming
    who = []
    low_vis = visible.lower()
    for p in out["who"]:
        name, role = p["name"].strip(), p["role"].strip()
        if 2 < len(name) <= 60 and name.lower().split()[-1] in low_vis and 8 <= len(role) <= 140 and not looks_like_garbage(role) \
                and not check_claim(role, known, 0.0):
            who.append({"name": name, "role": role.rstrip(".")})
    explain = {
        "gist": out["gist"].strip(),
        "why": final,
        "so_what": out["so_what"].strip(),
        "terms": _terms(out["terms"], visible),
        "next": nxt[:3],
        "who": who[:5],
        "level": level,
        "v": EXPLAIN_VERSION,
        "dropped": dropped[:5],
        "grounded_steps": sum(1 for s in final if s["basis"] != "general"),
    }
    explain["questions"] = suggested_questions(explain, entities or [])
    return explain
