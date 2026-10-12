"""ContextLens v6 — personal digest.

Two kinds of "watch", both answering "what's new that I should know about, beyond the news feed":
  repo  a GitHub project ("owner/name" or its URL) — a weekly LLM digest of that repo's real commits (never invented).
  topic a subject you're learning about (typed, or the topics pulled out of a document you upload) — matched against
        this app's OWN aggregated news by embedding similarity, so it costs no extra LLM call and cannot hallucinate:
        it only ever lists stories that are actually in the database.

Both are cheap and best-effort: GitHub's unauthenticated API is rate-limited, so a watch is only re-checked when
its last digest is stale (`STALE_HOURS`), not on every page view.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from backend import db
from backend.services import llm
from backend.services.llm import LLMError

log = logging.getLogger("watch")

GITHUB_API = "https://api.github.com"
STALE_HOURS = 20
_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "ContextLens (local news reader)"}
REPO_RE = re.compile(r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def parse_repo(text: str) -> Optional[str]:
    m = REPO_RE.match((text or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


# ── adding a watch ──────────────────────────────────────────────────────────

async def _insert(kind: str, label: str, meta: dict) -> dict:
    wid = db.new_id()
    await db.execute("INSERT INTO watches(id,kind,label,target,meta,created_at) VALUES(?,?,?,?,?,?)",
                     (wid, kind, label, label, db.jdump(meta), db.now_iso()))
    return {"id": wid, "kind": kind, "label": label}


async def add_repo(text: str) -> dict:
    repo = parse_repo(text)
    if not repo:
        raise ValueError("Give a GitHub repo as “owner/name” or paste its URL.")
    if await db.fetch_one("SELECT 1 FROM watches WHERE kind='repo' AND target=?", (repo,)):
        raise ValueError(f"Already tracking {repo}.")
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as c:
        try:
            r = await c.get(f"{GITHUB_API}/repos/{repo}")
        except httpx.HTTPError as e:
            raise ValueError(f"Couldn't reach GitHub ({e}).") from e
    if r.status_code == 404:
        raise ValueError(f"GitHub repo “{repo}” wasn't found (private repos need a token, which isn't set up).")
    if r.status_code == 403:
        raise ValueError("GitHub's rate limit was hit — try again in a few minutes.")
    r.raise_for_status()
    info = r.json()
    meta = {"description": info.get("description") or "", "stars": info.get("stargazers_count", 0),
            "default_branch": info.get("default_branch") or "main"}
    return await _insert("repo", repo, meta)


async def add_topic(text: str, topics: Optional[list[str]] = None, source: str = "typed") -> dict:
    label = (text or "").strip()[:80]
    if not label:
        raise ValueError("Give a topic to track.")
    return await _insert("topic", label, {"topics": topics or [label], "source": source})


TOPIC_SCHEMA = {"type": "object", "properties": {"topics": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string"}}},
                "required": ["topics"], "additionalProperties": False}


def pdf_text(data: bytes, max_chars: int = 14000) -> str:
    import fitz  # PyMuPDF — already a dependency (used by services.paper for newspaper uploads)
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        out, n = [], 0
        for page in doc:
            t = page.get_text()
            out.append(t)
            n += len(t)
            if n >= max_chars:
                break
        return "".join(out)[:max_chars]
    finally:
        doc.close()


async def add_doc(filename: str, data: bytes) -> dict:
    """Pull a few topics out of an uploaded document (PDF or plain text) and track news about them."""
    text = pdf_text(data) if (filename or "").lower().endswith(".pdf") else data.decode("utf-8", "ignore")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 200:
        raise ValueError("Couldn't find enough text in that file to work out what it's about.")
    out = await llm.chat_json(
        "editor",
        "Name 1 to 6 short, specific topics/technologies/subjects this document excerpt is about — 2 to 4 words "
        "each, no sentences, no generic words like 'technology' alone. These will be used to find related news.",
        text[:6000], TOPIC_SCHEMA, schema_name="topics", max_tokens=200,
    )
    topics = [t.strip() for t in out.get("topics", []) if t.strip()][:6]
    if not topics:
        raise ValueError("Couldn't work out a topic from that document.")
    label = (filename or "Uploaded document").rsplit("/", 1)[-1][:80]
    return await add_topic(label, topics, source="doc")


# ── listing / removing ───────────────────────────────────────────────────────

async def list_watches() -> list[dict]:
    rows = await db.fetch_all("SELECT id,kind,label,target,meta,created_at,checked_at,digest FROM watches ORDER BY created_at DESC")
    return [_decode(r) for r in rows]


def _decode(r) -> dict:
    return {**dict(r), "meta": db.jload(r["meta"], {}), "digest": db.jload(r["digest"], {})}


async def get_watch(watch_id: str) -> Optional[dict]:
    r = await db.fetch_one("SELECT id,kind,label,target,meta,created_at,checked_at,digest FROM watches WHERE id=?", (watch_id,))
    return _decode(r) if r else None


async def remove_watch(watch_id: str) -> None:
    await db.execute("DELETE FROM watches WHERE id=?", (watch_id,))


# ── refreshing ────────────────────────────────────────────────────────────────

DIGEST_SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string"}, "highlights": {"type": "array", "maxItems": 5, "items": {"type": "string"}}},
    "required": ["summary", "highlights"], "additionalProperties": False}


async def _repo_commits(repo: str, branch: str, since_iso: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as c:
        r = await c.get(f"{GITHUB_API}/repos/{repo}/commits", params={"sha": branch, "since": since_iso, "per_page": 30})
        if r.status_code != 200:
            return []
        return r.json()


async def refresh_repo(w: dict) -> dict:
    repo = w["target"]
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    branch = (w["meta"] or {}).get("default_branch") or "main"
    now = db.now_iso()
    try:
        commits = await _repo_commits(repo, branch, since)
    except httpx.HTTPError as e:
        digest = {"generated_at": now, "summary": f"Couldn't reach GitHub ({str(e)[:100]}).", "highlights": [], "count": None}
        return digest
    if not commits:
        digest = {"generated_at": now, "summary": "No commits in the last 7 days.", "highlights": [], "count": 0}
    else:
        msgs = [f"- {(c['commit']['message'].splitlines() or [''])[0][:140]}"
                f" ({(c.get('author') or {}).get('login') or c['commit']['author'].get('name', 'someone')})" for c in commits[:25]]
        try:
            out = await llm.chat_json(
                "writer",
                f"You are summarizing a week of GitHub activity on {repo} for someone tracking the project as they learn from it. "
                "Write ONE short plain sentence of overall summary, then up to 5 short highlight bullets of the most notable, "
                "concrete changes. Use ONLY the commit list given — never invent a feature or reason not stated in a message.",
                "\n".join(msgs), DIGEST_SCHEMA, schema_name="repo_digest", max_tokens=380,
            )
            digest = {"generated_at": now, "summary": out.get("summary", ""), "highlights": out.get("highlights", []), "count": len(commits)}
        except LLMError as e:
            digest = {"generated_at": now, "summary": f"{len(commits)} commits this week (LLM summary unavailable: {str(e)[:100]}).",
                      "highlights": [(c["commit"]["message"].splitlines() or [""])[0][:140] for c in commits[:5]], "count": len(commits)}
    return digest


async def refresh_topic(w: dict) -> dict:
    """Render-only: lists this app's own recent stories that match the topic. Never generates prose, so it cannot hallucinate."""
    topics = (w["meta"] or {}).get("topics") or [w["label"]]
    query = ", ".join(topics)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    now = db.now_iso()
    rows = await db.fetch_all(
        "SELECT id,headline,dek,topic,centroid,outlet_count FROM stories WHERE kind IN ('feed','paper','link') "
        "AND tier IN ('primary','more','wire') AND centroid IS NOT NULL AND COALESCE(published_at,created_at) >= ?", (cutoff,))
    if not rows:
        digest = {"generated_at": now, "summary": "No stories yet — refresh the news feed first.", "items": []}
        return digest
    try:
        qv = (await llm.embed([query], kind="query"))[0]
    except LLMError as e:
        digest = {"generated_at": now, "summary": f"Couldn't search for this right now ({str(e)[:100]}).", "items": []}
        return digest
    scored = []
    for r in rows:
        v = llm.from_blob(r["centroid"])
        if v is None or v.shape != qv.shape:
            continue
        scored.append((float(v @ qv), r))
    scored.sort(key=lambda x: -x[0])
    items = [{"id": r["id"], "headline": r["headline"], "dek": r["dek"], "topic": r["topic"], "outlet_count": r["outlet_count"]}
             for s, r in scored[:6] if s >= 0.5]
    summary = (f"{len(items)} of this week's stories touch on {', '.join(topics[:3])}." if items
               else f"Nothing in this week's coverage matched {', '.join(topics[:3])} yet.")
    digest = {"generated_at": now, "summary": summary, "items": items}
    return digest


async def refresh(w: dict) -> dict:
    digest = await (refresh_repo(w) if w["kind"] == "repo" else refresh_topic(w))
    await db.execute("UPDATE watches SET checked_at=?, digest=? WHERE id=?", (digest["generated_at"], db.jdump(digest), w["id"]))
    return digest


async def refresh_stale(max_age_hours: int = STALE_HOURS) -> int:
    """Called from the scheduler tick; re-checks whatever has gone stale, quietly skipping failures."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    n = 0
    for w in await list_watches():
        if not w["checked_at"] or w["checked_at"] < cutoff:
            try:
                await refresh(w)
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("digest refresh failed for %s: %s", w["label"], e)
    return n
