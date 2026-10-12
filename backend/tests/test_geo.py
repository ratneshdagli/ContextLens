"""Globe: the offline geotagger (ambiguity, fail-closed) and the /api/globe endpoints (network faked)."""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import app
from backend.services import geo, geo_live

NOW = datetime.now(timezone.utc).isoformat()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def loc(headline, snippet="", domain=""):
    return geo.locate([(headline, 3.0), (snippet, 1.0)], domains=[domain] if domain else [])


def test_city_and_country_context_disambiguate():
    assert loc("Floods hit Houston after storm")["place"] == "Houston"
    r = loc("Paris, Texas holds town meeting on new school")
    assert (r["place"], r["cc"]) == ("Paris", "US")
    r = loc("Paris mourns after attack near the Louvre", "France said")
    assert (r["place"], r["cc"]) == ("Paris", "FR")
    assert loc("India talks with Moscow and Kyiv on grain")["place"] in ("Moscow", "Kyiv", "India")


def test_country_only_is_labelled_country_not_a_street_pin():
    r = loc("Parliament in Japan passes new budget")
    assert r["precision"] == "country" and r["cc"] == "JP"


def test_fails_closed_without_a_confident_place():
    assert loc("Intel chips find an elusive formula and Acer proves it") is None
    assert loc("Don't tell Weird Al, but Kyle Gordon's satires are great") is None       # person, not the town of Kyle
    assert loc("Alex Pereira warned before the fight") is None                          # surname that is also a city
    assert loc("Oil little changed as Middle East crude exports rise") is None          # 'East' is not the Cameroon region
    assert loc("") is None


def test_alias_and_old_names():
    assert loc("US strikes hit port")["cc"] == "US"
    assert loc("Bombay court rejects plea")["cc"] == "IN"


def test_search_and_nearest_city():
    assert geo.search("tokyo")[0]["name"] == "Tokyo"
    assert geo.search("x") == []
    assert geo.nearest_city(40.7, -74.0).name in ("New York City", "Jersey City", "Newark")
    assert geo.nearest_city(-60.0, -150.0) is None                                      # open ocean


