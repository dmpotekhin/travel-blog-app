"""Tri-Face Carousel Factory (ADR-107).

Three verticals — Travel, QA, Vibecoding — over one deterministic pipeline:

    source -> research -> vertical -> hooks -> narrative -> slide plan
           -> render -> verify -> approval -> publish -> analytics -> learnings

Layout of this package:

* :mod:`modules.carousels.enums` / :mod:`modules.carousels.models` — façades over
  ``core.models`` (the canonical definitions live in ``core`` because
  ``core.database`` must know every table's model).
* :mod:`modules.carousels.state_machine` — job lifecycle + approval gate.
* :mod:`modules.carousels.database_helpers` — repository façade (all SQL stays
  in ``core.database``).
* :mod:`modules.carousels.service` — job lifecycle service (Phase 1).

Nothing in here invents facts: content may only come from a resolved source,
and technical slides are rendered deterministically (never drawn by an image
model). Publication requires an ``approved`` job unless an operator explicitly
configures ``mode: full_autonomous`` with ``require_human_approval: false``.
"""
from .enums import (
    TECHNICAL_SLIDE_TYPES,
    VISUAL_SLIDE_TYPES,
    CarouselLearningScope,
    CarouselSourceType,
    CarouselStatus,
    CarouselVertical,
    CarouselVerificationStatus,
    HookCategory,
    SlideType,
    resolve_slide_type,
    resolve_source_type,
)
from .models import (
    CarouselBundle,
    CarouselGenerateRequest,
    CarouselJob,
    CarouselSlide,
    CarouselSlidePlan,
    CarouselSourceContext,
    CarouselSourceRecord,
    CodeSnippet,
    ImageAsset,
    Metric,
)
from .service import CarouselFactory
from .state_machine import (
    HAPPY_PATH,
    MODE_FULL_AUTONOMOUS,
    MODE_SUPERVISED,
    describe,
    next_states,
    require_publish_allowed,
    status_value,
)

__all__ = [
    "CarouselBundle",
    "CarouselFactory",
    "CarouselGenerateRequest",
    "CarouselJob",
    "CarouselLearningScope",
    "CarouselSlide",
    "CarouselSlidePlan",
    "CarouselSourceContext",
    "CarouselSourceRecord",
    "CarouselSourceType",
    "CarouselStatus",
    "CarouselVertical",
    "CarouselVerificationStatus",
    "CodeSnippet",
    "HAPPY_PATH",
    "HookCategory",
    "ImageAsset",
    "MODE_FULL_AUTONOMOUS",
    "MODE_SUPERVISED",
    "Metric",
    "SlideType",
    "TECHNICAL_SLIDE_TYPES",
    "VISUAL_SLIDE_TYPES",
    "describe",
    "next_states",
    "require_publish_allowed",
    "resolve_slide_type",
    "resolve_source_type",
    "status_value",
]
