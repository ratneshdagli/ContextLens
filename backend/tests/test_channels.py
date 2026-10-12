"""Reader-defined channels: CRUD, matching, source discovery (network faked), ingest windows and the feed API."""

import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import app
from backend.services import channels, feeds, llm, ranking

NOW = datetime.now(timezone.utc)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _unit(i, n=8):
    v = np.zeros(n, dtype=np.float32)
    v[i] = 1.0
    return v


def _ch(name="Classical music", desc="orchestras, conductors, new recordings, opera, festivals", vec=0, weight=2, muted=()):
    return channels.prepare({"id": "c1", "name": name, "description": desc, "weight": weight, "depth": 2, "size": 12, "days": 5,
                             "muted": list(muted), "vec": _unit(vec) if vec is not None else None})


def _cluster(topic, title, vec, snippet=""):
    return {"id": "k", "title": title, "topic": topic, "outlet_count": 2, "last_seen": NOW.isoformat(), "centroid": _unit(vec),
            "members": [{"title": title, "snippet": snippet, "trust_rank": 2, "domain": "x.com"}]}


def _profile():
    return {"interests": [], "topics": {"world": 1}, "muted_keywords": [], "muted_outlets": [], "followed": []}


def _score(c, chans):
    return ranking.score_cluster(c, _profile(), [], None, None, chans)


# ── CRUD over the API ────────────────────────────────────────────────────────

def test_channel_crud_clamps_and_reorders(client):
    a = client.post("/api/channels", json={"name": "  Classical   music ", "description": "orchestras and opera", "weight": 9,
                                           "depth": 0, "size": 500, "days": 99, "muted": [" gossip ", "gossip"]}).json()
    assert a["name"] == "Classical music" and a["weight"] == 3 and a["depth"] == 1 and a["size"] == 30 and a["days"] == 7
    assert a["muted"] == ["gossip"] and a["feeds"] == [] and a["stats"]["stories"] == 0
    b = client.post("/api/channels", json={"name": "Indian startups"}).json()
    assert client.post("/api/channels", json={"name": "  "}).status_code == 400
    client.post("/api/channels/reorder", json={"ids": [b["id"], a["id"]]})
    assert [c["name"] for c in client.get("/api/channels").json()] == ["Indian startups", "Classical music"]
    p = client.patch(f"/api/channels/{a['id']}", json={"weight": 0, "description": "opera only"}).json()
    assert p["weight"] == 0 and p["description"] == "opera only"
    assert client.patch("/api/channels/nope", json={"weight": 1}).status_code == 404
    assert client.delete(f"/api/channels/{b['id']}").json() == {"ok": True}
    assert client.delete(f"/api/channels/{b['id']}").status_code == 404
    client.delete(f"/api/channels/{a['id']}")


def test_editing_the_description_clears_the_embedding(client):
    ch = client.post("/api/channels", json={"name": "Jazz", "description": "live jazz"}).json()
    _run(db.execute("UPDATE channels SET vec=? WHERE id=?", (llm.to_blob(_unit(0)), ch["id"])))
    client.patch(f"/api/channels/{ch['id']}", json={"weight": 1})
    assert _run(channels.get(ch["id"], vec=True))["vec"] is not None            # unrelated edit keeps it
    client.patch(f"/api/channels/{ch['id']}", json={"description": "bebop and fusion"})
    assert _run(channels.get(ch["id"], vec=True))["vec"] is None                # new text must be re-embedded
    client.delete(f"/api/channels/{ch['id']}")


# ── matching ─────────────────────────────────────────────────────────────────

def test_own_source_story_joins_its_channel_and_says_why():
    r = _score(_cluster("ch:c1", "Berlin Philharmonic names new chief conductor", 0), [_ch()])
    assert r["channel"] == "c1" and "Classical music" in r["why"] and r["components"]["interest"] >= 0.8


def test_a_channel_story_does_not_claim_an_unrelated_interest():
    """The reader's interest text matches weakly (orthogonal vector); the channel match must not make that label look earned."""
    items = [{"id": "i", "text": "Anything related to India markets", "weight": 2}]
    ivecs = np.stack([_unit(1)])
    r = ranking.score_cluster(_cluster("ch:c1", "Berlin Philharmonic names new chief conductor", 0), _profile(), items, ivecs, None, [_ch()])
    assert r["channel"] == "c1" and "India" not in r["why"] and r["why"].startswith("in your channel")


