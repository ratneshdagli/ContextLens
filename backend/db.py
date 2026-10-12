"""ContextLens v3 — SQLite layer (aiosqlite, WAL)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import aiosqlite

from backend.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    url_hash      TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    snippet       TEXT NOT NULL DEFAULT '',
    outlet        TEXT NOT NULL DEFAULT '',
    domain        TEXT NOT NULL DEFAULT '',
    trust_rank    INTEGER NOT NULL DEFAULT 3,
    topic         TEXT NOT NULL DEFAULT 'world',
    published_at  TEXT,
    fetched_at    TEXT NOT NULL,
    image_url     TEXT NOT NULL DEFAULT '',
    embedding     BLOB,
    cluster_id    TEXT,
    full_text     TEXT,
    is_paywall    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_articles_cluster ON articles(cluster_id);
CREATE INDEX IF NOT EXISTS idx_articles_pub     ON articles(published_at);

CREATE TABLE IF NOT EXISTS clusters (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    topic         TEXT NOT NULL DEFAULT 'world',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    outlet_count  INTEGER NOT NULL DEFAULT 1,
    article_count INTEGER NOT NULL DEFAULT 1,
    centroid      BLOB,
    story_id      TEXT,
    score         REAL NOT NULL DEFAULT 0,
    triage        TEXT
);
CREATE INDEX IF NOT EXISTS idx_clusters_last ON clusters(last_seen);

CREATE TABLE IF NOT EXISTS stories (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL DEFAULT 'feed',      -- feed | research | paper | link
    cluster_id        TEXT,
    query             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    headline          TEXT NOT NULL DEFAULT '',
    headline_original TEXT NOT NULL DEFAULT '',
    dek               TEXT NOT NULL DEFAULT '',
    what_happened     TEXT NOT NULL DEFAULT '',
    why_it_matters    TEXT NOT NULL DEFAULT '',
    why_for_you       TEXT NOT NULL DEFAULT '',
    key_points        TEXT NOT NULL DEFAULT '[]',
    report            TEXT,                                -- research: {sections:[{heading,text,cites}]}
    topic             TEXT NOT NULL DEFAULT 'world',
    tags              TEXT NOT NULL DEFAULT '[]',
    trust_score       REAL NOT NULL DEFAULT 0,
    interest_match    REAL NOT NULL DEFAULT 0,
    matched_interest  TEXT NOT NULL DEFAULT '',
    outlet_count      INTEGER NOT NULL DEFAULT 1,
    reading_minutes   INTEGER NOT NULL DEFAULT 2,
    image_url         TEXT NOT NULL DEFAULT '',
    tier              TEXT NOT NULL DEFAULT 'pending',     -- primary | more | wire | rejected | pending | failed
    gate              TEXT NOT NULL DEFAULT '{}',          -- triage + publish decisions and reasons
    verification      TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'complete',    -- researching | complete | failed
    error             TEXT,
    published_at      TEXT,
    extras            TEXT NOT NULL DEFAULT '{}'           -- claims provenance, coverage lens, background
);
CREATE INDEX IF NOT EXISTS idx_stories_tier ON stories(tier, published_at);
CREATE INDEX IF NOT EXISTS idx_stories_cluster ON stories(cluster_id);

CREATE TABLE IF NOT EXISTS story_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id      TEXT NOT NULL,
    n             INTEGER NOT NULL DEFAULT 0,
    url           TEXT NOT NULL,
    outlet        TEXT NOT NULL DEFAULT '',
    domain        TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    trust_rank    INTEGER NOT NULL DEFAULT 3,
    published_at  TEXT,
    excerpt       TEXT NOT NULL DEFAULT '',
    has_full_text INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ss_story ON story_sources(story_id);

CREATE TABLE IF NOT EXISTS timeline_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id      TEXT NOT NULL,
    date          TEXT NOT NULL,          -- ISO date
    precision     TEXT NOT NULL DEFAULT 'day',
    text          TEXT NOT NULL,
    quote         TEXT NOT NULL DEFAULT '',
    source_url    TEXT NOT NULL DEFAULT '',
    source_outlet TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tl_story ON timeline_events(story_id);

CREATE TABLE IF NOT EXISTS entities (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id  TEXT NOT NULL,
    name      TEXT NOT NULL,
    type      TEXT NOT NULL DEFAULT 'other',
    mentions  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_ent_story ON entities(story_id);

CREATE TABLE IF NOT EXISTS relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    relation    TEXT NOT NULL,
    object      TEXT NOT NULL,
    evidence    TEXT NOT NULL DEFAULT '',
    source_url  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rel_story ON relations(story_id);

CREATE TABLE IF NOT EXISTS geo_tags (                -- where a story / headline happened (empty place = no confident location)
    ref_kind   TEXT NOT NULL,                        -- story | article
    ref_id     TEXT NOT NULL,
    lat        REAL,
    lon        REAL,
    place      TEXT NOT NULL DEFAULT '',
    cc         TEXT NOT NULL DEFAULT '',
    country    TEXT NOT NULL DEFAULT '',
    precision  TEXT NOT NULL DEFAULT '',             -- city | region | country
    score      REAL NOT NULL DEFAULT 0,
    v          INTEGER NOT NULL DEFAULT 0,
    tagged_at  TEXT NOT NULL,
    PRIMARY KEY (ref_kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_geo_pos ON geo_tags(lat, lon);

CREATE TABLE IF NOT EXISTS live_news (               -- live local headlines fetched for the Globe, kept for a week so a restart or a pan away and back doesn't lose them
    id           TEXT PRIMARY KEY,                   -- 'L' + md5(url)[:12], the same id the map dot has
    place        TEXT NOT NULL,
    cc           TEXT NOT NULL DEFAULT '',
    country      TEXT NOT NULL DEFAULT '',
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    title        TEXT NOT NULL,
    outlet       TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL,
    published_at TEXT NOT NULL,
    seen_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_pos ON live_news(lat, lon);
CREATE INDEX IF NOT EXISTS idx_live_pub ON live_news(published_at);

CREATE TABLE IF NOT EXISTS follows (
    id           TEXT PRIMARY KEY,
    story_id     TEXT NOT NULL,
    title        TEXT NOT NULL,
    centroid     BLOB,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id   TEXT NOT NULL,
    action     TEXT NOT NULL,      -- like | dislike | save | open | dismiss
    created_at TEXT NOT NULL,
    dwell_ms   INTEGER
);

CREATE TABLE IF NOT EXISTS saved (
    story_id TEXT PRIMARY KEY,
    saved_at TEXT NOT NULL,
    note     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id   TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_story ON messages(story_id);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,     -- feed | research
    label       TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    progress    REAL NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL DEFAULT '',
    stats       TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    story_id    TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    ts      TEXT NOT NULL,
    stage   TEXT NOT NULL,
    level   TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    data    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_re_run ON run_events(run_id);

-- ── v4: living knowledge base ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kb_entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,          -- normalised name
    name        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'other',
    summary     TEXT NOT NULL DEFAULT '',
    wiki_url    TEXT NOT NULL DEFAULT '',
    first_seen  TEXT,
    last_seen   TEXT,
    story_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kbe_count ON kb_entities(story_count);

CREATE TABLE IF NOT EXISTS kb_mentions (
    entity_id INTEGER NOT NULL,
    story_id  TEXT NOT NULL,
    mentions  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (entity_id, story_id)
);
CREATE INDEX IF NOT EXISTS idx_kbm_story ON kb_mentions(story_id);

CREATE TABLE IF NOT EXISTS threads (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    topic       TEXT NOT NULL DEFAULT 'world',
    centroid    BLOB,
    first_at    TEXT,
    last_at     TEXT,
    story_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS thread_stories (
    story_id  TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ts_thread ON thread_stories(thread_id, at);

CREATE TABLE IF NOT EXISTS story_links (
    a      TEXT NOT NULL,
    b      TEXT NOT NULL,
    kind   TEXT NOT NULL DEFAULT 'related',
    score  REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (a, b)
);
CREATE INDEX IF NOT EXISTS idx_sl_b ON story_links(b);

-- cached on-demand answers: deep dives, inspector results, explanations
CREATE TABLE IF NOT EXISTS dives (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (story_id, kind, key)
);

-- ── v4: uploaded newspapers ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS papers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    filename      TEXT NOT NULL DEFAULT '',
    uploaded_at   TEXT NOT NULL,
    pages         INTEGER NOT NULL DEFAULT 0,
    article_count INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'processing',
    stats         TEXT NOT NULL DEFAULT '{}',
    error         TEXT
);

CREATE TABLE IF NOT EXISTS paper_articles (
    id          TEXT PRIMARY KEY,
    paper_id    TEXT NOT NULL,
    page        INTEGER NOT NULL DEFAULT 1,
    headline    TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    words       INTEGER NOT NULL DEFAULT 0,
    topic       TEXT NOT NULL DEFAULT 'world',
    interest    REAL NOT NULL DEFAULT 0,
    kind        TEXT NOT NULL DEFAULT 'article',   -- article | skipped
    skip_reason TEXT NOT NULL DEFAULT '',
    story_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pa_paper ON paper_articles(paper_id);

-- ── v6: personal digest (GitHub repos / topics from your own docs) ──────────
CREATE TABLE IF NOT EXISTS watches (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,               -- repo | topic
    label      TEXT NOT NULL,               -- "owner/repo", or a short topic phrase
    target     TEXT NOT NULL DEFAULT '',    -- repo slug, or the topic query text
    meta       TEXT NOT NULL DEFAULT '{}',  -- {description, stars, default_branch} or {topics:[...], source}
    created_at TEXT NOT NULL,
    checked_at TEXT,
    digest     TEXT NOT NULL DEFAULT '{}'   -- last generated {generated_at, summary, highlights|items}
);

-- ── v6.2: reader-defined channels (own categories, even very niche ones) ─────
CREATE TABLE IF NOT EXISTS channels (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    weight        INTEGER NOT NULL DEFAULT 2,      -- 0 off .. 3 high
    depth         INTEGER NOT NULL DEFAULT 2,      -- 1..3 full-text articles per story
    size          INTEGER NOT NULL DEFAULT 12,     -- stories kept on the channel page
    days          INTEGER NOT NULL DEFAULT 5,      -- freshness window (niche topics publish slowly)
    position      INTEGER NOT NULL DEFAULT 0,
    muted         TEXT NOT NULL DEFAULT '[]',      -- muted keywords, this channel only
    queries       TEXT NOT NULL DEFAULT '[]',      -- search queries (discovery + each refresh)
    feeds         TEXT NOT NULL DEFAULT '[]',      -- [{url,outlet,ok,manual,n,off}]
    vec           BLOB,                            -- embedding of "name. description" (NULL = re-embed)
    discovered_at TEXT,
    created_at    TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def jload(s: Optional[str], default: Any = None) -> Any:
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_PATH, timeout=30)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=30000")
    try:
        yield db
    finally:
        await db.close()


def _sidestep_legacy_db() -> None:
    """The v2 prototype used the same file name with an incompatible `stories` table. Move it aside (never delete)."""
    path = Path(DB_PATH)
    if not path.exists():
        return
    con = sqlite3.connect(DB_PATH)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(stories)")]
    finally:
        con.close()
    if cols and "headline" not in cols:
        legacy = path.with_name(f"{path.stem}.v2-legacy{path.suffix}")
        n = 1
        while legacy.exists():
            n += 1
            legacy = path.with_name(f"{path.stem}.v2-legacy{n}{path.suffix}")
        shutil.move(str(path), str(legacy))
        for ext in ("-wal", "-shm"):
            side = Path(str(path) + ext)
            if side.exists():
                side.unlink()
        print(f"  Moved the old v2 database to {legacy.name} (kept as a backup); starting fresh.")


MIGRATIONS = [
    ("stories", "extras", "TEXT NOT NULL DEFAULT '{}'"),
    ("stories", "centroid", "BLOB"),
    ("stories", "source_ref", "TEXT NOT NULL DEFAULT ''"),     # paper id / pasted-link url
    ("stories", "built_at", "TEXT"),                            # when the full explainer was written (NULL = wire only)
    ("timeline_events", "kind", "TEXT NOT NULL DEFAULT 'reported'"),   # reported (dated in an article) | history (dated by publication)
    ("clusters", "merged", "TEXT NOT NULL DEFAULT '[]'"),      # headlines of clusters folded into this one ("developments")
    ("stories", "channel_id", "TEXT NOT NULL DEFAULT ''"),     # the reader's channel this story belongs to ('' = none)
]


async def init_db() -> None:
    _sidestep_legacy_db()
    async with get_db() as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            cols = [r[1] for r in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()]
            if col not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        # v3 stories were fully built before the wire tier existed: mark them built so they keep showing as full stories
        await db.execute("UPDATE stories SET built_at=updated_at WHERE built_at IS NULL AND tier IN ('primary','more') AND headline!=''")
        # explanations written by the first (unvalidated) explainer are removed; they are rebuilt by the queue or on demand
        await db.execute("UPDATE stories SET extras=json_remove(extras,'$.explain') "
                         "WHERE json_extract(extras,'$.explain') IS NOT NULL AND json_extract(extras,'$.explain.v') IS NULL")
        await db.commit()


async def fetch_all(sql: str, params: Iterable = ()) -> list[dict]:
    async with get_db() as db:
        cur = await db.execute(sql, tuple(params))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def fetch_one(sql: str, params: Iterable = ()) -> Optional[dict]:
    async with get_db() as db:
        cur = await db.execute(sql, tuple(params))
        row = await cur.fetchone()
        return dict(row) if row else None


async def execute(sql: str, params: Iterable = ()) -> None:
    async with get_db() as db:
        await db.execute(sql, tuple(params))
        await db.commit()


async def executemany(sql: str, rows: list[Iterable]) -> None:
    if not rows:
        return
    async with get_db() as db:
        await db.executemany(sql, [tuple(r) for r in rows])
        await db.commit()


# ── key/value ────────────────────────────────────────────────────────────────

async def kv_get(key: str, default: Any = None) -> Any:
    row = await fetch_one("SELECT value FROM kv WHERE key=?", (key,))
    return jload(row["value"], default) if row else default


async def kv_set(key: str, value: Any) -> None:
    await execute(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, jdump(value)),
    )


# ── row (de)serialisation for stories ────────────────────────────────────────

STORY_JSON_COLS = ("key_points", "tags", "gate", "verification", "report", "extras")


def story_from_row(row: dict) -> dict:
    out = dict(row)
    for c in STORY_JSON_COLS:
        default = [] if c in ("key_points", "tags") else ({} if c != "report" else None)
        out[c] = jload(out.get(c), default)
    return out
