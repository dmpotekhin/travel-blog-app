"""Slide planner (Phase 3): the SlidePlan JSON the renderer will consume.

Everything here is deterministic and extractive: each slide is filled from the
facts, quotes, code snippets and metrics the resolver actually read, clipped to
the profile's text budget (which is what keeps copy out of the bottom 20% of the
frame). Anything the source does not support is dropped and reported as a
warning instead of being rewritten into something prettier.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, List, Optional, Sequence, Tuple

from loguru import logger

from core.exceptions import CarouselError
from core.models import (
    CarouselHookCandidate,
    CarouselSlide,
    CarouselSlidePlan,
    CarouselSourceContext,
    CarouselVertical,
    CodeSnippet,
    ImageAsset,
    Metric,
    SlideType,
)

from ..fact_guard import FactGuard
from ..vertical_profiles import DEFAULT_ACCENTS, VerticalProfile, profile_for

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the planner importable
    from core.config import CarouselBrandConfig

_WHITESPACE = re.compile(r"\s+")

#: Thresholds for "this fact is about X" routing (all lowercase substrings).
PROBLEM_MARKERS = ("падает", "не работает", "не работал", "ошибк", " проблем", "закрыт", "сломал", "flaky", "bug")
IMPACT_MARKERS = ("стоил", "потерял", "часов", "дней", "замороз", "блокир", "релиз", "довери", "потрат")
HYPOTHESIS_MARKERS = ("похоже", "провер", "причин", "оказал", "гипотез", "подозр", "порядок")
CULTURE_MARKERS = ("чаевые", "обыча", "местные", "не принято", "этикет", "язык", "культур")
OLD_WAY_MARKERS = ("раньше", "было", "вручную", "ручной", "excel", "папк", "копипаст")
NEW_WAY_MARKERS = ("стало", "теперь", "автоматич", "пайплайн", "pipeline", "агент", "модул")


def clip(text: str, limit: int) -> str:
    """Trim to ``limit`` chars on a word boundary (prefix stays a substring)."""
    cleaned = _WHITESPACE.sub(" ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit].rsplit(" ", 1)[0] or cleaned[:limit]
    return f"{cut.rstrip(' ,;:.-')}…"


@dataclass
class SlideDraft:
    """Unvalidated slide content, still carrying the ref of every claim."""

    headline: str = ""
    headline_ref: str = ""
    subheadline: str = ""
    subheadline_ref: str = ""
    body: str = ""
    body_ref: str = ""
    bullet_pairs: List[Tuple[str, str]] = field(default_factory=list)
    code: Optional[CodeSnippet] = None
    metrics: List[Metric] = field(default_factory=list)
    image: Optional[ImageAsset] = None
    notes: List[str] = field(default_factory=list)
    #: CTA-style slides make no factual claim, so the fact guard does not apply.
    claim_free: bool = False


class SlidePlanner:
    """Turns (source context + chosen hook + profile) into six slides."""

    def __init__(
        self,
        *,
        guard_factory: Callable[[CarouselSourceContext], FactGuard] = FactGuard,
        brand: Optional["CarouselBrandConfig"] = None,
        cta_override: str = "",
    ) -> None:
        self._guard_factory = guard_factory
        #: Brand strategy from ``carousels.brand``: it drives the travel guard
        #: and the funnel CTA. ``None`` keeps the pre-brand behaviour.
        self._brand = brand
        #: An operator's own CTA wins over the configured one.
        self._cta_override = cta_override
        self._material: List[Tuple[str, str]] = []
        self._cursor = 0
        #: indices of ``_material`` already used by a slide (no early repeats)
        self._used: set = set()
        self._context: Optional[CarouselSourceContext] = None
        self._hook: Optional[CarouselHookCandidate] = None
        self._job_id: Optional[int] = None

    # -- public API ----------------------------------------------------

    def plan(
        self,
        context: CarouselSourceContext,
        *,
        vertical: object = None,
        hook: Optional[CarouselHookCandidate] = None,
        job_id: Optional[int] = None,
        slide_count: Optional[int] = None,
    ) -> CarouselSlidePlan:
        """Build the plan; ``slide_count`` is a hard requirement (6 by default)."""
        profile = profile_for(vertical or context.vertical)
        guard = self._guard_factory(context)
        self._material = self._collect_material(context)
        self._cursor = 0
        self._used = set()
        self._context = context
        self._hook = hook
        self._job_id = job_id
        # The hero slide quotes the chosen hook; that sentence must not come
        # back on a later slide, or the carousel reads as a loop.
        self._mark_used(hook.source_support if hook is not None else "")

        sequence = list(profile.slide_sequence)
        wanted = slide_count or profile.slide_count
        if wanted != len(sequence):
            sequence = self._resize_sequence(sequence, wanted)

        warnings: List[str] = []
        if not self._material:
            warnings.append("source has no facts — slides stay empty on purpose")
        # Brand strategy (config carousels.brand.travel_rules): a travel carousel
        # must still read as a system case study. Supervised default = this is a
        # warning — the human approval gate remains the only thing that blocks.
        warnings.extend(self._travel_builder_angle_warnings(profile.vertical, context))

        slides: List[CarouselSlide] = []
        for order, slide_type in enumerate(sequence, start=1):
            draft = self._build(slide_type, profile, context, hook)
            slide, slide_warnings = self._finalise(order, slide_type, draft, guard, profile, context)
            slides.append(slide)
            warnings.extend(slide_warnings)

        plan = CarouselSlidePlan(
            job_id=job_id,
            vertical=profile.vertical,
            title=context.title,
            logline=self._logline(context, hook),
            caption=self._caption(context, guard, hook),
            hashtags=list(profile.hashtags),
            visual_style=profile.visual_style,
            slides=slides,
            warnings=warnings,
        )
        logger.debug(
            "slide planner: {} slides for {} ({} warnings)",
            len(slides),
            profile.vertical.value,
            len(warnings),
        )
        return plan

    # -- material ------------------------------------------------------

    @property
    def _active_context(self) -> CarouselSourceContext:
        """The context of the current plan (set by :meth:`plan`)."""
        if self._context is None:
            raise CarouselError("SlidePlanner.plan() must run before the slide builders")
        return self._context

    @staticmethod
    def _collect_material(context: CarouselSourceContext) -> List[Tuple[str, str]]:
        """``(text, ref)`` pairs the planner may quote, in source order."""
        material: List[Tuple[str, str]] = []
        seen: set = set()
        for fact in context.sourced_facts:
            if fact.text and fact.text not in seen:
                seen.add(fact.text)
                material.append((fact.text, fact.source_ref or "source"))
        for text in context.facts:
            if text and text not in seen:
                seen.add(text)
                material.append((text, "source"))
        return material

    def _next(self) -> Optional[Tuple[str, str]]:
        """Next fact not handed out yet; cycles only when everything is used.

        Cycling is allowed (six slides, few facts) but a sentence never comes
        back until the whole source has had its turn — carousels read as a loop
        otherwise.
        """
        if not self._material:
            return None
        total = len(self._material)
        for offset in range(total):
            index = (self._cursor + offset) % total
            if index not in self._used:
                self._cursor = index + 1
                self._used.add(index)
                return self._material[index]
        # everything used: start the cycle again, still in source order
        index = self._cursor % total
        self._cursor = index + 1
        return self._material[index]

    def _pick(self, markers: Sequence[str]) -> Optional[Tuple[str, str]]:
        """First fact mentioning any marker that was not used yet."""
        total = len(self._material)
        for offset in range(total):
            index = (self._cursor + offset) % total
            if index in self._used:
                continue
            text, ref = self._material[index]
            lowered = text.lower()
            if any(marker in lowered for marker in markers):
                self._cursor = index + 1
                self._used.add(index)
                return text, ref
        return None

    def _mark_used(self, text: str) -> None:
        """Reserve a source line that is already spoken for (the hero hook)."""
        needle = (text or "").strip()
        if not needle:
            return
        for index, (candidate, _ref) in enumerate(self._material):
            if candidate.strip() == needle:
                self._used.add(index)
                return

    def _few(self, count: int, markers: Optional[Sequence[str]] = None) -> List[Tuple[str, str]]:
        """Up to ``count`` facts (marker-matched first, then in order)."""
        picked: List[Tuple[str, str]] = []
        if markers:
            for _ in range(count):
                item = self._pick(markers)
                if item is None:
                    break
                picked.append(item)
        while len(picked) < count:
            item = self._next()
            if item is None or item in picked:
                break
            picked.append(item)
        return picked

    @staticmethod
    def _resize_sequence(sequence: List[SlideType], wanted: int) -> List[SlideType]:
        """Grow/shrink a sequence while keeping the CTA last."""
        if wanted < 1:
            return sequence[:1]
        cta = sequence[-1]
        body = [slide_type for slide_type in sequence if slide_type is not SlideType.CTA]
        while len(body) + 1 < wanted:
            body.append(body[-1] if body else SlideType.TASK_CONTEXT)
        body = (body + [SlideType.PROOF] * wanted)[: max(wanted - 1, 0)]
        return body + [cta]

    # -- builders ------------------------------------------------------

    def _build(
        self,
        slide_type: SlideType,
        profile: VerticalProfile,
        context: CarouselSourceContext,
        hook: Optional[CarouselHookCandidate],
    ) -> SlideDraft:
        builders = {
            SlideType.HERO_HOOK: self._hero,
            SlideType.BOLD_CLAIM: self._hero,
            SlideType.PHOTO_CARD: self._photo,
            SlideType.PROBLEM: lambda draft: self._marker_slide(draft, PROBLEM_MARKERS, "problem"),
            SlideType.AGITATION: self._agitation,
            SlideType.SYMPTOM: lambda draft: self._list_slide(draft, 3, ("падает", "локально", " ci", "красн")),
            SlideType.CHECKLIST: lambda draft: self._list_slide(draft, 3, None),
            SlideType.PREVENTION_CHECKLIST: self._prevention,
            SlideType.INVESTIGATION: lambda draft: self._list_slide(draft, 3, HYPOTHESIS_MARKERS),
            SlideType.FIX_CODE: self._code,
            SlideType.CODE_BLOCK: self._code,
            SlideType.TERMINAL_BLOCK: self._terminal,
            SlideType.ARCHITECTURE_DIAGRAM: self._architecture,
            SlideType.METRICS_COMPARISON: self._metrics,
            SlideType.ROUTE_MAP: self._route,
            SlideType.CULTURAL_NOTE: lambda draft: self._marker_slide(draft, CULTURE_MARKERS, "culture"),
            SlideType.QUOTE_CARD: self._quote,
            SlideType.OLD_WAY: lambda draft: self._list_slide(draft, 3, OLD_WAY_MARKERS),
            SlideType.NEW_WAY: lambda draft: self._list_slide(draft, 3, NEW_WAY_MARKERS),
            SlideType.TASK_CONTEXT: lambda draft: self._list_slide(draft, 3, None),
            SlideType.SOLUTION: lambda draft: self._list_slide(draft, 3, NEW_WAY_MARKERS),
            SlideType.FEATURE: lambda draft: self._list_slide(draft, 3, None),
            SlideType.PROOF: self._proof,
            SlideType.CTA: lambda draft: self._cta(draft, profile),
        }
        builder = builders.get(slide_type, self._generic)
        draft = SlideDraft()
        builder(draft)
        if slide_type in (SlideType.PHOTO_CARD,) and not draft.image:
            draft.notes.append("no image in source — slide rendered as text")
        _ = context
        return draft

    def _hero(self, draft: SlideDraft) -> None:
        hook = self._hook
        if hook is not None and hook.text:
            draft.headline = hook.text
            draft.headline_ref = "hook"
        if hook is not None and hook.source_support:
            draft.subheadline = hook.source_support
            draft.subheadline_ref = "hook"
        if not draft.headline:
            item = self._next()
            if item:
                draft.headline, draft.headline_ref = item

    def _photo(self, draft: SlideDraft) -> None:
        context = self._active_context
        item = self._next()
        if item:
            draft.headline, draft.headline_ref = item
        if context.images:
            draft.image = context.images[0]

    def _marker_slide(self, draft: SlideDraft, markers: Sequence[str], kind: str) -> None:
        item = self._pick(markers) or self._next()
        if item is None:
            return
        draft.headline, draft.headline_ref = item
        if kind == "problem":
            draft.body, draft.body_ref = item
            draft.body_ref = draft.headline_ref

    def _agitation(self, draft: SlideDraft) -> None:
        item = self._pick(IMPACT_MARKERS)
        if item is None:
            draft.notes.append("no impact line in source — slide stays factual")
            item = self._next()
        if item:
            draft.headline, draft.headline_ref = item

    def _list_slide(self, draft: SlideDraft, count: int, markers: Optional[Sequence[str]]) -> None:
        picked = self._few(count, markers)
        draft.bullet_pairs.extend(picked)
        if len(picked) < 2:
            draft.notes.append("fewer than two source lines for this list")

    def _prevention(self, draft: SlideDraft) -> None:
        picked = self._few(3, ("чтобы", "надо", "следует", "лучше", "изоляц", "фикстур", "порядок"))
        draft.bullet_pairs.extend(picked)
        if not picked:
            draft.notes.append("source names no prevention steps")

    def _code(self, draft: SlideDraft) -> None:
        item = self._next()
        if item:
            draft.headline, draft.headline_ref = item
        snippets = [snippet for snippet in self._active_context.code_snippets if snippet.code.strip()]
        if snippets:
            snippet = snippets[0]
            draft.code = snippet
            if snippet.truncated:
                draft.notes.append("code shown as a fragment (source was truncated)")
        else:
            draft.notes.append("no code in source — slide rendered as text")

    def _terminal(self, draft: SlideDraft) -> None:
        shells = [
            snippet
            for snippet in self._active_context.code_snippets
            if snippet.language.lower() in ("bash", "sh", "shell", "console", "terminal", "zsh")
        ]
        if shells:
            draft.code = shells[0]
            item = self._next()
            if item:
                draft.headline, draft.headline_ref = item
            return
        draft.notes.append("no terminal output in source — slide rendered as text")
        item = self._next()
        if item:
            draft.headline, draft.headline_ref = item

    def _architecture(self, draft: SlideDraft) -> None:
        draft.notes.append("diagram is drawn from source steps, never generated by a model")
        picked = self._few(4, None)
        draft.bullet_pairs.extend(picked)

    def _metrics(self, draft: SlideDraft) -> None:
        verified = [metric for metric in self._active_context.metrics if metric.is_verified][:4]
        if verified:
            draft.metrics = verified
        else:
            draft.notes.append("no verified metrics in source — slide stays textual")
        item = self._next()
        if item:
            draft.body, draft.body_ref = item

    def _route(self, draft: SlideDraft) -> None:
        item = self._pick(("→", "—", "->", "маршрут", "route")) or self._next()
        if item is None:
            return
        text, ref = item
        draft.headline = text
        draft.headline_ref = ref
        parts = [part.strip() for part in re.split(r"→|—|->|,", text) if part.strip()]
        if len(parts) >= 2:
            draft.bullet_pairs.extend((part, ref) for part in parts[:5])
        else:
            draft.body, draft.body_ref = text, ref

    def _quote(self, draft: SlideDraft) -> None:
        if self._active_context.quotes:
            draft.headline = self._active_context.quotes[0]
            draft.headline_ref = "quote:0"
            return
        item = self._next()
        if item:
            draft.headline, draft.headline_ref = item

    def _proof(self, draft: SlideDraft) -> None:
        item = self._next()
        if item:
            draft.headline, draft.headline_ref = item

    def _generic(self, draft: SlideDraft) -> None:
        item = self._next()
        if item:
            draft.headline, draft.headline_ref = item

    def _cta(self, draft: SlideDraft, profile: VerticalProfile) -> None:
        draft.claim_free = True
        draft.headline = self._cta_text(profile)

    # -- brand strategy (config carousels.brand) -----------------------

    def _cta_text(self, profile: VerticalProfile) -> str:
        """Funnel CTA for a vertical: the operator's words, then the config.

        The strings live in ``carousels.brand.cta_funnel`` — the profile's own
        ``cta_style`` is only the fallback when no brand config is injected.
        """
        if self._cta_override:
            return self._cta_override
        funnel = getattr(self._brand, "cta_funnel", None)
        configured = funnel.cta_for(profile.vertical) if funnel is not None else ""
        return configured or profile.cta_style

    def _travel_builder_angle_warnings(
        self, vertical: object, context: CarouselSourceContext
    ) -> List[str]:
        """Warn — never block — when a travel plan has no builder angle in sight."""
        brand = self._brand
        if brand is None or vertical is not CarouselVertical.TRAVEL:
            return []
        rules = brand.travel_rules
        if not getattr(rules, "require_builder_angle", True):
            return []
        if rules.has_builder_angle(self._context_text(context)):
            return []
        note = (
            "TRAVEL_BUILDER_ANGLE_MISSING: карусель выйдет как pure lifestyle. "
            "Подтвердите или добавьте builder-angle в hook/narrative."
        )
        if not getattr(rules, "warning_only", True):
            note = f"{note} (warning_only=false — требуется подтверждение)"
        return [note]

    @staticmethod
    def _context_text(context: CarouselSourceContext) -> str:
        """Every word the resolver actually read (the guard reads, never writes)."""
        parts: List[str] = [context.title, context.summary]
        parts.extend(context.facts)
        parts.extend(fact.text for fact in context.sourced_facts if fact.text)
        return " ".join(part for part in parts if part)

    # -- validation ----------------------------------------------------

    def _finalise(
        self,
        order: int,
        slide_type: SlideType,
        draft: SlideDraft,
        guard: FactGuard,
        profile: VerticalProfile,
        context: CarouselSourceContext,
    ) -> Tuple[CarouselSlide, List[str]]:
        warnings = [f"слайд {order}: {note}" for note in draft.notes]
        refs: List[str] = []

        def keep(text: str, ref: str, limit: int) -> str:
            if not text:
                return ""
            if not draft.claim_free and not guard.is_supported(text):
                warnings.append(f"слайд {order}: убрана неподтверждённая строка ({clip(text, 40)!r})")
                return ""
            if ref:
                refs.append(ref)
            return clip(text, limit)

        headline = keep(draft.headline, draft.headline_ref, profile.max_headline_chars)
        subheadline = keep(draft.subheadline, draft.subheadline_ref, profile.max_headline_chars)
        body = keep(draft.body, draft.body_ref, profile.max_body_chars)

        bullets: List[str] = []
        for text, ref in draft.bullet_pairs:
            if len(bullets) >= profile.bullets_max:
                break
            cleaned = keep(text, ref, profile.bullet_chars)
            if cleaned and cleaned not in bullets:
                bullets.append(cleaned)

        asset = draft.image
        accent = context.brand_colors[0] if context.brand_colors else DEFAULT_ACCENTS[profile.vertical]
        slide = CarouselSlide(
            job_id=self._job_id,
            order=order,
            slide_type=slide_type,
            headline=headline,
            subheadline=subheadline,
            body_text=body,
            bullets_json=json.dumps(bullets, ensure_ascii=False),
            code_json=draft.code.model_dump_json() if draft.code else json.dumps({}),
            metrics_json=json.dumps(
                [json.loads(metric.model_dump_json()) for metric in draft.metrics], ensure_ascii=False
            ),
            image_asset_json=asset.model_dump_json() if asset else json.dumps({}),
            source_refs_json=json.dumps(list(dict.fromkeys(refs)), ensure_ascii=False),
            background_prompt=self._background_prompt(profile, context),
            background_style=profile.visual_style,
            accent_color=accent,
            alt_text=self._alt_text(order, slide_type, headline, body, bullets),
        )
        return slide, warnings

    @staticmethod
    def _alt_text(
        order: int, slide_type: SlideType, headline: str, body: str, bullets: Sequence[str]
    ) -> str:
        """Accessibility text: what a screen reader should hear on this slide."""
        core = headline or body or (bullets[0] if bullets else "текст из источника")
        label = slide_type.value.replace("_", " ")
        return f"Слайд {order} из 6 ({label}): {clip(core, 80)}"

    def _background_prompt(self, profile: VerticalProfile, context: CarouselSourceContext) -> str:
        """Style-only prompt: no facts, no letters — Gemini paints the mood, not the data."""
        if not profile.allow_generated_background:
            return ""
        return (
            f"{profile.visual_style} background for a {profile.face} carousel, "
            "no text, no letters, no numbers, cinematic lighting"
        )

    @staticmethod
    def _logline(context: CarouselSourceContext, hook: Optional[CarouselHookCandidate]) -> str:
        if hook is not None and hook.source_support:
            return clip(hook.source_support, 120)
        if context.facts:
            return clip(context.facts[0], 120)
        return ""

    @staticmethod
    def _caption(
        context: CarouselSourceContext,
        guard: FactGuard,
        hook: Optional[CarouselHookCandidate],
    ) -> str:
        """Caption: title + hook line, both already in the source."""
        parts = [context.title] if context.title else []
        if hook is not None and hook.source_support:
            parts.append(hook.source_support)
        elif context.summary:
            parts.append(context.summary)
        text = " — ".join(part for part in parts if part)
        if not text:
            return ""
        if not guard.is_supported(text):
            return clip(context.title or context.summary, 200)
        return clip(text, 220)


__all__ = ["SlidePlanner", "SlideDraft", "clip"]