def test_off_topic_story_from_a_channel_source_is_dropped():
    r = _score(_cluster("ch:c1", "Stocks fall as rates rise", 1), [_ch()])
    assert r["excluded"] and "off-topic" in r["excluded"]


def test_general_feed_story_can_still_match_a_channel():
    r = _score(_cluster("culture", "Orchestra premieres new symphony at festival", 0, "the conductor led the orchestra"), [_ch()])
    assert r["channel"] == "c1"
    assert _score(_cluster("culture", "Film awards season opens", 3), [_ch()])["channel"] is None


def test_switched_off_channel_claims_nothing_and_muted_keywords_exclude():
    off = _ch(weight=0)
    assert _score(_cluster("ch:c1", "New opera season", 0), [off])["excluded"]                      # its own sources are dropped
    assert _score(_cluster("world", "Orchestra premieres new symphony", 0), [off])["channel"] is None   # general news is untouched
    r = _score(_cluster("ch:c1", "Opera star in celebrity gossip row", 0), [_ch(muted=["gossip"])])
    assert "gossip" in r["excluded"]


def test_without_an_embedding_key_terms_only_admit_a_channels_own_sources():
    ch = _ch(vec=None)
    text = "orchestra conductor opera festival recordings"
    assert channels.MEMBER_MIN_OWN <= channels.affinity(_unit(0), text, ch) < channels.MEMBER_MIN_GENERAL
    assert channels.affinity(_unit(0), "weather today", ch) < channels.MEMBER_MIN_OWN
    own = _score(_cluster("ch:c1", "New opera season", 0, text), [ch])
    assert own["channel"] == "c1"                                                                    # not silently empty when the model is offline
    assert _score(_cluster("culture", "New opera season", 0, text), [ch])["channel"] is None


def test_only_public_hostnames_are_fetched():
    for bad in ("localhost:8000", "127.0.0.1", "192.168.1.5", "10.0.0.2", "user@evil.com", "printer.local", "nodot", "[::1]", ""):
        assert not channels._public_host(bad), bad
    assert channels._public_host("gramophone.co.uk") and channels._is_source("slippedisc.com")
    assert not channels._is_source("192.168.0.1") and not channels._is_source("youtube.com")


def test_channel_member_ignores_the_neutral_default_interest():
    r = _score(_cluster("ch:c1", "Berlin Philharmonic names new chief conductor", 0), [_ch()])
    assert r["components"]["interest"] > 0.35                                    # not flattened to "no interests yet"


# ── the editor judges a channel's story as a follower of that channel would ──

def _ans(**k):
    base = {"headline_matches_facts": True, "headline_is_clickbait": False, "summary_support": "fully",
            "understandable_standalone": True, "newsworthy_for_reader": "secondary", "issue": "none"}
    return {**base, **k}


def test_niche_stories_are_not_demoted_for_being_single_outlet_or_minor():
    from backend.services import editor
    g = {"score": 0.9}
    assert editor.decide_publish(_ans(), g, "balanced", 1, 0.55)["tier"] == "more"                       # general news: needs 2+ outlets
    assert editor.decide_publish(_ans(), g, "balanced", 1, 0.55, niche=True)["tier"] == "primary"       # a niche beat is single-outlet by nature
    trivial = _ans(newsworthy_for_reader="reject")
    assert editor.decide_publish(trivial, g, "balanced", 1, 0.8, niche=True)["tier"] == "more"           # on-topic but minor: kept, lower priority
    assert editor.decide_publish(trivial, g, "balanced", 1, 0.3, niche=True)["tier"] == "rejected"       # barely on-topic: still dropped
    assert editor.decide_publish(trivial, g, "balanced", 1, 0.8)["tier"] == "rejected"                   # general news is unchanged
    assert editor.decide_publish(_ans(headline_matches_facts=False), g, "balanced", 1, 0.9, niche=True)["tier"] == "rejected"   # facts still gate it


