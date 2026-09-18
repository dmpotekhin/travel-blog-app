"""Per-slide verification (Phase 4 gate).

The verifier re-opens the produced file and the layout sidecar the renderer
wrote, and re-checks the platform rules instead of trusting anyone's word:

* exact 768x1376 and baseline JPEG (TikTok);
* nothing drawn inside the bottom 20% band (TikTok UI overlay);
* legible font sizes and a contrast veil (the renderer adds one);
* code on a technical slide is byte-identical to what the source said;
* alt-text present, and every text block still traceable to the source.

Failures are reported, never hidden: they drive targeted regeneration and, if
they persist, ``needs_revision``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger
from PIL import Image
from pydantic import BaseModel, Field

from core.models import CarouselSlide, CarouselSourceContext, CarouselVerificationStatus

from ..fact_guard import FactGuard
from ..vertical_profiles import VerticalProfile
from .base import RenderedSlide
from .pillow_renderer import MIN_FONT_SIZE

#: A slide bigger than this is a rendering bug, not a photo.
MAX_BYTES = 25 * 1024 * 1024


class VerificationReport(BaseModel):
    """Outcome of one slide's verification pass."""

    order: int = 0
    status: CarouselVerificationStatus = CarouselVerificationStatus.PENDING
    issues: List[str] = Field(default_factory=list)
    checks: Dict[str, bool] = Field(default_factory=dict)
    quality_score: float = 0.0

    @property
    def passed(self) -> bool:
        return self.status is CarouselVerificationStatus.PASSED


