"""P12 Admin API — minimal FastAPI surface over the pipeline services.

Run: ``python app.py`` (or ``./run.sh``). A single async Database connection is
opened in the lifespan handler and reused on the event loop, so aiosqlite is
tied to one loop and never leaks across reruns. Read-mostly: stats/calendar are
GET; any action (content generation, scheduler tick) is an explicit POST.

Endpoints:
    GET  /health
    GET  /api/stats
    GET  /api/calendar
    POST /api/scheduler/tick
    POST /api/scheduler/publish-due
    POST /api/pipeline/content/{city_id}
"""
from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import Config
from core.database import Database
from modules.scheduler import Scheduler
from modules.stats import StatsService


def _load_config() -> Config:
    """Single config source for the API entrypoint (ADR-101).

    Always read config.yaml — never a bare ``Config()`` (which would silently
    ignore ``app.dry_run``, ``publishing.*`` and any user-edited settings).
    """
    from core.config import load_config_file

    return load_config_file()


@asynccontextmanager
async def lifespan(app_obj: FastAPI):
    app_obj.state.config = _load_config()
    app_obj.state.db = Database()
    await app_obj.state.db.connect()
    try:
        yield
    finally:
        await app_obj.state.db.close()


app = FastAPI(title="Travel Blog Automation API", version="1.0.0", lifespan=lifespan)


def _sched() -> Scheduler:
    return Scheduler(app.state.db, app.state.config)


@app.get("/health")
async def health():
    return {"status": "ok", "db": "connected"}


@app.get("/api/stats")
async def stats():
    svc = StatsService(app.state.db)
    return {
        "summary": await svc.summary(),
        "by_status": await svc.by_status(),
        "by_platform": await svc.by_platform(),
    }


@app.get("/api/calendar")
async def calendar():
    now = dt.datetime.now(dt.timezone.utc)
    due = await app.state.db.get_due_publications(now, limit=100)
    return {
        "due": [
            {
                "id": p.id,
                "city_id": p.city_id,
                "platform": p.platform,
                "status": p.status,
                "scheduled_at": p.scheduled_at,
            }
            for p in due
        ],
    }


@app.post("/api/scheduler/tick")
async def scheduler_tick():
    return await _sched().tick()


@app.post("/api/scheduler/publish-due")
async def publish_due():
    """Run due publications and return which actually reached PUBLISHED.

    Historically the endpoint used ``getattr(r, "success", False)`` but the rows
    are Publication objects (no ``success`` attr) so it always returned an empty
    list. Now we filter on the real status (ADR-105, F9).
    """
    results = await _sched().run_due(limit=20)
    published = [
        p for p in results
        if (p.status.value if hasattr(p.status, "value") else p.status) == "published"
    ]
    return {
        "published": [p.platform for p in published],
        "count": len(published),
    }


@app.post("/api/pipeline/content/{city_id}")
async def pipeline_content(city_id: int):
    from modules.content.engine import ContentEngine

    ce = ContentEngine(app.state.db, app.state.config)
    result = await ce.process_city(city_id)
    return {"ok": True, "city_id": city_id, "result": result}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
