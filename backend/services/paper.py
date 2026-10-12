"""ContextLens v4 — newspaper upload: PDF → articles → what matters to you → explained stories linked into the knowledge base.

  1. parse       PyMuPDF text blocks with font sizes and positions
  2. split       headlines = blocks noticeably larger than body text; every body block belongs to the nearest headline
                 above it whose horizontal span covers it (this is how newspaper columns work)
  3. filter      classifieds, tenders, horoscopes, puzzles, ads and stubs are skipped (and listed, with the reason)
  4. score       embeddings vs. your interests + topic weights + followed/muted keywords (same rules as the feed)
  5. explain     the best few are built into full stories (background search, timeline, why-chain); every other article
                 can be opened and explained on demand
Also: `create_story_from_text` turns any pasted link/text into the same kind of story ("explain this article").
Scanned/image-only PDFs have no text layer; those are reported clearly (no OCR is bundled).
"""

from __future__ import annotations

import asyncio
import logging
import re
import statistics
import traceback
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from backend import db
from backend.config import TOPICS
from backend.services import feeds, interests, kb, llm, runs
from backend.services.llm import LLMError

log = logging.getLogger("paper")

SKIP_STRONG = re.compile(
    r"\b(classifieds?|matrimonial|obituar\w+|horoscope|astrology|tenders?|public notice|situations? vacant|sudoku|crossword|"
    r"panchang|stock (?:table|quotes)|lottery)\b", re.I)
SKIP_WEAK = re.compile(r"\b(sale|offers?|discount|packages?|admission open|wanted|for sale|to let|tickets?|weather)\b", re.I)
MIN_WORDS = 70
_topic_vecs: Optional[tuple[list[str], np.ndarray]] = None


# ── 1+2: PDF → articles ───────────────────────────────────────────────────────

def _blocks(page) -> list[dict]:
    out = []
    for b in page.get_text("dict").get("blocks", []):
        if b.get("type") != 0:
            continue
        chars, sized, bold, parts = 0, 0.0, 0, []
        for ln in b.get("lines", []):
            t = "".join(sp["text"] for sp in ln.get("spans", [])).strip()
            if not t:
                continue
            parts.append(t)
            for sp in ln.get("spans", []):
                n = len(sp["text"].strip())
                chars += n
                sized += sp["size"] * n
                bold += n if (sp["flags"] & 16 or "bold" in sp.get("font", "").lower()) else 0
        if not chars:
            continue
        text = re.sub(r"-\s+(?=[a-z])", "", " ".join(parts))
        x0, y0, x1, y1 = b["bbox"]
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "size": sized / chars, "bold": bold / chars > 0.6,
                    "text": re.sub(r"\s+", " ", text).strip(), "chars": chars})
    return out


def _overlap(a: dict, b: dict) -> float:
    w = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
    return max(0.0, w) / max(1.0, min(a["x1"] - a["x0"], b["x1"] - b["x0"]))


def _reading_order(blocks: list[dict]) -> list[dict]:
    """Columns left→right, top→bottom within a column."""
    cols: list[float] = []
    for b in sorted(blocks, key=lambda b: b["x0"]):
        if not cols or b["x0"] - cols[-1] > 18:
            cols.append(b["x0"])
    def col(b):
        return min(range(len(cols)), key=lambda i: abs(cols[i] - b["x0"]))
    return sorted(blocks, key=lambda b: (col(b), b["y0"]))


