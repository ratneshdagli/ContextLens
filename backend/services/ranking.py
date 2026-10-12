"""ContextLens v3 — deterministic candidate ranking (stage 3: no LLM).

score = 0.42*interest + 0.10*topic + 0.18*coverage + 0.06*trust + 0.14*recency + 0.10*taste
then MMR for diversity so the feed isn't ten variants of one story.
Every component is returned so the UI can say *why* a story is shown.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from backend.services import channels as CH
from backend.services import interests as I

W = {"interest": 0.42, "topic": 0.10, "coverage": 0.18, "trust": 0.06, "recency": 0.14, "taste": 0.10}


def _age_hours(iso: Optional[str]) -> float:
    if not iso:
        return 48.0
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 3600)
    except Exception:
        return 48.0


def coverage_score(outlets: int) -> float:
    return min(1.0, math.log1p(max(outlets, 1) - 1) / math.log1p(7))     # 1 outlet=0, 8+ outlets=1


def channel_of(c: dict, text: str, channels: Optional[list[dict]]) -> tuple[Optional[dict], float]:
    """The reader's channel this event belongs to (best affinity above the bar), or None. Events collected for a channel's
    own sources need a lower bar than events from general feeds; own-source events that fit nothing are off-topic."""
    own = c["topic"][len(CH.PREFIX):] if CH.is_channel_topic(c["topic"]) else None
    best, best_a = None, 0.0
    for ch in channels or []:
        if ch["weight"] <= 0:                            # a switched-off channel claims nothing: its stories fall back to normal news
            continue
        a = CH.affinity(c["centroid"], text, ch)
        bar = CH.MEMBER_MIN_OWN if ch["id"] == own else CH.MEMBER_MIN_GENERAL
        if a >= bar and a > best_a:
            best, best_a = ch, a
    return best, best_a


def score_cluster(c: dict, profile: dict, items, ivecs, taste: Optional[np.ndarray], channels: Optional[list[dict]] = None) -> dict:
    """`c` needs: title, topic, outlet_count, last_seen, centroid(np), members[{title,snippet,trust_rank,domain}]
    `channels` (from channels.load_ready) lets an event belong to one of the reader's own categories."""
    text = " ".join(m["title"] + " " + (m.get("snippet") or "") for m in c["members"])
    reasons: list[str] = []

    muted = I.keyword_hit(text, profile["muted_keywords"])
    if muted:
        return {"excluded": f"muted keyword '{muted}'", "score": 0.0}
    domains = {m["domain"] for m in c["members"]}
    if domains and domains <= set(profile["muted_outlets"]):
        return {"excluded": "all outlets muted", "score": 0.0}
    ch, ch_aff = channel_of(c, text, channels)
    if ch is None and CH.is_channel_topic(c["topic"]):
        return {"excluded": "off-topic for the channel it was collected for", "score": 0.0}
    if ch is not None:
        cm = I.keyword_hit(text, ch["muted"])
        if cm:
            return {"excluded": f"muted keyword '{cm}' in channel “{ch['name']}”", "score": 0.0}
    topic_w = ch["weight"] if ch else profile["topics"].get(c["topic"], 1)

    interest, matched = I.match_cluster(c["centroid"], items, ivecs)
    i_only = interest                                    # what the reader's own interest text matched: only this may be claimed as "matches your interest"
    if ch is not None:                                   # name + description + key terms, and your own interest text, together
        interest = min(1.0, max(interest, ch_aff) + (0.1 if interest >= 0.45 and ch_aff >= 0.45 else 0.0))
    followed = I.keyword_hit(text, profile["followed"])
    if followed:
        interest = max(interest, 0.85)
        matched = matched or f"you follow “{followed}”"
        reasons.append(f"mentions {followed}")

    if not items and not followed and ch is None:
        interest = 0.35          # no stated interests yet → neutral, topic weights decide
    topic = {0: 0.0, 1: 0.5, 2: 0.8, 3: 1.0}[topic_w]
    cov = coverage_score(c["outlet_count"])
    trust = float(np.mean([(5 - min(m["trust_rank"], 4)) / 4 for m in c["members"]]))
    rec = math.exp(-_age_hours(c["last_seen"]) / (72.0 if ch else 20.0))      # niche channels publish slowly: older stories still count
    taste_s = 0.0
    if taste is not None and taste.shape == c["centroid"].shape:
        taste_s = float(np.clip(float(c["centroid"] @ taste) * 2.2, -1.0, 1.0))
    taste_pos = max(taste_s, 0.0)

    score = (W["interest"] * interest + W["topic"] * topic + W["coverage"] * cov + W["trust"] * trust
             + W["recency"] * rec + W["taste"] * taste_pos + 0.12 * min(taste_s, 0.0))
    if topic_w == 0 and interest < 0.6 and not followed:
        return {"excluded": "topic switched off", "score": 0.0}

    if matched and i_only >= 0.45 and not matched.startswith("you follow"):
        reasons.insert(0, f"matches your interest in {matched}")
    if ch is not None:
        reasons.insert(0, f"in your channel “{ch['name']}”")
    if c["outlet_count"] >= 4:
        reasons.append(f"covered by {c['outlet_count']} outlets")
    if taste_s > 0.3:
        reasons.append("similar to stories you liked")
    if not reasons:
        reasons.append(f"top {c['topic']} story")
    return {
        "score": round(float(score), 4), "excluded": None,
        "components": {"interest": round(interest, 3), "topic": round(topic, 2), "coverage": round(cov, 3),
                       "trust": round(trust, 3), "recency": round(rec, 3), "taste": round(taste_s, 3)},
        "matched_interest": matched, "why": "; ".join(reasons[:2]),
        "channel": ch["id"] if ch else None, "channel_name": ch["name"] if ch else "",
    }


def mmr_select(scored: list[dict], k: int, lam: float = 0.72) -> list[dict]:
    """Greedy Maximal Marginal Relevance on centroid similarity.

    Keeps, for every candidate, its highest similarity to anything already chosen, and refreshes it with one
    matrix-vector product per pick: O(k·n) instead of the O(k·n·k) pairwise loop (60 s for 2,000 events, k=200).
    Same picks, same order, ties resolved to the earlier candidate exactly as the loop did."""
    pool = sorted(scored, key=lambda x: -x["score"])
    if not pool or k <= 0:
        return []
    n = len(pool)
    shape = pool[0]["centroid"].shape
    same = np.array([p["centroid"].shape == shape for p in pool])          # vectors of another dimension are never compared
    C = np.stack([p["centroid"] if ok else np.zeros(shape, dtype=np.float32) for p, ok in zip(pool, same)]).astype(np.float64)
    S = np.array([p["score"] for p in pool], dtype=np.float64)
    top = np.full(n, -np.inf)                                              # best similarity to any chosen item so far
    alive = np.ones(n, dtype=bool)
    chosen: list[dict] = []
    while len(chosen) < min(k, n):
        div = np.where(np.isfinite(top), top, 0.0)                         # no chosen item yet -> no diversity penalty
        val = np.where(alive, lam * S - (1 - lam) * div, -np.inf)
        i = int(np.argmax(val))                                            # first maximum, like the strict '>' in the loop
        chosen.append(pool[i])
        alive[i] = False
        if same[i]:
            sim = C @ C[i]
            sim[~same] = -np.inf
            top = np.maximum(top, sim)
    return chosen
