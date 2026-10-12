"""v6 tests: the personal digest (tracked GitHub repos and topics)."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import app
from backend.services import llm, watch


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_slate(client):
    run(db.execute("DELETE FROM watches"))


import httpx as _httpx  # noqa: E402
_REAL_CLIENT = _httpx.AsyncClient


def _mock_http(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def test_parse_repo_accepts_slug_and_url_forms():
    assert watch.parse_repo("facebook/react") == "facebook/react"
    assert watch.parse_repo("https://github.com/facebook/react") == "facebook/react"
    assert watch.parse_repo("https://github.com/facebook/react.git") == "facebook/react"
    assert watch.parse_repo("https://github.com/facebook/react/") == "facebook/react"
    assert watch.parse_repo("not a repo") is None
    assert watch.parse_repo("") is None


def test_add_repo_rejects_a_repo_that_does_not_exist(monkeypatch):
    def handler(request):
        return httpx.Response(404, json={"message": "Not Found"})
    _mock_http(monkeypatch, handler)
    with pytest.raises(ValueError, match="wasn't found"):
        run(watch.add_repo("nope/nothing-here"))


def test_add_repo_stores_it_and_refresh_summarizes_real_commits_only(client, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[
                {"commit": {"message": "Fix crash on empty input\n\nlonger body", "author": {"name": "Ann"}}, "author": {"login": "ann"}},
                {"commit": {"message": "Add dark mode toggle", "author": {"name": "Bo"}}, "author": {"login": "bo"}},
            ])
        return httpx.Response(200, json={"description": "A demo repo", "stargazers_count": 42, "default_branch": "main"})
    _mock_http(monkeypatch, handler)

    async def fake_chat_json(role, system, user, schema, **kw):
        assert "Fix crash" in user and "dark mode" in user
        return {"summary": "Two small fixes landed this week.", "highlights": ["Fixed a crash on empty input", "Added a dark mode toggle"]}
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)

    w = run(watch.add_repo("octo/demo"))
    assert w["kind"] == "repo" and w["label"] == "octo/demo"
    row = run(watch.get_watch(w["id"]))
    assert row["meta"]["description"] == "A demo repo" and row["meta"]["stars"] == 42

    digest = run(watch.refresh(row))
    assert digest["count"] == 2 and "Two small fixes" in digest["summary"] and len(digest["highlights"]) == 2

    listed = client.get("/api/digest").json()["items"]
    assert any(x["id"] == w["id"] and x["digest"]["count"] == 2 for x in listed)


def test_add_repo_twice_is_rejected(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"description": "", "stargazers_count": 0, "default_branch": "main"})
    _mock_http(monkeypatch, handler)
    run(watch.add_repo("octo/again"))
    with pytest.raises(ValueError, match="Already tracking"):
        run(watch.add_repo("octo/again"))


def test_repo_with_no_recent_commits_says_so_without_calling_the_model(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"description": "", "stargazers_count": 0, "default_branch": "main"})
    _mock_http(monkeypatch, handler)

    async def boom(*a, **k):
        raise AssertionError("no commits means nothing to summarize")
    monkeypatch.setattr(llm, "chat_json", boom)
    w = run(watch.add_repo("octo/quiet"))
    row = run(watch.get_watch(w["id"]))
    digest = run(watch.refresh(row))
    assert digest["count"] == 0 and "No commits" in digest["summary"]


def test_topic_watch_lists_only_this_apps_own_matching_stories(client, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import numpy as np

    def vec(seed):
        v = np.random.default_rng(seed).normal(size=32)
        return (v / np.linalg.norm(v)).astype(np.float32)

    base = vec(5)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    run(db.execute(
        "INSERT OR REPLACE INTO stories(id,kind,created_at,updated_at,headline,dek,what_happened,topic,tier,centroid,published_at,gate,extras,outlet_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("wt1", "feed", now, now, "New WebGPU renderer ships in the browser", "It's faster.", "It's faster.",
         "tech", "primary", llm.to_blob(base), now, "{}", "{}", 3)))
    old = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
    run(db.execute(
        "INSERT OR REPLACE INTO stories(id,kind,created_at,updated_at,headline,dek,what_happened,topic,tier,centroid,published_at,gate,extras,outlet_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("wt2", "feed", old, old, "Old WebGPU story", "Old.", "Old.", "tech", "primary", llm.to_blob(base), old, "{}", "{}", 3)))

    async def fake_embed(texts, kind="query"):
        return np.stack([base for _ in texts])
    monkeypatch.setattr(llm, "embed", fake_embed)
    w = run(watch.add_topic("WebGPU", topics=["WebGPU"], source="typed"))
    row = run(watch.get_watch(w["id"]))
    digest = run(watch.refresh(row))
    ids = {i["id"] for i in digest["items"]}
    assert "wt1" in ids and "wt2" not in ids                    # matches on topic, but only within the 7-day window


def test_delete_watch_removes_it(client, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"description": "", "stargazers_count": 0, "default_branch": "main"})
    _mock_http(monkeypatch, handler)
    w = run(watch.add_repo("octo/bye"))
    assert client.delete(f"/api/digest/{w['id']}").json() == {"ok": True}
    assert run(watch.get_watch(w["id"])) is None


def test_add_doc_pulls_topics_with_the_model_and_tracks_them(monkeypatch):
    async def fake_chat_json(role, system, user, schema, **kw):
        assert "Rust" in user
        return {"topics": ["Rust", "WebAssembly"]}
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    text = ("This document is about the Rust programming language and how it compiles to WebAssembly "
            "for fast, safe code in the browser. " * 20)
    w = run(watch.add_doc("notes.txt", text.encode("utf-8")))
    assert w["kind"] == "topic" and w["label"] == "notes.txt"
    row = run(watch.get_watch(w["id"]))
    assert row["meta"]["source"] == "doc" and row["meta"]["topics"] == ["Rust", "WebAssembly"]
