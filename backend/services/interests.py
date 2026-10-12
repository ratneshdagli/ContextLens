"""ContextLens v3 — the user's Interest Profile + learned taste.

The profile is what every gate checks against:
  interests  free-text statements ("AI chips & semiconductors", "Indian economy / RBI") with weight 1-3
  topics     coarse topic weights 0-3 (0 = never show)
  muted_*    hard exclusions        followed   keywords that force a story up
Feedback (like / dislike / save) trains a *taste vector* (Rocchio) that nudges ranking.
"""

from __future__ import annotations

import base64
import re
from typing import Optional

import numpy as np

from backend import db
from backend.config import TOPICS
from backend.services import llm

PROFILE_KEY = "profile"
TASTE_KEY = "taste"


def default_profile() -> dict:
    return {
        "onboarded": False,
        "interests": [],                       # [{id,text,weight}]
        "topics": {k: 1 for k in TOPICS},      # 0..3
        "muted_keywords": [],
        "muted_outlets": [],
        "followed": [],
        "created_at": db.now_iso(),
    }


async def get_profile() -> dict:
    p = await db.kv_get(PROFILE_KEY)
    base = default_profile()
    if p:
        base.update(p)
        base["topics"] = {**default_profile()["topics"], **(p.get("topics") or {})}
    return base


def _clean_list(xs, limit=60, maxlen=80) -> list[str]:
    out, seen = [], set()
    for x in xs or []:
        s = re.sub(r"\s+", " ", str(x)).strip()[:maxlen]
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out[:limit]


async def save_profile(patch: dict) -> dict:
    p = await get_profile()
    if "interests" in patch:
        items = []
        for it in patch["interests"] or []:
            text = (it.get("text") if isinstance(it, dict) else str(it)) or ""
            text = re.sub(r"\s+", " ", text).strip()[:200]
            if not text:
                continue
            w = int(it.get("weight", 2)) if isinstance(it, dict) else 2
            items.append({"id": (it.get("id") if isinstance(it, dict) and it.get("id") else db.new_id()),
                          "text": text, "weight": max(1, min(3, w))})
        p["interests"] = items[:24]
    if "topics" in patch:
        for k, v in (patch["topics"] or {}).items():
            if k in TOPICS:
                p["topics"][k] = max(0, min(3, int(v)))
    for key in ("muted_keywords", "muted_outlets", "followed"):
        if key in patch:
            p[key] = _clean_list(patch[key])
    if "onboarded" in patch:
        p["onboarded"] = bool(patch["onboarded"])
    await db.kv_set(PROFILE_KEY, p)
    return p


def split_interest_text(text: str) -> list[dict]:
    """'AI chips, Indian economy; F1 racing' -> interest items (used by onboarding free-text box)."""
    parts = re.split(r"[\n;,]+|\band\b(?=\s+[A-Z])", text or "")
    return [{"text": p.strip(), "weight": 2} for p in parts if len(p.strip()) > 2][:12]


def profile_brief(p: dict) -> str:
    """Compact natural-language description of the reader for LLM prompts."""
    lines = []
    if p["interests"]:
        lines.append("Reader's specific interests: " + "; ".join(
            f"{i['text']}{' (high priority)' if i['weight'] >= 3 else ''}" for i in p["interests"]))
    high = [TOPICS[k]["label"] for k, v in p["topics"].items() if v >= 2 and k in TOPICS]
    normal = [TOPICS[k]["label"] for k, v in p["topics"].items() if v == 1 and k in TOPICS]
    off = [TOPICS[k]["label"] for k, v in p["topics"].items() if v == 0 and k in TOPICS]
    if high:
        lines.append("Topics they follow closely: " + ", ".join(high))
    if normal:
        lines.append("Topics they follow generally: " + ", ".join(normal))
    if off:
        lines.append("Topics they do NOT want: " + ", ".join(off))
    if p["followed"]:
        lines.append("Specifically follows: " + ", ".join(p["followed"]))
    if p["muted_keywords"]:
        lines.append("Wants to avoid: " + ", ".join(p["muted_keywords"]))
    return "\n".join(lines) or "General news reader with broad interests."


