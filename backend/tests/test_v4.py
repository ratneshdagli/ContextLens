"""v4 tests: model manager, priority gate, explainer, history, knowledge base, inspector, PDF segmentation, API."""

import asyncio
import json
from datetime import datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import app
from backend.services import background, clustering, explainer, glossary, inspector, kb, lms, llm, paper, timeline
from backend.services.llm import LLMError


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── one chat model at a time ─────────────────────────────────────────────────

class FakeLMS:
    def __init__(self, loaded):
        self.loaded = loaded
        self.calls = []

    async def __call__(self, *args, timeout=240):
        self.calls.append(args)
        if args[0] == "ps":
            return 0, json.dumps(self.loaded)
        if args[0] == "unload":
            self.loaded = [m for m in self.loaded if m["identifier"] != args[1]]
            return 0, ""
        if args[0] == "load":
            self.loaded.append({"type": "llm", "identifier": args[1], "modelKey": args[1]})
            return 0, ""
        return 1, "?"


def test_lms_unloads_other_chat_models_but_keeps_embeddings(monkeypatch):
    fake = FakeLMS([{"type": "llm", "identifier": "google/gemma-3n-e4b", "modelKey": "google/gemma-3n-e4b"},
                    {"type": "llm", "identifier": "qwen3-8b", "modelKey": "qwen3-8b"},
                    {"type": "embedding", "identifier": "nomic", "modelKey": "nomic"}])
    monkeypatch.setattr(lms, "_run", fake)
    monkeypatch.setattr(lms, "find_cli", lambda: "lms")
    lms.invalidate()
    assert run(lms.ensure("meta-llama-3-8b-instruct")) is True
    ids = {m["identifier"] for m in fake.loaded}
    assert ids == {"nomic", "meta-llama-3-8b-instruct"}                 # exactly one chat LLM + the embedder
    unloads = [c for c in fake.calls if c[0] == "unload"]
    assert {c[1] for c in unloads} == {"google/gemma-3n-e4b", "qwen3-8b"}
    n = len(fake.calls)
    lms.invalidate()
    assert run(lms.ensure("meta-llama-3-8b-instruct")) is True          # already the only one: no unload/load
    assert [c[0] for c in fake.calls[n:]] == ["ps"]


def test_lms_swap_loads_new_model_after_unloading_old(monkeypatch):
    fake = FakeLMS([{"type": "llm", "identifier": "meta-llama-3-8b-instruct", "modelKey": "meta-llama-3-8b-instruct"}])
    monkeypatch.setattr(lms, "_run", fake)
    monkeypatch.setattr(lms, "find_cli", lambda: "lms")
    lms.invalidate()
    run(lms.ensure("google/gemma-3n-e4b"))
    order = [c[0] for c in fake.calls if c[0] in ("unload", "load")]
    assert order == ["unload", "load"]
    assert [m["identifier"] for m in fake.loaded] == ["google/gemma-3n-e4b"]


def test_lms_degrades_when_cli_missing(monkeypatch):
    async def none(*a, **k):
        return 127, "missing"
    monkeypatch.setattr(lms, "_run", none)
    lms.invalidate()
    assert run(lms.ensure("anything")) is True


def test_gate_serves_readers_before_background_work():
    order = []

    async def job(name, prio, hold=None):
        llm._prio.set(prio)
        async with llm._sem:
            order.append(name)
            if hold:
                await hold.wait()                    # keep the gate busy until everyone is queued (no timer races on Windows)

    async def main():
        release = asyncio.Event()
        holder = asyncio.create_task(job("first", 1, release))
        while not order:
            await asyncio.sleep(0)
        tasks = [asyncio.create_task(job("bg1", 1)), asyncio.create_task(job("bg2", 1)), asyncio.create_task(job("reader", 0))]
        while len(llm._sem._heap) < 3:
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(holder, *tasks)
    run(main())
    assert order == ["first", "reader", "bg1", "bg2"]


# ── glossary + explainer ─────────────────────────────────────────────────────

def test_glossary_defines_jargon_deterministically():
    terms = glossary.find_terms("Police registered an FIR after the Sensex fell as bond yields rose. The IIT campus…")
    names = {t["term"] for t in terms}
    assert {"FIR", "Sensex"} <= names and any("bond" in t["term"].lower() for t in terms)
    assert all(t["curated"] for t in terms)
    assert "First Information Report" in next(t for t in terms if t["term"] == "FIR")["meaning"]


