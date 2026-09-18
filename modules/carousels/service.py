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
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from loguru import logger

from core.config import CarouselConfig, Config
from core.database import Database, utcnow
from core.exceptions import CarouselError, NotFoundError, SourceResolutionError
from core.models import (
    CarouselBundle,
    CarouselHookCandidate,
    CarouselJob,
    CarouselLearning,
    CarouselPublication,
    CarouselSlide,
    CarouselSourceContext,
    CarouselVerificationStatus,
    CarouselSourceRecord,
    CarouselStatus,
    CarouselVertical,
)

from . import database_helpers as repo
from .enums import CarouselSourceType, enum_text, resolve_source_type
from .hooks import HookEngine
from .narrative import SlidePlanner, clip
from .render import (
    BaseSlideRenderer,
    PillowSlideRenderer,
    RenderedSlide,
    SlideVerifier,
    VerificationReport,
    provider_for,
)
from .vertical_profiles import profile_for
from .sources import (
    BaseSourceResolver,
    ResolveOptions,
    merge_warnings,
    source_resolver_for,
)
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

#: Warning a fresh job carries until research has actually read the source.
NOT_RESOLVED_WARNING = "source not resolved yet"
#: Added when the source did not carry enough verified facts.
LOW_CONFIDENCE_WARNING = "low confidence source: manual confirmation required"


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
        report["warnings"] = list(job.warnings)
        report["needs_manual_input"] = bool(job.warnings)
        return report

    # ------------------------------------------------------------------
    # research (Phase 2)
    # ------------------------------------------------------------------

    async def research(
        self,
        job_id: int,
        *,
        resolver: Optional[BaseSourceResolver] = None,
        options: Optional[ResolveOptions] = None,
        detect_vertical: bool = True,
    ) -> CarouselJob:
        """Read the job's source into an audited :class:`CarouselSourceContext`.

        The source is fetched by a resolver (URL / GitHub / mock for dry runs).
        Nothing is invented: an unreadable source raises
        :class:`SourceResolutionError` and the job is marked ``failed`` with the
        reason; the detected vertical only overrides the configured default
        (an explicit user choice always wins).
        """
        job = await self.get_job(job_id)
        await self.transition(job_id, CarouselStatus.RESEARCHING)

        active = resolver or source_resolver_for(
            job.source_type, self.settings, dry_run=job.dry_run
        )
        rows = await repo.list_sources(self.db, job_id=job_id)
        row = rows[0] if rows else None
        source_ref = (row.source_ref if row else "") or job.source_url or job.title

        try:
            context = await active.resolve(source_ref)
        except SourceResolutionError as exc:
            logger.error("carousel job {}: source resolution failed: {}", job_id, exc)
            await repo.fail_job(self.db, job_id, str(exc))
            raise
        logger.info(
            "carousel job {}: resolved {} via {} (confidence {})",
            job_id,
            source_ref,
            active.name,
            context.confidence,
        )

        # A resolver that only warns (e.g. GraphQL discussion without a token)
        # is still a resolved job — the warning travels with it.
        warnings = merge_warnings(
            [w for w in job.warnings if w != NOT_RESOLVED_WARNING], context.warnings
        )
        if context.is_low_confidence:
            warnings = merge_warnings(warnings, [LOW_CONFIDENCE_WARNING])

        vertical = job.vertical
        if (
            detect_vertical
            and job.vertical is CarouselVertical.HYBRID
            and context.vertical is not CarouselVertical.HYBRID
        ):
            vertical = context.vertical

        fields: Dict[str, Any] = {
            "source_context_json": context.model_dump_json(),
            "confidence": float(context.confidence),
            "warnings_json": json.dumps(warnings, ensure_ascii=False),
            "vertical": enum_text(vertical),
            "source_type": enum_text(context.source_type),
        }
        if not job.title and context.title:
            fields["title"] = context.title
        if not job.source_url and (context.canonical_url or source_ref):
            fields["source_url"] = context.canonical_url or source_ref
        await self.update_job(job_id, **fields)

        canonical_url = context.canonical_url or (row.source_ref if row else "") or source_ref
        audit = {
            "source_type": enum_text(context.source_type),
            "vertical": enum_text(vertical),
            "canonical_url": canonical_url,
            "external_id": context.external_id,
            "title": context.title,
            "content_type": context.content_type,
            "confidence": float(context.confidence),
            "warnings_json": json.dumps(list(context.warnings), ensure_ascii=False),
            "context_json": context.model_dump_json(),
            "raw_payload_json": context.raw_payload_json or "{}",
            "resolver": active.name,
        }
        if row is not None:
            await repo.update_source(self.db, row.id, **audit)
        else:
            await repo.add_source(
                self.db,
                CarouselSourceRecord(
                    job_id=job_id,
                    source_ref=source_ref,
                    **audit,
                ),
            )

        updated = await self.transition(job_id, CarouselStatus.RESEARCHED)
        logger.info("carousel job {}: research stored ({} facts)", job_id, len(context.facts))
        return updated

    # ------------------------------------------------------------------
    # narrative + slide planning (Phase 3)
    # ------------------------------------------------------------------

    async def draft_narrative(
        self, job_id: int, *, limit: int = 5, engine: Optional[HookEngine] = None
    ) -> CarouselJob:
        """Propose ranked, source-backed hooks for a researched job.

        In supervised mode these are *candidates*: a human picks one (or the
        highest-scoring one is used when nobody cares). An empty source yields
        zero hooks and a warning — never filler.
        """
        job = await self.get_job(job_id)
        context = job.source_context()
        if context is None:
            raise CarouselError("job has no source context — run research() first")

        candidates = (engine or HookEngine()).generate(
            context, vertical=job.vertical, limit=limit
        )
        await repo.save_hook_candidates(self.db, job_id, candidates)

        warnings = list(job.warnings)
        if not candidates:
            warnings = merge_warnings(
                warnings, ["no hook candidates: the source matched none of the patterns"]
            )
        fields: Dict[str, Any] = {"warnings_json": json.dumps(warnings, ensure_ascii=False)}
        if candidates:
            fields["logline"] = clip(candidates[0].source_support, 120)
        await self.update_job(job_id, **fields)

        updated = await self.transition(job_id, CarouselStatus.NARRATIVE_DRAFTED)
        logger.info("carousel job {}: {} hook candidates", job_id, len(candidates))
        return updated

    async def plan_slides(
        self,
        job_id: int,
        *,
        hook_id: Optional[int] = None,
        planner: Optional[SlidePlanner] = None,
        slide_count: Optional[int] = None,
    ) -> CarouselBundle:
        """Turn the source context into the six-slide plan and persist it."""
        job = await self.get_job(job_id)
        context = job.source_context()
        if context is None:
            raise CarouselError("job has no source context — run research() first")

        candidates = await repo.get_hook_candidates(self.db, job_id)
        hook = self._choose_hook(job, candidates, hook_id)
        plan = (planner or SlidePlanner()).plan(
            context,
            vertical=job.vertical,
            hook=hook,
            job_id=job_id,
            slide_count=slide_count,
        )
        slides = await repo.save_carousel_slides(self.db, job_id, plan.slides)

        if hook is not None and hook.id is not None:
            await repo.select_hook_candidate(self.db, job_id, hook.id)

        warnings = merge_warnings(list(job.warnings), plan.warnings)
        await self.update_job(
            job_id,
            selected_hook_id=hook.id if hook is not None else job.selected_hook_id,
            logline=plan.logline or job.logline,
            caption=plan.caption or job.caption,
            hashtags_json=json.dumps(list(plan.hashtags), ensure_ascii=False),
            warnings_json=json.dumps(warnings, ensure_ascii=False),
        )
        await self.transition(job_id, CarouselStatus.SLIDES_PLANNED)
        bundle = await self.get_bundle(job_id)
        logger.info("carousel job {}: planned {} slides", job_id, len(slides))
        return bundle

    @staticmethod
    def _choose_hook(
        job: CarouselJob,
        candidates: Sequence[CarouselHookCandidate],
        hook_id: Optional[int],
    ) -> Optional[CarouselHookCandidate]:
        """Explicit pick > previous pick > highest score (never invented)."""
        if hook_id is not None:
            for candidate in candidates:
                if candidate.id == hook_id:
                    return candidate
            raise NotFoundError(f"hook candidate {hook_id} not found for job {job.id}")
        if job.selected_hook_id is not None:
            for candidate in candidates:
                if candidate.id == job.selected_hook_id:
                    return candidate
        return candidates[0] if candidates else None

    # ------------------------------------------------------------------
    # rendering + verification (Phase 4)
    # ------------------------------------------------------------------

    def renderer_for(self) -> BaseSlideRenderer:
        """Renderer named by config (``pillow`` today; never a silent fallback)."""
        name = (self.settings.renderer or "pillow").strip().lower()
        if name == "pillow":
            return PillowSlideRenderer(
                background_provider=provider_for(self.settings.gemini, dry_run=self.settings.dry_run)
            )
        raise CarouselError(f"carousel renderer {name!r} is not implemented (use 'pillow')")

    def verifier(self) -> SlideVerifier:
        """Verifier bound to the configured canvas, safe zone and health gates."""
        return SlideVerifier(
            width=self.settings.resolution.width,
            height=self.settings.resolution.height,
            safe_zone_pixels=self.settings.bottom_safe_zone_pixels,
            require_alt_text=self.settings.health.require_alt_text,
            require_source_refs=self.settings.health.require_source_refs_for_facts,
        )

    def job_output_dir(self, job_id: int) -> Path:
        """Where this job's JPGs live (``carousels.output_dir``/job_<id>)."""
        return Path(self.settings.output_dir) / f"job_{job_id}"

    async def render_slides(
        self, job_id: int, *, renderer: Optional[BaseSlideRenderer] = None
    ) -> CarouselBundle:
        """Paint every planned slide into a 768x1376 JPG (status RENDERED)."""
        job = await self.get_job(job_id)
        slides = await repo.get_carousel_slides(self.db, job_id)
        if not slides:
            raise CarouselError("job has no slides to render — run plan_slides() first")

        profile = profile_for(job.vertical)
        context = job.source_context()
        width = self.settings.resolution.width
        height = self.settings.resolution.height
        safe_zone = self.settings.bottom_safe_zone_pixels
        active = renderer or self.renderer_for()

        await self.transition(job_id, CarouselStatus.RENDERING)
        output_dir = self.job_output_dir(job_id)
        rendered: List[RenderedSlide] = []
        for slide in slides:
            destination = output_dir / f"slide_{slide.order:02d}.jpg"
            result = await active.render_async(
                slide,
                profile=profile,
                width=width,
                height=height,
                safe_zone_pixels=safe_zone,
                destination=destination,
                context=context,
                attempt=slide.regeneration_count,
            )
            await repo.update_carousel_slide(
                self.db,
                slide.id,
                final_image_path=result.path,
                background_image_path=result.background_path,
                verification_status=CarouselVerificationStatus.PENDING.value,
                verification_issues_json=json.dumps(result.warnings, ensure_ascii=False),
            )
            rendered.append(result)

        await self.transition(job_id, CarouselStatus.RENDERED)
        logger.info("carousel job {}: rendered {} slides into {}", job_id, len(rendered), output_dir)
        return await self.get_bundle(job_id)

    async def verify_slides(
        self, job_id: int, *, verifier: Optional[SlideVerifier] = None, renderer: Optional[BaseSlideRenderer] = None
    ) -> List[VerificationReport]:
        """Verify every rendered slide, regenerating failures a bounded number of times."""
        job = await self.get_job(job_id)
        slides = await repo.get_carousel_slides(self.db, job_id)
        if not slides:
            raise CarouselError("job has no slides to verify — run render_slides() first")

        profile = profile_for(job.vertical)
        context = job.source_context()
        active_verifier = verifier or self.verifier()

        await self.transition(job_id, CarouselStatus.VERIFYING)
        reports = await self._verify_pass(active_verifier, slides, context)

        attempts = 0
        while (
            any(not report.passed for report in reports)
            and attempts < self.settings.max_regeneration_attempts
        ):
            attempts += 1
            await self._regenerate(
                job_id, [report.order for report in reports if not report.passed],
                context=context, renderer=renderer or self.renderer_for(),
            )
            slides = await repo.get_carousel_slides(self.db, job_id)
            reports = await self._verify_pass(active_verifier, slides, context)

        failing = [report.order for report in reports if not report.passed]
        if failing:
            warnings = merge_warnings(
                list(job.warnings),
                [f"slides need revision after {attempts} regeneration attempt(s): {failing}"],
            )
            await self.update_job(job_id, warnings_json=json.dumps(warnings, ensure_ascii=False))
            await self.transition(job_id, CarouselStatus.NEEDS_REVISION)
            logger.warning("carousel job {}: slides {} failed verification", job_id, failing)
        else:
            await self.transition(job_id, CarouselStatus.VERIFIED)
            logger.info("carousel job {}: all {} slides verified", job_id, len(reports))
        return reports

    async def _verify_pass(
        self,
        verifier: SlideVerifier,
        slides: Sequence[CarouselSlide],
        context: Optional[CarouselSourceContext],
    ) -> List[VerificationReport]:
        """Verify each slide and persist its per-slide outcome."""
        from .fact_guard import FactGuard

        guard = FactGuard(context) if context is not None else None
        reports: List[VerificationReport] = []
        for slide in slides:
            report = verifier.verify(slide, context=context, guard=guard)
            await repo.update_carousel_slide(
                self.db,
                slide.id,
                verification_status=report.status.value,
                verification_issues_json=json.dumps(report.issues, ensure_ascii=False),
                quality_score=report.quality_score,
            )
            reports.append(report)
        return reports

    async def _regenerate(
        self,
        job_id: int,
        orders: Sequence[int],
        *,
        context: Optional[CarouselSourceContext],
        renderer: BaseSlideRenderer,
    ) -> None:
        """Re-render only the failing slides (targeted, not a full re-run)."""
        job = await self.get_job(job_id)
        profile = profile_for(job.vertical)
        slides = await repo.get_carousel_slides(self.db, job_id)
        width = self.settings.resolution.width
        height = self.settings.resolution.height
        for slide in slides:
            if slide.order not in orders:
                continue
            attempt = slide.regeneration_count + 1
            destination = Path(slide.final_image_path or self.job_output_dir(job_id) / f"slide_{slide.order:02d}.jpg")
            await renderer.render_async(
                slide,
                profile=profile,
                width=width,
                height=height,
                safe_zone_pixels=self.settings.bottom_safe_zone_pixels,
                destination=destination,
                context=context,
                attempt=attempt,
            )
            await repo.update_carousel_slide(
                self.db, slide.id, regeneration_count=attempt, final_image_path=str(destination)
            )

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
