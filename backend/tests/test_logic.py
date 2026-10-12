"""Unit tests for the deterministic parts of the pipeline (no LLM / network needed)."""

import numpy as np
import pytest

from backend.services import clustering, editor, feeds, graph, llm, ranking, timeline, verify


# ── llm output hygiene ───────────────────────────────────────────────────────

def test_strip_thinking_tags():
    assert llm.strip_thinking("<think>reasoning here</think>Final answer") == "Final answer"


@pytest.mark.parametrize("text", [
    "Here's a thinking process:\n\n1. **Analyze User Input:**",
    "[LM Studio not running]", "[AI error: boom]", "", "1. **Analyze User Input:** the headline editor",
])
def test_garbage_detected(text):
    assert llm.looks_like_garbage(text)


def test_real_headline_is_not_garbage():
    assert not llm.looks_like_garbage("France and Canada pledge closer ties amid Trump tensions")


def test_parse_json_variants():
    assert llm._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm._parse_json('noise {"a": [1,2]} trailing') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        llm._parse_json("not json at all")


# ── feeds ────────────────────────────────────────────────────────────────────

def test_clean_text_strips_html_and_entities():
    assert feeds.clean_text("<p>Trump&#8217;s   <b>deal</b></p>") == "Trump’s deal"


def test_clean_text_repairs_mojibake():
    assert feeds.clean_text("Donâ€™t panic") .startswith("Don")
    assert "â€" not in feeds.clean_text("Donâ€™t panic")


def test_url_normalisation_drops_tracking():
    a = feeds.url_hash("https://www.bbc.com/news/x-123?utm_source=rss&ocid=foo")
    b = feeds.url_hash("https://www.bbc.com/news/x-123")
    assert a == b


def test_junk_titles_filtered():
    assert feeds.JUNK_TITLE_RE.search("LIVE: Election results as they come in")
    assert feeds.JUNK_TITLE_RE.search("Today's crossword answers")
    assert not feeds.JUNK_TITLE_RE.search("Fed raises interest rates for first time in three years")


# ── clustering ───────────────────────────────────────────────────────────────

def test_anchors_pick_specific_tokens():
    a = clustering.anchors("Ed Sheeran says situation in Gaza ‘unjustifiable’ after Macklemore dropped")
    assert {"sheeran", "gaza", "macklemore"} <= a


def test_merge_requires_specific_anchor_when_not_near_duplicate():
    generic = frozenset({"trump"})
    # semantically similar but only share a ubiquitous anchor → must NOT merge
    assert not clustering.should_merge(0.82, "Trump deportation deals in Africa", "Trump AI Force announced", generic)
    # share a specific anchor → merge
    assert clustering.should_merge(0.82, "Trump strikes Greenland security deal", "Denmark reacts to Greenland deal", generic)
    # near-duplicate → merge regardless
    assert clustering.should_merge(0.93, "Alpha wins", "Beta loses", generic)
    assert not clustering.should_merge(0.6, "Greenland deal", "Greenland deal", generic)


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_cluster_articles_groups_same_event():
    e1, e2 = _unit([1, 0, 0]), _unit([0.99, 0.05, 0.0])
    other = _unit([0, 1, 0])
    arts = [
        {"id": "a", "title": "Merz vows to stay after election disaster", "embedding": e1, "published_at": "1"},
        {"id": "b", "title": "Germany’s Merz vows to stay after state election defeat", "embedding": e2, "published_at": "2"},
        {"id": "c", "title": "Nvidia unveils new chip", "embedding": other, "published_at": "3"},
    ]
    out = clustering.cluster_articles(arts, [])
    assert out["a"] == out["b"] != out["c"]


# ── ranking ──────────────────────────────────────────────────────────────────

def test_coverage_score_monotonic_and_bounded():
    vals = [ranking.coverage_score(n) for n in (1, 2, 4, 8, 20)]
    assert vals == sorted(vals) and vals[0] == 0 and vals[-1] == 1


def test_mmr_prefers_diverse_items():
    a = {"score": 0.9, "centroid": _unit([1, 0])}
    b = {"score": 0.88, "centroid": _unit([1, 0.01])}      # near-duplicate of a
    c = {"score": 0.6, "centroid": _unit([0, 1])}
    picked = ranking.mmr_select([a, b, c], 2)
    assert c in picked and a in picked