def test_niche_triage_keeps_routine_on_topic_news_and_still_vetoes_off_topic_and_junk():
    from backend.services import editor
    ans = {"what_happened": "x", "story_type": "routine_update", "has_concrete_new_fact": False, "impact": "local_minor",
           "matches_reader": "strong", "reader_value": 6}
    assert editor.decide_triage(ans, 0.4, 0.8, "lenient")["keep"] is False                              # general news: routine + minor + no fact
    assert editor.decide_triage(ans, 0.4, 0.8, "lenient", niche=True)["keep"] is True                   # a season announcement IS the niche beat
    off = {**ans, "matches_reader": "none"}
    d = editor.decide_triage(off, 0.4, 0.3, "lenient", niche=True)
    assert d["keep"] is False and "off-topic for the channel" in d["reasons"]
    junk = {**ans, "story_type": "service_or_how_to"}
    assert editor.decide_triage(junk, 0.9, 0.9, "lenient", niche=True)["keep"] is False


def test_channel_brief_is_the_channels_own_interest_not_the_general_profile():
    b = channels.brief_line(_ch())
    assert b.startswith("Reader's specific interests: Classical music: orchestras") and "Topics they follow closely: Classical music" in b


# ── discovery (network faked) ────────────────────────────────────────────────

class _Resp:
    def __init__(self, content=b"", code=200, url="https://x.test/feed", ctype="application/rss+xml"):
        self.content, self.status_code, self.url, self.headers = content, code, url, {"content-type": ctype}
        self.text = content.decode("utf-8", "ignore")