def segment_pdf(data: bytes, filename: str = "") -> dict:
    """Blocking (run in a thread). Returns {name, date, pages, articles:[{page,headline,body,words}], skipped:[...], has_text}."""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        import fitz as pymupdf
    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = []
    sizes: dict[int, int] = {}
    for pno, page in enumerate(doc, 1):
        h = page.rect.height
        raw = _blocks(page)
        bl = [b for b in raw if not ((b["y0"] < 0.045 * h or b["y1"] > 0.965 * h) and b["chars"] < 70)]
        pages.append((pno, page.rect, bl, raw))
        for b in bl:
            sizes[round(b["size"])] = sizes.get(round(b["size"]), 0) + b["chars"]
    total_chars = sum(sizes.values())
    if total_chars < 400:
        return {"name": "", "date": None, "pages": len(pages), "articles": [], "skipped": [], "has_text": False}
    body = max(sizes, key=sizes.get)
    body = float(body) if body else 9.0

    articles, skipped = [], []
    name = ""
    for pno, rect, bl, raw in pages:
        heads = [b for b in bl if (b["size"] >= 1.32 * body and 2 <= len(b["text"].split()) <= 26 and b["chars"] >= 10)
                 or (b["bold"] and b["size"] >= 1.15 * body and 3 <= len(b["text"].split()) <= 18 and b["chars"] <= 140
                     and not b["text"].rstrip().endswith((".", "?")))]
        if pno == 1:                                              # masthead: the biggest short text on page 1
            big = max(raw, key=lambda b: b["size"], default=None)             # the masthead often sits in the header band
            if big and len(big["text"].split()) <= 5 and big["size"] >= 2.2 * body:
                name = big["text"].title() if big["text"].isupper() else big["text"]
                heads = [h for h in heads if h is not big]
        merged: list[dict] = []
        for h in sorted(heads, key=lambda b: (b["y0"], b["x0"])):
            m = next((x for x in merged if 0 <= h["y0"] - x["y1"] <= 0.9 * max(x["size"], h["size"]) and _overlap(x, h) >= 0.6), None)
            if m:
                m["text"] += " " + h["text"]
                m["y1"], m["x0"], m["x1"] = h["y1"], min(m["x0"], h["x0"]), max(m["x1"], h["x1"])
            else:
                merged.append(dict(h))
        headset = {id(h) for h in heads}
        groups: dict[int, list[dict]] = {i: [] for i in range(len(merged))}
        for b in bl:
            if id(b) in headset or b["size"] < 0.72 * body or b["size"] > 1.3 * body:
                continue
            best, best_gap = None, 1e9
            for i, h in enumerate(merged):
                gap = b["y0"] - h["y1"]
                if gap >= -3 and _overlap(h, b) >= 0.45 and gap < best_gap:
                    best, best_gap = i, gap
            if best is not None and best_gap < 0.55 * rect.height:
                groups[best].append(b)
        for i, h in enumerate(merged):
            text = " ".join(b["text"] for b in _reading_order(groups[i]))
            words = len(text.split())
            head = re.sub(r"\s+", " ", h["text"]).strip()
            rec = {"page": pno, "headline": head, "body": text, "words": words}
            if SKIP_STRONG.search(head) or (words < 120 and SKIP_WEAK.search(head)):
                skipped.append({**rec, "reason": "advertisement / classified / puzzle"})
            elif words < MIN_WORDS:
                skipped.append({**rec, "reason": "too short to explain (photo caption, brief or stub)"})
            else:
                articles.append(rec)
    date = None
    first = " ".join(b["text"] for b in (pages[0][3] if pages else []))[:1500]
    m = re.search(r"\b(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+20\d{2}|"
                  r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2})", first, re.I)
    if m:
        import dateparser
        d = dateparser.parse(m.group(1))
        date = d.date().isoformat() if d else None
    if not name:
        name = re.sub(r"[_\-]+", " ", re.sub(r"\.pdf$", "", filename, flags=re.I)).strip().title() or "Uploaded newspaper"
    return {"name": name[:80], "date": date, "pages": len(pages), "articles": articles, "skipped": skipped, "has_text": True}


# ── 3+4: classify + score ─────────────────────────────────────────────────────

async def _topic_vectors() -> tuple[list[str], np.ndarray]:
    global _topic_vecs
    if _topic_vecs is None:
        keys = list(TOPICS)
        v = await llm.embed([f"{TOPICS[k]['label']}: {TOPICS[k]['desc']}" for k in keys], kind="query")
        _topic_vecs = (keys, v)
    return _topic_vecs


async def classify_topic(vec: np.ndarray) -> str:
    keys, tv = await _topic_vectors()
    if tv.shape[1] != vec.shape[0]:
        return "world"
    return keys[int(np.argmax(tv @ vec))]


