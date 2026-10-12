"""ContextLens v3 — grounding verification (deterministic, no model swap).

Every generated claim is checked against the source text:
  * numbers/percentages must appear in the sources
  * capitalised names must appear in the sources
  * content-word recall against the best-matching source window must clear a threshold
Returns a score in 0..1 plus the offending claims, stored with the story for transparency.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("""a an the and or but if of in on at to for with by from as is are was were be been being it its this that
these those he she they them his her their our we you i not no than then so such can could would should will may might
has have had do does did said says say after before over under into out up down about also more most other some any
new one two three per just which who whom whose what when where while during between against""".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s", "ly"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _content(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2]


_LEAD_JUNK = re.compile(r"^[\s\-–—•|]*(?:published|updated|posted)\b[\s:]*(?:\d{1,2}\s+\w+\s+\d{4}|\d+\s+(?:hours?|minutes?|days?)\s+ago)?[\s\-–—:|]*", re.I)


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'])", re.sub(r"\s+", " ", text.strip()))
    out = [_LEAD_JUNK.sub("", p).strip() for p in parts]
    return [p for p in out if len(p) > 15]


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*\.?\d*", text)}


def _names(text: str) -> set[str]:
    # Capitalised tokens that are not sentence-initial
    toks = re.findall(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z\-’']{2,})", text)
    return {t.lower().replace("’s", "").replace("'s", "") for t in toks}


def ground(claims: list[str], source_text: str) -> dict:
    src_sents = split_sentences(source_text)
    src_low = source_text.lower().replace("’", "'")
    src_nums = _numbers(source_text)
    # windows of two adjacent source sentences
    windows = [" ".join(src_sents[i:i + 2]) for i in range(max(len(src_sents) - 1, 1))] or [source_text]
    win_sets = [set(_content(w)) for w in windows]

    unsupported: list[dict] = []
    supported = 0
    total = 0
    for claim in claims:
        for sent in split_sentences(claim) or ([claim] if len(claim) > 15 else []):
            total += 1
            cw = set(_content(sent))
            recall = max((len(cw & ws) / len(cw) for ws in win_sets), default=0.0) if cw else 1.0
            missing_nums = [n for n in _numbers(sent) if n not in src_nums and n not in ("1", "2")]
            missing_names = [n for n in _names(sent) if n.replace("’", "'") not in src_low]
            problems = []
            if recall < 0.45:
                problems.append(f"low overlap {recall:.2f}")
            if missing_nums:
                problems.append(f"numbers not in sources: {missing_nums[:3]}")
            if len(missing_names) >= 2 or (missing_names and recall < 0.6):
                problems.append(f"names not in sources: {missing_names[:3]}")
            if problems:
                unsupported.append({"claim": sent[:200], "problems": problems})
            else:
                supported += 1
    score = supported / total if total else 0.0
    return {"score": round(score, 3), "checked": total, "unsupported": unsupported[:8]}


# ── claim-level provenance ───────────────────────────────────────────────────

def provenance(claims: list[tuple[str, int]], sources: list[dict], min_recall: float = 0.5) -> list[dict]:
    """For each (claim_text, idx) find the source sentence(s) that support it.

    sources: [{n, outlet, url, text}]. Returns [{idx, text, n, outlet, url, quote, score}] for claims that could be traced.
    Deterministic and offline: content-word recall against every source sentence (and adjacent-sentence windows),
    with numbers required to appear. This is what powers "show me where this came from" in the UI."""
    prepared = []
    for s in sources:
        sents = split_sentences(s["text"])
        sets = [set(_content(x)) for x in sents]
        prepared.append((s, sents, sets))
    out = []
    for text, idx in claims:
        cw = set(_content(text))
        if len(cw) < 3:
            continue
        nums = _numbers(text)
        best = None
        for s, sents, sets in prepared:
            for i, ss in enumerate(sets):
                single = len(cw & ss) / len(cw)
                pair_set = ss | (sets[i + 1] if i + 1 < len(sets) else set())
                pair = len(cw & pair_set) / len(cw)
                use_pair = pair - single >= 0.15
                score = pair if use_pair else single
                if best is None or score > best["score"]:
                    quote = sents[i] + (" " + sents[i + 1] if use_pair and i + 1 < len(sents) else "")
                    best = {"score": score, "n": s["n"], "outlet": s["outlet"], "url": s["url"], "quote": quote}
        if best and best["score"] >= min_recall:
            if nums and not all(n in _numbers(best["quote"]) or n in ("1", "2") for n in nums):
                continue                                   # numbers must match the quoted evidence
            out.append({"idx": idx, "text": text, "score": round(best["score"], 2), **{k: best[k] for k in ("n", "outlet", "url")},
                        "quote": best["quote"][:340]})
    return out


def acronyms(text: str) -> set[str]:
    """Initialisms of capitalised phrases in `text` ('Reserve Bank of India' -> 'rbi'), so a writer that abbreviates a name
    the source spells out is not treated as inventing a name."""
    out: set[str] = set()
    for m in re.finditer(r"\b((?:[A-Z][a-z]+(?:\s+(?:of|and|for|the)\s+|\s+))+[A-Z][a-z]+)\b", text):
        words = [w for w in re.split(r"\s+", m.group(1)) if w[0].isupper() and w not in ("The", "A", "An")]
        if 2 <= len(words) <= 6:
            out.add("".join(w[0] for w in words).lower())
    return out


def check_claim(claim: str, source_text: str, min_recall: float = 0.3, name_limit: int = 2) -> list[str]:
    """Looser support test for explanatory sentences (they contain connective words like 'because'/'so that' that
    never appear in the source). Numbers must match exactly and names must exist; content-word recall needs only
    `min_recall` against the best two-sentence window. Returns a list of problems (empty = supported)."""
    src_sents = split_sentences(source_text)
    windows = [" ".join(src_sents[i:i + 2]) for i in range(max(len(src_sents) - 1, 1))] or [source_text]
    cw = set(_content(claim))
    recall = max((len(cw & set(_content(w))) / len(cw) for w in windows), default=0.0) if cw else 1.0
    low = source_text.lower().replace("’", "'")
    src_nums = _numbers(source_text)
    problems = []
    if recall < min_recall:
        problems.append(f"low overlap {recall:.2f}")
    missing_nums = [n for n in _numbers(claim) if n not in src_nums and n not in ("1", "2")]
    if missing_nums:
        problems.append(f"numbers not in sources: {missing_nums[:3]}")
    known_acr = acronyms(source_text)
    missing_names = [n for n in _names(claim) if n.replace("’", "'") not in low and n not in known_acr]
    if len(missing_names) >= name_limit:
        problems.append(f"names not in sources: {missing_names[:3]}")
    return problems
