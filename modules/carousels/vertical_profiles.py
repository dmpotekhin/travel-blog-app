"""Vertical profiles (Phase 3): what each face of the factory actually is.

A profile is data, not behaviour: the six-slide sequence, the hook families,
the tone, the text budgets and the honesty rules for one vertical. Travel
sells a place, QA and Vibecoding sell a verified fact — hence
``precision_first`` for the technical faces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

from core.exceptions import CarouselError
from core.models import CarouselVertical, HookCategory, SlideType

from .enums import hook_categories_for


@dataclass(frozen=True)
class VerticalProfile:
    """Rendering + narrative rules for one vertical (Travel / QA / Vibecoding)."""

    vertical: CarouselVertical
    face: str
    slide_sequence: Tuple[SlideType, ...]
    hook_families: Tuple[HookCategory, ...]
    tone: str
    visual_style: str
    cta_style: str
    hashtags: Tuple[str, ...]
    #: Technical faces: accuracy beats beauty on every trade-off.
    precision_first: bool = False
    #: May Gemini paint a background here? Never for exact content.
    allow_generated_background: bool = True
    max_headline_chars: int = 90
    max_body_chars: int = 240
    bullets_max: int = 4
    bullet_chars: int = 70
    #: What a carousel of this face must never claim without a source line.
    banned_claims: Tuple[str, ...] = ()

    @property
    def slide_count(self) -> int:
        """The six-slide contract, derived from the sequence itself."""
        return len(self.slide_sequence)


def _families(vertical: CarouselVertical) -> Tuple[HookCategory, ...]:
    """Hook families of a vertical, ordered by their stored value (stable)."""
    return tuple(sorted(hook_categories_for(vertical), key=lambda category: category.value))


TRAVEL = VerticalProfile(
    vertical=CarouselVertical.TRAVEL,
    face="Travel",
    slide_sequence=(
        SlideType.HERO_HOOK,
        SlideType.PHOTO_CARD,
        SlideType.PROBLEM,
        SlideType.ROUTE_MAP,
        SlideType.CULTURAL_NOTE,
        SlideType.CTA,
    ),
    hook_families=_families(CarouselVertical.TRAVEL),
    tone="warm, first-person, concrete; no brochure superlatives",
    visual_style="cinematic_documentary",
    cta_style="Сохрани, если планируешь похожую поездку",
    hashtags=("#travel", "#путешествия", "#маршрут"),
    precision_first=False,
    allow_generated_background=True,
    max_headline_chars=90,
    max_body_chars=240,
    banned_claims=(
        "prices, discounts and opening hours that are not written in the source",
        "places the source never mentions",
        "dates and seasons the source never states",
    ),
)

QA = VerticalProfile(
    vertical=CarouselVertical.QA,
    face="QA",
    slide_sequence=(
        SlideType.HERO_HOOK,
        SlideType.SYMPTOM,
        SlideType.AGITATION,
        SlideType.INVESTIGATION,
        SlideType.FIX_CODE,
        SlideType.PREVENTION_CHECKLIST,
    ),
    hook_families=_families(CarouselVertical.QA),
    tone="precise, engineering, no drama; numbers only from the source",
    visual_style="technical_clean",
    cta_style="Сохрани чек-лист перед следующим релизом",
    hashtags=("#qa", "#testing", "#engineering"),
    precision_first=True,
    allow_generated_background=True,
    banned_claims=(
        "root causes the source marks as a guess",
        "metrics, logs, diffs and stack traces that are not in the source",
        "решения, которых не было в PR/issue",
    ),
)

VIBECODING = VerticalProfile(
    vertical=CarouselVertical.VIBECODING,
    face="Vibecoding",
    slide_sequence=(
        SlideType.BOLD_CLAIM,
        SlideType.TASK_CONTEXT,
        SlideType.OLD_WAY,
        SlideType.NEW_WAY,
        SlideType.METRICS_COMPARISON,
        SlideType.CTA,
    ),
    hook_families=_families(CarouselVertical.VIBECODING),
    tone="builder's voice, concrete stack, no benchmark claims without a source",
    visual_style="futuristic_minimal",
    cta_style="Напиши слово из слайда — скину разбор стека",
    hashtags=("#ai", "#agents", "#automation"),
    precision_first=True,
    allow_generated_background=True,
    banned_claims=(
        "benchmarks, token counts and speed numbers the source does not state",
        "model names and versions the source does not state",
        "results that were never actually run",
    ),
)

HYBRID = VerticalProfile(
    vertical=CarouselVertical.HYBRID,
    face="Hybrid",
    slide_sequence=(
        SlideType.HERO_HOOK,
        SlideType.TASK_CONTEXT,
        SlideType.PROBLEM,
        SlideType.PROOF,
        SlideType.CHECKLIST,
        SlideType.CTA,
    ),
    hook_families=_families(CarouselVertical.HYBRID),
    tone="neutral: whatever the source supports, nothing more",
    visual_style="editorial_clean",
    cta_style="Сохрани, если пригодится",
    hashtags=("#carousel",),
    precision_first=True,
    allow_generated_background=True,
    banned_claims=(
        "anything the source does not state",
    ),
)

PROFILES: Dict[CarouselVertical, VerticalProfile] = {
    CarouselVertical.TRAVEL: TRAVEL,
    CarouselVertical.QA: QA,
    CarouselVertical.VIBECODING: VIBECODING,
    CarouselVertical.HYBRID: HYBRID,
}

#: Default accent per face, used when the source carries no brand colour.
DEFAULT_ACCENTS: Dict[CarouselVertical, str] = {
    CarouselVertical.TRAVEL: "#E4572E",
    CarouselVertical.QA: "#FF3B30",
    CarouselVertical.VIBECODING: "#4F46E5",
    CarouselVertical.HYBRID: "#0F172A",
}


def profile_for(vertical: object) -> VerticalProfile:
    """Profile of a vertical (string values accepted, unknown -> error)."""
    if isinstance(vertical, CarouselVertical):
        key = vertical
    else:
        try:
            key = CarouselVertical(str(vertical))
        except ValueError as exc:
            raise CarouselError(f"Unknown carousel vertical: {vertical!r}") from exc
    return PROFILES[key]


def slide_sequence_for(vertical: object) -> Tuple[SlideType, ...]:
    """The six slide types of a vertical."""
    return profile_for(vertical).slide_sequence


__all__ = [
    "DEFAULT_ACCENTS",
    "HYBRID",
    "PROFILES",
    "QA",
    "TRAVEL",
    "VIBECODING",
    "VerticalProfile",
    "profile_for",
    "slide_sequence_for",
]
