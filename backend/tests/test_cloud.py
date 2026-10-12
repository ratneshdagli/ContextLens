"""Cloud fallback: local failure → cloud chain; a failing provider is parked and the next one answers."""

import asyncio

import pytest

from backend.services import cloud, llm


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("CLOUD_FALLBACK", "1")
    for k in ("GROQ_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(k, "x")
    for k in ("NVIDIA_API_KEY", "OPENROUTER_API_KEY", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    cloud.clear()
    yield
    cloud.clear()


class _Resp:
    def __init__(self, code, text="ok"):
        self.status_code, self._t = code, text
        self.text = text

    def json(self):
        return {"choices": [{"message": {"content": self._t}}]}


def _fake_http(monkeypatch, codes):
    calls = []

    class C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kw):
            calls.append(url)
            return _Resp(codes.get(url.split("/")[2], 200))

    monkeypatch.setattr(cloud.httpx, "AsyncClient", C)
    return calls


def test_failing_provider_is_parked_and_next_answers(monkeypatch):
    calls = _fake_http(monkeypatch, {"api.groq.com": 429})
    text, who = _run(cloud.chat("writer", [], temperature=0.1, max_tokens=50))
    assert who.startswith("gemini") and text == "ok"      # groq 429 → gemini (writer order)
    assert cloud.status()["groq"] == "cooling"
    calls.clear()
    _run(cloud.chat("writer", [], temperature=0.1, max_tokens=50))
    assert all("groq" not in c for c in calls)            # parked: not retried


def test_editor_leads_with_a_different_family(monkeypatch):
    _fake_http(monkeypatch, {})
    assert _run(cloud.chat("editor", [], temperature=0.1, max_tokens=50))[1].startswith("mistral")


def test_all_fail_raises(monkeypatch):
    _fake_http(monkeypatch, {"api.groq.com": 500, "api.mistral.ai": 500, "generativelanguage.googleapis.com": 401})
    with pytest.raises(RuntimeError):
        _run(cloud.chat("writer", [], temperature=0.1, max_tokens=50))


def test_cloud_answers_first_even_when_local_is_up(monkeypatch):
    async def local(*a, **k):
        raise AssertionError("local must not be called when the cloud answers")
    monkeypatch.setattr(llm, "_chat_local", local)
    _fake_http(monkeypatch, {})
    assert _run(llm.chat("writer", [{"role": "user", "content": "hi"}])) == "ok"


def test_local_is_the_last_resort_when_every_cloud_fails(monkeypatch):
    async def local(*a, **k):
        return "from-local"
    monkeypatch.setattr(llm, "_chat_local", local)
    _fake_http(monkeypatch, {"api.groq.com": 500, "api.mistral.ai": 500, "generativelanguage.googleapis.com": 500})
    assert _run(llm.chat("writer", [{"role": "user", "content": "hi"}])) == "from-local"


def test_stream_is_cloud_first_then_local(monkeypatch):
    async def local_stream(*a, **k):
        yield "L1"
        yield "L2"
    monkeypatch.setattr(llm, "_stream_local", local_stream)
    async def collect():
        return [p async for p in llm.stream_chat("writer", [])]
    _fake_http(monkeypatch, {})
    assert _run(collect()) == ["ok"]
    cloud.clear()
    _fake_http(monkeypatch, {"api.groq.com": 500, "api.mistral.ai": 500, "generativelanguage.googleapis.com": 500})
    assert _run(collect()) == ["L1", "L2"]


def test_llm_chat_falls_back_when_local_down(monkeypatch):
    async def down(*a, **k):
        raise llm.LLMError("LM Studio unreachable")
    monkeypatch.setattr(llm, "_chat_local", down)
    _fake_http(monkeypatch, {})
    assert _run(llm.chat("writer", [{"role": "user", "content": "hi"}])) == "ok"


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("CLOUD_FALLBACK", "0")
    async def down(*a, **k):
        raise llm.LLMError("LM Studio unreachable")
    monkeypatch.setattr(llm, "_chat_local", down)
    with pytest.raises(llm.LLMError):
        _run(llm.chat("writer", [{"role": "user", "content": "hi"}]))
