"""ContextLens v3 — Coverage lens (deterministic, no LLM).

Shows what a single-outlet reader never sees:
  * how each outlet framed the story (its own headline + its opening sentence),
  * details reported by only ONE outlet (sentences whose content is absent from the other sources),
  * which countries' press covered it.
"""

from __future__ import annotations

import re

from backend.config import TRUSTED_DOMAIN_MAP
from backend.services.feeds import lookup_source
from backend.services.verify import _content, _numbers, split_sentences

_BOILER = re.compile(r"^(read more|also read|subscribe|sign up|copyright|advertisement|follow us|listen to|watch|click here|"
                     r"download|share this|newsletter|related:|photo:|image:|\(reuters\)|\(ap\)|\(bloomberg\))", re.I)


_DANGLING = re.compile(r"^(this|these|those|that|they|them|it|its|he|she|his|her|many|some|such|both|also|however|but|and|while|meanwhile)\b", re.I)


def _on_topic(sentence: str, story_text: str) -> bool:
    """The detail must be about THIS story: share a specific anchor or enough content words with it."""
    from backend.services.clustering import anchors
    sw, tw = set(_content(sentence)), set(_content(story_text))
    overlap = len(sw & tw) / max(len(sw), 1)
    return bool(anchors(sentence) & anchors(story_text)) and overlap >= 0.12


def country_for(domain: str) -> str:
    hit = lookup_source(domain or "")
    return (hit or {}).get("country", "")


def _lead(text: str, title: str = "") -> str:
    text = re.sub(r"^[\s\-–—•]*(published|updated)[^.]{0,40}?(ago|\d{4})[\s\-–—:]*", "", text, flags=re.I)
    if title and text.lower().startswith(title.lower()[:40]):
        text = text[len(title):].lstrip(" -–—:")
    for s in split_sentences(text)[:6]:
        if len(s) >= 70 and not _BOILER.match(s) and not s.lower().startswith(("listen to", "summarized")):
            return s[:280]
    return ""


def build_lens(texts: list[dict], members: list[dict], story_text: str = "") -> dict:
    """texts: full-text sources [{n,outlet,url,title,text}] (n aligned with story_sources); members: all cluster articles."""
    angles, seen = [], set()
    text_by_url = {t["url"]: t for t in texts}
    for m in members:
        key = m["outlet"]
        if key in seen:
            continue
        seen.add(key)
        t = text_by_url.get(m["url"])
        angles.append({"outlet": m["outlet"], "domain": m["domain"], "country": country_for(m["domain"]), "url": m["url"],
                       "title": m["title"], "lead": _lead(t["text"], m["title"]) if t else ""})
    countries = sorted({a["country"] for a in angles if a["country"] and a["country"] != "INT"})

    unique = []
    if len(texts) >= 2:
        sets = {t["url"]: [(s, set(_content(s))) for s in split_sentences(t["text"])] for t in texts}
        for t in texts:
            others = set()
            for u, lst in sets.items():
                if u != t["url"]:
                    for _, cw in lst:
                        others |= cw
            cands = []
            for s, cw in sets[t["url"]]:
                if len(s) < 60 or len(s) > 300 or len(cw) < 5 or _BOILER.match(s):
                    continue
                novelty = 1 - len(cw & others) / len(cw)
                # concrete detail = contains a number or a proper noun mid-sentence
                concrete = bool(_numbers(s)) or bool(re.search(r"(?<=[a-z,] )[A-Z][a-z]{2,}", s))
                if _DANGLING.match(s) or (story_text and not _on_topic(s, story_text)):
                    continue                        # needs prior context, or is about something else
                if novelty >= 0.55 and concrete:
                    cands.append((novelty + (0.2 if _numbers(s) else 0), s))
            cands.sort(reverse=True)
            for _, s in cands[:1]:
                unique.append({"outlet": t["outlet"], "url": t["url"], "n": t["n"], "sentence": s})
    return {"angles": angles[:8], "unique": unique[:4], "countries": countries}
