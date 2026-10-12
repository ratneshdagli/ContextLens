"""ContextLens v3 — configuration.

Static defaults live here; anything the user can change at runtime (models, feed size,
strictness, …) is stored in the DB `kv` table under the key "settings" and merged over
DEFAULT_SETTINGS by `backend.settings_store`.
"""

from __future__ import annotations

import json
import os
import pathlib

try:
    from dotenv import load_dotenv
    _env = pathlib.Path(__file__).parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:  # pragma: no cover
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_NAME = "ContextLens"
APP_VERSION = "6.0.0"

DB_PATH = os.getenv("DB_PATH", str(ROOT / "contextlens.db"))
FRONTEND_DIST = ROOT / "frontend" / "dist"

# ── LLM endpoint (any OpenAI-compatible server: LM Studio, Ollama /v1, vLLM, hosted) ──
LLM_BASE_URL = os.getenv("LLM_BASE_URL", os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1235/v1"))
LLM_API_KEY = os.getenv("LLM_API_KEY", os.getenv("LM_STUDIO_API_KEY", "lm-studio"))
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080")

# ── Runtime-editable defaults ─────────────────────────────────────────────────
DEFAULT_SETTINGS: dict = {
    # Models. `writer` writes headlines/summaries/chat; `editor` is an independent judge (different family).
    "model_writer": os.getenv("MODEL_WRITER", "meta-llama-3-8b-instruct"),
    "model_editor": os.getenv("MODEL_EDITOR", "google/gemma-3n-e4b"),
    "model_embed": os.getenv("MODEL_EMBED", "text-embedding-nomic-embed-text-v1.5"),
    # One model resident at a time on small GPUs -> `single_model_mode` uses the writer for everything.
    "single_model_mode": False,
    "manage_models": True,         # keep exactly ONE chat LLM loaded in LM Studio (unload the previous before loading the next)
    "idle_unload_minutes": 10,     # LM Studio unloads the chat model after this many idle minutes (0 = never)
    # Feed
    "feed_size": 14,               # how many primary stories to keep fresh
    "wire_size": 200,              # stories shown instantly (no LLM) and upgraded in the background
    "build_budget": 30,            # max stories the background builder upgrades per refresh
    "candidate_pool": 36,          # clusters sent to editor triage
    "max_age_hours": 48,           # ignore RSS items older than this
    "refresh_minutes": 60,         # scheduler cadence
    "auto_refresh": True,
    "strictness": "balanced",      # lenient | balanced | strict  (how picky the editor gates are)
    "depth": 2,                    # 1 quick, 2 balanced, 3 thorough (articles fetched per story)
    "reading_level": "simple",     # simple | general | expert
    "ner_enabled": True,           # entity recognition model for story maps (~600 MB RAM); off = regex fallback
    "min_signal": 0,               # hide stories whose Signal score (1-10) is below this
    "min_outlets_primary": 2,      # independent outlets needed for the primary tier (unless very high interest)
}

STRICTNESS = {
    # triage_min: minimum combined score to survive triage; publish_min: minimum editor publish score
    "lenient":  {"triage_min": 0.30, "publish_min": 0.40, "topk_extra": 1.4},
    "balanced": {"triage_min": 0.42, "publish_min": 0.55, "topk_extra": 1.0},
    "strict":   {"triage_min": 0.55, "publish_min": 0.68, "topk_extra": 0.7},
}

DEPTH_ARTICLES = {1: 2, 2: 3, 3: 5}   # full-text articles fetched per story

# ── Topics (the vocabulary shown in onboarding / settings) ────────────────────
TOPICS: dict[str, dict] = {
    "world":     {"label": "World",            "desc": "international affairs, diplomacy, wars and conflicts, geopolitics, global events"},
    "politics":  {"label": "Politics",         "desc": "government, elections, legislation, courts, political parties, public policy"},
    "business":  {"label": "Business",         "desc": "companies, earnings, mergers, corporate leadership, industry trends, startups"},
    "markets":   {"label": "Markets & Economy", "desc": "stock markets, interest rates, inflation, central banks, trade, currencies, commodities"},
    "tech":      {"label": "Technology",       "desc": "software, hardware, AI, chips, internet platforms, cybersecurity, consumer tech"},
    "science":   {"label": "Science & Space",  "desc": "scientific research, discoveries, space exploration, climate and environment"},
    "health":    {"label": "Health",           "desc": "medicine, public health, disease, drugs and treatments, healthcare policy"},
    "india":     {"label": "India",            "desc": "India domestic news, Indian politics, economy, business and society"},
    "sports":    {"label": "Sports",           "desc": "sports results, leagues, athletes, tournaments and championships"},
    "culture":   {"label": "Culture",          "desc": "film, music, books, arts, media and entertainment industry news"},
}

# ── Sources ───────────────────────────────────────────────────────────────────
_SOURCES_JSON = pathlib.Path(__file__).parent / "trusted_sources.json"
TRUSTED_SOURCES: list[dict] = json.loads(_SOURCES_JSON.read_text(encoding="utf-8")) if _SOURCES_JSON.exists() else []
TRUSTED_DOMAIN_MAP: dict[str, dict] = {s["domain"]: s for s in TRUSTED_SOURCES}

BLOCKLIST_DOMAINS = {
    "buzzfeed.com", "infowars.com", "naturalnews.com", "theonion.com", "clickhole.com",
    "worldstar.com", "breitbart.com",
}

# (url, outlet, topic). The feed fetcher tolerates dead feeds, so this list can be generous.
FEEDS: list[tuple[str, str, str]] = [
    # World / general
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC News", "world"),
    ("https://feeds.bbci.co.uk/news/rss.xml", "BBC News", "world"),
    ("https://feeds.npr.org/1004/rss.xml", "NPR", "world"),
    ("https://www.theguardian.com/world/rss", "The Guardian", "world"),
    ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera", "world"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "The New York Times", "world"),
    ("https://rss.dw.com/rdf/rss-en-world", "Deutsche Welle", "world"),
    ("https://www.france24.com/en/rss", "France 24", "world"),
    ("https://feeds.washingtonpost.com/rss/world", "The Washington Post", "world"),
    ("https://www.cbsnews.com/latest/rss/world", "CBS News", "world"),
    ("https://feeds.skynews.com/feeds/rss/world.xml", "Sky News", "world"),
    ("https://abcnews.go.com/abcnews/internationalheadlines", "ABC News", "world"),
    # Politics
    ("https://feeds.bbci.co.uk/news/politics/rss.xml", "BBC News", "politics"),
    ("https://feeds.npr.org/1014/rss.xml", "NPR", "politics"),
    ("https://www.theguardian.com/politics/rss", "The Guardian", "politics"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml", "The New York Times", "politics"),
    ("https://www.politico.com/rss/politicopicks.xml", "Politico", "politics"),
    # Business / markets
    ("https://feeds.bbci.co.uk/news/business/rss.xml", "BBC News", "business"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", "The New York Times", "business"),
    ("https://www.theguardian.com/uk/business/rss", "The Guardian", "business"),
    ("https://feeds.washingtonpost.com/rss/business", "The Washington Post", "business"),
    ("https://www.cnbc.com/id/10001147/device/rss/rss.html", "CNBC", "business"),
    ("https://www.cnbc.com/id/20910258/device/rss/rss.html", "CNBC", "markets"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml", "The New York Times", "markets"),
    ("https://www.marketwatch.com/rss/topstories", "MarketWatch", "markets"),
    ("https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "The Economic Times", "markets"),
    ("https://www.livemint.com/rss/markets", "Mint", "markets"),
    # Tech
    ("https://feeds.arstechnica.com/arstechnica/index", "Ars Technica", "tech"),
    ("https://www.theverge.com/rss/index.xml", "The Verge", "tech"),
    ("https://www.wired.com/feed/rss", "Wired", "tech"),
    ("https://techcrunch.com/feed/", "TechCrunch", "tech"),
    ("https://feeds.bbci.co.uk/news/technology/rss.xml", "BBC News", "tech"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", "The New York Times", "tech"),
    ("https://www.technologyreview.com/feed/", "MIT Technology Review", "tech"),
    ("https://spectrum.ieee.org/feeds/feed.rss", "IEEE Spectrum", "tech"),
    ("https://www.theguardian.com/technology/rss", "The Guardian", "tech"),
    # Science / health
    ("https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "BBC News", "science"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/Science.xml", "The New York Times", "science"),
    ("https://www.nasa.gov/news-release/feed/", "NASA", "science"),
    ("https://www.nature.com/nature.rss", "Nature", "science"),
    ("https://www.theguardian.com/science/rss", "The Guardian", "science"),
    ("https://feeds.bbci.co.uk/news/health/rss.xml", "BBC News", "health"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/Health.xml", "The New York Times", "health"),
    ("https://feeds.npr.org/1128/rss.xml", "NPR", "health"),
    ("https://www.statnews.com/feed/", "STAT", "health"),
    # India
    ("https://www.thehindu.com/news/national/feeder/default.rss", "The Hindu", "india"),
    ("https://timesofindia.indiatimes.com/rssfeedstopstories.cms", "The Times of India", "india"),
    ("https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml", "Hindustan Times", "india"),
    ("https://feeds.feedburner.com/ndtvnews-top-stories", "NDTV", "india"),
    ("https://indianexpress.com/section/india/feed/", "The Indian Express", "india"),
    ("https://www.livemint.com/rss/news", "Mint", "india"),
    ("https://economictimes.indiatimes.com/rssfeedstopstories.cms", "The Economic Times", "india"),
    # Sports / culture
    ("https://feeds.bbci.co.uk/sport/rss.xml", "BBC Sport", "sports"),
    ("https://www.espn.com/espn/rss/news", "ESPN", "sports"),
    ("https://www.theguardian.com/sport/rss", "The Guardian", "sports"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/Arts.xml", "The New York Times", "culture"),
    ("https://www.theguardian.com/culture/rss", "The Guardian", "culture"),
    ("https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml", "BBC News", "culture"),
]

# Feeds added in v5 (probed live: each parsed to 3+ items and was fresh when added) — Indian and regional press,
# fact-checkers, explainer outlets, science/health/tech specialists. A dead feed is skipped at fetch time.
_EXTRA_JSON = pathlib.Path(__file__).parent / "feeds_extra.json"
if _EXTRA_JSON.exists():
    _have = {u for u, _, _ in FEEDS}
    FEEDS += [(f["url"], f["outlet"], f["topic"]) for f in json.loads(_EXTRA_JSON.read_text(encoding="utf-8")) if f["url"] not in _have]

# ── HTTP ──────────────────────────────────────────────────────────────────────
FEED_TIMEOUT = 12
CRAWL_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000"]
