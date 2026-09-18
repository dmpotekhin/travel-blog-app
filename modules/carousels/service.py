"""Carousel Factory service (Phase 1: job lifecycle).

Scope of this phase: create a job, load its bundle, move it through the state
machine, apply human edits. Research, hook generation, slide planning,
rendering, verification and publishing arrive in the following phases and are
wired into :class:`CarouselFactory` then — the class is the seam so the API,
UI, CLI and the scheduler never talk to the repository directly.

Nothing here invents content: a job starts empty (``confidence=0.0``, no
slides) and only gets facts from a resolver.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Union

from loguru import logger

from core.config import CarouselConfig, Config
from core.database import Database, utcnow
from core.exceptions import CarouselError, NotFoundError
from core.models import (
    CarouselBundle,
    CarouselJob,
    CarouselLearning,
    CarouselPublication,
    CarouselSlide,
    CarouselSourceRecord,
    CarouselStatus,
    CarouselVertical,
)

from . import database_helpers as repo
from .enums import CarouselSourceType, resolve_source_type
from .state_machine import (
    can_publish,
    describe,
    plan_next,
    require_publish_allowed,
    should_auto_approve,
    status_value,
    to_autonomy_mode,
)

__all__ = ["CarouselFactory", "looks_like_url"]


def looks_like_url(value: str) -> bool:
    """Cheap check: does this reference look like a fetchable URL?"""
    text = (value or "").strip().lower()
    return text.startswith(("http://", "https://"))


class CarouselFactory:
    """Job lifecycle for the Tri-Face Carousel Factory (Travel/QA/Vibecoding)."""

    def __init__(self, db: Database, config: Optional[Config] = None) -> None:
        self.db = db
        self.config = config or Config()
        self.settings: CarouselConfig = self.config.carousels

    # ------------------------------------------------------------------
    # config accessors
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    @property
    def autonomy_mode(self) -> str:
        """Effective autonomy mode (``supervised`` unless config says otherwise)."""
        return to_autonomy_mode(self.settings.mode).value

    @property
    def require_human_approval(self) -> bool:
        """True unless supervision was explicitly switched off in config."""
        return bool(self.settings.require_human_approval)

    def should_auto_approve(self) -> bool:
        """True only for ``full_autonomous`` *and* supervision switched off.

        Publishing code must consult this instead of trusting a single flag:
        both conditions are required by spec §13 (never autopilot by default).
        """
        return should_auto_approve(
            self.autonomy_mode,
            require_human_approval=self.require_human_approval,
        )

    @property
    def dry_run(self) -> bool:
        return bool(self.settings.dry_run)

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------

    async def create_job(
        self,
        *,
        source_type: Union[str, CarouselSourceType],
        source_ref: str,
        vertical: Optional[Union[str, CarouselVertical]] = None,
        title: str = "",
        created_by: str = "system",
        dry_run: Optional[bool] = None,
        platforms: Optional[Sequence[str]] = None,
    ) -> CarouselJob:
        """Create a ``pending`` carousel job and record its source reference.

        The source is *not* resolved here (Phase 2): the job is created first so
        research failures are visible on a real job row instead of vanishing.
        """
        if not self.enabled:
            raise CarouselError("Carousel Factory is disabled (carousels.enabled = false)")

        source_kind = resolve_source_type(source_type)
        vertical_value = self._resolve_vertical(vertical)
        platforms_value = list(platforms or self.settings.target_platforms)
        # One list feeds both rows: the job and its audit record must never
        # disagree about why the carousel is still unresolved.
        warnings = ["source not resolved yet"] if source_ref else ["no source reference given"]

        job = CarouselJob(
            vertical=vertical_value,
            source_type=source_kind,
            source_url=source_ref if looks_like_url(source_ref) else "",
            title=title,
            status=CarouselStatus.PENDING,
            autonomy_mode=to_autonomy_mode(self.settings.mode),
            target_platforms_json=json.dumps(platforms_value, ensure_ascii=False),
            source_context_json="{}",
            confidence=0.0,
            warnings_json=json.dumps(warnings, ensure_ascii=False),
            dry_run=self.dry_run if dry_run is None else bool(dry_run),
            created_by=created_by,
        )
        job = await repo.create_carousel_job(self.db, job)

        await repo.add_source(
            self.db,
            CarouselSourceRecord(
                job_id=job.id,
                source_type=source_kind,
                vertical=vertical_value,
                source_ref=source_ref,
                canonical_url=job.source_url,
                resolver="",
                confidence=0.0,
                warnings_json=json.dumps(warnings, ensure_ascii=False),
            ),
        )
        logger.info(
            "carousel job created: id={} vertical={} source_type={} dry_run={}",
            job.id,
            status_value(vertical_value),
            status_value(source_kind),
            job.dry_run,
        )
        return job

    async def get_job(self, job_id: int) -> CarouselJob:
        """Fetch a job or raise :class:`NotFoundError`."""
        job = await repo.get_carousel_job(self.db, job_id)
        if job is None:
            raise NotFoundError(f"Carousel job {job_id} not found")
        return job

    async def get_bundle(self, job_id: int) -> CarouselBundle:
        """Job + slides + hooks + publications (+ flattened verification issues)."""
        job = await self.get_job(job_id)
        slides = await repo.get_carousel_slides(self.db, job_id)
        hooks = await repo.get_hook_candidates(self.db, job_id)
        publications = await repo.list_publications(self.db, job_id=job_id)
        issues: List[str] = []
        for slide in slides:
            for issue in slide.verification_issues:
                issues.append(f"slide {slide.order}: {issue}")
        issues.extend(job.warnings)
        return CarouselBundle(
            job=job,
            slides=slides,
            hooks=hooks,
            publications=publications,
            issues=issues,
        )

    async def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        vertical: Optional[str] = None,
        source_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[CarouselJob]:
        """List jobs newest-first (filters mirror ``GET /api/carousels``)."""
        return await repo.list_carousel_jobs(
            self.db,
            status=status_value(status) if status else None,
            vertical=status_value(vertical) if vertical else None,
            source_type=status_value(source_type) if source_type else None,
            limit=limit,
            offset=offset,
        )

    async def update_job(self, job_id: int, **fields: Any) -> CarouselJob:
        """Update mutable job fields (``status`` goes through :meth:`transition`)."""
        updated = await repo.update_carousel_job(self.db, job_id, **fields)
        if updated is None:
            raise NotFoundError(f"Carousel job {job_id} not found")
        return updated

    async def transition(self, job_id: int, target: Union[str, CarouselStatus]) -> CarouselJob:
        """Move a job to ``target``; illegal moves raise ``StateTransitionError``."""
        updated = await repo.update_carousel_job_status(self.db, job_id, status_value(target))
        if updated is None:
            raise NotFoundError(f"Carousel job {job_id} not found")
        logger.info("carousel job {} -> {}", job_id, status_value(target))
        return updated

    async def status_report(self, job_id: int) -> Dict[str, object]:
        """Where the job stands, what it allows, what the next path step is."""
        job = await self.get_job(job_id)
        report = describe(job)
        report["next"] = plan_next(job)
        report["error_message"] = job.error_message
        return report

    # ------------------------------------------------------------------
    # slides
    # ------------------------------------------------------------------

    async def save_slides(
        self, job_id: int, slides: Sequence[CarouselSlide], replace: bool = True
    ) -> List[CarouselSlide]:
        """Persist a slide plan for a job."""
        await self.get_job(job_id)
        stored = await repo.save_carousel_slides(self.db, job_id, slides, replace=replace)
        logger.info("carousel job {}: saved {} slides", job_id, len(stored))
        return stored

    async def edit_slide(self, slide_id: int, **fields: Any) -> CarouselSlide:
        """Apply a human edit (Slide Editor) to one slide."""
        updated = await repo.update_carousel_slide(self.db, slide_id, **fields)
        if updated is None:
            raise NotFoundError(f"Carousel slide {slide_id} not found")
        return updated

    # ------------------------------------------------------------------
    # approval gate (shared by API, UI and CLI)
    # ------------------------------------------------------------------

    async def approve(self, job_id: int, *, approved_by: str = "human") -> CarouselJob:
        """Human approval (or an explicit ``full_autonomous`` auto-approval).

        The transition is the record of approval; ``approved_by``/``approved_at``
        are the audit trail the UI shows in the Approval Queue.
        """
        await self.get_job(job_id)  # 404/NotFound before the audit write
        await self.update_job(job_id, approved_by=approved_by, approved_at=utcnow())
        updated = await self.transition(job_id, CarouselStatus.APPROVED)
        logger.info("carousel job {} approved by {}", updated.id, approved_by)
        return updated

    async def reject(
        self, job_id: int, *, reason: str = "", rejected_by: str = "human"
    ) -> CarouselJob:
        """Send a job back for revision (``needs_revision``) with a reason."""
        await self.get_job(job_id)
        updates: Dict[str, Any] = {}
        if reason:
            updates["error_message"] = reason
        if updates:
            await self.update_job(job_id, **updates)
        updated = await self.transition(job_id, CarouselStatus.NEEDS_REVISION)
        logger.info("carousel job {} rejected by {}: {}", job_id, rejected_by, reason or "-")
        return updated

    async def ensure_publishable(self, job_id: int) -> CarouselJob:
        """Guard for the publisher: raises unless the job is ``approved``."""
        job = await self.get_job(job_id)
        require_publish_allowed(job)
        return job

    async def publishable_count(self, job_id: int) -> bool:
        """True when the job may be published right now (no exception raised)."""
        job = await self.get_job(job_id)
        return can_publish(job)

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _resolve_vertical(
        self, vertical: Optional[Union[str, CarouselVertical]]
    ) -> CarouselVertical:
        """User override wins; ``None``/``auto`` falls back to config default.

        Phase 2 will auto-detect a vertical from the source; until then the
        configured default (``hybrid``) is the honest answer.
        """
        if isinstance(vertical, CarouselVertical):
            return vertical
        value = str(vertical or "").strip().lower()
        if not value or value == "auto":
            return CarouselVertical(self.settings.default_vertical)
        try:
            return CarouselVertical(value)
        except ValueError as exc:
            raise CarouselError(
                f"Unknown vertical {vertical!r}; expected one of "
                f"{[v.value for v in CarouselVertical]}"
            ) from exc
