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
from typing import Any, Dict, List, Optional, Sequence

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core import models as m
from core.config import Config
from core.database import Database
from core.exceptions import (
    CarouselError,
    NotFoundError,
    PublishNotApprovedError,
    SourceResolutionError,
    StateTransitionError,
    StoryboardValidationError,
)
from core.models import CarouselSourceType, CarouselVertical
from modules.carousels.publishing import CAROUSEL_PLATFORMS
from modules.carousels.service import CarouselFactory
from modules.carousels.sources import MockSourceResolver
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


# -- Tri-Face Carousel Factory (P15, Phase 5) -------------------------------

CAROUSEL_SOURCE_TYPE_CHOICES = tuple(item.value for item in CarouselSourceType)
CAROUSEL_VERTICAL_CHOICES = tuple(item.value for item in CarouselVertical)
CAROUSEL_PLATFORM_CHOICES = tuple(CAROUSEL_PLATFORMS)


def _carousel() -> CarouselFactory:
    """Factory bound to the API's single Database connection."""
    return CarouselFactory(app.state.db, app.state.config)


def _carousel_source_type(value: str) -> CarouselSourceType:
    try:
        return CarouselSourceType(str(value).strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"unknown source_type {value!r}; allowed: {', '.join(CAROUSEL_SOURCE_TYPE_CHOICES)}",
        ) from exc


def _carousel_vertical(value: Optional[str]) -> Optional[CarouselVertical]:
    if value is None or not str(value).strip():
        return None
    try:
        return CarouselVertical(str(value).strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"unknown vertical {value!r}; allowed: {', '.join(CAROUSEL_VERTICAL_CHOICES)}",
        ) from exc


def _carousel_platforms(values: Optional[Sequence[str]]) -> Optional[List[str]]:
    if not values:
        return None
    cleaned = [str(item).strip().lower() for item in values if str(item).strip()]
    unknown = [item for item in cleaned if item not in CAROUSEL_PLATFORM_CHOICES]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unsupported platform(s) {', '.join(unknown)}; "
                f"allowed: {', '.join(CAROUSEL_PLATFORM_CHOICES)}"
            ),
        )
    return cleaned


class CarouselJobCreateRequest(BaseModel):
    source_type: str
    source_ref: str
    vertical: Optional[str] = None
    title: str = ""
    created_by: str = "api"
    dry_run: Optional[bool] = None
    platforms: Optional[List[str]] = None


class CarouselResearchRequest(BaseModel):
    resolver: Optional[str] = None
    detect_vertical: bool = True


class CarouselApprovalRequest(BaseModel):
    approved_by: str = "human"
    note: str = ""


class CarouselRejectionRequest(BaseModel):
    reason: str = ""
    rejected_by: str = "human"


class CarouselPublishRequest(BaseModel):
    platforms: Optional[List[str]] = None


@app.exception_handler(CarouselError)
async def _carousel_error_handler(request: Request, exc: CarouselError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc), "code": exc.code})


@app.exception_handler(SourceResolutionError)
async def _source_resolution_handler(request: Request, exc: SourceResolutionError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc), "code": exc.code})


