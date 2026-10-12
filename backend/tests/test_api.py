"""API integration tests: the real FastAPI app on a temporary SQLite DB, LLM server offline."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _seed_story(sid="s1", headline="France and Canada pledge closer ties amid Trump tensions", tier="primary"):
    async def go():
        now = db.now_iso()
        await db.execute(
            "INSERT OR REPLACE INTO stories(id,kind,created_at,updated_at,headline,headline_original,dek,what_happened,why_it_matters,"
            "key_points,topic,tags,outlet_count,tier,gate,status,published_at,extras) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "feed", now, now, headline, "Macron and Carney announce closer ties", "Leaders met off the Canadian coast to reset relations.",
             "Macron and Carney met on Saint-Pierre and Miquelon. They announced plans for stronger ties.", "It signals a shift away from Washington.",
             db.jdump(["Energy, space and defence were named as priority areas"]), "world", db.jdump(["Diplomacy"]), 6, tier,
             db.jdump({"rank": {"score": 0.7, "components": {"coverage": 0.8, "trust": 0.8}}, "triage": {"answers": {"impact": "major"}}}),
             "complete", now, "{}"))
        await db.execute("INSERT INTO story_sources(story_id,n,url,outlet,domain,title,trust_rank,published_at,excerpt,has_full_text) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?)", (sid, 1, "https://example.com/a", "BBC News", "bbc.com", "Macron and Carney", 1, now, "excerpt", 1))
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(go())


def test_health_reports_llm_offline_gracefully(client):
    r = client.get("/api/health").json()
    assert r["status"] == "ok" and r["llm"]["available"] is False


def test_onboarding_flow_builds_profile(client):
    assert client.get("/api/onboarding/status").json() == {"complete": False}
    p = client.post("/api/onboarding", json={"topics": ["tech", "markets"], "about": "AI chips, Indian economy; Formula 1",
                                              "muted": ["horoscope"]}).json()
    assert p["onboarded"] and p["topics"]["tech"] == 2 and p["topics"]["culture"] == 1
    assert [i["text"] for i in p["interests"]] == ["AI chips", "Indian economy", "Formula 1"]
    assert client.get("/api/onboarding/status").json() == {"complete": True}


def test_profile_patch_validates_and_clamps(client):
    p = client.patch("/api/profile", json={"topics": {"tech": 99, "bogus": 3}, "muted_keywords": ["  celebrity ", "celebrity"],
                                           "followed": ["Nvidia"]}).json()
    assert p["topics"]["tech"] == 3 and "bogus" not in p["topics"]
    assert p["muted_keywords"] == ["celebrity"] and p["followed"] == ["Nvidia"]


def test_settings_roundtrip_and_clamping(client):
    s = client.patch("/api/settings", json={"strictness": "nonsense", "feed_size": 999, "min_signal": 42, "auto_refresh": False}).json()
    assert s["strictness"] == "balanced" and s["feed_size"] == 40 and s["min_signal"] == 9 and s["auto_refresh"] is False
    client.patch("/api/settings", json={"min_signal": 0, "feed_size": 14})


def test_empty_feed_is_well_formed(client):
    f = client.get("/api/feed").json()
    assert f["lead"] is None and f["stories"] == [] and f["more"] == []


def test_feed_story_detail_save_and_search(client):
    _seed_story()
    f = client.get("/api/feed").json()
    assert f["lead"]["headline"].startswith("France and Canada") and 1 <= f["lead"]["signal"] <= 10
    d = client.get("/api/stories/s1").json()
    assert d["sources"][0]["outlet"] == "BBC News" and d["timeline"] == [] and d["graph"] is None
    assert client.post("/api/stories/s1/save").json() == {"saved": True}
    assert [c["id"] for c in client.get("/api/saved").json()] == ["s1"]
    assert client.get("/api/search", params={"q": "Canada"}).json()[0]["id"] == "s1"
    assert client.delete("/api/stories/s1/save").json() == {"saved": False}


def test_min_signal_hides_low_signal_primary_stories(client):
    _seed_story("s2", "A minor story with limited coverage worth little", "primary")
    client.patch("/api/settings", json={"min_signal": 9})
    ids = [c["id"] for c in ([client.get("/api/feed").json()["lead"]] + client.get("/api/feed").json()["stories"]) if c]
    assert "s2" not in ids
    client.patch("/api/settings", json={"min_signal": 0})


def test_unknown_story_is_404_and_feedback_validated(client):
    assert client.get("/api/stories/nope").status_code == 404
    assert client.post("/api/stories/s1/feedback", json={"action": "explode"}).status_code == 400
    assert client.post("/api/stories/s1/feedback", json={"action": "open"}).json() == {"ok": True}


def test_ask_refuses_without_llm_instead_of_bluffing(client):
    r = client.post("/api/ask", json={"question": "What happened with France and Canada?"})
    assert r.status_code == 200
    assert '"t": "error"' in r.text or "doesn" in r.text or "empty" in r.text      # never a fabricated answer


def test_research_rejects_short_queries(client):
    assert client.post("/api/research", json={"query": "a"}).status_code == 400


def test_clear_stories_keeps_profile(client):
    assert client.post("/api/data/clear-stories").json() == {"ok": True}
    assert client.get("/api/feed").json()["lead"] is None
    assert client.get("/api/profile").json()["onboarded"] is True


def test_spa_fallback_and_api_404(client):
    assert client.get("/api/does-not-exist").status_code in (404, 405)
