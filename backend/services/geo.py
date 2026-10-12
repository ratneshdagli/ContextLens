"""Places on the globe: a local gazetteer (GeoNames cities15000 + admin1 + countries, CC BY 4.0) and a deterministic,
fail-closed geotagger. No model ever invents a coordinate: a story/headline is placed only when a place named in its own
text resolves unambiguously; otherwise it simply is not on the map."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from backend import db
from backend.config import TRUSTED_SOURCES

log = logging.getLogger("geo")
DATA = Path(__file__).resolve().parent.parent / "data" / "geonames"
GEO_V = 1                      # bump to re-tag everything when the rules change

# Extra spellings people actually write (GeoNames uses the official name).
COUNTRY_ALIASES = {
    "us": "US", "u.s": "US", "usa": "US", "america": "US", "uk": "GB", "u.k": "GB", "britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "russia": "RU", "iran": "IR", "south korea": "KR", "north korea": "KP", "korea": "KR",
    "uae": "AE", "emirates": "AE", "vietnam": "VN", "syria": "SY", "laos": "LA", "turkiye": "TR", "turkey": "TR",
    "czech republic": "CZ", "ivory coast": "CI", "burma": "MM", "palestine": "PS", "gaza": "PS", "west bank": "PS",
    "congo": "CD", "drc": "CD", "holland": "NL", "taiwan": "TW", "hong kong": "HK", "macau": "MO", "kosovo": "XK",
    "brunei": "BN", "bolivia": "BO", "venezuela": "VE", "tanzania": "TZ", "moldova": "MD", "eswatini": "SZ",
}
# Single words that are also common names/words: they place a story only with outside evidence (see locate()).
AMBIGUOUS = {"reading", "nice", "mobile", "bath", "jackson", "lincoln", "franklin", "madison", "chelsea", "victoria",
             "orange", "hamilton", "kennedy", "marion", "salem", "cook", "wellington", "alexander", "dover", "buffalo",
             "charlotte", "florence", "normal", "nelson", "wells", "bristol", "chester", "troy", "hope", "union",
             "clinton", "lewis", "grant", "carter", "ross", "baker", "mason", "turner", "taylor", "wilson", "palmer",
             "perry", "king", "young", "long", "sale", "most", "march", "republic", "kingdom", "states", "island",
             "islands", "city", "park", "valley", "springs", "central", "american", "indian", "african", "european", "asian"}
COUNTRY_AMBIGUOUS = {"georgia", "jordan", "chad"}       # also a US state / a surname / a first name
FUNC = {"the", "in", "at", "from", "to", "near", "by", "how", "why", "what", "after", "as", "for", "and", "on", "with", "of",
        "this", "that", "his", "her", "its", "new", "over", "amid", "says", "say", "is", "are", "a", "an", "us", "uk"}
DIRECTIONAL = {"east", "west", "north", "south", "central", "centre", "center", "northern", "southern", "eastern", "western",
               "capital", "federal", "national", "coast", "middle", "upper", "lower", "state", "province", "district"}
SMALL = {"of", "de", "del", "la", "le", "al", "el", "upon", "on", "in", "da", "do", "des", "du", "den", "van", "am", "an"}
TOKEN = re.compile(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’.\-]*")
TOP_CITY = 1_000_000            # alternate (ASCII) spellings are indexed only for cities this big: "Bombay", "Peking"


@dataclass
class Place:
    gid: int
    name: str
    lat: float
    lon: float
    cc: str
    pop: int
    admin1: str = ""
    kind: str = "city"          # city | region | country


class Gazetteer:
    def __init__(self):
        self.names: dict[str, list[Place]] = defaultdict(list)       # lowercase name -> places (pop desc)
        self.countries: dict[str, Place] = {}                        # cc -> country (placed at the capital)
        self.country_names: dict[str, str] = {}                      # cc -> display name
        self.cname: dict[str, str] = {}                              # lowercase country name/alias -> cc
        self.regions: dict[str, list[Place]] = defaultdict(list)     # lowercase region name -> regions
        self.cities: list[Place] = []
        self.ok = False

    def load(self) -> None:
        f = DATA / "cities15000.txt"
        if not f.exists():
            log.warning("gazetteer missing (%s): geotagging is off", f)
            return
        capital: dict[str, Place] = {}
        by_admin: dict[str, list[Place]] = defaultdict(list)
        for line in f.read_text(encoding="utf-8").splitlines():
            c = line.split("\t")
            if len(c) < 15:
                continue
            p = Place(int(c[0]), c[1], float(c[4]), float(c[5]), c[8], int(c[14] or 0), f"{c[8]}.{c[10]}")
            self.cities.append(p)
            keys = {c[1].lower(), c[2].lower()}
            if p.pop >= TOP_CITY:
                keys |= {a.lower() for a in c[3].split(",") if a.isascii() and 3 <= len(a) <= 30}
            for k in keys:
                self.names[k].append(p)
            by_admin[p.admin1].append(p)
            if c[7] == "PPLC":
                capital[p.cc] = p
        for lst in self.names.values():
            lst.sort(key=lambda p: -p.pop)
        top: dict[str, Place] = {}
        for p in self.cities:
            if p.cc not in top or p.pop > top[p.cc].pop:
                top[p.cc] = p
        cf = DATA / "countryInfo.txt"
        if cf.exists():
            for line in cf.read_text(encoding="utf-8").splitlines():
                c = line.split("\t")
                if line.startswith("#") or len(c) < 8:
                    continue
                cc, name = c[0], c[4]
                at = capital.get(cc) or top.get(cc)
                if not at:
                    continue
                self.countries[cc] = Place(0, name, at.lat, at.lon, cc, int(c[7] or 0), kind="country")
                self.country_names[cc] = name
                self.cname[name.lower()] = cc
        for alias, cc in COUNTRY_ALIASES.items():
            if cc in self.countries:
                self.cname[alias] = cc
        af = DATA / "admin1CodesASCII.txt"
        if af.exists():
            for line in af.read_text(encoding="utf-8").splitlines():
                c = line.split("\t")
                if len(c) < 3 or c[0] not in by_admin:
                    continue
                cities = by_admin[c[0]]
                tot = sum(p.pop for p in cities) or 1
                lat = sum(p.lat * p.pop for p in cities) / tot
                lon = sum(p.lon * p.pop for p in cities) / tot
                reg = Place(0, c[1], lat, lon, c[0].split(".")[0], tot, c[0], "region")
                for k in {c[1].lower(), c[2].lower()}:
                    self.regions[k].append(reg)
        self.ok = bool(self.cities)
        log.info("gazetteer: %d cities, %d countries, %d regions", len(self.cities), len(self.countries), len(self.regions))


G = Gazetteer()


def ensure() -> bool:
    if not G.ok:
        G.load()
    return G.ok


def _outlet_cc(domain: str) -> str:
    for s in TRUSTED_SOURCES:
        if s.get("domain") and domain.endswith(s["domain"]):
            cc = (s.get("country") or "").upper()
            return "GB" if cc == "UK" else ("" if cc == "GLOBAL" else cc)
    return ""


def _known(phrase: str) -> bool:
    k = phrase.lower()
    return k in G.names or k in G.cname or k in G.regions


def _spans(text: str):
    """Capitalised word runs (1-3 words, allowing 'of/de/upon...' inside), longest first, as (token_index, phrase,
    next_to_a_name). A run that is a known place is consumed so its words are not matched again. next_to_a_name marks a
    word glued to another capitalised word ("Kyle Gordon", "Alex Pereira"): likely a person, not a place."""
    toks = [(m.group(0).strip(".'’-"), m.start()) for m in TOKEN.finditer(text)]
    toks = [t for t in toks if t[0]]
    i, n = 0, len(toks)
    while i < n:
        if not toks[i][0][0].isupper():
            i += 1
            continue
        hit = False
        for ln in (3, 2, 1):
            j = i + ln
            if j > n:
                continue
            words = [t[0] for t in toks[i:j]]
            if not all(x[0].isupper() or x.lower() in SMALL for x in words) or words[-1].lower() in SMALL:
                continue
            ph = " ".join(words)
            if _known(ph):
                prev = toks[i - 1][0] if i else ""
                nxt = toks[j][0] if j < n else ""
                adj = any(w and w[0].isupper() and w.lower() not in FUNC for w in (prev, nxt))
                yield i, ph, adj
                i, hit = j, True
                break
        if not hit:
            i += 1


def _pick(cands: list[Place], ctx: set[str], outlet: str, strong: set[str] = frozenset()) -> Place | None:
    """Choose one of several places sharing a name. A country named in the text beats population; population only
    wins when it is lopsided (3x); the outlet's own country breaks a tie. Otherwise ambiguous: drop (fail closed)."""
    inctx = [p for p in cands if p.cc in ctx]
    if inctx and not (cands[0] not in inctx and cands[0].pop >= 20 * inctx[0].pop and inctx[0].cc not in strong):
        return inctx[0]
    if len(cands) == 1:
        return cands[0]
    if cands[0].pop >= 3 * cands[1].pop:
        return cands[0]
    home = [p for p in cands if p.cc == outlet]
    return home[0] if home else None


