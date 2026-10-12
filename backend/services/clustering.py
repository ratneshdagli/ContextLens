"""ContextLens v3 — event clustering.

Groups articles that report the *same event* across outlets. Cluster breadth (number of
independent outlets) is the strongest cheap signal that something is actually news.

Merge rule (pure, unit-tested): cosine(article, centroid) >= HI            -> merge
                                cosine >= LO and titles share an anchor    -> merge
Anchors are proper-noun-ish tokens and numbers; they stop "two different earthquakes"
or "two different court rulings" from being merged on semantics alone.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from backend import db
from backend.services import llm

SIM_HI = 0.90
SIM_LO = 0.79

_STOP = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "with", "as", "by", "from", "after", "over",
    "into", "amid", "says", "say", "said", "new", "how", "why", "what", "who", "when", "will", "could", "would",
    "may", "more", "than", "not", "is", "are", "was", "were", "its", "his", "her", "their", "this", "that", "has",
    "have", "had", "about", "up", "out", "off", "its", "you", "your", "can", "all", "one", "two", "first", "last",
    "us", "uk", "year", "years", "day", "week", "news", "report", "reports", "latest", "live", "update", "updates",
}


_HYPHEN = re.compile(r"(?<=\w)[-‐-―](?=\w)")


def anchors(title: str) -> set[str]:
    toks = re.findall(r"[A-Za-z][A-Za-z'’\-]+|\d[\d,.%]*", _HYPHEN.sub(" ", title or ""))   # "IIT-Bombay" == "IIT Bombay"
    out: set[str] = set()
    for i, t in enumerate(toks):
        low = t.lower().strip("'’-")
        if low in _STOP or len(low) < 3 and not t.isdigit():
            continue
        if t[0].isdigit():
            out.add(re.sub(r"[,.%]", "", t))
        elif t[0].isupper() and i > 0 or t.isupper() and len(t) > 1:
            out.add(low.replace("’s", "").replace("'s", ""))
        elif i == 0 and t[0].isupper() and len(low) > 5:
            out.add(low)
    return {a for a in out if a}


def should_merge(sim: float, title_a: str, title_b: str, generic: frozenset = frozenset()) -> bool:
    """Semantic similarity alone only merges when it is very high; otherwise the titles must share
    a *specific* anchor (not a ubiquitous one like 'trump'/'iran') or at least two anchors."""
    if sim >= SIM_HI:
        return True
    if sim >= SIM_LO:
        shared = anchors(title_a) & anchors(title_b)
        specific = shared - generic
        return bool(specific) or len(shared) >= 2
    return False


# ── consolidation: merge clusters that turned out to be the same story ─────────
# Articles join an existing cluster one by one, so two clusters that formed independently ("Zoho founder backs the
# IIT Bombay professor" / "IIT Bombay faculty march") are never compared again. Centroid-to-centroid similarity is
# inflated (averaged vectors), so the thresholds are high and the shared *names* decide the middle band.
MERGE_HI = 0.96          # near-identical coverage
MERGE_MID = 0.89         # same story if they share one rare name (a person, place, product, case)
MERGE_LO = 0.86          # same story if they share two rare names


def title_df(titles: list[str]) -> Counter:
    df: Counter = Counter()
    for t in titles:
        df.update(anchors(t))
    return df


def should_consolidate(sim: float, title_a: str, title_b: str, df: Counter, n_titles: int) -> bool:
    if sim >= MERGE_HI:
        return True
    if sim < MERGE_LO:
        return False
    rare_df = max(4, round(0.007 * n_titles))
    rare = {t for t in anchors(title_a) & anchors(title_b) if df.get(t, 0) <= rare_df}
    return len(rare) >= 2 or (sim >= MERGE_MID and len(rare) >= 1)


def plan_merges(items: list[dict]) -> list[tuple[str, list[str]]]:
    """items: [{id,title,vec,rank}] — higher `rank` becomes the surviving cluster. Each survivor only absorbs clusters
    that match *it* (never through a chain of other clusters), so unrelated stories cannot snowball into one."""
    items = [i for i in items if i.get("vec") is not None]
    if len(items) < 2:
        return []
    dim = Counter(i["vec"].shape[0] for i in items).most_common(1)[0][0]
    items = sorted((i for i in items if i["vec"].shape[0] == dim), key=lambda i: i["rank"], reverse=True)
    sims = np.stack([i["vec"] for i in items]) @ np.stack([i["vec"] for i in items]).T
    df = title_df([i["title"] for i in items])
    used: set[int] = set()
    plan = []
    for a in range(len(items)):
        if a in used:
            continue
        absorbed = [b for b in range(a + 1, len(items)) if b not in used and sims[a, b] >= MERGE_LO
                    and should_consolidate(float(sims[a, b]), items[a]["title"], items[b]["title"], df, len(items))]
        if absorbed:
            used.add(a)
            used.update(absorbed)
            plan.append((items[a]["id"], [items[b]["id"] for b in absorbed]))
    return plan


def _centroid(vs: list[np.ndarray]) -> np.ndarray:
    c = np.mean(np.stack(vs), axis=0)
    n = np.linalg.norm(c)
    return c / n if n else c


def generic_anchors(titles: list[str], min_df: int = 8, frac: float = 0.012) -> frozenset:
    df: Counter = Counter()
    for t in titles:
        df.update(anchors(t))
    n = max(len(titles), 1)
    return frozenset(a for a, c in df.items() if c >= min_df and c / n >= frac)


def cluster_articles(articles: list[dict], clusters: list[dict]) -> dict[str, str]:
    """Pure function. `articles`: dicts with id,title,embedding(np),published_at.
    `clusters`: existing dicts with id,title,centroid(np). Returns {article_id: cluster_id}.
    New clusters get ids 'new:<n>' which the caller replaces.
    A candidate must match the cluster centroid AND its seed article (prevents centroid drift/chaining)."""
    generic = generic_anchors([a["title"] for a in articles] + [c["title"] for c in clusters])
    cents = [{"id": c["id"], "title": c["title"], "vecs": [c["centroid"]], "centroid": c["centroid"],
              "seed": c["centroid"], "n": 1} for c in clusters]
    assign: dict[str, str] = {}
    n_new = 0
    for a in sorted(articles, key=lambda x: x.get("published_at") or ""):
        v = a["embedding"]
        best, best_sim = None, -1.0
        for c in cents:
            if c["centroid"].shape != v.shape:
                continue
            s = float(v @ c["centroid"])
            if s <= best_sim or float(v @ c["seed"]) < SIM_LO - 0.03:
                continue
            if should_merge(s, a["title"], c["title"], generic):
                best, best_sim = c, s
        if best is None:
            n_new += 1
            cid = f"new:{n_new}"
            cents.append({"id": cid, "title": a["title"], "vecs": [v], "centroid": v, "seed": v, "n": 1})
            assign[a["id"]] = cid
        else:
            best["vecs"].append(v)
            best["centroid"] = _centroid(best["vecs"])
            assign[a["id"]] = best["id"]
    return assign


async def consolidate(window_hours: int) -> dict:
    """Merge clusters that report one story. The survivor keeps its story (a built one wins), takes over the other
    clusters' articles, and remembers their headlines as "developments". A duplicate story is retired, never deleted
    outright (threads and links may still point at it)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    rows = await db.fetch_all(
        "SELECT c.id,c.title,c.centroid,c.outlet_count,c.article_count,c.story_id,c.merged,s.tier,s.built_at "
        "FROM clusters c LEFT JOIN stories s ON s.id=c.story_id WHERE c.last_seen >= ? AND c.centroid IS NOT NULL", (cutoff,))
    by_id = {r["id"]: r for r in rows}
    items = []
    for r in rows:
        built = r["tier"] in ("primary", "more") and bool(r["built_at"])
        has_story = bool(r["story_id"]) and r["tier"] != "rejected"
        items.append({"id": r["id"], "title": r["title"], "vec": llm.from_blob(r["centroid"]),
                      "rank": (2 if built else 1 if has_story else 0, r["outlet_count"], r["article_count"])})
    plan = await asyncio.to_thread(plan_merges, items)
    if not plan:
        return {"groups": 0, "absorbed": 0}
    absorbed_n = 0
    async with db.get_db() as conn:
        for seed_id, gone in plan:
            seed = by_id[seed_id]
            updates = list(db.jload(seed["merged"], []) or [])
            for gid in gone:
                g = by_id[gid]
                best = await (await conn.execute(
                    "SELECT title,outlet,url,published_at FROM articles WHERE cluster_id=? ORDER BY trust_rank, LENGTH(title) DESC LIMIT 1",
                    (gid,))).fetchone()
                if best:
                    updates.append({"title": best[0], "outlet": best[1], "url": best[2], "at": best[3], "n": g["article_count"]})
                updates.extend(db.jload(g["merged"], []) or [])
                if g["story_id"] and g["story_id"] != seed["story_id"]:
                    await conn.execute(
                        "UPDATE stories SET tier='rejected', cluster_id=NULL, updated_at=?, "
                        "gate=json_set(COALESCE(gate,'{}'),'$.merged_into',?) WHERE id=? AND kind='feed'",
                        (db.now_iso(), seed["story_id"] or seed_id, g["story_id"]))
                await conn.execute("UPDATE articles SET cluster_id=? WHERE cluster_id=?", (seed_id, gid))
                await conn.execute("DELETE FROM clusters WHERE id=?", (gid,))
                absorbed_n += 1
            mem = await (await conn.execute(
                "SELECT embedding,domain,outlet,published_at FROM articles WHERE cluster_id=?", (seed_id,))).fetchall()
            vecs = [v for v in (llm.from_blob(m[0]) for m in mem if m[0]) if v is not None and v.shape == items[0]["vec"].shape]
            pubs = sorted(m[3] for m in mem if m[3])
            outlets = {m[1] or m[2] for m in mem}
            await conn.execute(
                "UPDATE clusters SET outlet_count=?, article_count=?, first_seen=COALESCE(?,first_seen), last_seen=COALESCE(?,last_seen), "
                "centroid=COALESCE(?,centroid), merged=? WHERE id=?",
                (len(outlets), len(mem), pubs[0] if pubs else None, pubs[-1] if pubs else None,
                 llm.to_blob(_centroid(vecs)) if vecs else None, db.jdump(updates[-12:]), seed_id))
            if seed["story_id"] and seed["tier"] in ("primary", "more"):
                # the built story predates these articles: have the queue rewrite it with all of them
                await conn.execute("UPDATE stories SET gate=json_set(COALESCE(gate,'{}'),'$.rebuild',1) WHERE id=?", (seed["story_id"],))
        await conn.commit()
    return {"groups": len(plan), "absorbed": absorbed_n}


