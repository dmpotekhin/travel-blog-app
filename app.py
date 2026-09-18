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
    GET  /api/cities/{city_id}/storyboard
    POST /api/cities/{city_id}/storyboard/generate
    PUT  /api/cities/{city_id}/storyboard
    POST /api/cities/{city_id}/storyboard/approve
    GET  /api/storyboards/{storyboard_id}
"""
from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core import models as m
from core.config import Config
from core.database import Database
from core.exceptions import NotFoundError, StateTransitionError, StoryboardValidationError
from modules.scheduler import Scheduler
from modules.stats import StatsService
from modules.visual_narrative_studio import (
    ApprovalRequest,
    StoryboardUpdateRequest,
    VisualNarrativeStudio,
)


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
        if p.status == m.PublicationStatus.PUBLISHED
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


# -- Visual Narrative Studio (P13, ADR-106) ---------------------------------


def _studio() -> VisualNarrativeStudio:
    """Studio bound to the API's single Database connection."""
    return VisualNarrativeStudio(app.state.db, app.state.config)


@app.exception_handler(NotFoundError)
async def _not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc), "code": exc.code})


@app.exception_handler(StateTransitionError)
async def _illegal_transition_handler(request: Request, exc: StateTransitionError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": exc.code})


@app.exception_handler(StoryboardValidationError)
async def _storyboard_invalid_handler(
    request: Request, exc: StoryboardValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc), "code": exc.code, "issues": exc.issues},
    )


@app.get("/api/cities/{city_id}/storyboard")
async def city_storyboard(city_id: int) -> m.StoryboardBundle:
    """Latest storyboard version of the city with beats, shots and issues."""
    return await _studio().bundle_for_city(city_id)


@app.post("/api/cities/{city_id}/storyboard/generate")
async def generate_city_storyboard(
    city_id: int, dry_run: Optional[bool] = None
) -> m.VisualNarrativeResult:
    """Build the visual narrative for the city.

    ``dry_run`` omitted -> inherit ``app.dry_run`` from config.yaml; an explicit
    ``dry_run=false`` forces a real write even while the app runs in dry-run mode
    (the query flag must never be silently swallowed by the config default).
    """
    return await _studio().generate(city_id, dry_run=dry_run)


@app.put("/api/cities/{city_id}/storyboard")
async def update_city_storyboard(
    city_id: int, payload: StoryboardUpdateRequest
) -> m.StoryboardBundle:
    """Apply human edits (header, shots, order) to the latest storyboard."""
    return await _studio().apply_update(city_id, payload)


@app.post("/api/cities/{city_id}/storyboard/approve")
async def approve_city_storyboard(
    city_id: int, payload: Optional[ApprovalRequest] = None
) -> m.StoryboardBundle:
    """Human gate: the storyboard is approved for platform content."""
    studio = _studio()
    storyboard = await studio.latest_storyboard(city_id)
    if storyboard is None:
        raise NotFoundError(f"City {city_id} has no storyboard yet")
    request = payload or ApprovalRequest()
    return await studio.approve(storyboard.id or 0, force=request.force, actor=request.actor)


@app.get("/api/storyboards/{storyboard_id}")
async def get_storyboard(storyboard_id: int) -> m.StoryboardBundle:
    return await _studio().bundle_for_id(storyboard_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
