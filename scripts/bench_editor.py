"""Benchmark candidate editor models on labelled real headlines (reader: AI chips, Indian economy/RBI/markets, F1)."""
import asyncio, json, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = "D:/newaai/_bench.db"
from backend import db, settings_store
from backend.services import editor, interests

LABELS = {  # title prefix -> should the reader see it on the front page?
 "US and China discuss AI safety":1, "Nvidia boss says there is":1, "Latest GMP compared":0, "Trump to create":1,
 "Top stocks in focus":0, "US Market: Fed rate path":1, "Pine Labs shares can rally":0, "HEG shares rally":0,
 "Meta's 'highest-paid'":0, "Advani, Jaishankar":1, "India bonds calm":1, "Global Market: Micron":1,
 "‘Kya hua tera vada’":0, "Goldman Sachs names 5":0, "FIIs pull out":1, "India’s REITs just":0,
 "‘RAMageddon’":1, "India’s chip push":1, "Karnataka FDA cracks":0, "German Chancellor Merz":1,
 "Government tests AI system":0, "'Exercise Tarang Shakti":0, "‘Mard ki aulaad":0, "IIT Bombay suicide":1,
}
async def main(models):
    con = sqlite3.connect("D:/newaai/_eval.db"); con.row_factory = sqlite3.Row
    items = []
    for c in con.execute("select id,title,outlet_count from clusters where triage is not null"):
        lab = next((v for k, v in LABELS.items() if c["title"].startswith(k)), None)
        if lab is None: continue
        mem = [dict(r) for r in con.execute("select title,snippet,outlet,domain,trust_rank from articles where cluster_id=?", (c["id"],))]
        items.append(({"members": mem, "outlet_count": c["outlet_count"]}, lab, c["title"]))
    await db.init_db()
    prof = {"interests":[{"text":"AI chips, semiconductors and AI infrastructure","weight":3},{"text":"Indian economy, RBI and markets","weight":2},{"text":"Formula 1 and motorsport","weight":2}],
            "topics":{"tech":2,"markets":2,"india":2,"world":1,"politics":1,"sports":1,"culture":0,"health":1,"science":1,"business":2},"muted_keywords":[],"followed":[],"muted_outlets":[]}
    brief = interests.profile_brief(prof)
    for m in models:
        await settings_store.update_settings({"model_editor": m, "single_model_mode": False})
        t = time.time(); ok = 0; kept = 0; wrong = []; vals = []
        for c, lab, title in items:
            d = await editor.triage(c, brief, 0.5, 0.5, "balanced")
            a = d.get("answers", {}); vals.append((a.get("impact"), a.get("matches_reader"), a.get("reader_value")))
            kept += d["keep"]
            if int(d["keep"]) == lab: ok += 1
            else: wrong.append(("FP" if d["keep"] else "FN", title[:55], d["reasons"][:2]))
        print(f"\n### {m}: accuracy {ok}/{len(items)} kept {kept} in {time.time()-t:.0f}s; distinct answer-combos={len(set(vals))}")
        for w in wrong: print("  ", *w)
asyncio.run(main(sys.argv[1:]))