def locate(parts: list[tuple[str, float]], hints=frozenset(), domains=()) -> dict | None:
    """parts: [(text, weight)], e.g. headline x3, dek x2, body x1. hints: place names the NER step already flagged.
    Returns {place, country, cc, lat, lon, precision, score} or None."""
    if not ensure():
        return None
    low_hints = {h.lower() for h in hints}
    spans = [list(_spans(text)) for text, _ in parts]
    ctx = {G.cname[ph.lower()] for sp in spans for _i, ph, _a in sp if ph.lower() in G.cname}      # countries named in the text
    strong: set[str] = set()                              # countries vouched for by a region name
    for sp in spans:                                      # a region only named in one country ("Texas") also gives country context
        for _i, ph, _a in sp:
            ccs = {r.cc for r in G.regions.get(ph.lower(), [])}
            if len(ccs) == 1 and ph.lower() not in DIRECTIONAL and ph.lower() not in G.names:
                ctx |= ccs
                strong |= ccs
    outlets = [c for c in (_outlet_cc(d) for d in domains) if c]
    outlet = max(set(outlets), key=outlets.count) if outlets else ""
    lower = {t.lower().rstrip("s") for text, _ in parts for t in TOKEN.findall(text) if t[0].islower()}     # words also used as plain nouns
    score: dict[tuple, float] = defaultdict(float)
    obj: dict[tuple, Place] = {}
    for (_text, w), sp in zip(parts, spans):
        for i, ph, adj in sp:
            k = ph.lower()
            single = " " not in ph
            hinted = k in low_hints
            place = None
            if k in G.names:
                c = G.names[k]
                vouched = hinted or any(p.cc in ctx for p in c)
                if not (single and (k in AMBIGUOUS or len(k) <= 3) and not vouched):
                    place = _pick(c, ctx, outlet, strong)
                    if place and single and place.pop < 1_000_000 and not vouched and adj:
                        place = None    # a small town glued to another name is a person ("Kyle Gordon"), not a place
                    if place and single and place.pop < 300_000 and not hinted and k.rstrip("s") in lower:
                        place = None    # "State Bank ... banks": a small town that is also a common noun in this very text
            if place is None and k in G.regions and k not in DIRECTIONAL and not (single and len(k) <= 4 and not hinted):
                place = _pick(G.regions[k], ctx, outlet, strong)
            if place is None and k in G.cname and (k not in COUNTRY_AMBIGUOUS or hinted):
                place = G.countries.get(G.cname[k])
            if place is None:
                continue
            key = (place.kind, place.gid, place.admin1, place.cc, place.name)
            score[key] += w + (0.8 if hinted else 0.0) - min(0.2, 0.005 * i)     # earlier = dateline position counts a bit more
            obj[key] = place
    if not score:
        return None
    rank = {"city": 2, "region": 1, "country": 0}
    best = max(score, key=lambda k: (round(score[k], 1) + (0.4 if obj[k].kind == "city" else 0), rank[obj[k].kind]))
    p, s = obj[best], score[best]
    if s < 1.0:
        return None
    # a small city in a country the text never names, while another country IS named: probably a name clash
    if p.kind == "city" and ctx and p.cc not in ctx and p.pop < 1_000_000:
        named = [(score[k], o) for k, o in obj.items() if o.kind == "country" and o.cc in ctx]
        if not named:
            return None
        s, p = max(named, key=lambda t: t[0])
    return {"place": p.name, "country": G.country_names.get(p.cc, p.cc), "cc": p.cc, "lat": round(p.lat, 4),
            "lon": round(p.lon, 4), "precision": p.kind, "score": round(s, 2)}