# ── editor ───────────────────────────────────────────────────────────────────

def test_headline_validator():
    ok = "France and Canada pledge closer ties amid Trump tensions"
    assert editor.validate_headline(ok, "France Canada ties Trump") is None
    assert editor.validate_headline("Here's a thinking process: 1. Analyze", "") is not None
    assert editor.validate_headline("You won't believe what Iran said about the US", "") is not None
    assert editor.validate_headline("Short", "") is not None
    assert editor.validate_headline("Shares drop 47% after surprise announcement today", "shares drop 21 percent") is not None


BASE = {"what_happened": "x", "story_type": "development", "has_concrete_new_fact": True,
        "impact": "major", "matches_reader": "strong", "reader_value": 9}


def test_triage_keeps_strong_match():
    assert editor.decide_triage(BASE, 0.6, 0.7, "balanced")["keep"]


@pytest.mark.parametrize("patch", [
    {"story_type": "promotion_or_deals"}, {"story_type": "service_or_how_to"}, {"has_concrete_new_fact": False},
    {"matches_reader": "none", "impact": "local_minor"}, {"story_type": "routine_update", "impact": "local_minor"},
])
def test_triage_rejects_noise(patch):
    assert not editor.decide_triage({**BASE, **patch}, 0.6, 0.7, "balanced")["keep"]


def test_soft_type_veto_only_when_evidence_is_weak():
    chatter = {**BASE, "story_type": "stock_tips_or_market_chatter", "impact": "notable", "matches_reader": "partial"}
    assert not editor.decide_triage(chatter, 0.45, 0.5, "balanced")["keep"]     # weak evidence → vetoed
    assert editor.decide_triage(chatter, 0.75, 0.5, "balanced")["keep"]         # strong evidence → label ignored


def test_triage_major_news_survives_without_interest_match():
    assert editor.decide_triage({**BASE, "matches_reader": "none", "impact": "major"}, 0.6, 0.1, "balanced")["keep"]


def test_triage_broad_coverage_rescues_soft_model_answers():
    soft = {**BASE, "impact": "notable", "matches_reader": "partial", "reader_value": 4}
    assert editor.decide_triage(soft, 0.75, 0.5, "balanced")["keep"]          # widely covered → kept
    assert not editor.decide_triage(soft, 0.15, 0.5, "balanced")["keep"]      # barely covered → dropped


PUB = {"headline_matches_facts": True, "headline_is_clickbait": False, "summary_support": "fully",
       "understandable_standalone": True, "newsworthy_for_reader": "primary", "issue": "none"}


def test_publish_primary_when_everything_checks_out():
    assert editor.decide_publish(PUB, {"score": 0.95}, "balanced", 4, 0.6)["tier"] == "primary"


def test_publish_rejects_unfaithful_headline():
    assert editor.decide_publish({**PUB, "headline_matches_facts": False}, {"score": 0.95}, "balanced", 4, 0.6)["tier"] == "rejected"


def test_publish_demotes_low_grounding_and_single_outlet():
    assert editor.decide_publish(PUB, {"score": 0.3}, "balanced", 4, 0.6)["tier"] == "more"
    assert editor.decide_publish(PUB, {"score": 0.95}, "balanced", 1, 0.3)["tier"] == "more"


# ── verification ─────────────────────────────────────────────────────────────

SRC = ("The Federal Reserve raised interest rates last week for the first time in three years. "
       "Oil prices reached $100 a barrel on Thursday. Analysts at Goldman Sachs expect gold to recover.")


def test_grounded_claims_pass():
    r = verify.ground(["The Federal Reserve raised interest rates for the first time in three years."], SRC)
    assert r["score"] == 1.0


def test_invented_number_and_names_fail():
    r = verify.ground(["Oil prices reached $250 a barrel according to Warren Buffett."], SRC)
    assert r["score"] == 0.0 and r["unsupported"]


# ── timeline ─────────────────────────────────────────────────────────────────