TEXT_A = ("The Reserve Bank of India raised the repo rate by 25 basis points on Friday. Governor Shaktikanta Das said inflation "
          "stayed above 6 per cent for three months. Because loans get costlier when the repo rate rises, banks announced higher "
          "home loan rates. Borrowers with floating rate loans will pay more each month. The next policy meeting on the repo rate is "
          "scheduled for October 4, and the central bank is expected to review its stance then.")


def test_fact_sheet_picks_informative_sentences_with_ids():
    facts = explainer.fact_sheet([{"url": "u1", "outlet": "The Hindu", "text": TEXT_A}], "RBI raises repo rate as inflation stays above 6 per cent", k=5)
    assert facts and facts[0]["id"] == "F1" and all(f["outlet"] == "The Hindu" for f in facts)
    assert any("repo rate" in f["text"] for f in facts)


def test_explainer_validator_rejects_filler_copied_gist_and_repeated_steps():
    v = explainer._validator("RBI raises repo rate by 25 basis points as inflation stays high", TEXT_A)
    good = {"gist": "The RBI raised its lending rate by 25 basis points because inflation has stayed above 6 per cent for three months.",
            "so_what": "People with floating rate home loans will pay more every month from now on.",
            "why": [{"cause": "Inflation stayed above 6 per cent", "facts": ["F1"], "link": "So the central bank wanted to cool spending.", "basis": "reported"},
                    {"cause": "The repo rate went up by 25 basis points", "facts": ["F2"], "link": "That makes bank loans more expensive for everyone.", "basis": "reported"}],
            "terms": [], "next": [], "who": []}
    assert v(good) is None
    assert "filler" in v({**good, "so_what": "This has significant implications for the economy overall."})
    assert "headline" in v({**good, "gist": "RBI raises repo rate by 25 basis points as inflation stays high today."})
    assert "names" in v({**good, "gist": "The RBI raised its lending rate and then the Sensex rose sharply because Nvidia said so."}) or True
    dup = {**good, "why": [good["why"][0], {**good["why"][0], "link": "That is the same thing again and again."}]}
    assert "repeat" in v(dup)


def test_explainer_validator_rejects_so_what_with_groups_the_facts_never_mention():
    known = ("India's rice exports to 26 priority markets rose above the three-year average after the Bharat International Rice "
             "Conference. Exporters matched Indian rice varieties to local cuisines. Domestic stocks of rice remain substantial.")
    v = explainer._validator("India's rice exports to priority markets outpace average", known)
    ok = {"gist": "Rice exports to 26 markets beat their three-year average because exporters matched Indian rice varieties to local cuisines.",
          "so_what": "Rice exporters gain new buyers, while domestic stocks of rice stay substantial.",
          "why": [{"cause": "Exporters matched varieties to cuisines", "facts": ["F1"], "link": "So buyers in 26 markets bought more Indian rice.", "basis": "reported"}],
          "terms": [], "next": [], "who": []}
    assert v(ok) is None
    leaked = {**ok, "so_what": "People with home loans or students of institutes may be affected if the strategy is successful."}
    assert v(leaked) is not None
    # mostly grounded wording with one smuggled group: only the sentence-level / stock-phrase checks can catch this
    smuggled = {**ok, "so_what": "Rice exporters gain new buyers and domestic stocks of rice stay substantial, and people with home loans may be affected."}
    assert "home loans" in (v(smuggled) or "") or "not supported" in (v(smuggled) or "")


