"""Run one full feed cycle against live feeds and print every gate decision.

Usage: venv/python.exe scripts/eval_feed.py [--db PATH] [--size N] [--pool N]
Uses a scratch DB by default so the real one is untouched."""
import argparse, asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ap = argparse.ArgumentParser()
ap.add_argument("--db", default="D:/newaai/_eval.db"); ap.add_argument("--size", type=int, default=5)
ap.add_argument("--pool", type=int, default=14); ap.add_argument("--fresh", action="store_true")
a = ap.parse_args()
if a.fresh and os.path.exists(a.db): os.remove(a.db)
os.environ["DB_PATH"] = a.db
from backend import db, settings_store
from backend.services import interests, pipeline

async def main():
    await db.init_db()
    await settings_store.update_settings({"build_budget": a.size, "candidate_pool": a.pool, "wire_size": 30})
    await interests.save_profile({"interests": [{"text": "AI chips, semiconductors and AI infrastructure", "weight": 3},
        {"text": "Indian economy, RBI and markets", "weight": 2}, {"text": "Formula 1 and motorsport", "weight": 2}],
        "topics": {"tech": 2, "markets": 2, "india": 2, "world": 1, "politics": 1, "sports": 1, "culture": 0, "health": 1, "science": 1, "business": 2},
        "muted_keywords": ["horoscope", "royal family"], "onboarded": True})
    rid = await pipeline.run_feed_cycle("eval")
    await asyncio.sleep(0.5)
    while pipeline._enrich_lock.locked():          # background enrichment (timeline / story map / primer)
        await asyncio.sleep(2)
    await pipeline.enrich_modules()
    for e in await __import__("backend.services.runs", fromlist=["x"]).load_events(rid):
        print(f"{e['ts'][11:19]} [{e['stage']:8}] {e['level'][:4]} {e['message']}")
    print("\n================ STORIES ================")
    for s in await db.fetch_all("select * from stories order by tier, published_at desc"):
        print(f"\n[{s['tier'].upper()}] {s['headline']}\n  orig: {s['headline_original']}\n  dek: {s['dek']}\n  what: {s['what_happened']}")
        print("  points:", *[f"\n    - {k}" for k in db.jload(s['key_points'], [])])
        print("  why:", s['why_it_matters'], "| for you:", s['why_for_you'])
        ex = (db.jload(s['extras'], {}) or {}).get('explain')
        if ex:
            print("  GIST:", ex['gist'])
            for i, w in enumerate(ex['why'], 1): print(f"   {i}. [{w['basis']}] {w['cause']} -> {w['link']}")
        g = db.jload(s['gate'], {}); print("  gate:", g.get('publish',{}).get('reasons'), "| tl:", g.get('timeline',{}).get('reason'), "| graph:", g.get('graph',{}).get('reason'))
        for e in await db.fetch_all("select date,text from timeline_events where story_id=? order by date",(s['id'],)): print("   TL", e['date'], e['text'])
        for r in await db.fetch_all("select subject,relation,object from relations where story_id=?",(s['id'],)): print("   REL", r['subject'],"--",r['relation'],"->",r['object'])
asyncio.run(main())