def _rss(n=4, days_old=1):
    t = (NOW - timedelta(days=days_old)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = "".join(f"<item><title>Item number {i} about opera</title><link>https://x.test/{i}</link><pubDate>{t}</pubDate></item>" for i in range(n))
    return (f"<?xml version='1.0'?><rss version='2.0'><channel><title>Gramophone - Classical Music</title>{items}</channel></rss>").encode()


class _Client:
    def __init__(self, routes):
        self.routes = routes

    async def get(self, url, **kw):
        if url in self.routes:
            return self.routes[url]
        return _Resp(b"", 404, url)


def test_validate_feed_accepts_live_and_rejects_stale_or_thin():
    ok = _run(channels.validate_feed(_Client({"https://x.test/feed": _Resp(_rss())}), "https://x.test/feed"))
    assert ok and ok["outlet"] == "Gramophone" and ok["n"] == 4 and ok["domain"] == "x.test"
    assert _run(channels.validate_feed(_Client({"https://x.test/feed": _Resp(_rss(days_old=400))}), "https://x.test/feed")) is None
    assert _run(channels.validate_feed(_Client({"https://x.test/feed": _Resp(_rss(n=2))}), "https://x.test/feed")) is None
    assert _run(channels.validate_feed(_Client({}), "https://x.test/feed")) is None


def test_probe_reads_the_advertised_feed_link_then_falls_back_to_paths():
    html = b'<html><head><link rel="alternate" type="application/rss+xml" href="/custom.xml"><link rel="alternate" type="application/rss+xml" href="/comments/feed"></head></html>'
    routes = {"https://site.test/": _Resp(html, url="https://site.test/", ctype="text/html"),
              "https://site.test/custom.xml": _Resp(_rss(), url="https://site.test/custom.xml")}
    hit = _run(channels.probe_feed(_Client(routes), "site.test", asyncio.Semaphore(1)))
    assert hit and hit["url"] == "https://site.test/custom.xml"
    routes = {"https://b.test/rss": _Resp(_rss(), url="https://b.test/rss")}
    assert _run(channels.probe_feed(_Client(routes), "b.test", asyncio.Semaphore(1)))["url"] == "https://b.test/rss"
    assert _run(channels.probe_feed(_Client({}), "dead.test", asyncio.Semaphore(1))) is None


def test_discover_finds_live_feeds_skips_general_and_non_sources(client, monkeypatch):
    ch = client.post("/api/channels", json={"name": "Classical music", "description": "orchestras and opera"}).json()

    async def gnews(c, q):
        return ["gramophone.co.uk", "gramophone.co.uk", "slippedisc.com", "bbc.co.uk", "youtube.com", "dead.example", "facebook.com"]

    async def searx(c, q, categories="news", time_range="month"):
        return [{"url": "https://www.gramophone.co.uk/reviews/x"}, {"url": "https://musicalamerica.com/a"}]

    async def probe(c, domain, sem):
        return None if domain == "dead.example" else {"url": f"https://{domain}/feed", "outlet": domain, "ok": True, "n": 5, "domain": domain}

    async def queries(n, d):
        return ["classical music", "opera news"]

    monkeypatch.setattr(channels, "_gnews", gnews)
    monkeypatch.setattr(channels, "_searx", searx)
    monkeypatch.setattr(channels, "probe_feed", probe)
    monkeypatch.setattr(channels, "make_queries", queries)
    monkeypatch.setattr(channels, "_GENERAL_DOMAINS", {"bbc.co.uk"})
    out = client.post(f"/api/channels/{ch['id']}/discover").json()
    urls = {f["url"] for f in out["feeds"]}
    assert urls == {"https://gramophone.co.uk/feed", "https://slippedisc.com/feed", "https://musicalamerica.com/feed"}
    assert out["queries"] == ["classical music", "opera news"] and out["feeds_live"] == 3
    assert out["discovery"]["covered_by_general_feeds"] == ["bbc.co.uk"]        # already ingested for everyone; matched by meaning
    assert not {"youtube.com", "facebook.com"} & {f["domain"] for f in out["feeds"]}
    again = client.post(f"/api/channels/{ch['id']}/discover").json()
    assert len(again["feeds"]) == 3                                              # re-running never duplicates
    off = client.patch(f"/api/channels/{ch['id']}/feeds", json={"url": "https://slippedisc.com/feed", "off": True}).json()
    assert off["feeds_live"] == 2
    gone = client.request("DELETE", f"/api/channels/{ch['id']}/feeds", params={"url": "https://slippedisc.com/feed"}).json()
    assert len(gone["feeds"]) == 2
    assert client.post("/api/channels/nope/discover").status_code == 404
    client.delete(f"/api/channels/{ch['id']}")


def test_add_feed_rejects_a_dead_address(client, monkeypatch):
    ch = client.post("/api/channels", json={"name": "Jazz"}).json()

    async def none(*a, **k):
        return None
    monkeypatch.setattr(channels, "validate_feed", none)
    monkeypatch.setattr(channels, "probe_feed", none)
    r = client.post(f"/api/channels/{ch['id']}/feeds", json={"url": "nothing.test"})
    assert r.status_code == 400 and "No working feed" in r.json()["detail"]
    for internal in ("http://127.0.0.1:1235/v1/models", "localhost:8000", "http://169.254.169.254/latest"):
        r = client.post(f"/api/channels/{ch['id']}/feeds", json={"url": internal})
        assert r.status_code == 400 and "public website" in r.json()["detail"]                       # never fetched
    client.delete(f"/api/channels/{ch['id']}")


def test_discovery_keeps_feeds_added_while_it_ran(client, monkeypatch):
    ch = client.post("/api/channels", json={"name": "Jazz", "description": "live jazz"}).json()

    async def gnews(c, q):
        return ["jazztimes.com"]

    async def searx(c, q, categories="news", time_range="month"):
        return []

    async def probe(c, domain, sem):                       # the reader adds a feed by hand while the (slow) probing is still running
        await channels.db.execute("UPDATE channels SET feeds=? WHERE id=?", (channels.db.jdump([{"url": "https://mine.test/feed", "ok": True, "manual": True, "domain": "mine.test"}]), ch["id"]))
        return {"url": "https://jazztimes.com/feed", "outlet": "JazzTimes", "ok": True, "n": 5, "domain": "jazztimes.com"}

    async def queries(n, d):
        return ["jazz"]
    monkeypatch.setattr(channels, "_gnews", gnews)
    monkeypatch.setattr(channels, "_searx", searx)
    monkeypatch.setattr(channels, "probe_feed", probe)
    monkeypatch.setattr(channels, "make_queries", queries)
    out = client.post(f"/api/channels/{ch['id']}/discover").json()
    assert {f["url"] for f in out["feeds"]} == {"https://mine.test/feed", "https://jazztimes.com/feed"}   # nothing the reader did was lost
    assert out["discovery"]["search_hits"] == 0
    client.delete(f"/api/channels/{ch['id']}")


def test_search_items_become_feed_items_and_junk_is_filtered(monkeypatch):
    async def searx(c, q, categories="news", time_range="month"):
        return [{"url": "https://gramophone.co.uk/a", "title": "Berlin Philharmonic names its new chief conductor", "content": "<b>Kirill</b> takes over",
                 "publishedDate": "2026-10-04T10:00:00Z"},
                {"url": "https://youtube.com/watch?v=1", "title": "Concert video of the orchestra tonight", "content": ""},
                {"url": "https://gramophone.co.uk/opinion/b", "title": "Why I love opera more than ever", "content": ""},
                {"url": "https://gramophone.co.uk/c", "title": "Live blog: opening night", "content": ""}]
    monkeypatch.setattr(channels, "_searx", searx)
    ch = {"id": "c1", "name": "Classical", "weight": 2, "queries": ["classical music"]}
    out = _run(channels.search_items([ch, {**ch, "id": "c2", "weight": 0}]))
    assert len(out) == 1 and out[0]["topic"] == "ch:c1" and out[0]["domain"] == "gramophone.co.uk"
    assert out[0]["snippet"] == "Kirill takes over" and out[0]["published_at"].startswith("2026-10-04")


# ── ingest keeps channel items longer than general ones ──────────────────────

def test_ingest_gives_channel_topics_their_own_longer_window():
    old = (NOW - timedelta(hours=100)).isoformat()

    def item(url, topic):
        return {"url": url, "title": "A headline that is long enough", "snippet": "", "outlet": "X", "domain": "x.test", "trust_rank": 3,
                "topic": topic, "published_at": old, "image_url": ""}
    r = _run(feeds.ingest(48, feeds=[], extra_items=[item("https://x.test/own", "ch:c1"), item("https://x.test/general", "world")],
                          windows={"ch:c1": 120}))
    assert r["feeds_total"] == 0 and r["fresh"] == 1 and r["new"] == 1
    assert _run(db.fetch_one("SELECT topic FROM articles WHERE url=?", ("https://x.test/own",)))["topic"] == "ch:c1"
    assert _run(db.fetch_one("SELECT 1 FROM articles WHERE url=?", ("https://x.test/general",))) is None


# ── feed API ─────────────────────────────────────────────────────────────────

def test_feed_lists_channel_stories_even_with_few_outlets_and_old(client, request):
    ch = client.post("/api/channels", json={"name": "Classical music"}).json()
    client.patch("/api/settings", json={"min_signal": 5})
    request.addfinalizer(lambda: (client.patch("/api/settings", json={"min_signal": 0}), client.delete(f"/api/channels/{ch['id']}")))   # even if an assert fails
    old = (NOW - timedelta(days=5)).isoformat()
    _run(db.execute(
        "INSERT OR REPLACE INTO stories(id,kind,created_at,updated_at,headline,dek,topic,tier,published_at,outlet_count,gate,extras,channel_id,why_for_you) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("chs1", "feed", old, old, "Berlin Philharmonic names new chief conductor", "A dek", "culture", "wire", old, 1,
         db.jdump({"rank": {"score": 0.5, "components": {"coverage": 0.0, "trust": 0.5}}}), "{}", ch["id"], "in your channel “Classical music”")))
    feed = client.get("/api/feed").json()
    cards = ([feed["lead"]] if feed["lead"] else []) + feed["stories"]
    card = next((s for s in cards if s["id"] == "chs1"), None)
    assert card, "channel story hidden by Signal or the 3-day window"
    assert card["topic"] == f"ch:{ch['id']}" and card["channel_name"] == "Classical music" and "channel" in card["why_for_you"]
    assert any(c["id"] == ch["id"] and c["name"] == "Classical music" and c["count"] == 1 for c in feed["channels"])
    assert client.get("/api/stories/chs1").json()["channel_name"] == "Classical music"        # the story page can label it too
    assert client.get("/api/feed", params={"topic": f"ch:{ch['id']}"}).json()["total"] == 1
    assert client.get("/api/feed/all", params={"topic": f"ch:{ch['id']}", "days": 7}).json()["total"] == 1
    client.patch(f"/api/channels/{ch['id']}", json={"weight": 0})                # switching a channel off hides its stories with it
    off = client.get("/api/feed").json()
    assert "chs1" not in [s["id"] for s in ([off["lead"]] if off["lead"] else []) + off["stories"] + off["more"]]
    client.patch(f"/api/channels/{ch['id']}", json={"weight": 2})
    client.patch("/api/settings", json={"min_signal": 0})
    client.delete(f"/api/channels/{ch['id']}")
    assert _run(db.fetch_one("SELECT channel_id FROM stories WHERE id='chs1'"))["channel_id"] == ""   # deleting a channel keeps its stories
    kept = client.get("/api/feed/all", params={"days": 7, "limit": 120}).json()["items"]
    assert "chs1" in [i["id"] for i in kept] and next(i for i in kept if i["id"] == "chs1")["topic"] == "culture"   # ...as ordinary news