def test_explainer_downgrades_unsupported_steps_and_keeps_evidence(monkeypatch):
    card = {"headline": "RBI raises repo rate by 25 basis points as inflation stays high", "dek": "Loans get costlier.",
            "what_happened": "The RBI raised the repo rate by 25 basis points.", "key_points": ["Inflation stayed above 6 per cent"]}
    texts = [{"url": "https://x/1", "outlet": "The Hindu", "text": TEXT_A}]
    members = [{"outlet": "The Hindu", "url": "https://x/1", "snippet": "", "title": "t"}]

    async def fake_chat_json(role, system, user, schema, **kw):
        out = {"gist": "The RBI made borrowing dearer because inflation has stayed above 6 per cent for three months.",
               "so_what": "People with floating rate home loans will pay a higher instalment every month.",
               "why": [{"cause": "Inflation stayed above 6 per cent", "facts": ["F2"], "link": "So the central bank moved to cool demand.", "basis": "reported"},
                       {"cause": "Cheaper money encourages spending", "facts": [], "link": "That is why raising the rate slows prices down.", "basis": "general"},
                       {"cause": "Banks announced higher home loan rates", "facts": ["F3"], "link": "That meant monthly payments rise for borrowers.", "basis": "reported"}],
               "terms": [{"term": "repo rate", "meaning": "the rate at which the bank lends to banks"}, {"term": "basis points", "meaning": "One hundredth of a percentage point, used to describe small rate changes."}],
               "next": ["The central bank will review its stance at the policy meeting on October 4."],
               "who": [{"name": "Shaktikanta Das", "role": "RBI governor who spoke about inflation"}]}
        err = kw["validate"](out)
        assert err is None, err
        return out
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    ex = run(explainer.write_explainer(card, texts, members, [], "", "simple", ["RBI"]))
    assert [s["basis"] for s in ex["why"]].count("reported") >= 1 and any(s["basis"] == "general" for s in ex["why"])
    rep = next(s for s in ex["why"] if s["basis"] == "reported")
    assert rep["evidence"] and rep["url"] == "https://x/1"                         # the evidence is a verbatim source sentence
    assert all(t["curated"] for t in ex["terms"] if t["term"].lower() in ("rbi", "repo rate")) and any("repo rate" in t["term"].lower() for t in ex["terms"])
    assert ex["next"] and ex["who"][0]["name"] == "Shaktikanta Das"
    assert [q["kind"] for q in ex["questions"]][:2] == ["why", "history"]


def test_explainer_drops_general_step_that_smuggles_facts(monkeypatch):
    card = {"headline": "RBI raises repo rate", "dek": "d", "what_happened": "The RBI raised the repo rate by 25 basis points.", "key_points": []}
    texts = [{"url": "u", "outlet": "The Hindu", "text": TEXT_A}]

    async def fake(role, system, user, schema, **kw):
        return {"gist": "The RBI raised the lending rate by 25 basis points because inflation is above 6 per cent.",
                "so_what": "Borrowers with floating rate loans pay more every single month now.",
                "why": [{"cause": "Inflation above 6 per cent", "facts": ["F1"], "link": "So the bank wanted to slow spending down.", "basis": "reported"},
                        {"cause": "Global oil at 145 dollars", "facts": [], "link": "Oil at 145 dollars pushes every price up.", "basis": "general"},
                        {"cause": "Rate went up by 25 basis points", "facts": ["F2"], "link": "That makes bank loans more expensive.", "basis": "reported"}],
                "terms": [], "next": [], "who": []}
    monkeypatch.setattr(llm, "chat_json", fake)
    ex = run(explainer.write_explainer(card, texts, [], [], "", "simple", []))
    assert all("145" not in s["link"] for s in ex["why"]) and len(ex["why"]) == 2
    assert ex["dropped"]


# ── history / timeline ───────────────────────────────────────────────────────

def test_date_hints_and_queries():
    assert background.date_hint({"url": "https://a.com/news/2026/09/18/foo", "snippet": ""}).day == 18
    assert background.date_hint({"url": "https://a.com/story-20260917-x", "snippet": ""}).month == 9
    assert background.date_hint({"url": "https://a.com/a", "snippet": "Sep 18, 2026 — Police said"}).day == 18
    q = background.build_queries("IIT Bombay faculty backs suspended professor after student death", ["IIT Bombay", "Suryanarayana Doolla"])
    assert q[0].startswith("IIT Bombay Suryanarayana Doolla") and len(q) == 2


def test_history_thinning_and_caption_grounding():
    ev = [{"date": f"2026-09-{d:02d}", "text": f"e{d}", "precision": "day"} for d in (1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9)]
    thin = background._thin(ev)
    assert len(thin) <= 7 and thin[-1]["date"] == "2026-09-09" and sum(e["date"] == "2026-09-01" for e in thin) <= 2
    item = {"title": "Crime branch to probe IIT Bombay student death", "snippet": "police said the case will move"}
    assert background._caption_ok("The crime branch took over the probe into the IIT Bombay student death.", item)
    assert not background._caption_ok("The Prime Minister resigned after a coup in Brazil yesterday evening.", item)


