"""Repository helpers for carousel entities.

The invariant of this project is that **all** SQL lives in ``core/database.py``
(the single owner of the connection and of ``_SCHEMA``). This module therefore
holds no SQL: it is a stable, task-oriented façade over the ``Database``
methods, so carousel modules (and the CLI/API) can depend on names from the
specification (``create_carousel_job``, ``save_metric``, ...) instead of the
repository's own naming.

Why a façade and not a second repository class: a second SQL owner would need a
second connection and would silently diverge from the schema/migration story.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from core.database import Database
from core.models import (
    CarouselHookCandidate,
    CarouselJob,
    CarouselLearning,
    CarouselMetric,
    CarouselPublication,
    CarouselSlide,
    CarouselSourceRecord,
    CarouselTemplate,
    QAArtifact,
    VibecodingSession,
)

__all__ = [
    "add_source",
    "carousel_already_published",
    "create_carousel_job",
    "create_qa_artifact",
    "create_vibecoding_session",
    "get_carousel_job",
    "get_carousel_slides",
    "get_hook_candidates",
    "get_learnings",
    "get_publication_by_request_id",
    "list_carousel_jobs",
    "list_carousel_templates",
    "list_metrics",
    "list_publications",
    "list_qa_artifacts",
    "list_sources",
    "list_vibecoding_sessions",
    "save_carousel_slides",
    "save_hook_candidates",
    "save_metric",
    "save_publication",
    "select_hook_candidate",
    "update_carousel_job",
    "update_carousel_job_status",
    "update_carousel_slide",
    "update_publication",
    "upsert_learning",
]


async def create_carousel_job(db: Database, job: CarouselJob) -> CarouselJob:
    """Insert a carousel job (always lands in ``pending``)."""
    return await db.create_carousel_job(job)


async def get_carousel_job(db: Database, job_id: int) -> Optional[CarouselJob]:
    """Fetch one job by id."""
    return await db.get_carousel_job(job_id)


async def list_carousel_jobs(
    db: Database,
    *,
    status: Optional[str] = None,
    vertical: Optional[str] = None,
    source_type: Optional[str] = None,
    created_after: Optional[datetime] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[CarouselJob]:
    """List jobs newest-first with the filters of ``GET /api/carousels``."""
    return await db.list_carousel_jobs(
        status=status,
        vertical=vertical,
        source_type=source_type,
        created_after=created_after,
        limit=limit,
        offset=offset,
    )


async def update_carousel_job(db: Database, job_id: int, **fields: Any) -> Optional[CarouselJob]:
    """Update mutable job fields (status is NOT one of them)."""
    return await db.update_carousel_job(job_id, **fields)


async def update_carousel_job_status(db: Database, job_id: int, status: str) -> Optional[CarouselJob]:
    """Move a job through the state machine (raises StateTransitionError)."""
    return await db.update_carousel_job_status(job_id, status)


async def save_carousel_slides(
    db: Database, job_id: int, slides: Sequence[CarouselSlide], replace: bool = True
) -> List[CarouselSlide]:
    """Persist the slide plan of a job (replace drops the previous rows)."""
    return await db.save_carousel_slides(job_id, list(slides), replace=replace)


async def get_carousel_slides(db: Database, job_id: int) -> List[CarouselSlide]:
    """Slides of a job, ordered by ``order``."""
    return await db.get_carousel_slides(job_id)


async def update_carousel_slide(
    db: Database, slide_id: int, **fields: Any
) -> Optional[CarouselSlide]:
    """Apply a human edit to one slide."""
    return await db.update_carousel_slide(slide_id, **fields)


async def save_hook_candidates(
    db: Database, job_id: int, candidates: Sequence[CarouselHookCandidate], replace: bool = True
) -> List[CarouselHookCandidate]:
    """Persist hook candidates for a job."""
    return await db.save_hook_candidates(job_id, list(candidates), replace=replace)


async def get_hook_candidates(
    db: Database, job_id: int, selected_only: bool = False
) -> List[CarouselHookCandidate]:
    """Hook candidates of a job (highest score first)."""
    return await db.get_hook_candidates(job_id, selected_only=selected_only)


async def select_hook_candidate(
    db: Database, job_id: int, candidate_id: int
) -> Optional[CarouselHookCandidate]:
    """Mark one candidate as chosen and record it on the job."""
    return await db.select_hook_candidate(job_id, candidate_id)


async def add_source(db: Database, record: CarouselSourceRecord) -> CarouselSourceRecord:
    """Append one source-resolution audit row."""
    return await db.add_carousel_source(record)


async def list_sources(
    db: Database, job_id: Optional[int] = None
) -> List[CarouselSourceRecord]:
    """Source audit rows (optionally for one job)."""
    return await db.list_carousel_sources(job_id)


async def save_publication(db: Database, publication: CarouselPublication) -> CarouselPublication:
    """Insert a publication row; idempotent by ``request_id``."""
    return await db.save_carousel_publication(publication)


async def update_publication(
    db: Database, publication_id: int, **fields: Any
) -> Optional[CarouselPublication]:
    """Update a publication row (status/url/error/raw response)."""
    return await db.update_carousel_publication(publication_id, **fields)


async def get_publication_by_request_id(
    db: Database, request_id: str
) -> Optional[CarouselPublication]:
    """Look a publication up by the Upload-Post ``request_id``."""
    return await db.get_publication_by_request_id(request_id)


async def list_publications(
    db: Database, job_id: Optional[int] = None, platform: Optional[str] = None
) -> List[CarouselPublication]:
    """Publications of a job / platform."""
    return await db.list_carousel_publications(job_id=job_id, platform=platform)


async def carousel_already_published(db: Database, job_id: int) -> bool:
    """True when at least one platform already reports ``published``."""
    return await db.carousel_published_count(job_id) > 0


async def save_metric(db: Database, metric: CarouselMetric) -> CarouselMetric:
    """Persist one collected metric datapoint."""
    return await db.save_carousel_metric(metric)


async def list_metrics(
    db: Database,
    *,
    job_id: Optional[int] = None,
    publication_id: Optional[int] = None,
    platform: Optional[str] = None,
    metric_name: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> List[CarouselMetric]:
    """Read collected metrics with the analytics filters."""
    return await db.list_carousel_metrics(
        job_id=job_id,
        publication_id=publication_id,
        platform=platform,
        metric_name=metric_name,
        since=since,
        limit=limit,
    )


async def upsert_learning(db: Database, learning: CarouselLearning) -> CarouselLearning:
    """Insert-or-update an aggregated learning (scope + metric key)."""
    return await db.upsert_carousel_learning(learning)


async def get_learnings(
    db: Database,
    *,
    scope_type: Optional[str] = None,
    scope_value: Optional[str] = None,
    min_sample_size: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[CarouselLearning]:
    """Read aggregated learnings, best metric first."""
    return await db.get_carousel_learnings(
        scope_type=scope_type,
        scope_value=scope_value,
        min_sample_size=min_sample_size,
        limit=limit,
    )


async def create_qa_artifact(db: Database, artifact: QAArtifact) -> QAArtifact:
    """Persist a QA artifact (bug/flaky/CI incident) for carousel sources."""
    return await db.create_qa_artifact(artifact)


async def list_qa_artifacts(
    db: Database,
    *,
    source_type: Optional[str] = None,
    severity: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[QAArtifact]:
    """Read QA artifacts."""
    return await db.list_qa_artifacts(source_type=source_type, severity=severity, limit=limit)


async def create_vibecoding_session(
    db: Database, session: VibecodingSession
) -> VibecodingSession:
    """Persist a vibecoding session description."""
    return await db.create_vibecoding_session(session)


async def list_vibecoding_sessions(
    db: Database, *, limit: Optional[int] = None
) -> List[VibecodingSession]:
    """Read vibecoding sessions (newest first)."""
    return await db.list_vibecoding_sessions(limit=limit)


async def seed_carousel_templates(
    db: Database, templates: Iterable[CarouselTemplate]
) -> List[CarouselTemplate]:
    """Insert templates, ignoring ones that already exist (idempotent)."""
    return await db.seed_carousel_templates(list(templates))


async def list_carousel_templates(
    db: Database, vertical: Optional[str] = None, active_only: bool = True
) -> List[CarouselTemplate]:
    """Read carousel templates (optionally for one vertical)."""
    return await db.list_carousel_templates(vertical=vertical, active_only=active_only)


def repository_methods() -> Dict[str, str]:
    """Map façade helper -> Database method (docs/tests: keeps the two in sync)."""
    return {
        "create_carousel_job": "Database.create_carousel_job",
        "get_carousel_job": "Database.get_carousel_job",
        "list_carousel_jobs": "Database.list_carousel_jobs",
        "update_carousel_job": "Database.update_carousel_job",
        "update_carousel_job_status": "Database.update_carousel_job_status",
        "save_carousel_slides": "Database.save_carousel_slides",
        "get_carousel_slides": "Database.get_carousel_slides",
        "update_carousel_slide": "Database.update_carousel_slide",
        "save_hook_candidates": "Database.save_hook_candidates",
        "get_hook_candidates": "Database.get_hook_candidates",
        "select_hook_candidate": "Database.select_hook_candidate",
        "add_source": "Database.add_carousel_source",
        "list_sources": "Database.list_carousel_sources",
        "save_publication": "Database.save_carousel_publication",
        "update_publication": "Database.update_carousel_publication",
        "get_publication_by_request_id": "Database.get_publication_by_request_id",
        "list_publications": "Database.list_carousel_publications",
        "save_metric": "Database.save_carousel_metric",
        "list_metrics": "Database.list_carousel_metrics",
        "upsert_learning": "Database.upsert_carousel_learning",
        "get_learnings": "Database.get_carousel_learnings",
        "create_qa_artifact": "Database.create_qa_artifact",
        "list_qa_artifacts": "Database.list_qa_artifacts",
        "create_vibecoding_session": "Database.create_vibecoding_session",
        "list_vibecoding_sessions": "Database.list_vibecoding_sessions",
        "seed_carousel_templates": "Database.seed_carousel_templates",
        "list_carousel_templates": "Database.list_carousel_templates",
    }


#: Type alias kept for callers that want to type a façade function.
JobLoader = Union[Database, Any]
