"""Globe: every geotagged story and headline as GeoJSON, nearby items for a place, live local headlines, place search."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from backend import db
from backend.routers.feed import signal_of
from backend.services import geo, geo_live

router = APIRouter(prefix="/api/globe", tags=["globe"])
SHOWN = "('primary','more','wire')"
SPREAD = {"city": 0.02, "region": 0.3, "country": 0.7}      # degrees: stories sharing a place fan out instead of stacking
# Articles that already belong to a cluster with a visible story are that story, not another headline.
NOT_IN_STORY = ("NOT EXISTS (SELECT 1 FROM clusters c JOIN stories s2 ON s2.id=c.story_id "
                f"WHERE c.id=a.cluster_id AND s2.tier IN {SHOWN})")


def _cut(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 24 * 14)))).isoformat()


def _jitter(ref: str, precision: str) -> tuple[float, float]:
    h = hashlib.md5(ref.encode()).digest()
    r = SPREAD.get(precision, 0.02) * math.sqrt(h[0] / 255)
    a = h[1] / 255 * 2 * math.pi
    return r * math.sin(a), r * math.cos(a)


def _story_props(r: dict) -> dict:
    gate = db.jload(r.get("gate"), {}) or {}
    topic = ("ch:" + r["channel_id"]) if r.get("channel_id") else ("world" if (r["topic"] or "").startswith("ch:") else r["topic"])
    return {"id": r["id"], "kind": "story", "title": r["headline"] or r["headline_original"], "topic": topic,
            "place": r["place"], "country": r["country"], "precision": r["precision"], "t": r["t"],
            "rank": round(max(0.05, signal_of(gate) / 10), 2), "outlets": r["outlet_count"], "tier": r["tier"],
            "channel": r.get("channel_id") or "", "outlet": "", "url": ""}


def _headline_props(r: dict) -> dict:
    return {"id": r["id"], "kind": "headline", "title": r["title"], "topic": r["topic"], "place": r["place"],
            "country": r["country"], "precision": r["precision"], "t": r["t"],
            "rank": round(0.12 + 0.06 * (4 - min(max(r["trust_rank"], 1), 3)), 2), "outlets": 1, "tier": "headline",
            "channel": "", "outlet": r["outlet"], "url": r["url"]}


def _live_props(it: dict) -> dict:
    return {"id": it["id"], "kind": "headline", "title": it["title"], "topic": "world", "place": it["place"], "country": it["country"],
            "precision": "city", "t": it["t"], "rank": 0.2, "outlets": 1, "tier": "live", "channel": "", "outlet": it["outlet"], "url": it["url"]}


def _bbox_sql(bbox: Optional[str]) -> tuple[str, list]:
    try:
        w, s, e, n = (float(x) for x in (bbox or "").split(","))
    except ValueError:
        return "", []
    lon = "g.lon BETWEEN ? AND ?" if w <= e else "(g.lon >= ? OR g.lon <= ?)"
    return f" AND g.lat BETWEEN ? AND ? AND {lon}", [max(-90, s), min(90, n), w, e]


async def _stories(cut: str, topic: str, channel: str, extra: str = "", params: Optional[list] = None, limit: int = 3000) -> list[dict]:
    sql = ("SELECT s.id,s.headline,s.headline_original,s.topic,s.channel_id,s.tier,s.outlet_count,s.gate,"
           "COALESCE(s.published_at,s.created_at) AS t,g.lat,g.lon,g.place,g.country,g.precision "
           "FROM stories s JOIN geo_tags g ON g.ref_kind='story' AND g.ref_id=s.id "
           f"WHERE g.lat IS NOT NULL AND s.tier IN {SHOWN} AND s.kind IN ('feed','link') AND COALESCE(s.published_at,s.created_at) > ?")
    p: list = [cut]
    if topic.startswith("ch:"):
        sql += " AND s.channel_id=?"
        p.append(topic[3:])
    elif topic:
        sql += " AND (s.topic=? OR (?='world' AND s.topic LIKE 'ch:%' AND s.channel_id=''))"
        p += [topic, topic]
    if channel:
        sql += " AND s.channel_id!=''" if channel == "any" else " AND s.channel_id=?"
        p += [] if channel == "any" else [channel]
    sql += extra + " ORDER BY t DESC LIMIT ?"
    return await db.fetch_all(sql, p + (params or []) + [limit])


async def _headlines(cut: str, topic: str, extra: str = "", params: Optional[list] = None, limit: int = 4000) -> list[dict]:
    if topic.startswith("ch:"):
        return []
    sql = ("SELECT a.id,a.title,a.topic,a.outlet,a.url,a.trust_rank,a.published_at AS t,g.lat,g.lon,g.place,g.country,g.precision "
           "FROM articles a JOIN geo_tags g ON g.ref_kind='article' AND g.ref_id=a.id "
           f"WHERE g.lat IS NOT NULL AND a.published_at > ? AND {NOT_IN_STORY}")
    p: list = [cut]
    if topic:
        sql += " AND a.topic=?"
        p.append(topic)
    sql += extra + " GROUP BY COALESCE(a.cluster_id, a.id) ORDER BY t DESC LIMIT ?"
    return await db.fetch_all(sql, p + (params or []) + [limit])


@router.get("")
async def globe(hours: int = 24, topic: str = "", channel: str = "", headlines: int = 1, bbox: str = ""):
    await geo.tag_recent()
    extra, ep = _bbox_sql(bbox)
    cut = _cut(hours)
    rows = [(r, _story_props(r)) for r in await _stories(cut, topic, channel, extra, ep)]
    if headlines and not channel:
        rows += [(r, _headline_props(r)) for r in await _headlines(cut, topic, extra, ep)]
    if headlines and not channel and not topic:       # local headlines fetched for the busiest cities (and any city the reader opened)
        box = tuple(float(x) for x in bbox.split(",")) if bbox.count(",") == 3 else (-180.0, -90.0, 180.0, 90.0)
        for it in await geo_live.points(hours, box):
            rows.append((dict(it, precision="city"), _live_props(it)))
    feats = []
    for r, props in rows:
        dy, dx = _jitter(props["id"], r["precision"])
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [round(r["lon"] + dx, 4), round(max(-85.0, min(85.0, r["lat"] + dy)), 4)]}})
    return {"type": "FeatureCollection", "features": feats, "updated": db.now_iso()}


def _with_pos(p: dict, r: dict) -> dict:
    dy, dx = _jitter(p["id"], r["precision"])                # the same fan-out as the map dot, so "fly to" lands on it
    return dict(p, lat=round(max(-85.0, min(85.0, r["lat"] + dy)), 4), lon=round(r["lon"] + dx, 4))


def _km(lat: float, lon: float, r: dict) -> float:
    return math.hypot((r["lon"] - lon) * 111.0 * max(0.05, math.cos(math.radians(lat))), (r["lat"] - lat) * 111.0)


@router.get("/near")
async def near(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180), km: float = Query(60, ge=1, le=3000),
               hours: int = 72, limit: int = Query(40, ge=1, le=100)):
    dlat = km / 111.0
    dlon = min(180.0, km / (111.0 * max(0.05, math.cos(math.radians(lat)))))
    extra = " AND g.lat BETWEEN ? AND ? AND g.lon BETWEEN ? AND ?"
    ep = [lat - dlat, lat + dlat, lon - dlon, lon + dlon]
    cut = _cut(hours)
    stories = [_with_pos(_story_props(r), r) for r in await _stories(cut, "", "", extra, ep, 400) if _km(lat, lon, r) <= km]
    heads = [_with_pos(_headline_props(r), r) for r in await _headlines(cut, "", extra, ep, 400) if _km(lat, lon, r) <= km]
    live = [_with_pos(_live_props(it), dict(it, precision="city")) for it in await geo_live.points(hours, (lon - dlon, lat - dlat, lon + dlon, lat + dlat))
            if _km(lat, lon, it) <= km]
    heads = sorted(heads + live, key=lambda p: p["t"] or "", reverse=True)
    stories.sort(key=lambda p: (-p["rank"], p["t"] or ""))
    pl = geo.nearest_city(lat, lon, max(40.0, km))
    place = {"name": pl.name, "country": geo.G.country_names.get(pl.cc, pl.cc), "lat": pl.lat, "lon": pl.lon} if pl else None
    return {"place": place, "items": (stories + heads)[:limit]}


@router.get("/area")
async def area(w: float = Query(..., ge=-180, le=180), s: float = Query(..., ge=-90, le=90), e: float = Query(..., ge=-180, le=180),
               n: float = Query(..., ge=-90, le=90), zoom: float = Query(4, ge=0, le=24)):
    """Zoomed into a region: start collecting live headlines for its cities. Poll until pending is 0, then re-read /api/globe."""
    return await geo_live.collect_area(w, s, e, n, zoom)


class LocalPlace(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    native: str = Field("", max_length=40)                  # the same place in its own script, from the map label


class LocalPlaces(BaseModel):
    places: list[LocalPlace] = Field(..., max_length=60)


@router.post("/places")
async def places(body: LocalPlaces):
    """Street-level zoom: the map's own neighbourhood labels. Poll by posting the same list until pending is 0."""
    ok = [(p.name.strip(), p.lat, p.lon, p.native.strip() if p.native.strip() != p.name.strip() else "") for p in body.places if p.name.strip()[:1].isupper() and len(p.name.strip()) >= 4]
    return await geo_live.collect_places(ok)


