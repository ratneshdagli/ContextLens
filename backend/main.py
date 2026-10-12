"""ContextLens v6 — FastAPI app. Serves the API and the built frontend on one port."""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import db
from backend.config import APP_NAME, APP_VERSION, CORS_ORIGINS, FRONTEND_DIST
from backend.routers import ask, channels, digest, discover, feed, follow, globe, inspect, kb, papers, profile, research, settings, sources, stories, voice
from backend.services import geo, geo_live, llm, ner_client, scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


async def _geo_warm():
    """Load the gazetteer, tag whatever is untagged (globe) and keep local headlines fresh, without delaying startup."""
    log = logging.getLogger("geo")
    try:
        await asyncio.to_thread(geo.ensure)
        while (await geo.tag_new(1500))["seen"] >= 1500:
            await asyncio.sleep(0.1)
    except Exception:  # noqa: BLE001
        log.exception("geo warm-up failed")
    while not os.environ.get("DISABLE_SCHEDULER"):      # tests and --no-scheduler runs never touch the network
        try:
            await geo.tag_recent()
            await geo_live.prune()
            await geo_live.warm()
        except Exception:  # noqa: BLE001
            log.exception("live headline refresh failed")
        await asyncio.sleep(20 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    h = await llm.health()
    state = f"online, {len(h['models'])} models" if h["available"] else f"OFFLINE - {h.get('error')}"
    print(f"\n  {APP_NAME} v{APP_VERSION}\n  LLM server: {state}\n")
    scheduler.start()
    geo_task = asyncio.create_task(_geo_warm())
    yield
    geo_task.cancel()
    scheduler.stop()
    await ner_client.shutdown()
    try:
        from backend.services import lms
        if lms.status()["current"]:
            await lms.unload_all_chat()                 # leave the GPU free when the app stops
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
for r in (feed.router, stories.router, follow.router, ask.router, profile.router, research.router, settings.router,
          inspect.router, kb.router, papers.router, sources.router, voice.router, digest.router, channels.router, globe.router, discover.router):
    app.include_router(r)


@app.middleware("http")
async def reader_first(request: Request, call_next):
    """LLM calls made while serving a request (chat, inspector, deep dives, builds, uploads) jump ahead of background work."""
    llm.interactive()
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    logging.getLogger("api").exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": str(exc)[:300]})


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse(status_code=404, content={"error": "not found"})
        f = FRONTEND_DIST / path
        return FileResponse(f if path and f.is_file() else FRONTEND_DIST / "index.html")
