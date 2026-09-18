"""Carousel Factory pydantic models.

Entities (CarouselJob, CarouselSlide, ...) are defined in ``core.models`` and
re-exported here for the same reason as the enums: the database layer has to
know the models, and ``core.*`` must not import ``modules.*``.

This module adds the *request* models that only the carousel API/CLI/service
use, so the domain layer stays free of transport concerns.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from core.models import (
    CarouselBundle,
    CarouselHookCandidate,
    CarouselJob,
    CarouselLearning,
    CarouselMetric,
    CarouselPublication,
    CarouselSlide,
    CarouselSlidePlan,
    CarouselSourceContext,
    CarouselSourceRecord,
    CarouselTemplate,
    CarouselVertical,
    CodeSnippet,
    ImageAsset,
    Metric,
    QAArtifact,
    VibecodingSession,
)

from .enums import CarouselSourceType, CarouselStatus

__all__ = [
    "CarouselBundle",
    "CarouselGenerateRequest",
    "CarouselHookCandidate",
    "CarouselJob",
    "CarouselLearning",
    "CarouselMetric",
    "CarouselPublication",
    "CarouselRunRequest",
    "CarouselSlide",
    "CarouselSlidePlan",
    "CarouselSourceContext",
    "CarouselSourceRecord",
    "CarouselTemplate",
    "CarouselVertical",
    "CodeSnippet",
    "ImageAsset",
    "Metric",
    "QAArtifact",
    "SlideEditRequest",
    "VibecodingSession",
]


class CarouselGenerateRequest(BaseModel):
    """Everything a user must supply to start a carousel (§6, §22).

    ``vertical=None`` means auto-detect: the source resolver proposes a
    vertical and the user can override it afterwards.
    """

    source_type: CarouselSourceType = CarouselSourceType.URL
    source_ref: str = Field(
        default="",
        description="URL, GitHub ref (owner/repo#42), city name/id or manual topic",
    )
    vertical: Optional[CarouselVertical] = None
    hook: Optional[str] = None
    platforms: List[str] = Field(default_factory=lambda: ["tiktok", "instagram"])
    dry_run: Optional[bool] = None


class CarouselRunRequest(BaseModel):
    """Optional knobs for one pipeline step (research/render/verify/...)."""

    dry_run: Optional[bool] = None
    force: bool = False
    notes: str = ""


class SlideEditRequest(BaseModel):
    """Human edit of a single slide in the Slide Editor (§17)."""

    headline: Optional[str] = None
    subheadline: Optional[str] = None
    body_text: Optional[str] = None
    bullets: Optional[List[str]] = None
    code: Optional[Dict[str, str]] = None
    alt_text: Optional[str] = None
    order: Optional[int] = None
    slide_type: Optional[str] = None
    accent_color: Optional[str] = None

    def to_repository_fields(self) -> Dict[str, object]:
        """Map request fields to storage columns (only what was set)."""
        import json

        fields: Dict[str, object] = {}
        if self.headline is not None:
            fields["headline"] = self.headline
        if self.subheadline is not None:
            fields["subheadline"] = self.subheadline
        if self.body_text is not None:
            fields["body_text"] = self.body_text
        if self.bullets is not None:
            fields["bullets_json"] = json.dumps(self.bullets, ensure_ascii=False)
        if self.code is not None:
            fields["code_json"] = json.dumps(self.code, ensure_ascii=False)
        if self.alt_text is not None:
            fields["alt_text"] = self.alt_text
        if self.order is not None:
            fields["order"] = int(self.order)
        if self.accent_color is not None:
            fields["accent_color"] = self.accent_color
        return fields


class CarouselJobListFilter(BaseModel):
    """Filters accepted by ``GET /api/carousels`` (§16)."""

    status: Optional[CarouselStatus] = None
    vertical: Optional[CarouselVertical] = None
    source_type: Optional[CarouselSourceType] = None
    created_after: Optional[str] = None
    limit: int = 50
    offset: int = 0
