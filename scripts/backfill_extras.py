"""Backfill claim provenance, coverage lens and Wikipedia background for stories built before those features.

Usage: venv/python.exe scripts/backfill_extras.py [--db PATH] [--no-wiki]"""
import argparse, asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ap = argparse.ArgumentParser(); ap.add_argument("--db"); ap.add_argument("--no-wiki", action="store_true")
a = ap.parse_args()
if a.db:
    os.environ["DB_PATH"] = a.db
from backend import db
from backend.services import context, lens
from backend.services.verify import provenance, split_sentences


async def main():
    await db.init_db()
    stories = await db.fetch_all("SELECT id,headline,dek,what_happened,key_points,extras FROM stories WHERE kind='feed' AND status='complete'")
    for s in stories:
        rows = await db.fetch_all(
            "SELECT ss.n,ss.url,ss.outlet,ss.domain,ss.title,a.full_text AS text FROM story_sources ss "
            "LEFT JOIN articles a ON a.url=ss.url WHERE ss.story_id=? ORDER BY ss.n", (s["id"],))
        texts = [r for r in rows if r["text"]]
        src = [{"n": r["n"], "outlet": r["outlet"], "url": r["url"], "text": r["text"]} for r in texts]
        claims = [(x, i) for i, x in enumerate(split_sentences(s["what_happened"]))]
        pts = [(x, i) for i, x in enumerate(db.jload(s["key_points"], []))]
        prov = [dict(c, kind="summary") for c in provenance(claims, src)] + [dict(c, kind="point") for c in provenance(pts, src)]
        extras = db.jload(s["extras"], {}) or {}
        extras["claims"] = prov
        extras["lens"] = lens.build_lens(texts, rows, f"{s['headline']}. {s['dek']} {s['what_happened']}")
        if not a.no_wiki:
            ents = await db.fetch_all("SELECT name,type,mentions FROM entities WHERE story_id=? ORDER BY mentions DESC", (s["id"],))
            extras["background"] = await context.background_for(ents)
        await db.execute("UPDATE stories SET extras=? WHERE id=?", (db.jdump(extras), s["id"]))
        print(s["id"], "claims", len(prov), "unique", len(extras["lens"]["unique"]), "bg", len(extras.get("background", [])))

asyncio.run(main())