# ── scoring ──────────────────────────────────────────────────────────────────

def calibrate(sim: float) -> float:
    """Map raw doc<->query cosine to 0..1. Measured on ~600 live news clusters with nomic-v1.5: the median cluster
    scores ~0.60 against any interest, off-topic stories ~0.65-0.67 (p90), genuine matches >= 0.70 (p95+)."""
    return float(np.clip((sim - 0.62) / (0.76 - 0.62), 0.0, 1.0))


async def interest_vectors(p: dict) -> tuple[list[dict], Optional[np.ndarray]]:
    items = [i for i in p["interests"] if i["text"]]
    if not items:
        return [], None
    vecs = await llm.embed([i["text"] for i in items], kind="query")
    return items, vecs


def match_cluster(centroid: np.ndarray, items: list[dict], ivecs: Optional[np.ndarray]) -> tuple[float, str]:
    """Best weighted interest match for one cluster. Returns (0..1, matched interest text)."""
    if ivecs is None or not len(items) or ivecs.shape[1] != centroid.shape[0]:
        return 0.0, ""
    sims = ivecs @ centroid
    best, best_txt = 0.0, ""
    for s, it in zip(sims, items):
        m = calibrate(float(s)) * (0.85 + 0.075 * it["weight"])
        if m > best:
            best, best_txt = m, it["text"]
    return min(best, 1.0), best_txt


def keyword_hit(text: str, words: list[str]) -> Optional[str]:
    low = text.lower()
    for w in words:
        if w and re.search(r"(?<!\w)" + re.escape(w.lower()) + r"(?!\w)", low):
            return w
    return None


# ── taste (Rocchio) ──────────────────────────────────────────────────────────

def _enc(v: Optional[np.ndarray]) -> Optional[str]:
    return base64.b64encode(np.asarray(v, dtype=np.float32).tobytes()).decode() if v is not None else None


def _dec(s: Optional[str]) -> Optional[np.ndarray]:
    return np.frombuffer(base64.b64decode(s), dtype=np.float32) if s else None


async def get_taste() -> tuple[Optional[np.ndarray], int]:
    t = await db.kv_get(TASTE_KEY) or {}
    pos, neg = _dec(t.get("pos")), _dec(t.get("neg"))
    if pos is None and neg is None:
        return None, 0
    dim = (pos if pos is not None else neg).shape[0]
    vec = np.zeros(dim, dtype=np.float32)
    if pos is not None:
        vec += pos / (np.linalg.norm(pos) or 1.0)
    if neg is not None:
        vec -= 0.6 * neg / (np.linalg.norm(neg) or 1.0)
    n = np.linalg.norm(vec)
    return (vec / n if n else None), int(t.get("n", 0))


async def apply_feedback(story_id: str, action: str) -> None:
    """action: like | save (positive) or dislike | dismiss (negative). Stored at the cluster/headline level."""
    row = await db.fetch_one(
        "SELECT s.headline, c.centroid FROM stories s LEFT JOIN clusters c ON c.id=s.cluster_id WHERE s.id=?",
        (story_id,))
    if not row:
        return
    v = llm.from_blob(row["centroid"])
    if v is None:
        try:
            v = (await llm.embed([row["headline"]], kind="document"))[0]
        except llm.LLMError:
            return                          # taste learning is best-effort; never block the user's action
    t = await db.kv_get(TASTE_KEY) or {}
    key = "pos" if action in ("like", "save") else "neg"
    cur = _dec(t.get(key))
    if cur is not None and cur.shape != v.shape:
        cur = None
    decayed = (cur * 0.95 if cur is not None else 0) + v
    t[key] = _enc(decayed)
    t["n"] = int(t.get("n", 0)) + 1
    await db.kv_set(TASTE_KEY, t)


async def reset_taste() -> None:
    await db.kv_set(TASTE_KEY, {})
