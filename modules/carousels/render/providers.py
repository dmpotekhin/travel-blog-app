"""Background providers (Phase 4).

Three honest options behind one interface:

* :class:`SolidGradientProvider` — deterministic paint from the accent colour
  (default; works offline, no API key, no model);
* :class:`SourceImageProvider` — only a real local file from the source, never
  a scraped remote image pretending to be the author's photo;
* :class:`GeminiBackgroundProvider` — background/illustration only. The prompt
  is style-only (no facts, no letters), and a missing key is reported, never
  faked.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import List, Optional

import httpx
from loguru import logger

from core.models import CarouselSlide, CarouselSourceContext

from ..vertical_profiles import DEFAULT_ACCENTS, VerticalProfile
from .base import BackgroundResult, BaseCarouselImageProvider

_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _hex_to_rgb(value: str, fallback: tuple = (15, 23, 42)) -> tuple:
    """``"#0F172A"`` -> ``(15, 23, 42)`` (fallback when the colour is unusable)."""
    raw = (value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(char * 2 for char in raw)
    if len(raw) != 6:
        return fallback
    try:
        return tuple(int(raw[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return fallback


class SolidGradientProvider(BaseCarouselImageProvider):
    """Deterministic vertical gradient — the offline default."""

    name = "solid_gradient"

    def __init__(self, *, top_shade: float = 0.55, bottom_shade: float = 0.12) -> None:
        self.top_shade = top_shade
        self.bottom_shade = bottom_shade

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
        from PIL import Image

        accent = _hex_to_rgb(slide.accent_color or DEFAULT_ACCENTS.get(profile.vertical, "#0F172A"))
        target_dir = destination_dir or Path(".")
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"bg_slide_{slide.order}_{slide.accent_color.strip('#') or 'default'}.jpg"

        top = tuple(max(int(channel * self.bottom_shade), 0) for channel in accent)
        bottom = tuple(int(channel * self.top_shade) for channel in accent)
        image = Image.new("RGB", (1, height))
        pixels = image.load()
        assert pixels is not None  # a freshly created RGB image always has one
        for y in range(height):
            ratio = y / max(height - 1, 1)
            pixels[0, y] = tuple(
                int(top[index] + (bottom[index] - top[index]) * ratio) for index in range(3)
            )
        image = image.resize((width, height))
        image.save(path, format="JPEG", quality=90)
        return BackgroundResult(path=str(path), provider=self.name, is_generated=False)


class SourceImageProvider(BaseCarouselImageProvider):
    """A photo that genuinely came from the source (local file only)."""

    name = "source_image"

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
        if context is None:
            return BackgroundResult(provider=self.name, reason="no source context — no photo to reuse")

        for image in context.images:
            candidate = Path(image.url_or_path)
            if candidate.is_file():
                logger.info("carousel background: reusing source photo {}", candidate)
                return BackgroundResult(
                    path=str(candidate), provider=self.name, is_generated=False
                )
        return BackgroundResult(
            provider=self.name,
            reason="source carries no local image file (remote URLs are not downloaded as photos)",
        )


class GeminiBackgroundProvider(BaseCarouselImageProvider):
    """Gemini-painted background (style only) — off without a key or client."""

    name = "gemini"

    def __init__(
        self,
        *,
        model: str = "gemini-3.1-flash-image-preview",
        api_key: str = "",
        client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: int = 60,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "") or os.environ.get(
            "GOOGLE_API_KEY", ""
        )
        self._client = client
        self.timeout_seconds = timeout_seconds

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
        if not self.api_key:
            return BackgroundResult(
                provider=self.name, reason="GEMINI_API_KEY is not set — no generated background"
            )
        if self._client is None:
            return BackgroundResult(
                provider=self.name, reason="no HTTP client wired for the Gemini provider"
            )
        if not slide.background_prompt:
            return BackgroundResult(
                provider=self.name, reason="slide has no style prompt — nothing to generate"
            )

        payload = {"contents": [{"parts": [{"text": slide.background_prompt}]}]}
        url = _GEMINI_ENDPOINT.format(model=self.model)
        try:
            response = await self._client.post(
                url, json=payload, headers={"x-goog-api-key": self.api_key}, timeout=self.timeout_seconds
            )
        except httpx.HTTPError as exc:
            logger.warning("gemini background failed: {}", exc)
            return BackgroundResult(provider=self.name, reason=f"Gemini request failed: {exc}")
        if response.status_code >= 400:
            return BackgroundResult(
                provider=self.name, reason=f"Gemini returned {response.status_code}"
            )

        data = response.json()
        blocks: List[dict] = []
        for candidate in data.get("candidates", []) or []:
            blocks.extend(candidate.get("content", {}).get("parts", []) or [])
        for block in blocks:
            inline = block.get("inlineData") or block.get("inline_data")
            if not inline:
                continue
            target_dir = destination_dir or Path(".")
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"gemini_slide_{slide.order}.jpg"
            path.write_bytes(base64.b64decode(inline.get("data", "")))
            return BackgroundResult(path=str(path), provider=self.name, is_generated=True)
        return BackgroundResult(provider=self.name, reason="Gemini response carried no image")


def provider_for(
    gemini_config: object,
    *,
    dry_run: bool,
    client: Optional[httpx.AsyncClient] = None,
) -> BaseCarouselImageProvider:
    """Pick the background provider: Gemini when explicitly allowed, else painted.

    ``dry_run`` never reaches out to Gemini, whatever the config says.
    """
    enabled = bool(getattr(gemini_config, "enabled", False))
    model = str(getattr(gemini_config, "model", "") or "gemini-3.1-flash-image-preview")
    if enabled and not dry_run and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        return GeminiBackgroundProvider(model=model, client=client)
    return SolidGradientProvider()


__all__ = [
    "GeminiBackgroundProvider",
    "SolidGradientProvider",
    "SourceImageProvider",
    "provider_for",
]