def test_globe_endpoints(client, monkeypatch):
    _run(db.execute(
        "INSERT INTO stories(id,kind,created_at,updated_at,headline,headline_original,topic,tier,published_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("g-s1", "feed", NOW, NOW, "Floods hit Houston", "Floods hit Houston after storm", "world", "wire", NOW)))
    _run(db.execute(
        "INSERT INTO articles(id,url,url_hash,title,snippet,outlet,domain,topic,published_at,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("g-a1", "https://example.com/a", "h-g-a1", "Mayor of Leeds opens new library", "", "Example", "example.com", "culture", NOW, NOW)))
    _run(db.execute(
        "INSERT INTO articles(id,url,url_hash,title,snippet,outlet,domain,topic,published_at,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("g-a2", "https://example.com/b", "h-g-a2", "Chips find an elusive formula", "", "Example", "example.com", "tech", NOW, NOW)))
    geo._last_tag = 0.0                                                                 # tag the rows just inserted
    r = client.get("/api/globe?hours=24").json()
    by = {f["properties"]["id"]: f for f in r["features"]}
    assert by["g-s1"]["properties"]["kind"] == "story" and by["g-s1"]["properties"]["place"] == "Houston"
    assert by["g-a1"]["properties"]["kind"] == "headline" and by["g-a1"]["properties"]["place"] == "Leeds"
    assert "g-a2" not in by                                                             # no confident place: not on the map
    lon, lat = by["g-s1"]["geometry"]["coordinates"]
    assert abs(lat - 29.76) < 0.5 and abs(lon + 95.37) < 0.5
    assert not [f for f in client.get("/api/globe?topic=sports").json()["features"] if f["properties"]["id"] == "g-s1"]
    assert not [f for f in client.get("/api/globe?headlines=0").json()["features"] if f["properties"]["kind"] == "headline"]
    assert client.get("/api/globe?bbox=-100,25,-90,35").json()["features"]              # Houston is inside
    assert not [f for f in client.get("/api/globe?bbox=0,0,10,10").json()["features"] if f["properties"]["id"] == "g-s1"]

    near = client.get("/api/globe/near?lat=29.76&lon=-95.37&km=40").json()
    assert near["place"]["name"] == "Houston" and any(i["id"] == "g-s1" and abs(i["lat"] - 29.76) < 0.5 for i in near["items"])
    assert client.get("/api/globe/near?lat=0&lon=0&km=5").json()["items"] == []
    assert client.get("/api/globe/near?lat=999&lon=0").status_code == 422

    s = client.get("/api/globe/stats").json()
    assert s["stories"] >= 1 and s["headlines"] >= 1

    async def fake(place, cc="", limit=25, at=()):
        return [{"title": f"{place} council votes", "outlet": "X", "url": "https://n.example/1", "published_at": NOW}], False
    monkeypatch.setattr(geo_live, "headlines", fake)
    live = client.get("/api/globe/live?lat=29.76&lon=-95.37").json()
    assert live["place"]["name"] == "Houston" and live["items"][0]["title"].startswith("Houston")
    assert client.get("/api/globe/live?lat=-60&lon=-150").json()["items"] == []
    assert client.get("/api/globe/search?q=lond").json()[0]["name"] == "London"


def test_live_headlines_only_keep_items_naming_the_place(monkeypatch):
    rss = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
           b'<item><title>Leeds council backs new tram line - BBC News</title><link>https://n/1</link><pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item>'
           b'<item><title>Totally unrelated national story about taxes - Sky</title><link>https://n/2</link></item></channel></rss>')

    class R:
        content = rss

        def raise_for_status(self):
            pass

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return R()

    monkeypatch.setattr(geo_live.httpx, "AsyncClient", C)
    geo_live._cache.clear()
    items, cached = _run(geo_live.headlines("Leeds", "GB"))
    assert [i["title"] for i in items] == ["Leeds council backs new tram line"] and items[0]["outlet"] == "BBC News" and not cached
    assert _run(geo_live.headlines("Leeds", "GB"))[1] is True


def test_stored_live_headlines_become_map_points(client):
    _run(geo_live.store("Leeds", "GB", (53.8, -1.55, "United Kingdom", "Leeds"), [{"title": "Leeds council votes on tram", "outlet": "BBC", "url": "https://n/9", "published_at": NOW}]))
    feats = client.get("/api/globe?hours=24&bbox=-10,50,2,60").json()["features"]
    live = [f for f in feats if f["properties"]["tier"] == "live"]
    assert live and live[0]["properties"]["place"] == "Leeds" and live[0]["properties"]["url"] == "https://n/9"
    assert not [f for f in client.get("/api/globe?hours=24&bbox=100,0,110,10").json()["features"] if f["properties"]["tier"] == "live"]
    assert not [f for f in client.get("/api/globe?hours=24&topic=sports").json()["features"] if f["properties"]["tier"] == "live"]


def test_area_collection_queues_the_regions_cities(client, monkeypatch):
    assert [p.name for p in geo.cities_in(-1, 51, 1, 52, 3)][0] == "London"
    seen = []

    async def fake(place, cc="", limit=25, at=(), deep=False, ttl=900, native=""):
        seen.append((place, deep))
        return [], False
    monkeypatch.setattr(geo_live, "headlines", fake)
    geo_live._cache.clear()
    r = client.get("/api/globe/area?w=-3&s=50&e=2&n=54&zoom=7").json()
    assert r["places"] > 5 and r["queued"] == r["places"] and r["pending"] <= r["places"]
    assert client.get("/api/globe/area?w=-3&s=50&e=2&n=54&zoom=2").json()["places"] == 0        # whole-world view: nothing to collect
    assert any(p == "London" and d for p, d in seen)                                            # area fetches are the deep kind


def test_street_level_places_are_collected_from_map_labels(client, monkeypatch):
    seen = []

    async def fake(place, cc="", limit=25, at=(), deep=False, ttl=900, native=""):
        seen.append((place, cc, deep))
        return [], False
    monkeypatch.setattr(geo_live, "headlines", fake)
    geo_live._cache.clear()
    body = {"places": [{"name": "Borivali", "lat": 19.23, "lon": 72.86}, {"name": "12", "lat": 19.2, "lon": 72.8}, {"name": "road", "lat": 19.2, "lon": 72.8}]}
    r = client.post("/api/globe/places", json=body).json()
    assert r["places"] == 1 and r["queued"] == 1                                    # digits and lowercase labels are not place names
    assert any(p == "Borivali" and cc == "IN" and d for p, cc, d in seen)
    assert client.post("/api/globe/places", json={"places": [{"name": "x" * 41, "lat": 0, "lon": 0}]}).status_code == 422


def test_quiet_places_are_topped_up_from_the_week_and_own_script_names_are_found():
    old = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=3)).isoformat()
    _run(geo_live.store("Palghar", "IN", (19.69, 72.77, "India", "Palghar"), [{"title": "Palghar road accident kills two", "outlet": "X", "url": "https://n/p1", "published_at": old}]))
    assert any(i["place"] == "Palghar" for i in _run(geo_live.points(24)))                     # nothing in 24 h, but the week's news is shown
    assert geo_live._langs("पालघर") == ("mr", "hi") and geo_live._langs("London") == ()


def test_source_auditor_accepts_only_fresh_english_feeds(monkeypatch, tmp_path):
    import json
    from backend.services import audit, channels

    def feed(titles, days=0):
        d = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        items = "".join(f"<item><title>{t}</title><link>https://x/{i}</link><pubDate>{d}</pubDate></item>" for i, t in enumerate(titles))
        return f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{items}</channel></rss>'.encode()

    eng = feed([f"Council approves new road plan number {i}" for i in range(6)])
    mar = feed(["पालघर मध्ये पावस आज %d" % i for i in range(6)])
    old = feed([f"Council approves old road plan number {i}" for i in range(6)], days=30)
    bodies = {"good.in": eng, "marathi.in": mar, "stale.in": old}

    async def probe(client, domain, sem):
        return {"url": f"https://{domain}/rss", "outlet": domain.split(".")[0].title(), "domain": domain} if domain in bodies else None

    class R:
        def __init__(self, b):
            self.content = b

    class C:
        async def get(self, url):
            return R(bodies[url.split("/")[2]])

    async def no_stealth(*a, **k):
        return None
    monkeypatch.setattr(channels, "probe_feed", probe)
    monkeypatch.setattr(audit.extract, "stealth_eval", no_stealth)
    async def go():
        return await asyncio.gather(*(audit.audit_domain(C(), asyncio.Semaphore(1), d) for d in (*bodies, "dead.in")))
    res = {r["domain"]: r for r in _run(go())}
    assert [res[d]["verdict"] for d in ("good.in", "marathi.in", "stale.in", "dead.in")] == ["accept", "local-language", "reject", "reject"]
    (tmp_path / "feeds_extra.json").write_text("[]")
    (tmp_path / "trusted_sources.json").write_text("[]")
    monkeypatch.setattr(audit, "DIR", tmp_path)
    assert audit.apply(list(res.values())) == 1 and audit.apply(list(res.values())) == 0       # added once, never twice
    assert json.loads((tmp_path / "feeds_extra.json").read_text())[0]["topic"] == "india"
    assert json.loads((tmp_path / "trusted_sources.json").read_text())[0]["verified_by"] == "auditor"


def test_near_includes_stored_live_headlines(client):
    _run(geo_live.store("Leeds", "GB", (53.8, -1.55, "United Kingdom", "Leeds"), [{"title": "Leeds council votes on tram", "outlet": "BBC", "url": "https://n/9", "published_at": NOW}]))
    items = client.get("/api/globe/near?lat=53.8&lon=-1.55&km=20&hours=24").json()["items"]
    assert any(i["tier"] == "live" and i["place"] == "Leeds" and abs(i["lat"] - 53.8) < 0.1 for i in items)


def test_local_shelf_and_blindspots(client, monkeypatch):
    async def fake(place, cc="", limit=25, at=(), deep=False, ttl=900, native=""):
        return [], False
    monkeypatch.setattr(geo_live, "headlines", fake)
    assert client.get("/api/local").json() == {"place": None, "items": []}                      # no home yet
    h = client.get("/api/local/search?q=houston").json()[0]
    assert client.put("/api/local", json={"name": h["name"], "country": h["country"], "lat": h["lat"], "lon": h["lon"]}).status_code == 200
    r = client.get("/api/local").json()
    assert r["place"]["name"] == "Houston" and any(i["id"] == "g-s1" for i in r["items"])        # the story placed in Houston
    client.delete("/api/local")
    assert client.get("/api/local").json()["place"] is None
    _run(db.execute("UPDATE stories SET tier='primary', outlet_count=4, extras=? WHERE id='g-s1'", ('{"lens": {"countries": ["IN"]}}',)))
    spots = client.get("/api/blindspots").json()
    assert [s["id"] for s in spots] == ["g-s1"] and spots[0]["only_cc"] == "IN" and spots[0]["only_country"] == "India"
    _run(db.execute("UPDATE stories SET extras=? WHERE id='g-s1'", ('{"lens": {"countries": ["IN", "GB"]}}',)))
    assert client.get("/api/blindspots").json() == []                                           # two countries covered it: no blind spot


def test_shorts_are_whole_sentences_under_sixty_words_and_alerts_use_signal(client):
    from backend.routers.discover import _sixty
    s = "The central bank raised rates by a quarter point on Tuesday, citing stubborn inflation. Officials said further moves depend on incoming data. Markets fell sharply after the announcement."
    assert _sixty(s) == s and _sixty("Too short.") == ""                                         # under 20 words: no card
    long = " ".join(["This sentence has exactly ten words in it for counting."] * 8)
    assert len(_sixty(long).split()) <= 60 and _sixty(long).endswith(".")                       # cut at a sentence end, never mid-sentence
    gate = '{"rank": {"components": {"coverage": 1.0, "trust": 1.0}}, "triage": {"answers": {"impact": "historic"}}}'
    _run(db.execute("INSERT INTO stories(id,kind,created_at,updated_at,headline,headline_original,topic,tier,published_at,built_at,what_happened,gate,outlet_count) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g-s2", "feed", NOW, NOW, "Central bank raises rates", "Central bank raises rates", "business", "primary", NOW, NOW, s, gate, 9)))
    deck = client.get("/api/shorts").json()
    mine = [d for d in deck if d["id"] == "g-s2"]
    assert mine and mine[0]["body"] == s and 20 <= mine[0]["words"] <= 60
    a = client.get("/api/alerts?since=2000-01-01T00:00:00Z&min_signal=9").json()["items"]
    assert [i["id"] for i in a] == ["g-s2"] and a[0]["signal"] == 10
    assert client.get("/api/alerts?since=2999-01-01T00:00:00Z&min_signal=1").json()["items"] == []   # nothing newer than the future
