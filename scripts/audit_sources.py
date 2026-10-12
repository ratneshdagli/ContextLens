"""Audit news websites and add the good ones to ContextLens's source list.

  venv\python.exe scripts/audit_sources.py                      # dry run: audit what the live globe fetchers have seen (backend on :8000)
  venv\python.exe scripts/audit_sources.py --search "Palghar news" --apply
  venv\python.exe scripts/audit_sources.py --domain abplive.com --domain loksatta.com

Accepted is a machine verdict: eyeball it (tax blogs, gossip and aggregators can pass) and use --only to write just the vetted domains.
Accepted = a working RSS/Atom feed with 5+ items, something in the last 7 days, English. Added to backend/feeds_extra.json and
backend/trusted_sources.json (trust rank 3, verified_by "auditor"); restart the backend to start ingesting them."""

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from backend.services import audit  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--search", default="")
    ap.add_argument("--domain", action="append", default=[])
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="", help="with --apply: comma-separated domains you vetted; only these are written")
    a = ap.parse_args()
    res = await audit.run(a.api, a.search, tuple(a.domain), a.limit)
    for r in sorted(res, key=lambda r: r["verdict"]):
        print(f'{r["verdict"]:15} {r["domain"]:32} {r.get("n", "-"):>4} items  {r["reason"]}')
    print(f'\n{len(res)} audited, {sum(r["verdict"] == "accept" for r in res)} accepted')
    if a.apply:
        print(f"added {audit.apply(res, tuple(d for d in a.only.split(",") if d))} feeds - restart the backend to ingest them")


asyncio.run(main())