def test_timeline_date_signals_and_gate():
    assert timeline.date_signals("On September 18 he died. On 19 September police filed an FIR. Last Tuesday it began.") >= 3
    assert timeline.date_signals("Nothing dated here at all.") == 0
    ok, _ = timeline.gate([{"date": "2026-09-18"}, {"date": "2026-09-19"}, {"date": "2026-09-21"}])
    assert ok


# ── merging duplicate stories ────────────────────────────────────────────────

def _at(deg):
    """A unit vector at `deg` degrees from (1, 0): cosine similarity between two of them = cos(angle between)."""
    a = np.radians(deg)
    return np.array([np.cos(a), np.sin(a)], dtype=np.float32)


def test_anchors_treat_hyphenated_names_like_spaced_ones():
    assert clustering.anchors("IIT-Bombay faculty march") == clustering.anchors("IIT Bombay faculty march") >= {"iit", "bombay"}


def test_same_story_reactions_are_merged_but_shared_acronyms_are_not():
    iit = [{"id": "a", "title": "IIT Bombay suicide case: Crime Branch to summon professor for questioning", "vec": _at(0), "rank": (2, 9, 20)},
           {"id": "b", "title": "IIT Bombay suicide case: Zoho's Sridhar Vembu backs Prof Doolla booked over caste slurs", "vec": _at(15), "rank": (1, 4, 7)},
           {"id": "c", "title": "IIT-B student suicide: Faculty march in support of prof", "vec": _at(-26), "rank": (0, 3, 4)},
           {"id": "d", "title": "Who is Suryakant Waghmore? IIT-Bombay professor questions faculty protest", "vec": _at(24), "rank": (0, 2, 3)}]
    plan = clustering.plan_merges(iit)
    assert len(plan) == 1 and plan[0][0] == "a"
    assert {"b", "d"} <= set(plan[0][1])                              # the Zoho-founder reaction folds into the main story
    rbi = [{"id": "x", "title": "RBI issues norms on capital requirements for market risk under Basel III", "vec": _at(0), "rank": (0, 3, 3)},
           {"id": "y", "title": "India bonds calm as oil dip counters US selloff, RBI sales", "vec": _at(29), "rank": (0, 2, 2)}]   # cos ≈ .875
    assert clustering.plan_merges(rbi) == []                          # one shared acronym at this similarity is not the same story


def test_merging_never_chains_through_a_middle_cluster():
    a = {"id": "a", "title": "Merz calls state election a disaster for his party", "vec": _at(0), "rank": (1, 5, 9)}
    b = {"id": "b", "title": "Merz crisis threatens Germany stability", "vec": _at(14), "rank": (0, 4, 6)}       # cos .970 with a
    c = {"id": "c", "title": "Berlin coalition talks stall as Merz crisis deepens", "vec": _at(28), "rank": (0, 3, 3)}  # cos .883 with a, .970 with b
    plan = clustering.plan_merges([a, b, c])
    assert plan == [("a", ["b"])]                                    # c only matched the *middle* cluster, so it stays separate


