"""Translation of short cards: the gate refuses changed numbers and untranslated echoes; the endpoint caches and fails closed."""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import app
from backend.routers.discover import _tr_ok
from backend.services import llm

NOW = datetime.now(timezone.utc).isoformat()
EN = "RBI raised rates by 25 basis points on 7 October, the first rise since 2023."
HI = "आरबीआई ने 7 अक्टूबर को दरों में 25 बेसिस पॉइंट की बढ़ोतरी की, जो 2023 के बाद पहली है।"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_gate_refuses_changed_numbers_and_untranslated_text():
    assert _tr_ok(EN, HI, "hi") is None
    assert _tr_ok(EN, HI.replace("25", "52"), "hi") == "numbers changed"
    assert _tr_ok(EN, EN, "hi") == "not in the target script"


def test_endpoint_translates_once_and_rejects_unknown_language(client, monkeypatch):
    body = EN + " Markets fell after the announcement, and analysts expect further moves."
    asyncio.new_event_loop().run_until_complete(db.execute(
        "INSERT INTO stories(id,kind,created_at,updated_at,headline,headline_original,topic,tier,what_happened) VALUES(?,?,?,?,?,?,?,?,?)",
        ("g-tr", "feed", NOW, NOW, "RBI raises rates by 25 bps", "RBI raises rates by 25 bps", "business", "primary", body)))
    calls = []

    async def fake(role, system, user, schema, **kw):
        calls.append(1)
        out = {"headline": "आरबीआई ने दरें 25 बीपीएस बढ़ाईं", "body": HI + " बाजार में गिरावट आई और विश्लेषकों को आगे और कदम की उम्मीद है।"}
        err = kw["validate"](out)
        if err:
            raise llm.LLMError(err)
        return out
    monkeypatch.setattr(llm, "chat_json", fake)
    r = client.post("/api/translate/g-tr", json={"lang": "hi"})
    assert r.status_code == 200 and "आरबीआई" in r.json()["headline"]
    client.post("/api/translate/g-tr", json={"lang": "hi"})
    assert len(calls) == 1                                          # the second ask comes from the cache
    assert client.post("/api/translate/g-tr", json={"lang": "xx"}).status_code == 400