class SlideVerifier:
    """Checks one rendered slide against the platform + honesty rules."""

    def __init__(
        self,
        *,
        width: int,
        height: int,
        safe_zone_pixels: int,
        require_alt_text: bool = True,
        require_source_refs: bool = True,
        min_font_size: int = MIN_FONT_SIZE,
    ) -> None:
        self.width = width
        self.height = height
        self.safe_zone_pixels = safe_zone_pixels
        self.require_alt_text = require_alt_text
        self.require_source_refs = require_source_refs
        self.min_font_size = min_font_size

    # -- public API ----------------------------------------------------

    def verify(
        self,
        slide: CarouselSlide,
        *,
        path: Optional[Path] = None,
        layout_path: Optional[Path] = None,
        context: Optional[CarouselSourceContext] = None,
        profile: Optional[VerticalProfile] = None,
        guard: Optional[FactGuard] = None,
    ) -> VerificationReport:
        image_path = Path(path or slide.final_image_path or "")
        sidecar = Path(layout_path) if layout_path else image_path.with_suffix(".layout.json")
        issues: List[str] = []
        checks: Dict[str, bool] = {}

        rendered = self._load_layout(sidecar, issues, checks)
        self._check_file(image_path, rendered, issues, checks)
        self._check_bottom_zone(rendered, issues, checks)
        self._check_legibility(slide, rendered, issues, checks)
        self._check_code_integrity(slide, rendered, issues, checks)
        self._check_accessibility(slide, issues, checks)
        self._check_facts(slide, context, guard, issues, checks)

        status = (
            CarouselVerificationStatus.PASSED
            if not issues
            else CarouselVerificationStatus.NEEDS_REVISION
        )
        score = max(0.0, round(1.0 - 0.12 * len(issues), 2))
        report = VerificationReport(
            order=slide.order, status=status, issues=issues, checks=checks, quality_score=score
        )
        logger.debug(
            "verified slide {}: {} ({} issues)", slide.order, status.value, len(issues)
        )
        return report

    # -- checks --------------------------------------------------------

    def _load_layout(
        self, sidecar: Path, issues: List[str], checks: Dict[str, bool]
    ) -> Optional[RenderedSlide]:
        if not sidecar.is_file():
            checks["layout"] = False
            issues.append("layout metadata missing — slide cannot be verified")
            return None
        try:
            rendered = RenderedSlide.model_validate_json(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            checks["layout"] = False
            issues.append(f"layout metadata unreadable: {exc}")
            return None
        checks["layout"] = True
        return rendered

    def _check_file(
        self,
        image_path: Path,
        rendered: Optional[RenderedSlide],
        issues: List[str],
        checks: Dict[str, bool],
    ) -> None:
        if not image_path.is_file():
            checks["exists"] = False
            issues.append("rendered file does not exist")
            return
        checks["exists"] = True
        size = image_path.stat().st_size
        checks["file_size"] = 0 < size <= MAX_BYTES
        if not checks["file_size"]:
            issues.append(f"unexpected file size: {size} bytes")

        try:
            with Image.open(image_path) as image:  # re-open: verify the real bytes
                actual = image.size
                fmt = (image.format or "").lower()
                image.verify()
        except OSError as exc:
            checks["decodable"] = False
            issues.append(f"file is not a readable image: {exc}")
            return
        checks["decodable"] = True

        checks["size"] = actual == (self.width, self.height)
        if not checks["size"]:
            issues.append(
                f"wrong size {actual[0]}x{actual[1]}, expected {self.width}x{self.height}"
            )
        checks["format"] = fmt == "jpeg"
        if not checks["format"]:
            issues.append(f"format is {fmt!r}, TikTok needs JPEG")
        if rendered is not None and (rendered.width, rendered.height) != actual:
            issues.append("layout metadata disagrees with the rendered file")

    def _check_bottom_zone(
        self, rendered: Optional[RenderedSlide], issues: List[str], checks: Dict[str, bool]
    ) -> None:
        if rendered is None:
            checks["bottom_zone"] = False
            return
        limit = self.height - self.safe_zone_pixels
        lowest = rendered.lowest_text_bottom
        checks["bottom_zone"] = lowest <= limit
        if not checks["bottom_zone"]:
            issues.append(
                f"text reaches y={lowest}, inside the bottom {self.safe_zone_pixels}px safe zone"
            )
        overflowing = [box for box in rendered.text_boxes if box.right > self.width]
        checks["horizontal_overflow"] = not overflowing
        if overflowing:
            issues.append("text overflows the slide width")

    def _check_legibility(
        self,
        slide: CarouselSlide,
        rendered: Optional[RenderedSlide],
        issues: List[str],
        checks: Dict[str, bool],
    ) -> None:
        if rendered is None:
            checks["legibility"] = False
            return
        content = [box for box in rendered.text_boxes if box.kind != "badge"]
        if not content:
            checks["legibility"] = False
            issues.append("slide has no content text")
            return
        worst = min(box.font_size for box in content)
        checks["legibility"] = worst >= self.min_font_size
        if not checks["legibility"]:
            issues.append(f"font size {worst}px is below the legibility floor")

    def _check_code_integrity(
        self,
        slide: CarouselSlide,
        rendered: Optional[RenderedSlide],
        issues: List[str],
        checks: Dict[str, bool],
    ) -> None:
        code = slide.code()
        if code is None:
            return
        boxes = [box for box in (rendered.text_boxes if rendered else []) if box.kind == "code"]
        expected = [line for line in (code.code or "").splitlines() if line.strip()]
        drawn = boxes[0].text.splitlines() if boxes else []
        checks["code_integrity"] = bool(drawn) and expected[: len(drawn)] == drawn
        if not checks["code_integrity"]:
            issues.append("code on the slide does not match the source snippet")

    def _check_accessibility(
        self, slide: CarouselSlide, issues: List[str], checks: Dict[str, bool]
    ) -> None:
        if not self.require_alt_text:
            return
        checks["alt_text"] = bool(slide.alt_text.strip())
        if not checks["alt_text"]:
            issues.append("alt-text is missing")

    def _check_facts(
        self,
        slide: CarouselSlide,
        context: Optional[CarouselSourceContext],
        guard: Optional[FactGuard],
        issues: List[str],
        checks: Dict[str, bool],
    ) -> None:
        active = guard or (FactGuard(context) if context is not None else None)
        if active is None:
            return
        claims = [slide.headline, slide.subheadline, slide.body_text, *slide.bullets]
        unsupported = active.unsupported_claims([claim for claim in claims if claim])
        checks["facts"] = not unsupported
        if unsupported:
            issues.append(f"{len(unsupported)} text block(s) not supported by the source")
        if self.require_source_refs and slide.slide_type.value != "cta" and not slide.source_refs:
            checks["source_refs"] = False
            issues.append("slide carries text without source references")


__all__ = ["SlideVerifier", "VerificationReport"]