async def score_article(vec: np.ndarray, headline: str, body: str, profile: dict, items, ivecs) -> dict:
    text = f"{headline} {body[:1200]}"
    topic = await classify_topic(vec)
    muted = interests.keyword_hit(text, profile["muted_keywords"])
    if muted:
        return {"score": 0.0, "topic": topic, "interest": 0.0, "excluded": f"muted keyword '{muted}'"}
    interest, matched = interests.match_cluster(vec, items, ivecs)
    followed = interests.keyword_hit(text, profile["followed"])
    if followed:
        interest = max(interest, 0.85)
    if not items and not followed:
        interest = 0.35
    tw = profile["topics"].get(topic, 1)
    if tw == 0 and interest < 0.6:
        return {"score": 0.0, "topic": topic, "interest": interest, "excluded": "topic switched off"}
    topic_s = {0: 0.0, 1: 0.5, 2: 0.8, 3: 1.0}[tw]
    length = min(1.0, len(body.split()) / 500)
    score = 0.6 * interest + 0.25 * topic_s + 0.15 * length
    return {"score": round(score, 3), "topic": topic, "interest": round(interest, 3), "matched": matched, "excluded": None}


# ── stories from text (papers, pasted links, pasted text) ─────────────────────

async def create_story_from_text(headline: str, body: str, *, outlet: str, url: str, kind: str, source_ref: str,
                                 topic: Optional[str] = None, published_at: Optional[str] = None,
                                 interest: float = 0.0, why: str = "") -> str:
    """Create an instant (wire-tier) story for one document. build_now(story_id) then explains it like any feed story."""
    vec = (await llm.embed([f"{headline}. {body[:600]}"]))[0]
    topic = topic or await classify_topic(vec)
    now = db.now_iso()
    pub = published_at or now
    aid, cid, sid = db.new_id(), db.new_id(), db.new_id()
    snippet = re.sub(r"\s+", " ", body).strip()[:260]
    async with db.get_db() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO articles(id,url,url_hash,title,snippet,outlet,domain,trust_rank,topic,published_at,fetched_at,"
            "embedding,cluster_id,full_text,is_paywall) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (aid, url, feeds.url_hash(url), headline[:300], snippet, outlet[:80], "upload", 2, topic, pub, now,
             llm.to_blob(vec), cid, body[:20000]))
        await conn.execute(
            "INSERT INTO clusters(id,title,topic,first_seen,last_seen,outlet_count,article_count,centroid,story_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (cid, headline[:300], topic, pub, pub, 1, 1, llm.to_blob(vec), sid))
        await conn.execute(
            "INSERT INTO stories(id,kind,cluster_id,created_at,updated_at,headline,headline_original,dek,topic,outlet_count,image_url,"
            "tier,gate,status,published_at,why_for_you,matched_interest,interest_match,centroid,reading_minutes,trust_score,source_ref) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, kind, cid, now, now, headline[:180], headline[:300], snippet, topic, 1, "", "wire",
             db.jdump({"wire": True, "rank": {"score": interest, "components": {"interest": interest, "coverage": 0.1, "trust": 0.6}}}),
             "complete", pub, why, "", interest, llm.to_blob(vec), max(1, round(len(body.split()) / 230)), 0.6, source_ref))
        await conn.commit()
    try:
        await kb.assign_thread(sid)
    except Exception as e:  # noqa: BLE001
        log.info("thread assign failed: %s", str(e)[:80])
    return sid


async def story_for_paper_article(paper_id: str, article_id: str) -> Optional[str]:
    a = await db.fetch_one("SELECT * FROM paper_articles WHERE id=? AND paper_id=?", (article_id, paper_id))
    if not a:
        return None
    if a["story_id"] and await db.fetch_one("SELECT 1 FROM stories WHERE id=?", (a["story_id"],)):
        return a["story_id"]
    p = await db.fetch_one("SELECT name,filename,stats FROM papers WHERE id=?", (paper_id,))
    stats = db.jload(p["stats"], {}) or {}
    sid = await create_story_from_text(a["headline"], a["body"], outlet=p["name"], url=f"paper://{paper_id}/{article_id}",
                                       kind="paper", source_ref=paper_id, topic=a["topic"], interest=a["interest"],
                                       published_at=stats.get("date") and f"{stats['date']}T06:00:00+00:00",
                                       why=f"from {p['name']}, page {a['page']}")
    await db.execute("UPDATE paper_articles SET story_id=? WHERE id=?", (sid, article_id))
    return sid