@router.get("/outlets")
async def outlets():
    """Every outlet the live fetchers have seen (domain, name, headlines): the source auditor's candidate list."""
    return geo_live.outlets()


@router.get("/live")
async def live(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)):
    pl = geo.nearest_city(lat, lon, 120.0)
    if not pl:
        return {"place": None, "items": [], "cached": False}
    country = geo.G.country_names.get(pl.cc, pl.cc)
    items, cached = await geo_live.headlines(pl.name, pl.cc, at=(pl.lat, pl.lon, country, pl.name))
    return {"place": {"name": pl.name, "country": geo.G.country_names.get(pl.cc, pl.cc), "lat": pl.lat, "lon": pl.lon},
            "items": items, "cached": cached}


@router.get("/search")
async def search(q: str = Query("", max_length=80)):
    return geo.search(q)


@router.get("/stats")
async def stats():
    await geo.tag_recent()
    r = await db.fetch_one("SELECT SUM(ref_kind='story' AND lat IS NOT NULL) AS s, SUM(ref_kind='article' AND lat IS NOT NULL) AS h, "
                           "COUNT(DISTINCT CASE WHEN lat IS NOT NULL THEN place END) AS places, MAX(tagged_at) AS updated FROM geo_tags")
    return {"stories": r["s"] or 0, "headlines": r["h"] or 0, "places": r["places"] or 0, "updated": r["updated"]}
