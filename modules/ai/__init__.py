"""AI layer: provider abstraction, cache, rate limit, factory.

``build_provider`` is the single entry point — call it with a Database + Config
to get a ready-to-use :class:`BaseAIProvider`.
"""

from .base import BaseAIProvider, ImageAnalysis
from .cache import AICache
from .ratelimit import RateLimiter, RateLimitExceeded
from .mock import MockProvider
from .registry import build_provider

__all__ = [
    "BaseAIProvider",
    "ImageAnalysis",
    "AICache",
    "RateLimiter",
    "RateLimitExceeded",
    "MockProvider",
    "build_provider",
]