# ── the upload job ────────────────────────────────────────────────────────────

async def process_paper(run: runs.Run, paper_id: str, data: bytes, filename: str, build_top: int = 5) -> None:
    from backend.services import pipeline
    try:
        await run.emit("parse", "Reading the PDF…", progress=0.03)
        loop = asyncio.get_running_loop()
        seg = await loop.run_in_executor(None, segment_pdf, data, filename)
        if not seg["has_text"]:
            raise ValueError("This PDF has no text layer (it looks like a scan). Upload a digital e-paper, or run it through OCR first.")
        arts = seg["articles"]
        await run.emit("split", f"{seg['pages']} pages → {len(arts)} articles ({len(seg['skipped'])} ads/briefs skipped)", "success", progress=0.15)
        if len(arts) < 2:                                           # could not find headline structure → treat as one document
            text = " ".join(a["body"] for a in arts) or ""
            if len(text) < 500:
                raise ValueError("Could not find articles in this PDF. Try 'Paste text' with the article instead.")
        profile = await interests.get_profile()
        items, ivecs = await interests.interest_vectors(profile)
        vecs = await llm.embed([f"{a['headline']}. {a['body'][:500]}" for a in arts]) if arts else np.zeros((0, 1))
        rows, matched = [], 0
        for a, v in zip(arts, vecs):
            sc = await score_article(v, a["headline"], a["body"], profile, items, ivecs)
            aid = db.new_id()
            keep = not sc["excluded"] and sc["score"] >= 0.3
            matched += int(keep)
            rows.append((aid, paper_id, a["page"], a["headline"][:300], a["body"][:20000], a["words"], sc["topic"], sc["interest"],
                         "article", "" if keep else (sc["excluded"] or "below your interest threshold"), None))
            a["_id"], a["_score"], a["_keep"] = aid, sc["score"], keep
        for s in seg["skipped"]:
            rows.append((db.new_id(), paper_id, s["page"], s["headline"][:300], s["body"][:4000], s["words"], "world", 0.0,
                         "skipped", s["reason"], None))
        await db.executemany(
            "INSERT INTO paper_articles(id,paper_id,page,headline,body,words,topic,interest,kind,skip_reason,story_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        await db.execute("UPDATE papers SET name=?, pages=?, article_count=?, matched_count=?, stats=? WHERE id=?",
                         (seg["name"], seg["pages"], len(arts), matched, db.jdump({"date": seg["date"], "skipped": len(seg["skipped"])}), paper_id))
        await run.emit("score", f"{matched} of {len(arts)} articles match your interests", "success", progress=0.25)

        top = sorted([a for a in arts if a["_keep"]], key=lambda a: -a["_score"])[:build_top]
        for i, a in enumerate(top):
            await run.emit("build", f"Explaining {i + 1}/{len(top)}: {a['headline'][:80]}", progress=0.25 + 0.7 * i / max(len(top), 1))
            sid = await story_for_paper_article(paper_id, a["_id"])
            if sid:
                await pipeline.build_now(sid, run=run)
        await db.execute("UPDATE papers SET status='ready' WHERE id=?", (paper_id,))
        await run.emit("done", f"Done — {len(top)} articles explained; open any other article to explain it", "success", progress=1.0)
        await run.finish("complete")
    except Exception as e:  # noqa: BLE001
        log.warning("paper failed: %s", traceback.format_exc()[-500:])
        await db.execute("UPDATE papers SET status='failed', error=? WHERE id=?", (str(e)[:300], paper_id))
        await run.emit("error", str(e)[:200], "error")
        await run.finish("failed", error=str(e)[:200])
