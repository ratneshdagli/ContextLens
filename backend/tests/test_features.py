"""Tests for the differentiating features (deterministic parts)."""

from backend.routers.feed import signal_of
from backend.services import lens, verify


def _gate(coverage, impact, trust=0.8):
    return {"rank": {"components": {"coverage": coverage, "trust": trust}}, "triage": {"answers": {"impact": impact}}}


def test_signal_scale_bounds_and_monotonic():
    lo = signal_of(_gate(0.0, "local_minor"))
    mid = signal_of(_gate(0.5, "notable"))
    hi = signal_of(_gate(1.0, "historic", 1.0))
    assert 1 <= lo < mid < hi <= 10
    assert signal_of({}) >= 1


def test_signal_is_reader_independent():
    a = {"rank": {"score": 0.9, "components": {"coverage": 0.5, "trust": 0.8, "interest": 1.0}}, "triage": {"answers": {"impact": "major"}}}
    b = {"rank": {"score": 0.3, "components": {"coverage": 0.5, "trust": 0.8, "interest": 0.0}}, "triage": {"answers": {"impact": "major"}}}
    assert signal_of(a) == signal_of(b)


SRC = [
    {"n": 1, "outlet": "BBC", "url": "u1", "text": "Top US officials met Chinese counterparts in New York on Sunday. A truce in the tariffs war "
     "between the world's two biggest economies is due to expire on 10 November. Talks covered an AI incident notification mechanism."},
    {"n": 2, "outlet": "NPR", "url": "u2", "text": "Top US officials met Chinese counterparts in New York on Sunday. The talks covered an AI incident "
     "notification mechanism ahead of the summit. Analysts warned that many systems were built before the age of cyber warfare."},
]


def test_provenance_traces_claims_to_source_sentences():
    out = verify.provenance([("Officials discussed an AI incident notification mechanism.", 0),
                             ("The tariff truce expires on 10 November.", 1),
                             ("The moon is made of cheese.", 2)], SRC)
    idxs = {c["idx"] for c in out}
    assert 0 in idxs and 1 in idxs and 2 not in idxs
    truce = next(c for c in out if c["idx"] == 1)
    assert truce["n"] == 1 and "10 November" in truce["quote"]


def test_provenance_requires_numbers_to_match():
    out = verify.provenance([("The tariff truce expires on 12 November.", 0)], SRC)
    assert out == []


def test_lens_finds_facts_reported_by_only_one_outlet():
    texts = [dict(s, title="t") for s in SRC]
    members = [{"outlet": s["outlet"], "domain": d, "url": s["url"], "title": "headline " + s["outlet"]}
               for s, d in zip(SRC, ("bbc.com", "npr.org"))]
    out = lens.build_lens(texts, members)
    assert {"UK", "US"} == set(out["countries"])
    assert any(u["outlet"] == "BBC" and "10 November" in u["sentence"] for u in out["unique"])
    assert len(out["angles"]) == 2


def test_claim_verdict_needs_two_independent_outlets():
    from backend.services.research import settle_verdict
    texts = [{"domain": "bbc.com"}, {"domain": "npr.org"}, {"domain": "bbc.com"}]
    one = [{"cites": [1, 3]}]                       # two citations but the same outlet
    two = [{"cites": [1]}, {"cites": [2]}]
    assert settle_verdict("supported", one, texts)[0] == "not_enough_evidence"
    assert settle_verdict("contradicted", one, texts)[0] == "not_enough_evidence"
    assert settle_verdict("supported", two, texts)[0] == "supported"
    assert settle_verdict("partly_supported", one, texts)[0] == "partly_supported"    # non-decisive verdicts pass through


def test_graph_rejects_relations_with_endpoints_outside_the_entity_list():
    from backend.services.graph import filter_relations
    text = ("IIT Bombay said on Monday that it apologised for earlier statements. The IIT Bombay Faculty Forum issued a statement "
            "expressing solidarity with Prof. Doolla. Shireesh Kedare accepted a charter of demands from students.")
    names = ["IIT Bombay", "Suryanarayan Doolla", "Shireesh Kedare"]
    raw = [
        {"subject": "IIT Bombay", "relation": "apologised for", "object": "statements contained in earlier communication",
         "evidence": "IIT Bombay said on Monday that it apologised for earlier statements."},        # object invented
        {"subject": "Shireesh Kedare", "relation": "accepted", "object": "students",
         "evidence": "Shireesh Kedare accepted a charter of demands from students."},               # object not an entity
        {"subject": "iit bombay", "relation": "criticised", "object": "Suryanarayan Doolla",
         "evidence": "IIT Bombay said on Monday that it apologised for earlier statements."},        # evidence lacks 2nd entity
    ]
    assert filter_relations(raw, text, "u", names) == []
    ok = [{"subject": "Shireesh Kedare", "relation": "accepted demands from", "object": "IIT Bombay",
           "evidence": "Shireesh Kedare accepted a charter of demands from students."}]
    assert filter_relations(ok, text, "u", names) == []            # evidence mentions Kedare but not IIT Bombay
