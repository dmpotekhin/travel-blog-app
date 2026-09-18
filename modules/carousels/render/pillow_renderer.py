"""Pillow renderer (Phase 4): 768x1376 JPG with a deterministic text overlay.

The renderer never invents content and never lets a model touch text: it paints
what :class:`modules.carousels.narrative.SlidePlanner` decided, on a background
that is either painted here or supplied by an image provider.

Two rules are enforced in code, because they are platform requirements:

* every text block stops above ``height - safe_zone_pixels`` (the bottom 20%
  of a TikTok frame is covered by the app's own UI);
* the file is always baseline JPEG — TikTok rejects PNG.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger
from PIL import Image, ImageDraw

from core.models import CarouselSlide, CarouselSourceContext

from ..vertical_profiles import DEFAULT_ACCENTS, VerticalProfile
from .base import BackgroundResult, BaseCarouselImageProvider, BaseSlideRenderer, RenderedSlide, TextBox
from .fonts import FontBook

HEADLINE_SIZE = 64
SUBHEADLINE_SIZE = 36
BODY_SIZE = 32
BULLET_SIZE = 32
CODE_SIZE = 24
METRIC_SIZE = 30
BADGE_SIZE = 22
#: Below this a slide is unreadable on a phone (verification re-checks it).
MIN_FONT_SIZE = 20

TEXT_COLORS = {
    "headline": "#FFFFFF",
    "subheadline": "#E5E7EB",
    "body": "#D1D5DB",
    "bullet": "#E5E7EB",
    "code": "#A7F3D0",
    "metric": "#FFFFFF",
    "badge": "#9CA3AF",
}
CODE_PANEL = "#0B1120"


class PillowSlideRenderer(BaseSlideRenderer):
    """Paints a planned slide into a JPEG and reports the layout it used."""

    name = "pillow"
    image_format = "jpeg"

    def __init__(
        self,
        *,
        quality: int = 88,
        fonts: Optional[FontBook] = None,
        background_provider: Optional[BaseCarouselImageProvider] = None,
    ) -> None:
        self.quality = quality
        self.fonts = fonts or FontBook()
        self.background_provider = background_provider
        self._last_background: BackgroundResult = BackgroundResult()

    # -- public API ----------------------------------------------------

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
        """Await the background provider, then paint (this is what the service calls)."""
        destination = Path(destination)
        background = BackgroundResult()
        if self.background_provider is not None:
            background = await self.background_provider.background(
                slide=slide,
                profile=profile,
                width=width,
                height=height,
                context=context,
                destination_dir=destination.parent,
            )
        self._last_background = background
        return self._paint(
            slide,
            profile=profile,
            width=width,
            height=height,
            safe_zone_pixels=safe_zone_pixels,
            destination=destination,
            context=context,
            attempt=attempt,
            background=background,
        )

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
        """Synchronous paint (offline use): reuses the last background if any.

        A remote provider cannot be awaited here, so the canvas falls back to
        the painted gradient — recorded as a warning, never silently.
        """
        return self._paint(
            slide,
            profile=profile,
            width=width,
            height=height,
            safe_zone_pixels=safe_zone_pixels,
            destination=Path(destination),
            context=context,
            attempt=attempt,
            background=self._last_background,
        )

    # -- painting ------------------------------------------------------

    def _paint(
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
        background: Optional[BackgroundResult] = None,
    ) -> RenderedSlide:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        warnings: List[str] = list(self.fonts.warnings)

        accent = slide.accent_color or DEFAULT_ACCENTS.get(profile.vertical, "#0F172A")
        canvas, background = self._canvas(
            slide, profile, width, height, destination.parent, warnings, background=background
        )
        draw = ImageDraw.Draw(canvas, "RGBA")

        margin = int(width * 0.09)
        content_width = width - margin * 2
        limit = height - safe_zone_pixels
        boxes: List[TextBox] = []

        # Accent bar + slide badge (neither carries a claim).
        draw.rectangle([0, 0, width, max(8, height // 180)], fill=accent)
        badge_font, _ = self.fonts.load(BADGE_SIZE, bold=False)
        badge = f"{slide.order}/{slide.order and 6}"
        draw.text((margin, int(height * 0.035)), badge, font=badge_font, fill=TEXT_COLORS["badge"])
        boxes.append(
            TextBox(
                kind="badge",
                text=badge,
                x=margin,
                y=int(height * 0.035),
                width=int(draw.textlength(badge, font=badge_font)),
                height=BADGE_SIZE + 4,
                font_size=BADGE_SIZE,
            )
        )

        cursor = int(height * 0.10)
        cursor = self._block(
            draw, boxes, slide.headline or slide.body_text or "",
            kind="headline", font_size=HEADLINE_SIZE, bold=True, margin=margin,
            y=cursor, max_width=content_width, limit=limit, warnings=warnings,
            attempt=attempt, max_lines=4,
        )

        if slide.subheadline:
            cursor = self._block(
                draw, boxes, slide.subheadline, kind="subheadline", font_size=SUBHEADLINE_SIZE,
                bold=False, margin=margin, y=cursor + 16, max_width=content_width, limit=limit,
                warnings=warnings, attempt=attempt, max_lines=3,
            )

        if slide.bullets:
            for bullet in slide.bullets:
                cursor = self._block(
                    draw, boxes, f"• {bullet}", kind="bullet", font_size=BULLET_SIZE, bold=False,
                    margin=margin, y=cursor + 10, max_width=content_width, limit=limit,
                    warnings=warnings, attempt=attempt, max_lines=2, color=TEXT_COLORS["bullet"],
                )

        code = slide.code()
        if code is not None:
            cursor = self._code_block(
                draw, boxes, code.code, margin=margin, y=cursor + 20, max_width=content_width,
                limit=limit, warnings=warnings, attempt=attempt, truncated=code.truncated,
            )

        metrics = [metric for metric in slide.metrics() if metric.is_verified]
        for metric in metrics:
            label = f"{metric.name}: {metric.raw_value or metric.value} {metric.unit}".strip()
            cursor = self._block(
                draw, boxes, label, kind="metric", font_size=METRIC_SIZE, bold=True,
                margin=margin, y=cursor + 10, max_width=content_width, limit=limit,
                warnings=warnings, attempt=attempt, max_lines=1, color=accent,
            )

        if not any(box.kind in ("headline", "body", "bullet", "code", "metric") for box in boxes):
            warnings.append("slide had no content to draw")
        if slide.body_text and not slide.bullets and code is None:
            cursor = self._block(
                draw, boxes, slide.body_text, kind="body", font_size=BODY_SIZE, bold=False,
                margin=margin, y=cursor + 16, max_width=content_width, limit=limit,
                warnings=warnings, attempt=attempt, max_lines=4, color=TEXT_COLORS["body"],
            )

        overflow = [box for box in boxes if box.bottom > limit]
        if overflow:
            warnings.append(
                f"{len(overflow)} text block(s) crossed the bottom safe zone — renderer bug"
            )

        rgb = canvas.convert("RGB")
        rgb.save(destination, format="JPEG", quality=self.quality, optimize=True)
        payload = destination.read_bytes()
        rendered = RenderedSlide(
            order=slide.order,
            path=str(destination),
            layout_path=str(destination.with_suffix(".layout.json")),
            width=rgb.width,
            height=rgb.height,
            image_format="jpeg",
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            provider=background.provider or "none",
            background_path=background.path,
            fonts=self.fonts.font_paths,
            text_boxes=boxes,
            warnings=list(dict.fromkeys(warnings)),
        )
        destination.with_suffix(".layout.json").write_text(
            json.dumps(rendered.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.debug(
            "rendered slide {} -> {} ({} text blocks, {} bytes)",
            slide.order,
            destination.name,
            len(boxes),
            rendered.byte_size,
        )
        return rendered

    # -- internals -----------------------------------------------------

    def _canvas(
        self,
        slide: CarouselSlide,
        profile: VerticalProfile,
        width: int,
        height: int,
        destination_dir: Path,
        warnings: List[str],
        background: Optional[BackgroundResult] = None,
    ) -> Tuple[Image.Image, BackgroundResult]:
        """Background image (provider) or painted gradient, darkened for contrast."""
        background = background or BackgroundResult()
        fallback_accent = slide.accent_color or DEFAULT_ACCENTS.get(profile.vertical, "#0F172A")
        if background.reason:
            warnings.append(f"background: {background.reason}")

        if background.available:
            try:
                base = Image.open(background.path).convert("RGB")
                base = _cover(base, width, height)
            except OSError as exc:
                warnings.append(f"background image unusable ({exc}) — painted gradient instead")
                base = _gradient(width, height, fallback_accent)
                background = BackgroundResult(provider="solid_gradient", reason="fallback paint")
        else:
            if self.background_provider is None and background.provider:
                warnings.append("no background provider wired — painted gradient used")
            base = _gradient(width, height, fallback_accent)

        # Contrast veil: the text must stay legible on any photo.
        veil = Image.new("RGBA", (width, height), (2, 6, 23, 150))
        return Image.alpha_composite(base.convert("RGBA"), veil), background

    def _block(
        self,
        draw: ImageDraw.ImageDraw,
        boxes: List[TextBox],
        text: str,
        *,
        kind: str,
        font_size: int,
        bold: bool,
        margin: int,
        y: int,
        max_width: int,
        limit: int,
        warnings: List[str],
        attempt: int = 0,
        max_lines: int = 3,
        color: str = "",
    ) -> int:
        """Draw a wrapped text block that always stops above the safe zone."""
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return y

        size = max(MIN_FONT_SIZE, font_size - min(attempt, 3) * 2)
        while True:
            font, _ = self.fonts.load(size, bold=bold)
            lines = _wrap(cleaned, font, draw, max_width)[:max_lines]
            line_height = int(size * 1.35)
            height = line_height * len(lines)
            if y + height <= limit or size <= MIN_FONT_SIZE:
                break
            size = max(MIN_FONT_SIZE, size - 2)

        if y + height > limit:
            keep = max(0, (limit - y) // line_height) if line_height else 0
            if keep < len(lines):
                warnings.append(f"{kind}: не все строки поместились над безопасной зоной")
            lines = lines[:keep]

        fill = color or TEXT_COLORS.get(kind, "#FFFFFF")
        for index, line in enumerate(lines):
            draw.text((margin, y + index * line_height), line, font=font, fill=fill)
        if lines:
            boxes.append(
                TextBox(
                    kind=kind,
                    text=" ".join(lines),
                    x=margin,
                    y=y,
                    width=max(int(draw.textlength(line, font=font)) for line in lines),
                    height=line_height * len(lines),
                    font_size=size,
                )
            )
        return y + (line_height * len(lines) if lines else 0)

    def _code_block(
        self,
        draw: ImageDraw.ImageDraw,
        boxes: List[TextBox],
        code: str,
        *,
        margin: int,
        y: int,
        max_width: int,
        limit: int,
        warnings: List[str],
        attempt: int = 0,
        truncated: bool = False,
    ) -> int:
        """Code is rendered verbatim (no wrapping that could change meaning)."""
        source_lines = [line for line in (code or "").splitlines() if line.strip()]
        if not source_lines:
            return y
        if truncated:
            warnings.append("code shown as a fragment (source block was truncated)")

        size = max(MIN_FONT_SIZE, CODE_SIZE - min(attempt, 3) * 2)
        line_height = int(size * 1.4)
        available = max(1, (limit - y - 24) // line_height)
        lines = source_lines[:available]
        if len(lines) < len(source_lines):
            warnings.append("code truncated to fit the frame")

        font, _ = self.fonts.load(size, mono=True)
        panel_bottom = y + line_height * len(lines) + 20
        draw.rectangle([margin - 12, y - 10, margin + max_width + 12, panel_bottom], fill=CODE_PANEL)
        for index, line in enumerate(lines):
            draw.text((margin, y + index * line_height), line, font=font, fill=TEXT_COLORS["code"])
        boxes.append(
            TextBox(
                kind="code",
                text="\n".join(lines),
                x=margin,
                y=y,
                width=max(int(draw.textlength(line, font=font)) for line in lines),
                height=line_height * len(lines),
                font_size=size,
            )
        )
        return panel_bottom


def _wrap(text: str, font, draw: ImageDraw.ImageDraw, max_width: int) -> List[str]:
    """Greedy word wrap against the real font metrics."""
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _gradient(width: int, height: int, accent: str) -> Image.Image:
    """Deterministic vertical gradient from the accent colour (dark at top)."""
    from .providers import _hex_to_rgb

    accent_rgb = _hex_to_rgb(accent)
    dark = tuple(max(int(channel * 0.14), 0) for channel in accent_rgb)
    light = tuple(int(channel * 0.55) for channel in accent_rgb)
    strip = Image.new("RGB", (1, height))
    pixels = strip.load()
    assert pixels is not None  # a freshly created RGB image always has one
    for y in range(height):
        ratio = y / max(height - 1, 1)
        pixels[0, y] = tuple(
            int(dark[index] + (light[index] - dark[index]) * ratio) for index in range(3)
        )
    return strip.resize((width, height))


def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """Center-crop to fill ``width x height`` without distorting the photo."""
    ratio = max(width / image.width, height / image.height)
    resized = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))))
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


__all__ = ["MIN_FONT_SIZE", "PillowSlideRenderer"]
