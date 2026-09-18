"""Source resolver contract (Phase 2).

One resolver turns a reference (article URL, GitHub repo / issue / PR /
discussion) into a :class:`CarouselSourceContext`: the facts, quotes, code,
metrics and images the carousel is *allowed* to use — each with the excerpt it
was read from.

Rules every implementation keeps:

* never invent content — an unreadable source raises
  :class:`~core.exceptions.SourceResolutionError` instead of producing filler;
* every fact carries ``source_excerpt`` + ``source_ref``;
* low confidence is reported in ``warnings``, never hidden.
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence, Union

from pydantic import BaseModel, Field

from core.models import (
    CarouselSourceContext,
    CarouselSourceType,
    CarouselVertical,
)

from ..enums import resolve_source_type


class ResolveOptions(BaseModel):
    """Caller-side overrides for a single resolution."""

    source_type: Optional[CarouselSourceType] = None
    vertical: Optional[CarouselVertical] = None
    language: str = ""
    extra: dict = Field(default_factory=dict)


def confidence_from(
    *,
    title: str = "",
    facts: int = 0,
    paragraphs: int = 0,
    verified_metrics: int = 0,
    verified_code: int = 0,
    language: str = "",
) -> float:
    """Deterministic confidence score — a function of what we actually read."""
    score = 0.0
    if title.strip():
        score += 0.20
    if facts:
        score += 0.25
    if facts >= 3:
        score += 0.15
    if paragraphs >= 3:
        score += 0.15
    if verified_metrics:
        score += 0.10
    if verified_code:
        score += 0.10
    if language:
        score += 0.05
    return round(min(score, 1.0), 2)


class BaseSourceResolver(ABC):
    """Base class for every source adapter (URL, GitHub, mock, future ones)."""

    #: Stable identifier written into the audit row (``carousel_sources.resolver``).
    name: str = "base"
    #: Source types this adapter knows how to resolve.
    source_types: frozenset = frozenset()

    def supports(self, source_type: Union[str, CarouselSourceType]) -> bool:
        """True when this resolver handles the given source type."""
        try:
            kind = resolve_source_type(source_type)
        except (ValueError, KeyError):
            return False
        return kind in self.source_types

    @abstractmethod
    async def resolve(
        self, source_ref: str, options: Optional[ResolveOptions] = None
    ) -> CarouselSourceContext:
        """Resolve one reference into sourced context."""

    async def aclose(self) -> None:
        """Release any owned HTTP client (no-op by default)."""
        return None

    def describe(self) -> dict:
        """Small dict for logs / the API (never contains secrets)."""
        return {
            "resolver": self.name,
            "source_types": sorted(str(t) for t in self.source_types),
        }


def merge_warnings(*groups: Sequence[str]) -> list:
    """Merge warning lists, dropping empties and duplicates (order kept)."""
    out: list = []
    for group in groups:
        for item in group or ():
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
    return out


__all__ = [
    "BaseSourceResolver",
    "ResolveOptions",
    "confidence_from",
    "merge_warnings",
]
