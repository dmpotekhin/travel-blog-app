"""Visual Narrative Studio (ADR-106): the visual layer between AI photo analysis
and platform content.

    PHOTO SELECTION
        v
    AI IMAGE ANALYSIS
        v
    VISUAL NARRATIVE STUDIO   <-- this module
        v
    BASE TRAVEL STORY
        v
    PLATFORM CONTENT

Input: the photographs of one city, whatever the AI already knows about them
(cached analyses, or analyses passed in by the pipeline) and, when it exists,
the base story. Output: a :class:`core.models.VisualNarrativePlan` — a narrative
arc (beats), a storyboard (ordered shots with captions, alt-texts, crop/focus
hints and visual rhythm) plus accessibility and cultural-sensitivity notes —
persisted as a versioned :class:`core.models.Storyboard`.

Design rules:

* every external service stays behind its ABC: all AI work goes through
  :class:`modules.ai.base.BaseAIProvider`, never Gemini/DeepSeek directly;
* the step is additive and idempotent: a disabled feature, a city without photos
  or a failing provider degrade the narrative (explicitly marked ``degraded``)
  but never break the city pipeline;
* pure narrative logic lives in :mod:`modules.narrative_heuristics`, prompt text
  in :mod:`modules.narrative_prompts`, SQL in :mod:`core.database`, so nothing is
  duplicated between the API, the UI and the pipeline.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from loguru import logger
from pydantic import BaseModel, Field

from core import models as m
from core.config import Config, VisualNarrativeConfig
from core.database import Database
from core.exceptions import AIProviderError, NotFoundError, StoryboardValidationError
from modules import narrative_heuristics as heuristics
from modules import narrative_prompts
from modules.ai.base import BaseAIProvider, ImageAnalysis
from modules.ai.registry import build_provider
from modules.content import prompts as content_prompts

#: Fallback when neither the settings nor the caller specify a budget.
DEFAULT_MAX_PHOTOS = 12


# --------------------------------------------------------------------------
# Transport-agnostic patch objects (shared by the admin API and the Streamlit UI)
# --------------------------------------------------------------------------


class StoryboardPatch(BaseModel):
    """Partial update of a storyboard header; ``None`` means "leave as is"."""

    title: Optional[str] = None
    logline: Optional[str] = None
    narrative_arc: Optional[str] = None
    emotional_journey: Optional[str] = None
    primary_theme: Optional[str] = None
    secondary_themes: Optional[List[str]] = None
    target_platforms: Optional[List[str]] = None


class ShotPatch(BaseModel):
    """Partial update of a single shot."""

    shot_id: int
    caption: Optional[str] = None
    alt_text: Optional[str] = None
    crop_recommendation: Optional[str] = None
    focus_point: Optional[str] = None
    visual_metaphor: Optional[str] = None
    pacing_weight: Optional[float] = None
    is_hero_image: Optional[bool] = None


class StoryboardUpdateRequest(BaseModel):
    """One "save" from a human surface: header + shots + explicit shot order."""

    storyboard: StoryboardPatch = Field(default_factory=StoryboardPatch)
    shots: List[ShotPatch] = Field(default_factory=list)
    shot_order: List[int] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    """Approval payload; ``force`` needs ``allow_manual_override`` in the config."""

    force: bool = False
    actor: str = "human"


# --------------------------------------------------------------------------
# Pure validation (no I/O) — used by the API, the UI and the pipeline gate
# --------------------------------------------------------------------------


def storyboard_issues(
    storyboard: m.Storyboard,
    beats: Sequence[m.NarrativeBeat],
    shots: Sequence[m.StoryboardShot],
    *,
    require_alt_text: bool = True,
    enforce_narrative_arc: bool = True,
) -> List[str]:
    """Return the reasons this storyboard is not publishable yet (empty = fine).

    Every item is ``code: human-readable detail`` so tests can assert on the code
    while the UI can show the detail as-is.
    """
    issues: List[str] = []

    if not shots:
        issues.append("no_shots: в раскадровке нет ни одного кадра")

    orders = [shot.order for shot in shots]
    if len(set(orders)) != len(orders):
        issues.append("shot_order_duplicated: у кадров совпадают номера порядка")

    paths = [shot.photo_path for shot in shots]
    if len(set(paths)) != len(paths):
        issues.append("duplicate_photos: одна фотография попала в раскадровку дважды")

    heroes = [shot for shot in shots if shot.is_hero_image]
    if len(heroes) > 1:
        issues.append("multiple_hero_images: hero image должен быть только один")

    if not storyboard.title.strip():
        issues.append("missing_title: у визуальной истории нет заголовка")
    if not storyboard.logline.strip():
        issues.append("missing_logline: логлайн не заполнен")

    if require_alt_text:
        for shot in shots:
            for problem in heuristics.alt_text_issues(shot.alt_text, shot.photo_path):
                issues.append(f"{problem}: {shot.photo_path}")

    if enforce_narrative_arc:
        present = {beat.beat_type.value for beat in beats}
        for required in heuristics.REQUIRED_BEATS:
            if required not in present:
                issues.append(f"missing_beat:{required}")

    return issues


def _photo_sort_key(photo: m.Photo) -> str:
    """ISO strings sort correctly and never mix naive/aware datetimes."""
    return str(photo.taken_at or photo.modified_at or "")


def _iso_str(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _narrative_photo(photo: m.Photo, analysis: Optional[ImageAnalysis]) -> m.NarrativePhoto:
    """Reduce a Photo + its optional analysis to the facts the narrative may use."""
    return m.NarrativePhoto(
        path=photo.path,
        filename=photo.filename or os.path.basename(photo.path),
        scene=analysis.scene if analysis else "",
        objects=list(analysis.objects) if analysis else [],
        mood=analysis.mood if analysis else "",
        text=analysis.text if analysis else "",
        quality_ok=bool(analysis.quality_ok) if analysis else True,
        taken_at=_iso_str(photo.taken_at),
    )


class VisualNarrativeStudio:
    """Builds, stores, edits and approves the visual narrative of a city."""

    def __init__(
        self,
        db: Database,
        config: Config,
        provider: Optional[BaseAIProvider] = None,
        *,
        settings: Optional[VisualNarrativeConfig] = None,
        max_photos: Optional[int] = None,
        dry_run: Optional[bool] = None,
    ) -> None:
        self.db = db
        self.config = config
        self.settings = settings or getattr(config, "visual_narrative", None) or VisualNarrativeConfig()
        self.max_photos = int(max_photos or self.settings.max_photos or DEFAULT_MAX_PHOTOS)
        # dry_run follows the application flag unless the caller overrides it.
        self.dry_run = bool(config.app.dry_run if dry_run is None else dry_run)
        self._provider = provider

    # -- provider ---------------------------------------------------------

    @property
    def provider_name(self) -> str:
        """``visual_narrative.provider``; empty means "follow ai.provider"."""
        configured = (self.settings.provider or "").strip()
        return configured or (self.config.ai.provider or "").strip() or "mock"

    @property
    def provider(self) -> BaseAIProvider:
        """Lazily built, so constructing the studio never needs credentials."""
        if self._provider is None:
            self._provider = build_provider(self.db, self.config, self.provider_name)
        return self._provider

    # -- reading the inputs ----------------------------------------------

    async def load_photos(self, city_id: int) -> List[m.Photo]:
        """Usable photographs of the city, newest first, capped by ``max_photos``."""
        photos = await self.db.get_photos_by_city(city_id)
        usable = [photo for photo in photos if photo.scan_status == m.ScanStatus.SCANNED]
        usable.sort(key=_photo_sort_key, reverse=True)
        return usable[: self.max_photos]

    async def load_analyses(
        self,
        photos: Sequence[m.Photo],
        analyses: Optional[Dict[str, ImageAnalysis]] = None,
        *,
        parallel: int = 3,
    ) -> Dict[str, ImageAnalysis]:
        """Analyses for the given photos: caller-provided first, cache second.

        ``BaseAIProvider.analyze_image`` is cached by image hash + prompt, so
        photos the pipeline already analysed are served from ``ai_cache`` without
        spending quota — the prompt is deliberately the pipeline's own.
        """
        known: Dict[str, ImageAnalysis] = dict(analyses or {})
        missing = [photo for photo in photos if photo.path not in known]
        if not missing:
            return known
        narrative_photos = [_narrative_photo(photo, None) for photo in missing]
        fresh = await self.provider.analyze_photo_sequence(
            narrative_photos, content_prompts.ANALYSIS_PROMPT, parallel=parallel
        )
        known.update(fresh)
        if len(fresh) < len(missing):
            logger.warning(
                "Visual narrative: {} of {} photos have no analysis; "
                "the plan will be built from partial facts",
                len(missing) - len(fresh),
                len(missing),
            )
        return known

    async def load_base_story(self, city_id: int) -> str:
        """Longest stored draft text as the "already generated" base story.

        The base story is not persisted as its own row (only the per-platform
        drafts are), so the richest draft is reused as narrative context; if no
        draft exists the studio simply works without it.
        """
        drafts = await self.db.get_drafts(city_id=city_id)
        bodies = [draft.content.strip() for draft in drafts if draft.content and draft.content.strip()]
        return max(bodies, key=len) if bodies else ""

    async def load_context(
        self, city_id: int, *, analyses: Optional[Dict[str, ImageAnalysis]] = None
    ) -> m.NarrativeContext:
        """Collect everything the narrative is allowed to know about the city."""
        city = await self.db.get_city(city_id)
        if city is None:
            raise NotFoundError(f"City {city_id} not found")

        photos = await self.load_photos(city_id)
        found = await self.load_analyses(photos, analyses)
        return m.NarrativeContext(
            city_id=city_id,
            city=city.name,
            country=city.country or "",
            year=city.year,
            language="ru",
            photos=[_narrative_photo(photo, found.get(photo.path)) for photo in photos],
            base_story=await self.load_base_story(city_id),
            target_platforms=self._target_platforms(),
        )

    def _target_platforms(self) -> List[str]:
        publishing = getattr(self.config, "publishing", None)
        if publishing is None:
            return []
        return [platform.value for platform in m.Platform if getattr(publishing, platform.value, False)]

    # -- planning ---------------------------------------------------------

    async def build_plan(
        self, city_id: int, *, analyses: Optional[Dict[str, ImageAnalysis]] = None
    ) -> m.VisualNarrativePlan:
        """Produce a plan without touching the database (dry-run / preview path)."""
        context = await self.load_context(city_id, analyses=analyses)
        plan = await self._ask_provider(context)
        plan = heuristics.ensure_arc(plan, context.photos)
        plan.city_id = city_id
        plan.dry_run = self.dry_run
        if not context.photos:
            plan.degraded = True
            plan.degradation_reason = "city has no analysed photos"
            plan.dry_run = True  # nothing to storyboard: never persist an empty plan
            logger.warning("Visual narrative: city {} has no usable photos", city_id)
        return plan

    async def _ask_provider(self, context: m.NarrativeContext) -> m.VisualNarrativePlan:
        """Ask the provider, falling back to the local builder instead of failing."""
        try:
            plan = await self.provider.generate_visual_narrative_plan(
                context, max_photos=self.max_photos
            )
        except (AIProviderError, ValueError) as exc:
            logger.warning(
                "Visual narrative provider {} failed for city {}: {}",
                self.provider_name,
                context.city_id,
                exc,
            )
            return heuristics.local_plan(
                context,
                provider="local_fallback",
                degraded=True,
                degradation_reason=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - the pipeline must survive any provider bug
            logger.exception(
                "Visual narrative provider {} crashed for city {}", self.provider_name, context.city_id
            )
            return heuristics.local_plan(
                context,
                provider="local_fallback",
                degraded=True,
                degradation_reason=f"{type(exc).__name__}: {exc}",
            )
        if not plan.provider:
            plan.provider = self.provider_name
        return plan

    async def generate(
        self,
        city_id: int,
        *,
        persist: bool = True,
        dry_run: Optional[bool] = None,
        analyses: Optional[Dict[str, ImageAnalysis]] = None,
    ) -> m.VisualNarrativeResult:
        """Full studio run: build the plan and (unless dry-run) store it.

        ``dry_run`` is also forced by ``app.dry_run`` and by a plan that carries no
        shots; in both cases the caller gets the typed result to preview and the
        database is left untouched.
        """
        effective_dry_run = self.dry_run if dry_run is None else bool(dry_run)
        previous_dry_run, self.dry_run = self.dry_run, effective_dry_run
        try:
            plan = await self.build_plan(city_id, analyses=analyses)
        finally:
            self.dry_run = previous_dry_run

        warnings: List[str] = []
        if plan.degraded:
            warnings.append(f"plan built without the AI provider: {plan.degradation_reason}")
        if effective_dry_run:
            logger.info("Visual narrative: dry-run for city {} (nothing persisted)", city_id)

        should_persist = persist and not effective_dry_run and bool(plan.shots)
        storyboard = (
            await self.save_plan(city_id, plan)
            if should_persist
            else self._storyboard_preview(city_id, plan)
        )
        return m.VisualNarrativeResult(
            city_id=city_id,
            storyboard=storyboard,
            plan=plan,
            created=should_persist,
            warnings=warnings,
            dry_run=effective_dry_run or not plan.shots,
        )

    def _storyboard_preview(self, city_id: int, plan: m.VisualNarrativePlan) -> m.Storyboard:
        """Unsaved storyboard-shaped view of a plan (for dry-run responses)."""
        return m.Storyboard(
            id=None,
            city_id=city_id,
            title=plan.title,
            logline=plan.logline,
            narrative_arc=plan.narrative_arc,
            emotional_journey=plan.emotional_journey,
            primary_theme=plan.primary_theme,
            secondary_themes_json=m.dump_json_list(plan.secondary_themes),
            target_platforms_json=m.dump_json_list(self._target_platforms()),
            accessibility_notes_json=m.dump_json_list(plan.accessibility_notes),
            cultural_sensitivity_notes_json=m.dump_json_list(plan.cultural_sensitivity_notes),
            status=m.StoryboardStatus.DRAFT,
            version=0,
        )

    async def save_plan(self, city_id: int, plan: m.VisualNarrativePlan) -> m.Storyboard:
        """Persist a plan as a new storyboard version, beats and shots included."""
        beats = [
            beat.model_copy(update={"storyboard_id": None, "order": index})
            for index, beat in enumerate(
                sorted(plan.beats, key=lambda beat: (heuristics.beat_rank(beat.beat_type.value), beat.order))
            )
        ]
        shots = [
            shot.model_copy(
                update={"storyboard_id": None, "order": index, "is_hero_image": shot.is_hero_image}
            )
            for index, shot in enumerate(plan.shots)
        ]
        storyboard = m.Storyboard(
            city_id=city_id,
            title=plan.title,
            logline=plan.logline,
            narrative_arc=plan.narrative_arc,
            emotional_journey=plan.emotional_journey,
            primary_theme=plan.primary_theme,
            secondary_themes_json=m.dump_json_list(plan.secondary_themes),
            target_platforms_json=m.dump_json_list(self._target_platforms()),
            accessibility_notes_json=m.dump_json_list(
                plan.accessibility_notes or heuristics.default_accessibility_notes(len(shots))
            ),
            cultural_sensitivity_notes_json=m.dump_json_list(
                plan.cultural_sensitivity_notes or heuristics.default_cultural_notes()
            ),
            status=m.StoryboardStatus.DRAFT,
            version=await self.db.next_storyboard_version(city_id),
        )
        saved = await self.db.add_storyboard_graph(storyboard, beats, shots)
        logger.info(
            "Visual narrative: storyboard v{} for city {} ({} beats, {} shots, provider={})",
            saved.version,
            city_id,
            len(beats),
            len(shots),
            plan.provider or self.provider_name,
        )
        return saved

    # -- reading it back --------------------------------------------------

    async def get_storyboard(self, storyboard_id: int) -> m.Storyboard:
        storyboard = await self.db.get_storyboard(storyboard_id)
        if storyboard is None:
            raise NotFoundError(f"Storyboard {storyboard_id} not found")
        return storyboard

    async def latest_storyboard(
        self, city_id: int, *, status: Optional[str] = None
    ) -> Optional[m.Storyboard]:
        return await self.db.get_city_storyboard(city_id, status=status)

    async def bundle(self, storyboard: m.Storyboard) -> m.StoryboardBundle:
        """Read model with beats, shots and the current validation issues."""
        beats = await self.db.get_beats(storyboard.id or 0)
        shots = await self.db.get_shots(storyboard.id or 0)
        return m.StoryboardBundle(
            storyboard=storyboard,
            beats=beats,
            shots=shots,
            issues=storyboard_issues(
                storyboard,
                beats,
                shots,
                require_alt_text=self.settings.require_alt_text,
                enforce_narrative_arc=self.settings.enforce_narrative_arc,
            ),
        )

    async def bundle_for_id(self, storyboard_id: int) -> m.StoryboardBundle:
        return await self.bundle(await self.get_storyboard(storyboard_id))

    async def bundle_for_city(self, city_id: int) -> m.StoryboardBundle:
        storyboard = await self.latest_storyboard(city_id)
        if storyboard is None:
            raise NotFoundError(f"City {city_id} has no storyboard yet")
        return await self.bundle(storyboard)

    # -- editing ----------------------------------------------------------

    async def update_storyboard(
        self, storyboard_id: int, patch: StoryboardPatch
    ) -> m.StoryboardBundle:
        """Apply a header patch; editing an approved storyboard re-opens it.

        Re-opening is deliberate: the approved text must always describe the
        storyboard as it is stored, so an edit demotes it to ``draft`` and a human
        has to approve again.
        """
        storyboard = await self.get_storyboard(storyboard_id)
        fields: Dict[str, object] = {}
        for name in ("title", "logline", "narrative_arc", "emotional_journey", "primary_theme"):
            value = getattr(patch, name)
            if value is not None:
                fields[name] = value
        if patch.secondary_themes is not None:
            fields["secondary_themes_json"] = m.dump_json_list(patch.secondary_themes)
        if patch.target_platforms is not None:
            fields["target_platforms_json"] = m.dump_json_list(patch.target_platforms)
        if fields:
            await self.db.update_storyboard(storyboard_id, **fields)
        updated = await self.get_storyboard(storyboard_id)
        if fields and updated.status is not m.StoryboardStatus.DRAFT:
            await self.db.update_storyboard_status(storyboard_id, m.StoryboardStatus.DRAFT.value)
            logger.info(
                "Visual narrative: storyboard {} demoted to draft after a manual edit", storyboard_id
            )
        return await self.bundle_for_id(storyboard_id)

    async def update_shot(self, shot_id: int, patch: ShotPatch) -> m.StoryboardShot:
        existing = await self.db.get_shot(shot_id)
        if existing is None:
            raise NotFoundError(f"Storyboard shot {shot_id} not found")
        fields: Dict[str, object] = {}
        for name in ("caption", "alt_text", "crop_recommendation", "focus_point", "visual_metaphor"):
            value = getattr(patch, name)
            if value is not None:
                fields[name] = value
        if patch.pacing_weight is not None:
            fields["pacing_weight"] = float(patch.pacing_weight)
        if patch.is_hero_image is not None:
            fields["is_hero_image"] = 1 if patch.is_hero_image else 0
        if fields:
            await self.db.update_shot(shot_id, **fields)
        shot = await self.db.get_shot(shot_id)
        if shot is None:  # pragma: no cover - row existed a moment ago
            raise NotFoundError(f"Storyboard shot {shot_id} disappeared during update")
        if shot.is_hero_image:
            await self._clear_other_heroes(shot.storyboard_id or 0, keep_shot_id=shot_id)
            shot = await self.db.get_shot(shot_id) or shot
        return shot

    async def _clear_other_heroes(self, storyboard_id: int, *, keep_shot_id: int) -> None:
        for shot in await self.db.get_shots(storyboard_id):
            if shot.is_hero_image and shot.id != keep_shot_id:
                await self.db.update_shot(shot.id or 0, is_hero_image=0)

    async def set_hero_image(self, shot_id: int) -> m.StoryboardShot:
        """Exactly one hero image per storyboard (the invariant the studio keeps)."""
        return await self.update_shot(shot_id, ShotPatch(shot_id=shot_id, is_hero_image=True))

    async def reorder_shots(
        self, storyboard_id: int, ordered_shot_ids: Sequence[int]
    ) -> List[m.StoryboardShot]:
        """Reorder the storyboard; the given ids must be exactly its shots."""
        current = await self.db.get_shots(storyboard_id)
        current_ids = {shot.id for shot in current}
        if set(ordered_shot_ids) != current_ids or len(ordered_shot_ids) != len(current):
            raise StoryboardValidationError(
                "shot_order must list every shot of this storyboard exactly once",
                issues=["shot_order_mismatch"],
            )
        return await self.db.replace_shot_order(storyboard_id, list(ordered_shot_ids))

    async def apply_update(self, city_id: int, request: StoryboardUpdateRequest) -> m.StoryboardBundle:
        """One call that the API and the UI both use for "save my edits"."""
        storyboard = await self.latest_storyboard(city_id)
        if storyboard is None:
            raise NotFoundError(f"City {city_id} has no storyboard yet")
        for shot_patch in request.shots:
            await self.update_shot(shot_patch.shot_id, shot_patch)
        if request.shot_order:
            await self.reorder_shots(storyboard.id or 0, request.shot_order)
        if request.storyboard.model_dump(exclude_none=True):
            return await self.update_storyboard(storyboard.id or 0, request.storyboard)
        return await self.bundle_for_id(storyboard.id or 0)

    # -- approval ---------------------------------------------------------

    async def approve(
        self, storyboard_id: int, *, force: bool = False, actor: str = "human"
    ) -> m.StoryboardBundle:
        """Approve a storyboard, refusing while the validity issues stand.

        ``force`` is honoured only when ``visual_narrative.allow_manual_override``
        is true, and the bypass is logged: a human may ship with known issues, but
        never silently.
        """
        storyboard = await self.get_storyboard(storyboard_id)
        bundle = await self.bundle(storyboard)
        if bundle.issues:
            if not force or not self.settings.allow_manual_override:
                raise StoryboardValidationError(
                    "storyboard has validation issues: " + "; ".join(bundle.issues),
                    issues=bundle.issues,
                )
            logger.warning(
                "Visual narrative: storyboard {} approved by {} WITH {} issue(s): {}",
                storyboard_id,
                actor,
                len(bundle.issues),
                "; ".join(bundle.issues),
            )
        m.storyboard_transition(storyboard.status.value, m.StoryboardStatus.APPROVED.value)
        await self.db.update_storyboard_status(storyboard_id, m.StoryboardStatus.APPROVED.value)
        logger.info("Visual narrative: storyboard {} approved by {}", storyboard_id, actor)
        return await self.bundle_for_id(storyboard_id)

    async def archive(self, storyboard_id: int) -> m.StoryboardBundle:
        storyboard = await self.get_storyboard(storyboard_id)
        m.storyboard_transition(storyboard.status.value, m.StoryboardStatus.ARCHIVED.value)
        await self.db.update_storyboard_status(storyboard_id, m.StoryboardStatus.ARCHIVED.value)
        return await self.bundle_for_id(storyboard_id)

    # -- pipeline support -------------------------------------------------

    async def narrative_block(self, city_id: int) -> str:
        """Render the storyboard for the base-story prompt ('' when there is none).

        The plan's ``hook``/``climax``/``ending`` are plan-level summaries; their
        persisted, human-editable equivalents are the beat descriptions, so the
        block is always rebuilt from the beats that are actually stored.
        """
        storyboard = await self.latest_storyboard(city_id)
        if storyboard is None:
            return ""
        beats = await self.db.get_beats(storyboard.id or 0)
        by_type: Dict[str, m.NarrativeBeat] = {}
        for beat in beats:
            by_type.setdefault(beat.beat_type.value, beat)
        setup = by_type.get(m.NarrativeBeatType.SETUP.value) or (beats[0] if beats else None)
        climax = by_type.get(m.NarrativeBeatType.CLIMAX.value)
        closure = by_type.get(m.NarrativeBeatType.RESOLUTION.value) or (beats[-1] if beats else None)
        lines = [
            narrative_prompts.NARRATIVE_BEAT_LINE.format(
                order=beat.order + 1,
                beat_type=beat.beat_type.value,
                title=beat.title or beat.beat_type.value,
                description=beat.description or "—",
                photos=", ".join(os.path.basename(path) for path in beat.photo_paths) or "—",
            )
            for beat in beats
        ]
        return narrative_prompts.NARRATIVE_STORY_BLOCK.format(
            title=storyboard.title or "—",
            logline=storyboard.logline or "—",
            arc=storyboard.narrative_arc or "—",
            journey=storyboard.emotional_journey or "—",
            hook=(setup.description if setup else "—") or "—",
            climax=(climax.description if climax else "—") or "—",
            ending=(closure.description if closure else "—") or "—",
            beats="\n".join(lines) or "—",
        )