def test_truncated_json_is_repaired_and_still_validated():
    from backend.services.llm import _parse_json
    cut = '{"headline": "A", "key_points": ["one", "two", "thr'
    obj = _parse_json(cut)
    assert obj["headline"] == "A" and obj["key_points"][:2] == ["one", "two"]
    assert _parse_json('{"a": 1,}') == {"a": 1}
    with pytest.raises(ValueError):
        _parse_json("sorry, I cannot help with that")


def test_thin_article_falls_back_to_other_outlets_snippets():
    from backend.services.story_builder import build_corpus
    texts = [{"url": "u1", "outlet": "A", "title": "T", "text": "x" * 300}]
    members = [{"url": "u1", "outlet": "A", "title": "T", "snippet": "s"},
               {"url": "u2", "outlet": "B", "title": "T2", "snippet": "Second outlet blurb " * 10}]
    corpus, plain = build_corpus(texts, members)
    assert "[2] B" in corpus and "Second outlet blurb" in plain
    big = [{**texts[0], "text": "y" * 2000}]
    assert "[2]" not in build_corpus(big, members)[0]


def test_bot_walled_fetch_falls_back_to_the_stealth_browser(monkeypatch):
    from backend.services import extract

    class R:
        status_code = 403
        headers = {"content-type": "text/html"}

    class C:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): ...
        async def get(self, url): return R()

    seen = []

    async def stealth(url):
        seen.append(url)
        return "<html>ok</html>"

    monkeypatch.setattr(extract.httpx, "AsyncClient", C)
    monkeypatch.setattr(extract, "_fetch_stealth", stealth)
    assert asyncio.run(extract._fetch("https://example.org/a")) == "<html>ok</html>" and seen == ["https://example.org/a"]
    R.status_code = 404                                     # a plain 404 is not a bot wall
    assert asyncio.run(extract._fetch("https://example.org/b")) is None and len(seen) == 1