ARTICLE = ("Mamata Banerjee moved the Supreme Court on September 18, hours after the Election Commission "
           "allotted separate names and symbols to the two rival factions.")


def test_quote_must_exist_in_article():
    assert timeline.quote_in_text("Mamata Banerjee moved the Supreme Court on September 18, hours after the Election Commission", ARTICLE)
    assert not timeline.quote_in_text("The Supreme Court dismissed the petition on September 19 after a brief hearing", ARTICLE)


def test_date_resolution_relative_and_absolute():
    from datetime import datetime
    pub = datetime(2026, 9, 21, 10, 0)
    d, prec = timeline.resolve_date("September 18", pub)
    assert d.isoformat() == "2026-09-18" and prec == "day"
    assert timeline.resolve_date("in March 2026", pub)[1] in ("month", "day")
    assert timeline.resolve_date("December 31", pub) is None                   # future → never guessed into another year
    assert timeline.resolve_date("September 24", pub) is None                  # scheduled event, not history
    assert timeline.resolve_date("garbage words", pub) is None


def _ev(date, text):
    return {"date": date, "precision": "day", "text": text, "quote": "q", "source_url": "u", "source_outlet": "o"}


def test_timeline_gate_rules():
    assert not timeline.gate([_ev("2026-09-01", "a b c"), _ev("2026-09-02", "d e f")])[0]                       # < 3 events
    assert not timeline.gate([_ev("2026-09-01", "one"), _ev("2026-09-01", "two"), _ev("2026-09-01", "three")])[0]  # single date
    ok, _ = timeline.gate([_ev("2026-08-01", "one"), _ev("2026-08-15", "two"), _ev("2026-09-01", "three")])
    assert ok


def test_future_and_planned_events_are_not_history():
    assert timeline.is_future_or_plan("The Trump-Xi summit is scheduled for September 24.")
    assert timeline.is_future_or_plan("The company plans to open a plant next year.")
    assert not timeline.is_future_or_plan("Mamata Banerjee moved the Supreme Court on September 18.")


def test_timeline_merge_dedupes_same_date_same_wording():
    evs = [_ev("2026-09-01", "Ukraine fired hundreds of drones at Moscow"),
           _ev("2026-09-01", "Ukraine fired hundreds of drones toward Moscow overnight")]
    assert len(timeline.merge_events(evs)) == 1


def test_timeline_relevance_filter():
    story = "Ukraine targets Moscow with hundreds of drones on final day of Russian election. Ukraine launched drone attack on Moscow refinery."
    assert timeline.relevant_to("Ukraine fired more than 1,000 drones at Moscow overnight", "q", story)
    assert not timeline.relevant_to("A woman was killed in Sofyino after a strike on a residential building", "q", story)


def test_entity_canonicalisation_strips_titles_and_wires():
    assert graph._canon("President Trump") == "Trump"
    assert graph._canon("Prime Minister Narendra Modi") == "Narendra Modi"
    assert "reuters" in graph.NEWS_ORGS


# ── story map ────────────────────────────────────────────────────────────────

def test_graph_gate_needs_four_entities_and_three_edges():
    r = lambda s, o: {"subject": s, "relation": "met with", "object": o, "evidence": "e", "source_url": "u"}
    assert not graph.gate([], [r("A", "B"), r("A", "C")])[0]
    assert not graph.gate([], [r("A", "B"), r("C", "D")])[0]       # 4 nodes but only 2 edges
    assert graph.gate([], [r("A", "B"), r("C", "D"), r("B", "C")])[0]


def test_graph_dedupes_one_edge_per_pair():
    r = lambda s, rel, o: {"subject": s, "relation": rel, "object": o, "evidence": "e", "source_url": "u"}
    out = graph.dedupe_relations([r("A", "met", "B"), r("B", "criticised", "A"), r("A", "met", "C")])
    assert len(out) == 2


def test_publish_demotes_market_chatter_with_narrow_coverage():
    assert editor.decide_publish(PUB, {"score": 0.95}, "balanced", 1, 0.9, "stock_tips_or_market_chatter")["tier"] == "more"
    assert editor.decide_publish(PUB, {"score": 0.95}, "balanced", 5, 0.9, "stock_tips_or_market_chatter")["tier"] == "primary"