async def embed_new_articles(window_hours: int) -> int:
    """Embed articles in the window that lack an embedding. Returns count embedded."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    rows = await db.fetch_all(
        "SELECT id,title,snippet FROM articles WHERE embedding IS NULL AND published_at >= ?", (cutoff,))
    if not rows:
        return 0
    texts = [f"{r['title']}. {r['snippet'][:240]}" for r in rows]
    vecs = await llm.embed(texts, kind="document")
    await db.executemany("UPDATE articles SET embedding=? WHERE id=?",
                         [(llm.to_blob(v), r["id"]) for v, r in zip(vecs, rows)])
    return len(rows)


async def run_clustering(window_hours: int) -> dict:
    """Assign every windowed article to a cluster (incrementally, IDs stay stable) and refresh stats."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    arts = await db.fetch_all(
        "SELECT id,title,outlet,domain,trust_rank,published_at,embedding,cluster_id,topic FROM articles "
        "WHERE published_at >= ? AND embedding IS NOT NULL", (cutoff,))
    for a in arts:
        a["embedding"] = llm.from_blob(a["embedding"])
    if not arts:
        return {"articles": 0, "clusters": 0, "new_clusters": 0}
    dim = Counter(a["embedding"].shape[0] for a in arts).most_common(1)[0][0]
    arts = [a for a in arts if a["embedding"].shape[0] == dim]

    existing_rows = await db.fetch_all("SELECT id,title,centroid FROM clusters WHERE last_seen >= ?", (cutoff,))
    existing = []
    for c in existing_rows:
        v = llm.from_blob(c["centroid"])
        if v is not None and v.shape[0] == dim:
            existing.append({"id": c["id"], "title": c["title"], "centroid": v})
    keep = {c["id"] for c in existing}

    already = [a for a in arts if a["cluster_id"] in keep]
    todo = [a for a in arts if a["cluster_id"] not in keep]
    assign = await asyncio.to_thread(cluster_articles, todo, existing)     # pure CPU: keep the event loop (and the API) responsive

    # Materialise new clusters
    remap: dict[str, str] = {}
    for a in todo:
        cid = assign[a["id"]]
        if cid.startswith("new:") and cid not in remap:
            remap[cid] = db.new_id()
        a["cluster_id"] = remap.get(cid, cid)
    members: dict[str, list[dict]] = {}
    for a in already + todo:
        members.setdefault(a["cluster_id"], []).append(a)

    now = db.now_iso()
    async with db.get_db() as conn:
        for cid, ms in members.items():
            best = sorted(ms, key=lambda m: (m["trust_rank"], -len(m["title"])))[0]
            cent = _centroid([m["embedding"] for m in ms])
            outlets = {m["domain"] or m["outlet"] for m in ms}
            pubs = sorted(m["published_at"] for m in ms if m["published_at"])
            topic = Counter(m["topic"] for m in ms).most_common(1)[0][0]
            await conn.execute(
                "INSERT INTO clusters(id,title,topic,first_seen,last_seen,outlet_count,article_count,centroid) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, topic=excluded.topic, "
                "last_seen=excluded.last_seen, outlet_count=excluded.outlet_count, article_count=excluded.article_count, "
                "centroid=excluded.centroid",
                (cid, best["title"], topic, pubs[0] if pubs else now, pubs[-1] if pubs else now,
                 len(outlets), len(ms), llm.to_blob(cent)))
        await conn.executemany("UPDATE articles SET cluster_id=? WHERE id=?",
                               [(a["cluster_id"], a["id"]) for a in todo])
        await conn.commit()
    merged = await consolidate(window_hours)
    return {"articles": len(arts), "clusters": len(members) - merged["absorbed"], "new_clusters": len(remap), "merged": merged["absorbed"]}