def test_stealth_fetch_is_off_when_unconfigured(monkeypatch):
    from backend.services import extract
    monkeypatch.setattr(extract, "CAMOFOX", "")
    assert asyncio.run(extract._fetch_stealth("https://example.org")) is None


def test_missing_comma_and_code_fence_are_repaired():
    from backend.services.llm import _parse_json
    raw = '```json\n{\n  "headline": "A",\n  "what": "text ends here."\n\n  "tags": [\n    "x"\n    "y"\n  ]\n}\n```'
    assert _parse_json(raw) == {"headline": "A", "what": "text ends here.", "tags": ["x", "y"]}


def test_main_feed_shows_only_a_taste_of_each_channel(client, request):
    import asyncio as _a
    from backend import db
    from backend.services import channels as chs
    ch = _a.run(chs.create({"name": "Taste test", "description": "taste channel"}))
    request.addfinalizer(lambda: _a.run(chs.delete(ch["id"])))
    now = "2099-01-01T00:00:00+00:00"
    async def seed():
        for i in range(6):
            await db.execute(
                "INSERT INTO stories(id,kind,headline,dek,topic,tier,status,published_at,created_at,updated_at,channel_id,outlet_count) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"taste{i}", "feed", f"Taste story {i}", "d", "ch:" + ch["id"], "wire", "complete", now, now, now, ch["id"], 1))
    _a.run(seed())
    request.addfinalizer(lambda: _a.run(db.execute("DELETE FROM stories WHERE id LIKE 'taste%'")))
    main = client.get("/api/feed").json()
    shown = [s for s in ([main["lead"]] if main["lead"] else []) + main["stories"] if s["channel"] == ch["id"]]
    assert len(shown) <= 2
    own = client.get(f"/api/feed?topic=ch:{ch['id']}").json()
    assert len([s for s in ([own["lead"]] if own["lead"] else []) + own["stories"]]) == 6
