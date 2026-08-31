"""AI provider abstraction (section 25-27).

Every AI backend implements ``BaseAIProvider``. Concrete providers live in
``modules/ai/*.py``; ``registry.py`` picks one by name (gemini / deepseek / mock).

Providers deliberately keep the *only* network (or mock) call inside
``_analyze_image_raw`` / ``_generate_text_raw``; caching (``cache.py``) and
request-rate limiting (``ratelimit.py``) wrap those calls so no provider code
duplicates the logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel, Field

from core.config import Config, get_secrets
from core.database import Database


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
