"""Content engine (section 28-31): city → photo facts → base story → platform drafts.

``ContentEngine.process_city`` is the orchestrator for one city:

* selects the best photos (scanned, non-duplicate),
* analyses each via the AI provider (cached),
* builds the factual base story,
* adapts it per enabled platform,
* saves :class:`~core.models.Draft` records,

and transitions the city QUEUED → PROCESSING → DRAFTED (or ERROR on failure).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Iterable, List, Optional

from core.config import Config
from core.database import Database
from core.exceptions import StoryboardValidationError, TravelBlogError
from core.models import City, CityStatus, Draft, Platform, ScanStatus, StoryboardStatus
from modules.ai.base import BaseAIProvider, ImageAnalysis
from modules.ai.registry import build_provider
from modules.queue import CityQueue
from . import prompts

log = logging.getLogger(__name__)

#: Only photos that are actually usable for a story.
_VALID_SCAN_STATUSES = {ScanStatus.SCANNED}


class ContentEngine:
    def __init__(
        self,
        db: Database,
        config: Config,
        provider: Optional[BaseAIProvider] = None,
        platforms: Optional[Iterable[Platform]] = None,
        *,
        max_photos: int = 6,
        parallel_analysis: int = 3,
    ) -> None:
        self.db = db
        self.config = config
        self.provider = provider or build_provider(db, config)
        self.platforms = list(platforms or Platform)
        self.max_photos = max_photos
        self.parallel_analysis = max(1, parallel_analysis)

    # -- photo selection ---------------------------------------------------

    async def select_photos(self, city_id: int, limit: int) -> List:
        """Pick the best analysed photos for a city (most recent first)."""
        photos = await self.db.get_photos_by_city(city_id)
        usable = [p for p in photos if p.scan_status in _VALID_SCAN_STATUSES]
        # prefer a spread across years, newest by taken_at
        usable.sort(key=lambda p: p.taken_at or p.modified_at, reverse=True)
        return usable[:limit]

    async def _analyse_photos(self, photos: List) -> List[tuple]:
        """Analyse photos concurrently (bounded) returning (photo, analysis)."""
        sem = asyncio.Semaphore(self.parallel_analysis)

        async def one(photo):
            async with sem:
                try:
                    analysis = await self.provider.analyze_image(
                        photo.path,
                        prompts.ANALYSIS_PROMPT,
                        image_hash=photo.sha256,
                    )
                    return photo, analysis, None
                except Exception as exc:  # noqa: BLE001
                    log.warning("Analysis failed for %s: %s", photo.path, exc)
                    return photo, None, exc

        return await asyncio.gather(*(one(p) for p in photos))

    # -- generation --------------------------------------------------------

    async def generate_base_story(
        self, city: City, photo_facts: List[str], narrative_block: str = ""
    ) -> str:
        """Base story for the city; ``narrative_block`` carries the storyboard (ADR-106).

        The extra argument is optional on purpose: the signature stays backwards
        compatible for callers (and tests) that know nothing about the studio.
        """
        user = prompts.BASE_STORY_USER.format(
            city=city.name,
            country=city.country or "",
            year=city.year or "",
            photo_count=len(photo_facts),
            facts="\n".join(photo_facts),
        )
        return await self.provider.generate_text(prompts.BASE_STORY_SYSTEM, user + narrative_block)

    async def generate_platform_variant(
        self, city: City, base: str, platform: Platform
    ) -> str:
        rule = prompts.PLATFORM_RULES.get(platform.value)
        if rule is None:
            return base  # unknown platform → keep the base story unchanged
        user = prompts.ADAPT_USER.format(base=base, platform=platform.value)
        return await self.provider.generate_text(rule["system"], user)

    # -- orchestration -----------------------------------------------------

    async def process_city(self, city_id: int) -> City:
        city = await self.db.get_city(city_id)
        if city is None:
            raise TravelBlogError(f"City {city_id} not found")

        queue = CityQueue(self.db, self.config)
        city = await queue.claim(city_id)  # QUEUED -> PROCESSING

        try:
            photos = await self.select_photos(city_id, self.max_photos)
            if not photos:
                log.warning("City %s has no usable photos; skipping", city.name)
                return await queue.mark_drafted(city_id)  # nothing to publish

            results = await self._analyse_photos(photos)
            photo_facts = self._facts_from_results(results)
            if not photo_facts:
                raise TravelBlogError(f"No photo could be analysed for city {city.name}")

            # VISUAL NARRATIVE STUDIO (ADR-106): analysed photos -> storyboard.
            # It sits between the analysis and the base story, is idempotent and
            # best-effort, and adds no new *city* status: the city machine stays
            # QUEUED -> PROCESSING -> DRAFTED, while the storyboard carries its own
            # draft/approved gate (see _build_visual_narrative).
            narrative_block = await self._build_visual_narrative(city, results)

            base = await self.generate_base_story(city, photo_facts, narrative_block)

            async def adapt(platform: Platform) -> Draft:
                content = await self.generate_platform_variant(city, base, platform)
                return Draft(
                    city_id=city.id,
                    platform=platform.value,
                    title=(content.splitlines()[0] if content else "")[:200],
                    content=content,
                    photos_json=self._photos_json(results),
                    ai_provider=self.provider.name,
                    ai_model=self._model_name(),
                )

            drafts = await asyncio.gather(*(adapt(p) for p in self.platforms))
            for draft in drafts:
                await self.db.add_draft(draft)

            return await queue.mark_drafted(city_id)  # PROCESSING -> DRAFTED
        except Exception as exc:
            log.error("City %s processing failed: %s", city.name, exc)
            # PROCESSING -> ERROR (legal transition)
            await self.db.update_city_status(city_id, CityStatus.ERROR)
            raise

    async def process_batch(self, batch_size: int = 1) -> List[City]:
        queue = CityQueue(self.db, self.config)
        cities = await queue.next(batch_size)
        done = []
        for city in cities:
            try:
                done.append(await self.process_city(city.id))
            except Exception as exc:  # noqa: BLE001
                log.warning("process_city(%s) failed: %s", city.id, exc)
        return done

    # -- helpers -----------------------------------------------------------

    async def _build_visual_narrative(self, city: City, results: List[tuple]) -> str:
        """Run the Visual Narrative Studio for this city (ADR-106).

        Returns the narrative block to append to the base-story prompt, or ``""``
        when the feature is disabled, there is nothing to tell, or the studio
        failed. Deliberately no new city status: the storyboard's own
        draft/approved machine is where the human gate lives, and
        ``visual_narrative.require_approval`` opts into failing the city until that
        gate is passed (it is off by default so the current pipeline is unchanged).
        """
        settings = getattr(self.config, "visual_narrative", None)
        if settings is None or not settings.enabled:
            return ""

        # Imported lazily: the studio imports the prompts of this package, so a
        # module-level import would create a cycle.
        from modules.visual_narrative_studio import VisualNarrativeStudio

        city_id = city.id
        if city_id is None:  # a stored city always has an id
            log.warning("Visual narrative skipped for %s: city has no id", city.name)
            return ""
        studio = VisualNarrativeStudio(
            self.db, self.config, provider=self.provider, settings=settings
        )
        analyses: Dict[str, ImageAnalysis] = {
            photo.path: analysis for photo, analysis, _error in results if analysis is not None
        }
        try:
            result = await studio.generate(city_id, analyses=analyses)
        except Exception as exc:  # noqa: BLE001 - the studio must never break the pipeline
            log.warning("Visual narrative studio failed for %s: %s", city.name, exc)
            return ""

        for warning in result.warnings:
            log.warning("Visual narrative (%s): %s", city.name, warning)
        if settings.require_approval and result.storyboard.status is not StoryboardStatus.APPROVED:
            raise StoryboardValidationError(
                f"storyboard for {city.name} must be approved before platform content "
                f"(status={result.storyboard.status.value})",
                issues=[f"storyboard_{result.storyboard.status.value}"],
            )
        return await studio.narrative_block(city_id)

    def _facts_from_results(self, results: List[tuple]) -> List[str]:
        facts = []
        for photo, analysis, err in results:
            if err is not None or analysis is None:
                continue
            facts.append(
                prompts.FACT_LINE.format(
                    filename=photo.filename,
                    scene=analysis.scene,
                    objects=", ".join(analysis.objects) or "—",
                    mood=analysis.mood or "—",
                    text=analysis.text or "—",
                )
            )
        return facts

    def _photos_json(self, results: List[tuple]) -> str:
        import json
        entries = []
        for photo, analysis, err in results:
            entries.append(
                {
                    "path": photo.path,
                    "filename": photo.filename,
                    "sha256": photo.sha256,
                    "analysis": analysis.model_dump() if analysis else {"error": str(err)} if err else {},
                }
            )
        return json.dumps({"photos": entries}, ensure_ascii=False)

    def _model_name(self) -> str:
        try:
            return self.provider._model_name()
        except NotImplementedError:
            return self.provider.name