def cities_in(w: float, s: float, e: float, n: float, limit: int) -> list[Place]:
    """Biggest cities inside a bbox (west > east wraps the antimeridian)."""
    ok = (lambda lo: w <= lo <= e) if w <= e else (lambda lo: lo >= w or lo <= e)
    hit = [p for p in G.cities if s <= p.lat <= n and ok(p.lon)]
    return sorted(hit, key=lambda p: -p.pop)[:limit]


def nearest_city(lat: float, lon: float, max_km: float = 150.0) -> Place | None:
    """The nearest city, or a clearly bigger one within 8 km of it (Brooklyn Heights -> New York City)."""
    if not ensure():
        return None
    cosl = max(0.05, math.cos(math.radians(lat)))
    near = []
    for p in G.cities:
        dy = (p.lat - lat) * 111.0
        if abs(dy) <= max_km:
            d = math.hypot((p.lon - lon) * 111.0 * cosl, dy)
            if d <= max_km:
                near.append((d, p))
    if not near:
        return None
    d0 = min(d for d, _p in near)
    return max((p for d, p in near if d <= d0 + 8.0), key=lambda p: p.pop)


def search(q: str, limit: int = 8) -> list[dict]:
    if not ensure() or len(q.strip()) < 2:
        return []
    k = q.strip().lower()
    cands = list(G.names.get(k, []))
    if len(k) >= 3:
        cands += [p for n, ps in G.names.items() if n.startswith(k) and n != k for p in ps[:2]]
    out, seen = [], set()
    for p in sorted(cands, key=lambda p: -p.pop):
        if p.gid in seen:
            continue
        seen.add(p.gid)
        out.append({"name": p.name, "country": G.country_names.get(p.cc, p.cc), "lat": p.lat, "lon": p.lon, "pop": p.pop})
    cc = G.cname.get(k)
    if cc:
        c = G.countries[cc]
        out.insert(0, {"name": c.name, "country": c.name, "lat": c.lat, "lon": c.lon, "pop": c.pop, "zoom": 4})
    return out[:limit]


