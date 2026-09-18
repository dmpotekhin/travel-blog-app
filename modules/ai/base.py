"""AI provider abstraction (section 25-27).

Every AI backend implements ``BaseAIProvider``. Concrete providers live in
``modules/ai/*.py``; ``registry.py`` picks one by name (gemini / deepseek / mock).

Providers deliberately keep the *only* network (or mock) call inside
``_analyze_image_raw`` / ``_generate_text_raw``; caching (``cache.py``) and
request-rate limiting (``ratelimit.py``) wrap those calls so no provider code
duplicates the logic.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from loguru import logger
from pydantic import BaseModel, Field

from core import models as m
from core.config import Config, get_secrets
from core.database import Database
from core.exceptions import AIProviderError
from modules import narrative_prompts


class ImageAnalysis(BaseModel):
    """Structured result of a single photo analysis."""

    subjects: list[str] = Field(default_factory=list)  # people / main objects
    scene: str = ""                                    # one-line scene description
    objects: list[str] = Field(default_factory=list)   # landmarks, details
    mood: str = ""                                     # emotional tone
    text: str = ""                                     # signs, captions, watermarks
    quality_ok: bool = True
    quality_reason: str = ""
    raw: str = ""                                      # raw provider output (diagnostics)


#: Beat types a provider payload may use (anything else degrades to development).
_BEAT_TYPE_VALUES = {beat_type.value for beat_type in m.NarrativeBeatType}

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _extract_json_object(raw: str) -> dict:
    """Pull the first JSON object out of a model reply (markdown fences tolerated)."""
    text = (raw or "").strip()
    if not text:
        raise AIProviderError("provider returned an empty visual narrative plan")
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise AIProviderError("provider reply contains no JSON object for the narrative plan")
    try:
        payload = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise AIProviderError(f"visual narrative plan is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AIProviderError("visual narrative plan must be a JSON object")
    return payload


def _photo_fact_line(index: int, photo: m.NarrativePhoto) -> str:
    """One deterministic fact line per photo (facts only — no invented detail)."""
    parts = [f"#{index} {photo.path}"]
    if photo.scene:
        parts.append(f"сцена: {photo.scene}")
    if photo.objects:
        parts.append("объекты: " + ", ".join(photo.objects))
    if photo.mood:
        parts.append(f"настроение: {photo.mood}")
    if photo.text:
        parts.append(f"текст на фото: {photo.text}")
    return " | ".join(parts)


def _plan_from_payload(
    context: m.NarrativeContext, payload: dict, *, max_photos: int
) -> m.VisualNarrativePlan:
    """Map a provider payload onto the domain plan, tolerating missing fields.

    Photo paths are validated against the analysed set: an invented path is
    dropped, so the storyboard can never reference a photograph that does not
    exist.
    """
    allowed = [photo.path for photo in context.photos[: max(1, max_photos)]]
    allowed_set = set(allowed)
    plan = m.VisualNarrativePlan(
        city_id=context.city_id,
        title=str(payload.get("title") or "").strip(),
        logline=str(payload.get("logline") or "").strip(),
        narrative_arc=str(payload.get("narrative_arc") or "").strip(),
        emotional_journey=str(payload.get("emotional_journey") or "").strip(),
        primary_theme=str(payload.get("primary_theme") or "").strip(),
        secondary_themes=[str(theme) for theme in payload.get("secondary_themes") or []],
        hook=str(payload.get("hook") or "").strip(),
        climax=str(payload.get("climax") or "").strip(),
        ending=str(payload.get("ending") or "").strip(),
        accessibility_notes=[str(note) for note in payload.get("accessibility_notes") or []],
        cultural_sensitivity_notes=[
            str(note) for note in payload.get("cultural_sensitivity_notes") or []
        ],
    )

    for item in payload.get("beats") or []:
        if not isinstance(item, dict):
            continue
        beat_type = str(item.get("beat_type") or "").strip().lower()
        if beat_type not in _BEAT_TYPE_VALUES:
            beat_type = m.NarrativeBeatType.DEVELOPMENT.value
        beat = m.NarrativeBeat(
            beat_type=m.NarrativeBeatType(beat_type),
            title=str(item.get("title") or "").strip(),
            description=str(item.get("description") or "").strip(),
            emotional_tone=str(item.get("emotional_tone") or "").strip(),
            visual_goal=str(item.get("visual_goal") or "").strip(),
        )
        paths = [str(path) for path in item.get("photo_paths") or [] if str(path) in allowed_set]
        plan.beats.append(beat.set_photo_paths(paths))

    for item in payload.get("shots") or []:
        if not isinstance(item, dict):
            continue
        photo_path = str(item.get("photo_path") or "").strip()
        if photo_path not in allowed_set:
            continue  # never storyboard a photograph we did not analyse
        plan.shots.append(
            m.StoryboardShot(
                photo_path=photo_path,
                caption=str(item.get("caption") or "").strip(),
                alt_text=str(item.get("alt_text") or "").strip(),
                crop_recommendation=str(item.get("crop_recommendation") or "").strip(),
                focus_point=str(item.get("focus_point") or "").strip(),
                visual_metaphor=str(item.get("visual_metaphor") or "").strip(),
                pacing_weight=_as_float(item.get("pacing_weight"), 1.0),
                is_hero_image=bool(item.get("is_hero_image")),
            )
        )

    plan.selected_photos = [shot.photo_path for shot in plan.shots]
    return plan


class BaseAIProvider(ABC):
    """Common skeleton: shared cache + rate limit + secret access."""

    #: machine name matched in config `ai.provider`
    name: str = "base"

    def __init__(self, db: Database, config: Config) -> None:
        from .cache import AICache
        from .ratelimit import RateLimiter
        self.db = db
        self.config = config
        self.limiter = RateLimiter(db, config)
        self.cache = AICache(db, self.name, self._model_name())
        self.secrets = get_secrets()

    # -- abstract raw calls (single network / mock touchpoint) ------------

    @abstractmethod
    async def _analyze_image_raw(self, image_path: str, prompt: str) -> ImageAnalysis:
        """Analyze one image and return the result. No cache, no limit."""

    @abstractmethod
    async def _generate_text_raw(
        self, system: str, user: str, *, max_tokens: Optional[int] = None
    ) -> str:
        """Generate text from a system+user prompt. No cache, no limit."""

    # -- public, wrapped API ---------------------------------------------

    async def analyze_image(
        self, image_path: str, prompt: str, image_hash: Optional[str] = None
    ) -> ImageAnalysis:
        """Cached + rate-limited image analysis. ``image_hash`` avoids re-hashing."""
        if image_hash is None:
            from modules.scanner import compute_sha256
            image_hash = await self._run_compute(image_path)
        cached = await self.cache.get(image_hash, prompt)
        if cached is not None:
            return cached
        result = await self._rate_limited(lambda: self._analyze_image_raw(image_path, prompt))
        await self.cache.put(image_hash, prompt, result)
        return result

    async def generate_text(
        self, system: str, user: str, *, max_tokens: Optional[int] = None
    ) -> str:
        """Rate-limited text generation (no cache for free-form text)."""
        return await self._rate_limited(lambda: self._generate_text_raw(system, user, max_tokens=max_tokens))

    # -- visual narrative (ADR-106) ---------------------------------------

    async def analyze_photo_sequence(
        self, photos: List[m.NarrativePhoto], prompt: str, *, parallel: int = 4
    ) -> Dict[str, ImageAnalysis]:
        """Analyse a photo sequence with bounded concurrency, keyed by photo path.

        Failures are logged and skipped on purpose: a missing analysis degrades the
        narrative (the studio reports it) instead of failing the whole city.
        ``analyze_image`` stays the single cached entry point, so re-running the
        studio never re-spends quota.
        """
        import asyncio

        semaphore = asyncio.Semaphore(max(1, parallel))
        results: Dict[str, ImageAnalysis] = {}

        async def analyse(photo: m.NarrativePhoto) -> None:
            async with semaphore:
                try:
                    results[photo.path] = await self.analyze_image(photo.path, prompt)
                except Exception as exc:  # noqa: BLE001 - one bad photo must not stop the rest
                    logger.warning("Photo analysis failed for {}: {}", photo.path, exc)

        await asyncio.gather(*(analyse(photo) for photo in photos))
        return results

    async def generate_visual_narrative_plan(
        self, context: m.NarrativeContext, *, max_photos: int = 12
    ) -> m.VisualNarrativePlan:
        """Build the visual narrative plan: one text call, one JSON document.

        Default implementation — providers that only implement ``_generate_text_raw``
        (gemini, deepseek, …) inherit it, so the feature works without touching
        their HTTP code. ``mock`` overrides it with a deterministic plan and
        ``local_vlm`` is resolved by the registry.

        Raises :class:`AIProviderError` when the reply cannot be parsed; the studio
        then falls back to the local heuristics and marks the plan as degraded.
        """
        selection = list(context.photos)[: max(1, max_photos)]
        facts = "\n".join(
            _photo_fact_line(index, photo) for index, photo in enumerate(selection, start=1)
        )
        story = context.base_story.strip()
        base_story_block = (
            f"БАЗОВАЯ ИСТОРИЯ (уже сгенерирована):\n{story[:2000]}\n\n" if story else ""
        )
        user = narrative_prompts.NARRATIVE_PLAN_USER.format(
            city=context.city,
            country=context.country or "—",
            year=context.year or "—",
            photo_count=len(selection),
            max_photos=max(1, max_photos),
            platforms=narrative_prompts.platform_list(context.target_platforms),
            facts=facts or "(анализ фотографий недоступен)",
            base_story_block=base_story_block,
            beat_guide=narrative_prompts.beat_guide_block(),
            shape=narrative_prompts.NARRATIVE_JSON_SHAPE,
        )
        raw = await self.generate_text(narrative_prompts.NARRATIVE_PLAN_SYSTEM, user)
        plan = _plan_from_payload(context, _extract_json_object(raw), max_photos=max_photos)
        plan.provider = self.name
        plan.model = self._model_name()
        plan.raw = raw
        return plan

    # -- internals --------------------------------------------------------

    async def _run_compute(self, image_path: str) -> str:
        # wrapper so tests can monkeypatch / it runs in a thread
        return await self._to_thread(_hash_file, image_path)

    async def _rate_limited(self, fn):
        await self.limiter.acquire()
        try:
            return await fn()
        except Exception:
            # split error accounting between client/server handled inside providers
            raise

    async def _to_thread(self, fn, *args):
        import asyncio
        return await asyncio.to_thread(fn, *args)

    def _model_name(self) -> str:
        raise NotImplementedError

    def _verify_credentials(self) -> None:
        """Raise if the provider has no API key configured."""


def _hash_file(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
