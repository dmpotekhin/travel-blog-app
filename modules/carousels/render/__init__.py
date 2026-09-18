"""Rendering + verification (Phase 4).

``PillowSlideRenderer`` paints the plan into 768x1376 JPGs; ``SlideVerifier``
re-checks the files against the platform rules (size, format, bottom 20% safe
zone, legibility, code integrity, alt-text, source support). Backgrounds come
from a provider, so Gemini stays optional and swappable.
"""

from .base import (
    BackgroundResult,
    BaseCarouselImageProvider,
    BaseSlideRenderer,
    RenderedSlide,
    TextBox,
)
from .fonts import FontBook
from .pillow_renderer import MIN_FONT_SIZE, PillowSlideRenderer
from .providers import (
    GeminiBackgroundProvider,
    SolidGradientProvider,
    SourceImageProvider,
    provider_for,
)
from .verification import SlideVerifier, VerificationReport

__all__ = [
    "BackgroundResult",
    "BaseCarouselImageProvider",
    "BaseSlideRenderer",
    "FontBook",
    "GeminiBackgroundProvider",
    "MIN_FONT_SIZE",
    "PillowSlideRenderer",
    "RenderedSlide",
    "SlideVerifier",
    "SolidGradientProvider",
    "SourceImageProvider",
    "TextBox",
    "VerificationReport",
    "provider_for",
]
