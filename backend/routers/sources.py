"""Where the news comes from: every outlet the app reads, grouped, with live counts. Powers the Sources page."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from backend import db
from backend.config import FEEDS, TRUSTED_SOURCES

router = APIRouter(prefix="/api", tags=["sources"])

TIERS = {1: {"label": "Top tier", "desc": "Wire services, papers of record and official bodies"},
         2: {"label": "Established", "desc": "Well-known newsrooms and fact-checkers with a track record"},
         3: {"label": "Specialist", "desc": "Strong in one field; read alongside others"}}

GROUPS = [
    ("factcheck", "Fact-checkers", "They test viral claims. Their findings are shown next to the story they debunk."),
    ("official", "Primary sources", "Governments, regulators and institutions speaking for themselves."),
    ("india", "India", "National and city papers, broadcasters and digital newsrooms."),
    ("explainer", "Explainers & analysis", "Outlets whose job is context: why, how and what next."),
    ("world", "World press", "Wires, broadcasters and papers across the Americas, Europe, Asia and Africa."),
    ("business", "Business & markets", "Economy, companies and money."),
    ("tech", "Technology", "Products, platforms, policy and startups."),
    ("science", "Science, health & climate", "Research, medicine and the environment."),
    ("culture", "Sports & culture", "Cricket, football, film and music."),
]


def _norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]", "", (name or "").lower())
    return re.sub(r"^the ", "", n).strip()


def _group_of(s: dict) -> str:
    t, c = s.get("type", ""), s.get("country", "")
    if t == "fact-checker":
        return "factcheck"
    if t == "official":
        return "official"
    if t == "explainer" or s["name"] in ("Vox", "The Atlantic", "Foreign Policy", "The Diplomat", "Foreign Affairs"):
        return "explainer"
    if t in ("financial",):
        return "india" if c == "IN" else "business"
    if t in ("tech",):
        return "tech"
    if t in ("scientific", "health", "research", "academic"):
        return "science"
    if t in ("sports", "culture"):
        return "culture"
    return "india" if c == "IN" else "world"


@router.get("/sources/overview")
async def sources_overview():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    arts = await db.fetch_all("SELECT outlet, COUNT(*) n, MAX(published_at) last FROM articles WHERE published_at >= ? GROUP BY outlet", (cutoff,))
    art_by = {_norm(a["outlet"]): a for a in arts}
    topics_by: dict[str, set] = defaultdict(set)
    feeds_by: dict[str, int] = defaultdict(int)
    for _, outlet, topic in FEEDS:
        feeds_by[_norm(outlet)] += 1
        topics_by[_norm(outlet)].add(topic)
    seen: dict[str, dict] = {}
    for s in TRUSTED_SOURCES:                       # one row per outlet (several domains can map to one name)
        k = _norm(s["name"])
        if k not in seen or s["trust_rank"] < seen[k]["trust_rank"]:
            seen[k] = s
    out: dict[str, list] = defaultdict(list)
    for k, s in seen.items():
        a = art_by.get(k)
        if not feeds_by.get(k) and not a:
            continue                                # listed for trust lookups only; nothing is read from it
        out[_group_of(s)].append({"name": s["name"], "domain": s["domain"], "country": s.get("country", ""), "type": s.get("type", ""),
                                  "trust_rank": s["trust_rank"], "feeds": feeds_by.get(k, 0), "articles": a["n"] if a else 0,
                                  "last_at": a["last"] if a else None, "topics": sorted(topics_by.get(k, []))})
    groups = []
    for key, label, desc in GROUPS:
        items = sorted(out.get(key, []), key=lambda o: (o["trust_rank"], -o["articles"], o["name"]))
        if items:
            groups.append({"key": key, "label": label, "desc": desc, "outlets": items})
    n_out = sum(len(g["outlets"]) for g in groups)
    return {"totals": {"outlets": n_out, "feeds": len(FEEDS), "articles_72h": sum(a["n"] for a in arts),
                       "countries": len({o["country"] for g in groups for o in g["outlets"] if o["country"]})},
            "tiers": [{"rank": k, **v} for k, v in TIERS.items()], "groups": groups}
