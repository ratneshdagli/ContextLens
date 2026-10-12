"""v5 tests: live voice (intent rules, speech shaping, whole turns through the API), sources overview, browse-everything."""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import app
from backend.services import llm, voice


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def _clean_slate(client):
    """The test database is shared across files; the voice tests reason about 'the top stories', so start from none."""
    run(db.execute("DELETE FROM stories"))
    run(db.execute("DELETE FROM timeline_events"))


def _vec(seed):
    v = np.random.default_rng(seed).normal(size=64)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _story(sid, headline, dek, vec, *, built=False, explain=None, topic="india", outlets=4, hours_ago=1, events=()):
    at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()
    extras = json.dumps({"explain": explain} if explain else {})
    run(db.execute(
        "INSERT OR REPLACE INTO stories(id,kind,created_at,updated_at,headline,dek,what_happened,topic,tier,centroid,published_at,gate,extras,built_at,outlet_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "feed", at, at, headline, dek, dek, topic, "primary" if built else "wire", llm.to_blob(vec), at, "{}", extras, at if built else None, outlets)))
    run(db.execute("DELETE FROM timeline_events WHERE story_id=?", (sid,)))
    for d, t in events:
        run(db.execute("INSERT INTO timeline_events(story_id,date,precision,text,quote,source_url,source_outlet,kind) VALUES(?,?,?,?,?,?,?,?)",
                       (sid, d, "day", t, "", "https://x", "The Hindu", "reported")))


EXPLAIN = {
    "gist": "IIT Bombay suspended a professor after a student died, because police registered a case against him.",
    "why": [{"cause": "Student's death", "link": "So the police started investigating what happened.", "basis": "reported"},
            {"cause": "Family complaint", "link": "That meant a case was registered against the professor.", "basis": "reported"}],
    "so_what": "Students at the institute and the professor's colleagues are affected by the inquiry.",
    "next": ["The institute will follow due process in responding to the demand for suspension."], "v": 2,
}


def _events(resp):
    return [json.loads(p[6:]) for p in resp.text.split("\n\n") if p.startswith("data: ")]


def _spoken(evs):
    return " ".join(e["c"] for e in evs if e["t"] == "say")


# ── understanding + speech shaping ───────────────────────────────────────────

def test_classify_routes_common_spoken_requests():
    c = voice.classify
    assert c("Give me today's highlights") == "brief" and c("what's happening?") == "brief" and c("catch me up") == "brief"
    assert c("next") == "next" and c("skip this one") == "next" and c("hello") == "hello" and c("stop") == "stop"
    assert c("tell me more about this") == "deeper" and c("go deeper") == "deeper"
    assert c("how did we get here") == "history" and c("show me the timeline") == "history"
    assert c("what happens next") == "future"
    assert c("why did this happen", True) == "why" and c("why?", True) == "why"
    assert c("who is Sridhar Vembu") == "who"
    assert c("is the rupee going to fall against the dollar") == "question"


def test_speech_shaping_drops_markdown_urls_and_citations_and_keeps_abbreviations_together():
    t = voice.clean_for_speech("**Sensex** rose [S1] 500 points, see https://x.com/a — a jump of ₹1,200 crore & more.")
    assert "*" not in t and "[S1]" not in t and "http" not in t and "1,200 crore rupees" in t and " and more" in t
    s, rest = voice.split_ready("Dr. Rao met U.S. officials on Monday. The talks ended late. Nothing was")
    assert s == ["Dr. Rao met U.S. officials on Monday.", "The talks ended late."] and rest == "Nothing was"
    long = "First a long clause with many words that keeps going and going, then another clause that also keeps going for a good while, and a final one that just runs on and on until it must be cut somewhere sensible."
    parts = voice.speakable(long, max_len=120)
    assert len(parts) >= 2 and all(len(p) <= 140 for p in parts)


# ── whole turns through the API ──────────────────────────────────────────────

def test_highlights_are_read_from_stored_explanations_without_any_model(client, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("the highlights must not call a language model")
    monkeypatch.setattr(llm, "chat", boom)
    monkeypatch.setattr(llm, "stream_chat", boom)
    _story("vc1", "IIT Bombay faculty backs suspended professor", "Faculty marched in support.", _vec(1), built=True, explain=EXPLAIN)
    _story("vc2", "Sensex jumps as oil cools", "Markets rose on Friday as crude fell.", _vec(2), topic="markets")
    evs = _events(client.post("/api/voice/turn", json={"text": "give me the highlights"}))
    kinds = [e["t"] for e in evs]
    assert kinds[0] == "intent" and kinds[-1] == "done" and "turn_done" in kinds
    cards = [e["story"]["id"] for e in evs if e["t"] == "card"]
    assert {"vc1", "vc2"} <= set(cards)
    said = _spoken(evs)
    assert "Number 1." in said and "because police registered a case" in said            # the stored gist, not a fresh model answer
    focus = [e for e in evs if e["t"] == "focus"][-1]
    assert focus["story_id"] in cards and focus["queue"][:2] == cards[:2]
    # each card comes right before the sentences about it, so the screen follows what is being said
    assert kinds.index("card") < kinds.index("say", kinds.index("card"))


def test_why_and_history_and_next_use_the_story_in_focus(client):
    _story("vc1", "IIT Bombay faculty backs suspended professor", "Faculty marched in support.", _vec(1), built=True, explain=EXPLAIN,
           events=[("2026-09-18", "Sahil was found dead in his hostel room"), ("2026-09-21", "The director removed the professor as dean")])
    focus = {"story_id": "vc1", "queue": ["vc1", "vc2"], "pos": 0}
    said = _spoken(_events(client.post("/api/voice/turn", json={"text": "why did this happen", "focus": focus})))
    assert "First, Student's death." in said and "a case was registered" in said and "Students at the institute" in said
    said = _spoken(_events(client.post("/api/voice/turn", json={"text": "how did we get here", "focus": focus})))
    assert "18 September" in said and "hostel room" in said
    said = _spoken(_events(client.post("/api/voice/turn", json={"text": "what happens next", "focus": focus})))
    assert "due process" in said
    evs = _events(client.post("/api/voice/turn", json={"text": "next", "focus": focus}))
    assert [e for e in evs if e["t"] == "focus"][-1]["story_id"] == "vc2" and "Number 2." in _spoken(evs)


def test_unbuilt_story_is_explained_on_demand_and_the_talker_fills_the_wait(client, monkeypatch):
    _story("vc3", "Rice exports beat three-year average", "Exports to 26 markets rose.", _vec(3), outlets=7)
    started = []

    async def fake_wait(story_id, patience_s=900):
        started.append(story_id)
        await db.execute("UPDATE stories SET tier='primary', built_at=?, extras=? WHERE id=?", (db.now_iso(), json.dumps({"explain": EXPLAIN}), story_id))
        yield {"t": "status", "c": "working", "phase": "build"}
    monkeypatch.setattr(voice, "_wait_for_build", fake_wait)
    evs = _events(client.post("/api/voice/turn", json={"text": "tell me more", "web": False, "focus": {"story_id": "vc3", "queue": ["vc3"], "pos": 0}}))
    said = _spoken(evs)
    assert started == ["vc3"] and "only skimmed this one" in said and "7 outlets" in said       # talks while the build runs
    assert said.index("only skimmed") < said.index("First, Student's death.")
    kinds = [e["t"] for e in evs]
    assert kinds.index("turn_done") < kinds.index("status")                                      # the client may listen during the wait


def test_question_is_answered_from_stories_and_unsupported_sentences_are_never_spoken(client, monkeypatch):
    base = _vec(11)
    _story("vq1", "IIT Bombay professor suspended after student death", "Police registered a case against the professor.", base,
           built=True, explain=EXPLAIN)
    _story("vq2", "Cricket: India win the series", "India beat Australia 3-1.", _vec(12), topic="sports")

    async def fake_embed(texts, kind="document"):
        return np.stack([base for _ in texts])
    async def fake_stream(role, messages, **kw):
        for piece in ["The professor was suspended after the student died. ", "Aliens landed in Paris on Tuesday. ", "Police registered a case."]:
            yield piece
    monkeypatch.setattr(llm, "embed", fake_embed)
    monkeypatch.setattr(llm, "stream_chat", fake_stream)
    evs = _events(client.post("/api/voice/turn", json={"text": "will the professor face arrest soon?", "web": False}))
    said = _spoken(evs)
    assert "professor was suspended" in said and "Police registered a case" in said
    assert "Aliens" not in said and "Paris" not in said                                        # ungrounded sentence dropped before speaking
    assert evs[1]["t"] == "say"                                                                # an instant acknowledgement comes first
    assert any(e["t"] == "card" and e["story"]["id"] == "vq1" for e in evs) and [e["t"] for e in evs][-1] == "done"


def _mixed(base, cos, seed):
    """A unit vector at exactly `cos` similarity to `base` — simulates an embedding model putting two unrelated
    stories at a moderate-but-not-overwhelming cosine because they share generic vocabulary."""
    other = _vec(seed)
    other = other - (other @ base) * base
    other = other / np.linalg.norm(other)
    v = cos * base + np.sqrt(1 - cos ** 2) * other
    return (v / np.linalg.norm(v)).astype(np.float32)


def test_retrieval_rejects_a_semantically_close_but_unrelated_story(client, monkeypatch):
    """Regression: a question about a comedian's FIR once matched an unrelated actor's-death story at a moderate
    (not overwhelming) cosine — both are short "case"/"probe" stories — and the talker spoke the wrong facts as
    if they answered the question. A candidate that shares no real content word with the query must now be
    dropped unless the raw cosine similarity clears STRONG_SIM."""
    base = _vec(21)
    _story("vr1", "Sushant Singh Rajput's cousin welcomes fresh CBI probe into Disha Salian's death", "A cousin spoke to reporters.", base, built=True, outlets=2)
    moderate = _mixed(base, 0.65, 999)                           # above MIN_SIM (0.62), well below STRONG_SIM (0.80)

    async def fake_embed(texts, kind="document"):
        return np.stack([moderate for _ in texts])
    monkeypatch.setattr(llm, "embed", fake_embed)
    stories, best = run(voice._retrieve("what happened to samay raina fir case", None))
    assert stories == []                                                     # no shared word with "samay"/"raina"/"fir": dropped despite passing MIN_SIM

    # a query that actually shares a content word with the story is still found the same way
    stories2, _ = run(voice._retrieve("what happened in the sushant singh rajput case", None))
    assert stories2 and stories2[0]["id"] == "vr1"


def test_unmatched_question_says_so_and_falls_through_to_the_web_not_a_wrong_story(client, monkeypatch):
    base = _vec(22)
    _story("vr2", "Sushant Singh Rajput's cousin welcomes fresh CBI probe into Disha Salian's death", "A cousin spoke to reporters.", base, built=True, outlets=2)
    moderate = _mixed(base, 0.65, 998)

    async def fake_embed(texts, kind="document"):
        return np.stack([moderate for _ in texts])

    async def boom(*a, **k):
        raise AssertionError("must not generate an answer from an unrelated story's context")
    monkeypatch.setattr(llm, "embed", fake_embed)
    monkeypatch.setattr(llm, "stream_chat", boom)
    evs = _events(client.post("/api/voice/turn", json={"text": "what happened to samay raina fir case", "web": False}))
    said = _spoken(evs)
    assert "couldn't find that" in said
    assert not any(e["t"] == "card" and e["story"]["id"] == "vr2" for e in evs)   # the unrelated story is never surfaced as an answer


def test_a_voice_turn_marks_itself_interactive_so_it_outranks_background_pipeline_work(client):
    """A spoken question must jump ahead of whatever background pipeline work already queued on the shared
    GPU gate — otherwise a scheduled refresh in progress can make Lens go silent until it finishes. The check has
    to happen inside the same coroutine/task as the generator (a contextvar set in a child task never leaks back
    to its caller), so this drives `turn()` to its first event and reads the var right there."""
    async def scenario():
        llm.background()
        assert llm._prio.get() == 1
        gen = voice.turn("hello")
        try:
            await gen.__anext__()
            return llm._prio.get()
        finally:
            await gen.aclose()
    assert run(scenario()) == 0


def test_already_explained_story_is_read_out_without_calling_the_model(client, monkeypatch):
    base = _vec(41)
    _story("vf1", "Rice exports beat three-year average", "Exports to 26 markets rose.", base, built=True, explain=EXPLAIN)

    async def fake_embed(texts, kind="document"):
        return np.stack([base for _ in texts])
    async def boom(*a, **k):
        raise AssertionError("an explained story must be answered from storage, not by the model")
    monkeypatch.setattr(llm, "embed", fake_embed)
    monkeypatch.setattr(llm, "stream_chat", boom)
    monkeypatch.setattr(llm, "chat_json", boom)
    for q in ("what happened with the rice exports?", "tell me about rice exports", "any update on the rice export story"):
        evs = _events(client.post("/api/voice/turn", json={"text": q, "web": False}))
        said = _spoken(evs)
        assert "Here's why it happened" in said and "First, Student's death." in said, q
        assert [e for e in evs if e["t"] == "focus"][-1]["story_id"] == "vf1" and evs[-1]["t"] == "done"


def test_web_reasoner_adds_only_supported_new_information(client, monkeypatch):
    base = _vec(21)
    _story("vw1", "IIT Bombay professor suspended after student death", "Police registered a case against the professor.", base, built=True, explain=EXPLAIN)
    article = ("The institute set up an inquiry committee that will report its findings within two weeks. "
               "The committee includes two external members and a retired judge from the high court. "
               "Students held a candlelight march on campus on Sunday evening to demand answers.")

    async def fake_embed(texts, kind="document"):
        return np.stack([base for _ in texts])
    async def fake_stream(role, messages, **kw):
        yield "The professor was suspended after the student died."
    async def fake_searx(q):
        return [{"url": "https://www.thehindu.com/news/a", "title": "Inquiry set up", "snippet": article, "published_at": None},
                {"url": "https://www.youtube.com/x", "title": "clip", "snippet": "short", "published_at": None}]
    async def fake_fetch(a):
        return {"text": article * 2, "paywall": False}
    async def fake_json(role, system, user, schema, **kw):
        fid = re.search(r"(F\d+) \[[^\]]*\]: The institute set up", user).group(1)
        return {"sentences": [{"text": "The institute set up an inquiry committee that will report within two weeks.", "facts": [fid]},
                              {"text": "The prime minister resigned in Brazil after a coup.", "facts": [fid]}]}
    monkeypatch.setattr(llm, "embed", fake_embed)
    monkeypatch.setattr(llm, "stream_chat", fake_stream)
    monkeypatch.setattr(llm, "chat_json", fake_json)
    monkeypatch.setattr(voice, "_searx", fake_searx)
    monkeypatch.setattr(voice.extract, "fetch_full_text", fake_fetch)
    evs = _events(client.post("/api/voice/turn", json={"text": "will the professor face arrest soon?", "web": True}))
    kinds = [e["t"] for e in evs]
    assert kinds.index("turn_done") < kinds.index("dig")                                        # the talker finished before the reasoner started
    said = _spoken(evs)
    assert "I dug a little deeper. The institute set up an inquiry committee" in said and "Brazil" not in said
    src = next(e for e in evs if e["t"] == "sources")["items"]
    assert src and src[0]["outlet"] == "The Hindu" and src[0]["url"].startswith("https://www.thehindu.com")
    assert [e for e in evs if e["t"] == "dig"][-1]["state"] == "done"


def test_model_outage_is_spoken_not_thrown(client, monkeypatch):
    async def down(texts, kind="document"):
        raise llm.LLMError("offline")
    monkeypatch.setattr(llm, "embed", down)
    evs = _events(client.post("/api/voice/turn", json={"text": "what is going on with the rupee and oil prices", "web": False}))
    assert "can't reach my language model" in _spoken(evs) and evs[-1]["t"] == "done"


def test_suggestions_come_from_todays_stories(client):
    s = client.get("/api/voice/suggest").json()
    assert s["prompts"][0] == "Give me today's highlights" and len(s["prompts"]) >= 3 and s["top"]


# ── sources + browse everything ──────────────────────────────────────────────

def test_sources_overview_groups_outlets_and_counts_feeds(client):
    d = client.get("/api/sources/overview").json()
    assert d["totals"]["feeds"] >= 200 and d["totals"]["outlets"] >= 60
    groups = {g["key"]: g for g in d["groups"]}
    assert {"factcheck", "india", "world"} <= set(groups)
    assert {"Alt News", "BOOM"} & {o["name"] for o in groups["factcheck"]["outlets"]}
    assert all(o["trust_rank"] in (1, 2, 3) for g in d["groups"] for o in g["outlets"])
    assert client.get("/api/sources").status_code == 200                                         # the older plain list still works


def test_all_stories_can_be_filtered_by_topic_and_text(client):
    _story("va1", "Sensex jumps as oil cools", "Markets rose.", _vec(31), topic="markets")
    _story("va2", "Monsoon session begins in Parliament", "Bills listed.", _vec(32), topic="politics")
    allr = client.get("/api/feed/all?limit=100").json()
    ids = {i["id"] for i in allr["items"]}
    assert {"va1", "va2"} <= ids and allr["total"] >= 2 and allr["topic_counts"].get("markets", 0) >= 1
    only = client.get("/api/feed/all?topic=markets").json()
    assert only["items"] and all(i["topic"] == "markets" for i in only["items"]) and only["topic_counts"].get("politics", 0) >= 1
    hit = client.get("/api/feed/all?q=monsoon").json()
    assert [i["id"] for i in hit["items"]] == ["va2"]


def test_search_matches_all_words_in_any_order_including_unexplained_stories(client):
    _story("vs1", "IIT-Bombay faculty support suspended professor amid student suicide probe", "Faculty marched.", _vec(51))
    r = client.get("/api/search?q=IIT professor").json()
    assert "vs1" in [x["id"] for x in r]                                      # neither the phrase nor the tier has to match exactly
    assert [x["id"] for x in client.get("/api/search?q=professor IIT suicide").json()] == ["vs1"]
    assert client.get("/api/search?q=professor cricket").json() == []         # every word must be present
    assert "vs1" in [i["id"] for i in client.get("/api/feed/all?q=suspended IIT").json()["items"]]


# ── calm mode + fact-checks ──────────────────────────────────────────────────

def test_heavy_classifier_flags_graphic_news_but_not_trade_wars_or_sport():
    from backend.routers.feed import is_heavy
    assert is_heavy("Student found dead in hostel, police register case", "", "india")
    assert is_heavy("Airstrikes hit Kyiv overnight", "", "world") and is_heavy("Army attack on border post", "", "india")
    assert not is_heavy("US-China trade war eases as tariffs cut", "", "markets")          # "war" only counts in general news
    assert not is_heavy("India beat Australia, attack fires in the Powerplay", "", "sports")
    assert not is_heavy("Sensex jumps 564 points as oil cools", "Markets rose.", "markets")


def test_calm_mode_hides_heavy_stories_from_the_front_page_but_not_from_everything(client):
    _story("vh1", "Student found dead in hostel as police register case", "Investigation under way.", _vec(61))
    _story("vh2", "Rice exports beat three-year average", "Exports rose.", _vec(62), topic="business")
    normal = client.get("/api/feed").json()
    calm = client.get("/api/feed?calm=true").json()
    ids = lambda d: {c["id"] for c in ([d["lead"]] if d["lead"] else []) + d["stories"]}       # noqa: E731
    assert "vh1" in ids(normal) and "vh1" not in ids(calm) and "vh2" in ids(calm)
    assert calm["hidden"] >= 1 and normal["hidden"] == 0
    assert "vh1" in {i["id"] for i in client.get("/api/feed/all?limit=100").json()["items"]}      # nothing is deleted, only tucked away
    assert next(i for i in client.get("/api/feed/all?limit=100").json()["items"] if i["id"] == "vh1")["heavy"] is True


def test_fact_check_outlets_tag_a_story_and_can_be_filtered(client):
    _story("vk1", "Viral claim about free laptops is false", "Fact check.", _vec(71))
    _story("vk2", "Budget session begins", "Bills listed.", _vec(72), topic="politics")
    run(db.execute("UPDATE stories SET cluster_id='cl-vk1' WHERE id='vk1'"))
    run(db.execute("UPDATE stories SET cluster_id='cl-vk2' WHERE id='vk2'"))
    run(db.execute("DELETE FROM articles WHERE id LIKE 'fk%'"))
    now = db.now_iso()
    for aid, cid, dom, outlet in (("fk1", "cl-vk1", "altnews.in", "Alt News"), ("fk2", "cl-vk2", "thehindu.com", "The Hindu")):
        run(db.execute("INSERT INTO articles(id,url,url_hash,title,outlet,domain,trust_rank,published_at,fetched_at,cluster_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (aid, f"https://{dom}/{aid}", f"h{aid}", "t", outlet, dom, 2, now, now, cid)))
    only = client.get("/api/feed/all?factcheck=true&limit=50").json()
    assert [i["id"] for i in only["items"]] == ["vk1"] and only["items"][0]["factcheck"] is True
    d = client.get("/api/stories/vk1").json()
    assert any(s["factcheck"] for s in d["sources"]) or d["sources"] == []                     # wire stories list their articles
    feed = client.get("/api/feed").json()
    cards = {c["id"]: c for c in ([feed["lead"]] if feed["lead"] else []) + feed["stories"]}
    assert cards["vk1"]["factcheck"] is True and cards["vk2"]["factcheck"] is False


# ── resilience: a model unloaded underneath a call must not fail the story ───

import httpx as _httpx
_REAL_CLIENT = _httpx.AsyncClient          # captured once, so two mocks in one test do not stack


def _mock_lm(monkeypatch, handler):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    calls = {"ensure": 0}

    async def fake_resolve(role):
        return "m"

    async def fake_ensure(model):
        calls["ensure"] += 1
    monkeypatch.setattr(llm, "resolve_model", fake_resolve)
    monkeypatch.setattr(llm, "_ensure_resident", fake_ensure)
    return calls


def test_tests_can_never_reach_the_real_lms_cli():
    from backend.services import lms
    assert lms.find_cli() is None            # DISABLE_LMS=1 in conftest: running pytest must not unload the developer's models


def test_chat_reloads_the_model_and_retries_once_when_it_was_unloaded_underneath(monkeypatch):
    import httpx
    posts = []

    def handler(request):
        posts.append(1)
        if len(posts) == 1:
            return httpx.Response(400, json={"error": "Model unloaded by user or API request."})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]})
    calls = _mock_lm(monkeypatch, handler)
    assert run(llm.chat("writer", [{"role": "user", "content": "hi"}])) == "hello"
    assert len(posts) == 2 and calls["ensure"] == 2                          # once before the call, once to bring the model back

    posts.clear()

    def always_400(request):
        posts.append(1)
        return httpx.Response(400, json={"error": "Model is unloaded."})
    _mock_lm(monkeypatch, always_400)
    with pytest.raises(llm.LLMError):
        run(llm.chat("writer", [{"role": "user", "content": "hi"}]))
    assert len(posts) == 2                                                    # exactly one retry, never a loop


def test_stream_reloads_and_retries_once_too(monkeypatch):
    import httpx
    n = []

    def handler(request):
        n.append(1)
        if len(n) == 1:
            return httpx.Response(400, json={"error": "Model unloaded by user or API request."})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b'data: {"choices":[{"delta":{"content":"Hi there. "}}]}\n\ndata: [DONE]\n\n')
    _mock_lm(monkeypatch, handler)

    async def collect():
        return [p async for p in llm.stream_chat("writer", [{"role": "user", "content": "hi"}])]
    assert "".join(run(collect())) == "Hi there. " and len(n) == 2


# ── performance: selecting the front page must not freeze the server ─────────

def _naive_mmr(scored, k, lam=0.72):
    """The original O(k·n·k) algorithm, kept here as the reference the fast one must agree with."""
    pool = sorted(scored, key=lambda x: -x["score"])
    chosen = []
    while pool and len(chosen) < k:
        best, best_val = None, -1e9
        for cand in pool:
            div = max((float(cand["centroid"] @ ch["centroid"]) for ch in chosen if ch["centroid"].shape == cand["centroid"].shape), default=0.0)
            val = lam * cand["score"] - (1 - lam) * div
            if val > best_val:
                best, best_val = cand, val
        chosen.append(best)
        pool.remove(best)
    return chosen


def test_mmr_matches_the_reference_algorithm_and_is_fast():
    import time
    from backend.services import ranking
    rng = np.random.default_rng(7)
    base = rng.normal(size=(12, 64))
    def item(i):
        v = base[i % 12] + 0.6 * rng.normal(size=64)
        return {"id": i, "score": float(rng.uniform(0.2, 1.0)), "centroid": (v / np.linalg.norm(v)).astype(np.float32)}
    small = [item(i) for i in range(160)]
    fast, slow = ranking.mmr_select(small, 40), _naive_mmr(small, 40)
    assert {x["id"] for x in fast} == {x["id"] for x in slow}                         # same stories
    assert [x["id"] for x in fast[:8]] == [x["id"] for x in slow[:8]]                 # and the same lead order
    big = [item(i) for i in range(2200)]
    t = time.time()
    picked = ranking.mmr_select(big, 200)
    assert len(picked) == 200 and len({x["id"] for x in picked}) == 200 and time.time() - t < 3   # was ~60 s
    odd = small[:5] + [{"id": 999, "score": 0.9, "centroid": np.ones(32, dtype=np.float32) / np.sqrt(32)}]
    assert {x["id"] for x in ranking.mmr_select(odd, 6)} == {x["id"] for x in odd}   # a vector of another dimension never crashes it
    assert ranking.mmr_select([], 5) == [] and len(ranking.mmr_select(small[:3], 10)) == 3