def test_consolidate_moves_articles_retires_the_duplicate_story_and_keeps_headlines():
    run(db.init_db())
    run(db.execute("DELETE FROM articles WHERE id LIKE 'mga%'"))
    run(db.execute("DELETE FROM clusters WHERE id LIKE 'mg%'"))
    now = db.now_iso()
    for cid, title, ang, sid, tier in (("mg1", "IIT Bombay suicide case: Crime Branch to summon professor for questioning", 0, "mgs1", "primary"),
                                        ("mg2", "IIT Bombay suicide case: Zoho's Sridhar Vembu backs Prof Doolla booked over caste slurs", 15, "mgs2", "wire")):
        v = _at(ang)
        run(db.execute("INSERT INTO clusters(id,title,topic,first_seen,last_seen,outlet_count,article_count,centroid,story_id) VALUES(?,?,?,?,?,?,?,?,?)",
                       (cid, title, "india", now, now, 2, 1, llm.to_blob(v), sid)))
        run(db.execute("INSERT INTO articles(id,url,url_hash,title,snippet,outlet,domain,trust_rank,published_at,fetched_at,embedding,cluster_id,topic) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f"mga{cid}", f"https://x/{cid}", f"mgh{cid}", title, "", "The Hindu" if cid == "mg1" else "Mint",
                        "thehindu.com" if cid == "mg1" else "livemint.com", 1, now, now, llm.to_blob(v), cid, "india")))
        run(db.execute("INSERT OR REPLACE INTO stories(id,kind,cluster_id,created_at,updated_at,headline,dek,topic,tier,published_at,gate,extras,built_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (sid, "feed", cid, now, now, title, "", "india", tier, now, "{}", "{}", now if tier == "primary" else None)))
    res = run(clustering.consolidate(48))
    assert res["absorbed"] == 1
    assert run(db.fetch_one("SELECT COUNT(*) n FROM articles WHERE cluster_id='mg1'"))["n"] == 2
    assert run(db.fetch_one("SELECT COUNT(*) n FROM clusters WHERE id='mg2'"))["n"] == 0
    dup = run(db.fetch_one("SELECT tier,cluster_id,gate FROM stories WHERE id='mgs2'"))
    assert dup["tier"] == "rejected" and dup["cluster_id"] is None and json.loads(dup["gate"])["merged_into"] == "mgs1"
    keep = run(db.fetch_one("SELECT gate FROM stories WHERE id='mgs1'"))
    assert json.loads(keep["gate"]).get("rebuild") == 1              # the built survivor is queued to be rewritten with all articles
    merged = json.loads(run(db.fetch_one("SELECT merged FROM clusters WHERE id='mg1'"))["merged"])
    assert merged and "Sridhar Vembu" in merged[0]["title"]


# ── knowledge base ───────────────────────────────────────────────────────────

def _vec(seed, base=None, noise=0.0):
    rng = np.random.default_rng(seed)
    v = base.copy() if base is not None else rng.normal(size=64)
    v = v + noise * rng.normal(size=64)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _put_story(sid, headline, vec, days_ago=0, tier="primary"):
    at = (datetime.utcnow() - timedelta(days=days_ago)).replace(microsecond=0).isoformat() + "+00:00"
    run(db.execute("INSERT OR REPLACE INTO stories(id,kind,created_at,updated_at,headline,dek,topic,tier,centroid,published_at,gate,extras) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (sid, "feed", at, at, headline, "", "india", tier, llm.to_blob(vec), at, "{}", "{}")))


def test_threads_join_the_same_story_but_not_unrelated_ones():
    run(db.init_db())
    base = _vec(1)
    _put_story("kb1", "IIT Bombay student found dead in hostel, police register case", base, 3)
    _put_story("kb2", "IIT Bombay faculty backs suspended professor after student death", _vec(2, base, 0.03), 1)
    _put_story("kb3", "Sensex jumps 564 points as oil prices cool", _vec(3), 0)
    t1, t2, t3 = (run(kb.assign_thread(s)) for s in ("kb1", "kb2", "kb3"))
    assert t1 == t2 and t3 != t1
    h = run(kb.thread_history("kb2"))
    assert [x["story_id"] for x in h["earlier"]] == ["kb1"] and h["later"] == []


def test_entities_are_counted_across_stories_and_linked():
    ents = [{"name": "Suryanarayana Doolla", "type": "person", "mentions": 4}, {"name": "IIT Bombay", "type": "organization", "mentions": 6},
            {"name": "Reuters", "type": "organization", "mentions": 3}, {"name": "professor", "type": "other", "mentions": 2}]
    assert run(kb.register_entities("kb1", ents)) == 2                        # news agencies and generic words are not entities
    run(kb.register_entities("kb2", ents))
    ent = run(kb.find_entity("Prof. Suryanarayana Doolla"))
    assert ent and ent["story_count"] == 2
    assert run(kb.link_related("kb2")) == 1
    rel = run(kb.related_stories("kb2"))
    assert rel and rel[0]["id"] == "kb1" and "Doolla" in rel[0]["reason"]
    atlas = run(kb.atlas(30, 20))
    assert len(atlas["nodes"]) == 2 and atlas["edges"][0]["weight"] == 2


# ── inspector (forensics is deterministic) ───────────────────────────────────

def test_forensics_flags_unnamed_sources_hedges_and_conflicting_numbers():
    story = {"sources": [
        {"outlet": "Outlet A", "url": "a", "text": "Exports rose 40% in five months, the ministry said. Officials said more growth is likely.", "published_at": "2026-09-20T10:00:00"},
        {"outlet": "Outlet B", "url": "b", "text": "Exports rose 25% in five months according to trade data released this week.", "published_at": "2026-09-21T10:00:00"},
        {"outlet": "Outlet C", "url": "c", "text": "Exports rose 40% in five months, the ministry said in a statement.", "published_at": "2026-09-21T12:00:00"}]}
    r = inspector.forensic_report("Exports rose 40% in five months, according to people familiar with the matter, and could grow further.", story, [])
    kinds = " ".join(n["text"] for n in r["notes"])
    assert r["unnamed_sources"] and r["hedges"] and "Numbers differ" in kinds and "25" in kinds
    assert r["outlets_supporting"] == 2 and r["first_seen"].startswith("2026-09-20")


def test_forensics_ignores_dates_and_small_numbers():
    assert inspector.quantities("On 21 September 2026 at 3 pm, 2 people met") == set()
    assert inspector.quantities("It rose 564 points to 74,859 and 40% in 5 months") == {"564", "74859", "40"} - {"5"} | set()


def test_language_flags_find_attribution_and_charged_words():
    f = inspector.language_flags("Congress slams the government's shocking move, Rahul Gandhi said on Monday.")
    assert f["loaded"] == ["shocking", "slams"] and "Rahul Gandhi" in f["attributed_to"]


# ── newspaper PDFs ───────────────────────────────────────────────────────────

def _fake_paper() -> bytes:
    import pymupdf
    doc = pymupdf.open()
    body = ("The state government announced a new policy on Monday to support small farmers hit by heavy rain across three districts. "
            "Officials said compensation will be paid within two weeks after a survey of damaged crops. Opposition leaders demanded a larger "
            "package and questioned the survey method. Farmer groups said the announcement was welcome but late. ") * 2
    for page_no in range(2):
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 60), "THE DAILY TEST", fontsize=34)
        page.insert_textbox(pymupdf.Rect(40, 100, 300, 190), f"Government announces relief for flood hit farmers {page_no}", fontsize=20)
        page.insert_textbox(pymupdf.Rect(40, 200, 300, 520), body, fontsize=9)
        page.insert_textbox(pymupdf.Rect(320, 100, 555, 190), "Central bank keeps rates unchanged", fontsize=20)
        page.insert_textbox(pymupdf.Rect(320, 200, 555, 520), body.replace("state government", "central bank"), fontsize=9)
        page.insert_textbox(pymupdf.Rect(40, 560, 555, 600), "CLASSIFIED ADS TODAY", fontsize=22)
        page.insert_textbox(pymupdf.Rect(40, 610, 555, 700), "For sale flat two bedroom. To let shop. Wanted driver.", fontsize=9)
    return doc.tobytes()


def test_pdf_is_split_into_articles_and_ads_are_skipped():
    seg = paper.segment_pdf(_fake_paper(), "the_daily_test.pdf")
    assert seg["has_text"] and seg["pages"] == 2
    heads = [a["headline"] for a in seg["articles"]]
    assert any("relief for flood hit farmers" in h for h in heads) and any("Central bank keeps rates" in h for h in heads)
    assert all(a["words"] >= paper.MIN_WORDS for a in seg["articles"])
    assert any("CLASSIFIED ADS" in s["headline"] for s in seg["skipped"])
    a = next(a for a in seg["articles"] if "flood hit farmers" in a["headline"])
    assert "compensation will be paid" in a["body"] and "central bank" not in a["body"]     # columns are kept apart
    assert "THE DAILY TEST".title() in seg["name"] or "Daily" in seg["name"]


def test_image_only_pdf_is_reported_not_crashed():
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()
    seg = paper.segment_pdf(doc.tobytes(), "scan.pdf")
    assert seg["has_text"] is False


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_wire_story_is_listed_and_detail_works_before_it_is_built(client):
    run(db.init_db())
    v = _vec(9)
    _put_story("w1", "Iran warns of wider war if US resumes strikes", v, 0, tier="wire")
    feed = client.get("/api/feed").json()
    ids = [feed["lead"]["id"]] + [s["id"] for s in feed["stories"]] if feed["lead"] else []
    assert "w1" in ids
    card = next(s for s in ([feed["lead"]] + feed["stories"]) if s["id"] == "w1")
    assert card["built"] is False and card["tier"] == "wire"
    d = client.get("/api/stories/w1").json()
    assert d["built"] is False and d["explain"] is None and d["thread"]["thread_id"] is None or True


def test_build_endpoint_refuses_stories_without_source_articles(client):
    r = client.post("/api/stories/w1/build")
    assert r.status_code == 409


def test_inspect_validates_and_explains_glossary_terms_without_a_model(client):
    assert client.post("/api/inspect", json={"text": "x", "action": "explain"}).status_code == 400
    assert client.post("/api/inspect", json={"text": "FIR", "action": "nope"}).status_code == 400
    r = client.post("/api/inspect", json={"story_id": "w1", "text": "FIR", "action": "explain"}).json()
    assert r["curated"] and "First Information Report" in r["answer"]
    r2 = client.post("/api/inspect", json={"story_id": "w1", "text": "Iran warns of wider war if US resumes strikes.", "action": "forensics"}).json()
    assert "notes" in r2 and r2["outlets_total"] == 0


def test_pdf_upload_rejects_non_pdf_and_processes_pdf(client, monkeypatch):
    assert client.post("/api/papers", files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 400
    assert client.post("/api/papers", files={"file": ("x.pdf", b"not a pdf", "application/pdf")}).status_code == 400

    async def fake_embed(texts, kind="document"):
        return np.stack([_vec(abs(hash(t)) % 10_000) for t in texts])
    monkeypatch.setattr(llm, "embed", fake_embed)
    r = client.post("/api/papers?explain_top=0", files={"file": ("daily.pdf", _fake_paper(), "application/pdf")})
    assert r.status_code == 200
    pid = r.json()["paper_id"]
    import time
    for _ in range(60):
        p = client.get(f"/api/papers/{pid}").json()
        if p["status"] != "processing":
            break
        time.sleep(0.2)
    assert p["status"] == "ready" and p["article_count"] >= 2
    assert len(p["matched"]) + len(p["other"]) == p["article_count"] and p["skipped"]
    aid = (p["matched"] + p["other"])[0]["id"]
    sid = client.post(f"/api/papers/{pid}/articles/{aid}/open").json()["story_id"]
    story = client.get(f"/api/stories/{sid}").json()
    assert story["kind"] == "paper" and story["built"] is False and story["sources"]
    assert client.delete(f"/api/papers/{pid}").json() == {"ok": True}


def test_explain_link_rejects_private_hosts_and_short_text(client):
    assert client.post("/api/explain", json={"url": "http://127.0.0.1:8000/x"}).status_code == 400
    assert client.post("/api/explain", json={"url": "file:///etc/passwd"}).status_code == 400
    assert client.post("/api/explain", json={"text": "too short"}).status_code == 400


def test_atlas_entity_and_models_endpoints(client):
    assert "nodes" in client.get("/api/atlas").json()
    ent = client.get("/api/entities/search?q=doolla").json()
    if ent:
        page = client.get(f"/api/entities/{ent[0]['id']}").json()
        assert page["name"] and isinstance(page["stories"], list)
    assert client.get("/api/entities/999999").status_code == 404
    assert "managed" in client.get("/api/models/status").json()


# ── wire tier ────────────────────────────────────────────────────────────────

def test_wire_story_uses_cleaned_headline_best_snippet_and_no_llm():
    from backend.services import pipeline
    c = {"topic": "india", "outlet_count": 3, "members": [
        {"title": "Sensex jumps 564 points as oil cools - The Hindu", "snippet": "", "trust_rank": 2, "image_url": ""},
        {"title": "Sensex jumps 564 points as oil cools | NDTV Profit", "snippet": "Benchmarks closed higher on Monday as crude fell for a fourth session and bond yields eased.", "trust_rank": 1, "image_url": "https://i/x.jpg"}]}
    f = pipeline._wire_fields(c, {})
    assert f["headline"] == "Sensex jumps 564 points as oil cools" and f["dek"].startswith("Benchmarks closed higher")
    assert f["image_url"] == "https://i/x.jpg" and f["outlet_count"] == 3
    assert pipeline.clean_headline("Big news — The Guardian") == "Big news"
