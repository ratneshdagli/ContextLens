"""Newspaper upload (PDF), and 'explain this link / text'."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from backend import db
from backend.services import extract, paper, pipeline, runs

router = APIRouter(prefix="/api", tags=["papers"])
MAX_PDF = 60 * 1024 * 1024


@router.post("/papers")
async def upload_paper(file: UploadFile = File(...), explain_top: int = 5):
    name = file.filename or "paper.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")
    data = await file.read()
    if len(data) > MAX_PDF:
        raise HTTPException(413, "That PDF is larger than 60 MB.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "That file does not look like a PDF.")
    pid = db.new_id()
    await db.execute("INSERT INTO papers(id,name,filename,uploaded_at,status) VALUES(?,?,?,?,?)",
                     (pid, re.sub(r"\.pdf$", "", name, flags=re.I)[:80], name[:200], db.now_iso(), "processing"))
    run = await runs.Run.start("paper", name[:60])
    asyncio.create_task(paper.process_paper(run, pid, data, name, max(0, min(explain_top, 12))))
    return {"paper_id": pid, "run_id": run.id}


@router.get("/papers")
async def list_papers():
    rows = await db.fetch_all("SELECT id,name,filename,uploaded_at,pages,article_count,matched_count,status,stats,error FROM papers "
                              "ORDER BY uploaded_at DESC LIMIT 30")
    return [{**r, "stats": db.jload(r["stats"], {})} for r in rows]


@router.get("/papers/{paper_id}")
async def get_paper(paper_id: str):
    p = await db.fetch_one("SELECT * FROM papers WHERE id=?", (paper_id,))
    if not p:
        raise HTTPException(404, "Not found")
    rows = await db.fetch_all(
        "SELECT a.id,a.page,a.headline,a.words,a.topic,a.interest,a.kind,a.skip_reason,a.story_id,substr(a.body,1,280) AS lead,"
        "s.tier AS story_tier,s.built_at,json_extract(s.extras,'$.explain.gist') AS gist "
        "FROM paper_articles a LEFT JOIN stories s ON s.id=a.story_id WHERE a.paper_id=? ORDER BY a.interest DESC, a.page", (paper_id,))
    arts = [{**r, "built": bool(r["built_at"]) and r["story_tier"] in ("primary", "more"),
             "building": bool(r["story_id"]) and r["story_id"] in pipeline._building} for r in rows]
    return {**p, "stats": db.jload(p["stats"], {}),
            "matched": [a for a in arts if a["kind"] == "article" and not a["skip_reason"]],
            "other": [a for a in arts if a["kind"] == "article" and a["skip_reason"]],
            "skipped": [a for a in arts if a["kind"] == "skipped"]}


@router.delete("/papers/{paper_id}")
async def delete_paper(paper_id: str):
    sids = [r["story_id"] for r in await db.fetch_all("SELECT story_id FROM paper_articles WHERE paper_id=? AND story_id IS NOT NULL", (paper_id,))]
    for sid in sids:
        for tbl in ("story_sources", "timeline_events", "entities", "relations", "kb_mentions", "thread_stories", "messages", "dives"):
            await db.execute(f"DELETE FROM {tbl} WHERE story_id=?", (sid,))
        await db.execute("DELETE FROM stories WHERE id=?", (sid,))
    await db.execute("DELETE FROM paper_articles WHERE paper_id=?", (paper_id,))
    await db.execute("DELETE FROM papers WHERE id=?", (paper_id,))
    return {"ok": True}


@router.post("/papers/{paper_id}/articles/{article_id}/open")
async def open_article(paper_id: str, article_id: str):
    sid = await paper.story_for_paper_article(paper_id, article_id)
    if not sid:
        raise HTTPException(404, "Not found")
    return {"story_id": sid}


# ── explain a link or pasted text ─────────────────────────────────────────────

class ExplainIn(BaseModel):
    url: str = ""
    text: str = ""
    title: str = ""


def _public_http(url: str) -> bool:
    """Only fetch public http(s) hosts (never localhost / private networks)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    try:
        for fam, _, _, _, addr in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(addr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except Exception:
        return False
    return True


@router.post("/explain")
async def explain_link(body: ExplainIn):
    url, text, title = body.url.strip(), body.text.strip(), body.title.strip()
    outlet, ref = "Pasted text", ""
    if url:
        if not _public_http(url):
            raise HTTPException(400, "Enter a public http(s) link.")
        html = await extract._fetch(url)
        if not html:
            raise HTTPException(422, "Could not open that link. Paste the article text instead.")
        loop = asyncio.get_running_loop()
        text, paywall = await loop.run_in_executor(None, extract.extract_text, html)
        soup = BeautifulSoup(html, "lxml")
        og = soup.find("meta", property="og:title")
        title = title or (og.get("content") if og and og.get("content") else (soup.title.string if soup.title and soup.title.string else ""))
        title = re.sub(r"\s+[-|–—]\s+[^-|–—]{2,40}$", "", (title or "").strip())
        outlet = urlparse(url).hostname.replace("www.", "") if urlparse(url).hostname else "Link"
        ref = url
        if paywall or len(text) < 400:
            raise HTTPException(422, "That page is behind a paywall or has no readable article text. Paste the text instead.")
    else:
        if len(text) < 300:
            raise HTTPException(400, "Paste at least a few paragraphs of the article.")
        if not title:
            first = re.split(r"(?<=[.!?])\s|\n", text, 1)[0]
            title = first[:120]
        ref = "pasted"
    stamp = db.new_id()
    sid = await paper.create_story_from_text(title[:180] or "Untitled article", text[:20000], outlet=outlet,
                                             url=url or f"pasted://{stamp}", kind="link", source_ref=ref or stamp)
    asyncio.create_task(pipeline.build_now(sid))
    for _ in range(60):
        await asyncio.sleep(0.05)
        if sid in pipeline._build_runs:
            break
    return {"story_id": sid, "run_id": pipeline._build_runs.get(sid)}
