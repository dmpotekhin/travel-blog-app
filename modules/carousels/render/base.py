"""Renderer contracts (Phase 4).

Two abstractions keep every external dependency swappable:

* :class:`BaseCarouselImageProvider` — where backgrounds come from
  (deterministic gradient, a real photo from the source, or Gemini);
* :class:`BaseSlideRenderer` — how the slide is painted (Pillow today, an
  HTML/Satori renderer later).

Both are pure interfaces: the service never imports Pillow, httpx or Google.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from core.models import CarouselSlide, CarouselSourceContext

from ..vertical_profiles import VerticalProfile


class TextBox(BaseModel):
    """Where one piece of text landed on the canvas (used by verification)."""

    kind: str = ""
    text: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    font_size: int = 0

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def right(self) -> int:
        return self.x + self.width


class RenderedSlide(BaseModel):
    """Result of painting one slide: a JPG plus the layout it was painted with."""

    order: int = 0
    path: str = ""
    layout_path: str = ""
    width: int = 0
    height: int = 0
    image_format: str = ""
    byte_size: int = 0
    sha256: str = ""
    provider: str = ""
    background_path: str = ""
    fonts: List[str] = Field(default_factory=list)
    text_boxes: List[TextBox] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    @property
    def lowest_text_bottom(self) -> int:
        """Bottom edge of the lowest text block (0 when nothing was drawn)."""
        return max((box.bottom for box in self.text_boxes), default=0)


class BackgroundResult(BaseModel):
    """A background image on disk, or an honest reason why there is none."""

    path: str = ""
    provider: str = ""
    is_generated: bool = False
    reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.path)


class BaseCarouselImageProvider(ABC):
    """Source of slide backgrounds (Gemini / local file / deterministic paint)."""

    name: str = "base"

    @abstractmethod
    async def background(
        self,
        *,
        slide: CarouselSlide,
        profile: VerticalProfile,
        width: int,
        height: int,
        context: Optional[CarouselSourceContext] = None,
        destination_dir: Optional[Path] = None,
    ) -> BackgroundResult:
        """Return a background for this slide (never a fabricated photo)."""


class BaseSlideRenderer(ABC):
    """Paints one planned slide into a 768x1376 JPG."""

    name: str = "base"
    image_format: str = "jpeg"

    @abstractmethod
    def render(
        self,
        slide: CarouselSlide,
        *,
        profile: VerticalProfile,
        width: int,
        height: int,
        safe_zone_pixels: int,
        destination: Path,
        context: Optional[CarouselSourceContext] = None,
        attempt: int = 0,
    ) -> RenderedSlide:
        """Render ``slide`` to ``destination`` and describe what was drawn."""

    async def render_async(
        self,
        slide: CarouselSlide,
        *,
        profile: VerticalProfile,
        width: int,
        height: int,
        safe_zone_pixels: int,
        destination: Path,
        context: Optional[CarouselSourceContext] = None,
        attempt: int = 0,
    ) -> RenderedSlide:
        """Async entry point used by the service (backgrounds may be remote).

        The default implementation just calls :meth:`render`, so a purely local
        renderer needs no async plumbing at all.
        """
        return self.render(
            slide,
            profile=profile,
            width=width,
            height=height,
            safe_zone_pixels=safe_zone_pixels,
            destination=destination,
            context=context,
            attempt=attempt,
        )


__all__ = [
    "BackgroundResult",
    "BaseCarouselImageProvider",
    "BaseSlideRenderer",
    "RenderedSlide",
    "TextBox",
]
