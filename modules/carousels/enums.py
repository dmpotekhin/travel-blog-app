"""Carousel Factory enums.

This module is a *façade* over ``core.models``: the canonical enum definitions
live there, because ``core.database`` must know every table's model and
importing ``modules.*`` from ``core.*`` would create an import cycle. Exactly
the same arrangement is used by ADR-106 (Visual Narrative Studio).

Import carousel enums from here, not from ``core.models``: this module also
carries the slide-type aliases and the technical/visual split that the renderer
and the fact-guard need.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Iterable, Set, Union

from core.exceptions import CarouselError
from core.models import (
    CAROUSEL_PUBLISHABLE_STATUSES,
    CarouselAutonomyMode,
    CarouselLearningScope,
    CarouselSourceType,
    CarouselStatus,
    CarouselVerificationStatus,
    CarouselVertical,
    HookCategory,
    SLIDE_TYPE_ALIASES as _CORE_SLIDE_TYPE_ALIASES,
    PublicationStatus,
    SlideType,
)

__all__ = [
    "CAROUSEL_PUBLISHABLE_STATUSES",
    "CarouselAutonomyMode",
    "CarouselLearningScope",
    "CarouselSourceType",
    "CarouselStatus",
    "CarouselVerificationStatus",
    "CarouselVertical",
    "HookCategory",
    "SLIDE_TYPE_ALIASES",
    "TECHNICAL_SLIDE_TYPES",
    "VISUAL_SLIDE_TYPES",
    "SlideType",
    "enum_text",
    "hook_categories_for",
    "is_technical_slide",
    "resolve_slide_type",
    "resolve_source_type",
    "source_type_aliases",
]


#: Slide types whose payload is machine-checkable text: code, logs, numbers,
#: API names, diffs. They must be rendered deterministically (Pillow/HTML
#: overlay), never by a diffusion model — one hallucinated digit in a QA
#: carousel is a factual bug, not a style issue.
TECHNICAL_SLIDE_TYPES: FrozenSet[SlideType] = frozenset(
    {
        SlideType.SYMPTOM,
        SlideType.INVESTIGATION,
        SlideType.FIX_CODE,
        SlideType.PREVENTION_CHECKLIST,
        SlideType.CODE_BLOCK,
        SlideType.TERMINAL_BLOCK,
        SlideType.METRICS_COMPARISON,
        SlideType.ARCHITECTURE_DIAGRAM,
        SlideType.CHECKLIST,
        SlideType.OLD_WAY,
        SlideType.NEW_WAY,
    }
)

#: Slide types carried mostly by a real photo or an illustration: a generated
#: background is allowed, and no exact text rides on the image itself.
VISUAL_SLIDE_TYPES: FrozenSet[SlideType] = frozenset(
    {
        SlideType.HERO_HOOK,
        SlideType.PHOTO_CARD,
        SlideType.ROUTE_MAP,
        SlideType.QUOTE_CARD,
        SlideType.CULTURAL_NOTE,
        SlideType.CTA,
    }
)


#: Names used in the specification examples (§10) and by UI/CLI users, mapped
#: to the canonical members. Aliases never introduce new slide behaviour.
#: Built on top of ``core.models.SLIDE_TYPE_ALIASES`` so the two can never
#: drift apart; the extra spellings below are carousel-facing only.
SLIDE_TYPE_ALIASES: Dict[str, SlideType] = {
    **{alias: SlideType(target) for alias, target in _CORE_SLIDE_TYPE_ALIASES.items()},
    "hero_hook": SlideType.HERO_HOOK,
    "symptom_list": SlideType.SYMPTOM,
    "symptoms": SlideType.SYMPTOM,
    "investigation_log": SlideType.INVESTIGATION,
    "code_fix": SlideType.FIX_CODE,
    "fix": SlideType.FIX_CODE,
    "fix_code": SlideType.FIX_CODE,
    "prevention_checklist": SlideType.PREVENTION_CHECKLIST,
    "checklist_slide": SlideType.CHECKLIST,
    "metrics": SlideType.METRICS_COMPARISON,
    "metrics_comparison": SlideType.METRICS_COMPARISON,
    "architecture": SlideType.ARCHITECTURE_DIAGRAM,
    "terminal": SlideType.TERMINAL_BLOCK,
    "code": SlideType.CODE_BLOCK,
    "photo": SlideType.PHOTO_CARD,
    "quote": SlideType.QUOTE_CARD,
    "route": SlideType.ROUTE_MAP,
}


#: Human/CLI spellings of a source type -> canonical member.
_SOURCE_TYPE_ALIASES: Dict[str, CarouselSourceType] = {
    "url": CarouselSourceType.URL,
    "article": CarouselSourceType.ARTICLE_URL,
    "article_url": CarouselSourceType.ARTICLE_URL,
    "blog": CarouselSourceType.ARTICLE_URL,
    "docs": CarouselSourceType.ARTICLE_URL,
    "release_notes": CarouselSourceType.ARTICLE_URL,
    "telegram": CarouselSourceType.TELEGRAM_POST,
    "telegram_post": CarouselSourceType.TELEGRAM_POST,
    "zen": CarouselSourceType.ZEN_POST,
    "zen_post": CarouselSourceType.ZEN_POST,
    "repo": CarouselSourceType.GITHUB_REPO,
    "github": CarouselSourceType.GITHUB_REPO,
    "github_repo": CarouselSourceType.GITHUB_REPO,
    "issue": CarouselSourceType.GITHUB_ISSUE,
    "github_issue": CarouselSourceType.GITHUB_ISSUE,
    "pr": CarouselSourceType.GITHUB_PR,
    "pull_request": CarouselSourceType.GITHUB_PR,
    "github_pr": CarouselSourceType.GITHUB_PR,
    "discussion": CarouselSourceType.GITHUB_DISCUSSION,
    "github_discussion": CarouselSourceType.GITHUB_DISCUSSION,
    "release": CarouselSourceType.GITHUB_RELEASE,
    "github_release": CarouselSourceType.GITHUB_RELEASE,
    "city": CarouselSourceType.CITY,
    "photo_archive": CarouselSourceType.PHOTO_ARCHIVE,
    "test_report": CarouselSourceType.TEST_REPORT,
    "code_snippet": CarouselSourceType.CODE_SNIPPET,
    "qa_artifact": CarouselSourceType.QA_ARTIFACT,
    "vibecoding_post": CarouselSourceType.VIBECODING_POST,
    "vibecoding_session": CarouselSourceType.VIBECODING_SESSION,
    "manual": CarouselSourceType.MANUAL_TOPIC,
    "manual_topic": CarouselSourceType.MANUAL_TOPIC,
}


def enum_text(value: Union[str, Enum]) -> str:
    """Stored string form of any carousel enum (``CarouselVertical.QA`` -> ``"qa"``).

    ``status_value`` in the state machine does this for statuses only; database
    writes of ``vertical``/``source_type`` need the same unwrapping.
    """
    if isinstance(value, Enum) and isinstance(value.value, str):
        return value.value
    return str(value)


def source_type_aliases() -> Dict[str, CarouselSourceType]:
    """Copy of the source-type alias map (read-only use)."""
    return dict(_SOURCE_TYPE_ALIASES)


def resolve_slide_type(value: Union[str, SlideType]) -> SlideType:
    """Resolve an alias or raw value to a canonical :class:`SlideType`.

    Raises :class:`CarouselError` for an unknown name so callers can answer
    with a 422 instead of silently rendering the wrong layout.
    """
    if isinstance(value, SlideType):
        return value
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if key in SLIDE_TYPE_ALIASES:
        return SLIDE_TYPE_ALIASES[key]
    try:
        return SlideType(key)
    except ValueError as exc:
        raise CarouselError(f"Unknown slide type: {value!r}") from exc


def resolve_source_type(value: Union[str, CarouselSourceType]) -> CarouselSourceType:
    """Resolve an alias or raw value to a canonical :class:`CarouselSourceType`."""
    if isinstance(value, CarouselSourceType):
        return value
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if key in _SOURCE_TYPE_ALIASES:
        return _SOURCE_TYPE_ALIASES[key]
    try:
        return CarouselSourceType(key)
    except ValueError as exc:
        raise CarouselError(f"Unknown carousel source type: {value!r}") from exc


def is_technical_slide(slide_type: Union[str, SlideType]) -> bool:
    """True when the slide must be rendered by the deterministic overlay path."""
    return resolve_slide_type(slide_type) in TECHNICAL_SLIDE_TYPES


def hook_categories_for(vertical: Union[str, CarouselVertical]) -> Set[HookCategory]:
    """Hook categories that make sense for a vertical (UI suggestions only).

    The authoritative per-vertical list is the *vertical profile* in
    ``modules/carousels/vertical_profiles.py``; this is a cheap default used
    before profiles are loaded.
    """
    vertical_value = vertical.value if isinstance(vertical, CarouselVertical) else str(vertical)
    mapping: Dict[str, Iterable[HookCategory]] = {
        "travel": (
            HookCategory.HIDDEN_PLACE,
            HookCategory.EXPECTATION_VS_REALITY,
            HookCategory.BUDGET,
            HookCategory.SOLO_TRAVEL,
            HookCategory.CULTURAL_CONTRAST,
            HookCategory.ROUTE_GUIDE,
            HookCategory.PERSONAL_EXPERIENCE,
            HookCategory.SECRET_SPOT,
            HookCategory.SEASONALITY,
        ),
        "qa": (
            HookCategory.FAILURE,
            HookCategory.CONTRARIAN,
            HookCategory.NUMBER_LIST,
            HookCategory.MYSTERY,
            HookCategory.COST,
            HookCategory.BEFORE_AFTER,
            HookCategory.MYTH_BUSTING,
            HookCategory.FLAKY_TEST,
            HookCategory.CI_SLOWDOWN,
            HookCategory.ROOT_CAUSE,
        ),
        "vibecoding": (
            HookCategory.SPEED,
            HookCategory.LOCAL_AI,
            HookCategory.AGENT_WORKFLOW,
            HookCategory.PROMPT_TO_PRODUCT,
            HookCategory.EXPERIMENT,
            HookCategory.ANTI_PATTERN,
            HookCategory.STACK_TOUR,
            HookCategory.AI_VS_HUMAN,
            HookCategory.EVENING_BUILD,
            HookCategory.TOOL_COMPARISON,
        ),
    }
    if vertical_value == "hybrid":
        # a hybrid carousel may borrow from any family
        return set().union(*mapping.values())
    return set(mapping.get(vertical_value, ()))