# ── tagging (stories + headlines) ───────────────────────────────────────────

async def tag_new(limit: int = 4000) -> dict:
    """Tag stories and recent articles that have no (current) tag. Pure Python, a few ms per item. Items with no
    confident place get an empty tag so they are not re-scanned."""
    if not ensure():
        return {"stories": 0, "articles": 0, "seen": 0}
    now = db.now_iso()
    rows = await db.fetch_all(
        "SELECT s.* FROM stories s LEFT JOIN geo_tags g ON g.ref_kind='story' AND g.ref_id=s.id "
        "WHERE s.tier IN ('primary','more','wire') AND s.kind IN ('feed','link') AND (g.ref_id IS NULL OR g.v<? OR g.tagged_at<s.updated_at) LIMIT ?",
        (GEO_V, limit))
    jobs = []
    for r in rows:
        ents = await db.fetch_all("SELECT name FROM entities WHERE story_id=? AND type='place'", (r["id"],))
        doms = [x["domain"] for x in await db.fetch_all("SELECT domain FROM story_sources WHERE story_id=?", (r["id"],))]
        body = " ".join(str(x) for x in (db.jload(r.get("key_points"), []) or [])[:6])
        parts = [(r["headline"] or r["headline_original"] or "", 3.0), (r["headline_original"] or "", 2.0), (r["dek"] or "", 2.0),
                 (r["what_happened"] or "", 1.0), (body, 0.7)]
        jobs.append(("story", r["id"], [p for p in parts if p[0]], {e["name"] for e in ents}, doms))
    arts = await db.fetch_all(
        "SELECT a.id,a.title,a.snippet,a.domain FROM articles a LEFT JOIN geo_tags g ON g.ref_kind='article' AND g.ref_id=a.id "
        "WHERE a.published_at > datetime('now','-10 day') AND (g.ref_id IS NULL OR g.v<?) LIMIT ?", (GEO_V, limit))
    for a in arts:
        jobs.append(("article", a["id"], [(a["title"], 3.0), (a["snippet"] or "", 1.0)], frozenset(), [a["domain"]]))
    out = await asyncio.to_thread(lambda: [(k, rid, locate(parts, h, d)) for k, rid, parts, h, d in jobs])     # CPU work: off the event loop
    rows2 = [(k, rid, t and t["lat"], t and t["lon"], t["place"] if t else "", t["cc"] if t else "", t["country"] if t else "",
              t["precision"] if t else "", t["score"] if t else 0, GEO_V, now) for k, rid, t in out]
    if rows2:
        await db.executemany("INSERT OR REPLACE INTO geo_tags(ref_kind,ref_id,lat,lon,place,cc,country,precision,score,v,tagged_at) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows2)
    return {"stories": sum(1 for k, _r, t in out if k == "story" and t), "articles": sum(1 for k, _r, t in out if k == "article" and t),
            "seen": len(out)}


_last_tag = 0.0


async def tag_recent() -> None:
    """Called by the globe endpoints: tag what the last feed cycle added, at most every 30 s."""
    global _last_tag
    if time.monotonic() - _last_tag > 30:
        _last_tag = time.monotonic()
        await tag_new(800)