@app.exception_handler(PublishNotApprovedError)
async def _publish_gate_handler(request: Request, exc: PublishNotApprovedError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": exc.code})


@app.get("/api/carousels/jobs")
async def list_carousel_jobs(
    status: Optional[str] = None,
    vertical: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Jobs newest-first; filters mirror the factory's ``list_jobs``."""
    jobs = await _carousel().list_jobs(
        status=status, vertical=vertical, source_type=source_type, limit=limit
    )
    return {
        "items": [_job_payload(job) for job in jobs],
        "count": len(jobs),
    }


def _job_payload(job: m.CarouselJob) -> Dict[str, Any]:
    payload = job.model_dump(mode="json")
    payload["job_id"] = job.id
    #: parsed list (the stored column is a raw JSON string) — the UI/queue read this
    payload["warnings"] = list(job.warnings)
    return payload


def _bundle_payload(bundle: m.CarouselBundle) -> Dict[str, Any]:
    """One shape for every carousel step: job (+parsed warnings), slides, publications."""
    return {
        "job": _job_payload(bundle.job),
        "slides": [slide.model_dump(mode="json") for slide in bundle.slides],
        "hooks": [hook.model_dump(mode="json") for hook in bundle.hooks],
        "publications": [item.model_dump(mode="json") for item in bundle.publications],
        "issues": list(bundle.issues),
    }
@app.post("/api/carousels/jobs", status_code=201)
async def create_carousel_job(payload: CarouselJobCreateRequest) -> Dict[str, Any]:
    """Register a job for a URL or GitHub reference (nothing is fetched here)."""
    job = await _carousel().create_job(
        source_type=_carousel_source_type(payload.source_type),
        source_ref=payload.source_ref,
        vertical=_carousel_vertical(payload.vertical),
        title=payload.title,
        created_by=payload.created_by,
        dry_run=payload.dry_run,
        platforms=_carousel_platforms(payload.platforms),
    )
    return _job_payload(job)


@app.get("/api/carousels/jobs/{job_id}")
async def get_carousel_job(job_id: int) -> Dict[str, Any]:
    """Job + slides + hooks + publications (+ verification issues)."""
    return _bundle_payload(await _carousel().get_bundle(job_id))


@app.get("/api/carousels/jobs/{job_id}/status")
async def carousel_job_status(job_id: int) -> Dict[str, Any]:
    """Where the job stands, whether it may publish, and the next path step."""
    return await _carousel().status_report(job_id)


@app.post("/api/carousels/jobs/{job_id}/research")
async def research_carousel_job(
    job_id: int, payload: Optional[CarouselResearchRequest] = None
) -> Dict[str, Any]:
    """Read the source into an audited context (``resolver="mock"`` stays offline)."""
    factory = _carousel()
    request = payload or CarouselResearchRequest()
    resolver = None
    if request.resolver and request.resolver.strip().lower() == "mock":
        resolver = MockSourceResolver()
    await factory.research(
        job_id, resolver=resolver, detect_vertical=request.detect_vertical
    )
    return _bundle_payload(await factory.get_bundle(job_id))


@app.post("/api/carousels/jobs/{job_id}/narrative")
async def draft_carousel_narrative(job_id: int) -> Dict[str, Any]:
    """Pick a hook and draft the narrative (facts stay traceable)."""
    factory = _carousel()
    await factory.draft_narrative(job_id)
    return _bundle_payload(await factory.get_bundle(job_id))


@app.post("/api/carousels/jobs/{job_id}/slides")
async def plan_carousel_slides(job_id: int) -> Dict[str, Any]:
    """Turn the narrative into the six-slide plan (768x1376, bottom-safe)."""
    factory = _carousel()
    await factory.plan_slides(job_id)
    return _bundle_payload(await factory.get_bundle(job_id))


@app.post("/api/carousels/jobs/{job_id}/render")
async def render_carousel_slides(job_id: int) -> Dict[str, Any]:
    """Paint every slide into a deterministic 768x1376 JPG."""
    factory = _carousel()
    await factory.render_slides(job_id)
    return _bundle_payload(await factory.get_bundle(job_id))


@app.post("/api/carousels/jobs/{job_id}/verify")
async def verify_carousel_slides(job_id: int) -> Dict[str, Any]:
    """Re-open the rendered files and check size, format, safe zone, alt text."""
    factory = _carousel()
    reports = await factory.verify_slides(job_id)
    bundle = await factory.get_bundle(job_id)
    return {
        "job": _job_payload(bundle.job),
        "reports": [report.model_dump(mode="json") for report in reports],
        "issues": bundle.issues,
    }


@app.post("/api/carousels/jobs/{job_id}/approve")
async def approve_carousel_job(
    job_id: int, payload: Optional[CarouselApprovalRequest] = None
) -> Dict[str, Any]:
    """Human gate: only an approved carousel may reach TikTok or Instagram."""
    request = payload or CarouselApprovalRequest()
    factory = _carousel()
    await factory.approve(job_id, approved_by=request.approved_by, note=request.note)
    return _bundle_payload(await factory.get_bundle(job_id))


@app.post("/api/carousels/jobs/{job_id}/reject")
async def reject_carousel_job(
    job_id: int, payload: Optional[CarouselRejectionRequest] = None
) -> Dict[str, Any]:
    """Send the carousel back to revision with the reviewer's reason attached."""
    request = payload or CarouselRejectionRequest()
    if not request.reason.strip():
        raise HTTPException(
            status_code=422,
            detail="rejection requires a reason (a silent reject teaches the factory nothing)",
        )
    factory = _carousel()
    await factory.reject(job_id, reason=request.reason, rejected_by=request.rejected_by)
    return _bundle_payload(await factory.get_bundle(job_id))


@app.post("/api/carousels/jobs/{job_id}/submit")
async def submit_carousel_for_approval(job_id: int) -> Dict[str, Any]:
    """Move a verified carousel into the human queue (auto-approves only in full autonomy)."""
    factory = _carousel()
    await factory.submit_for_approval(job_id)
    return _bundle_payload(await factory.get_bundle(job_id))


@app.get("/api/carousels/queue")
async def carousel_approval_queue(limit: int = 50) -> Dict[str, Any]:
    """Everything a human still has to look at (awaiting approval first)."""
    items = await _carousel().approval_queue(limit=limit)
    return {"items": [_job_payload(job) for job in items], "count": len(items)}


@app.post("/api/carousels/jobs/{job_id}/publish")
async def publish_carousel_job(
    job_id: int, payload: Optional[CarouselPublishRequest] = None
) -> Dict[str, Any]:
    """Upload the approved carousel (Upload-Post); supervised mode returns 409."""
    request = payload or CarouselPublishRequest()
    bundle = await _carousel().publish(
        job_id, platforms=_carousel_platforms(request.platforms)
    )
    return _bundle_payload(bundle)


@app.get("/api/carousels/jobs/{job_id}/publications")
async def list_carousel_publications(job_id: int) -> Dict[str, Any]:
    """One row per platform: status, request_id, post URL, error, raw response."""
    factory = _carousel()
    await factory.get_job(job_id)
    publications = await factory.list_publications(job_id)
    return {
        "items": [publication.model_dump(mode="json") for publication in publications],
        "count": len(publications),
    }


# -- Tri-Face Carousel Factory: analytics + learnings (Phase 6) --------------


@app.get("/api/carousels/jobs/{job_id}/metrics")
async def list_job_metrics(job_id: int) -> Dict[str, Any]:
    """Every collected number for one carousel, raw value kept next to it."""
    rows = await _carousel().list_metrics(job_id)
    return {"items": [row.model_dump(mode="json") for row in rows], "count": len(rows)}


@app.post("/api/carousels/jobs/{job_id}/metrics/collect")
async def collect_job_metrics(job_id: int) -> Dict[str, Any]:
    """Ask the platform what happened; an empty answer stays empty."""
    factory = _carousel()
    rows = await factory.collect_metrics(job_id)
    return {
        "items": [row.model_dump(mode="json") for row in rows],
        "count": len(rows),
        "job": _job_payload(await factory.get_job(job_id)),
    }


@app.get("/api/carousels/jobs/{job_id}/score")
async def score_carousel_job(job_id: int) -> Dict[str, Any]:
    """Composite score + components + why the basis is what it is."""
    score = await _carousel().score_job(job_id)
    return {
        "job_id": job_id,
        "score": score.score,
        "basis": score.basis,
        "components": score.components,
        "sample_size": score.sample_size,
        "warnings": list(score.warnings),
        "explain": score.explain(),
    }


@app.get("/api/carousels/learnings")
async def list_carousel_learnings(
    scope_type: Optional[str] = None,
    scope_value: Optional[str] = None,
    min_sample_size: int = 0,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Stored learnings (aggregates); aggregation happens on refresh, not on read."""
    rows = await _carousel().list_learnings(
        scope_type=scope_type,
        scope_value=scope_value,
        min_sample_size=min_sample_size,
        limit=limit,
    )
    return {"items": [row.model_dump(mode="json") for row in rows], "count": len(rows)}


@app.post("/api/carousels/learnings/refresh")
async def refresh_carousel_learnings(vertical: Optional[str] = None) -> Dict[str, Any]:
    """Re-aggregate published results into learnings (idempotent)."""
    rows = await _carousel().refresh_learnings(vertical=vertical)
    return {"items": [row.model_dump(mode="json") for row in rows], "count": len(rows)}


@app.get("/api/carousels/recommendations")
async def list_carousel_recommendations(
    vertical: Optional[str] = None, limit: int = 5
) -> Dict[str, Any]:
    """What the history suggests — empty until a group has enough samples."""
    picks = await _carousel().recommendations(vertical=vertical, limit=limit)
    return {
        "vertical": vertical or "all",
        "items": [
            {
                "scope_type": pick.scope_type,
                "scope_value": pick.scope_value,
                "metric_name": pick.metric_name,
                "metric_value": pick.metric_value,
                "sample_size": pick.sample_size,
                "confidence": pick.confidence,
                "rationale": pick.rationale,
            }
            for pick in picks
        ],
        "count": len(picks),
    }


@app.get("/api/carousels/analytics/summary")
async def carousel_analytics_summary(limit: int = 200) -> Dict[str, Any]:
    """Pipeline health in numbers: what waits, what went out, what was learned."""
    factory = _carousel()
    jobs = await factory.list_jobs(limit=limit)
    by_status: Dict[str, int] = {}
    by_vertical: Dict[str, int] = {}
    for job in jobs:
        status = str(getattr(job.status, "value", job.status))
        vertical = str(getattr(job.vertical, "value", job.vertical))
        by_status[status] = by_status.get(status, 0) + 1
        by_vertical[vertical] = by_vertical.get(vertical, 0) + 1
    learnings = await factory.list_learnings(limit=500)
    return {
        "jobs": {"total": len(jobs), "by_status": by_status, "by_vertical": by_vertical},
        "learnings": {"total": len(learnings)},
        "autonomy": {
            "mode": factory.settings.mode,
            "requires_human_approval": factory.settings.require_human_approval,
            "dry_run": factory.settings.dry_run,
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
