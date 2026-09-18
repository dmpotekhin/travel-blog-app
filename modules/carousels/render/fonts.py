"""Font discovery (Phase 4).

Fonts are the one thing the renderer cannot invent: a missing font must be
reported, and Cyrillic must actually be covered (the factory writes Russian
copy). Candidates are probed in order — macOS first, then Linux — and the
chosen file is recorded on every rendered slide for auditability.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple, Union

from loguru import logger
from PIL import ImageFont

#: Pillow returns either flavour depending on how the font was loaded.
AnyFont = Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]

#: Bold display font — headlines. Arial/DejaVu both cover Cyrillic.
BOLD_CANDIDATES: Tuple[str, ...] = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
)

REGULAR_CANDIDATES: Tuple[str, ...] = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
)

#: Monospace for code/terminal slides — verbatim text without layout surprises.
MONO_CANDIDATES: Tuple[str, ...] = (
    "/System/Library/Fonts/Supplemental/Andale Mono.ttf",
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
)


def first_existing(candidates: Tuple[str, ...]) -> Optional[str]:
    """First candidate that exists on this machine (``None`` when none do)."""
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


class FontBook:
    """Resolves and caches the fonts the renderer needs."""

    def __init__(
        self,
        *,
        bold: Optional[str] = None,
        regular: Optional[str] = None,
        mono: Optional[str] = None,
    ) -> None:
        self.bold_path = bold or first_existing(BOLD_CANDIDATES)
        self.regular_path = regular or first_existing(REGULAR_CANDIDATES) or self.bold_path
        self.mono_path = mono or first_existing(MONO_CANDIDATES) or self.regular_path
        self.warnings: List[str] = []
        if not self.bold_path:
            self.warnings.append(
                "no TrueType font found — falling back to PIL's bitmap font "
                "(Cyrillic will not render; install Arial or DejaVu)"
            )
            logger.warning("carousel renderer: no TrueType font available")

    @property
    def font_paths(self) -> List[str]:
        """Distinct font files actually used (for the audit trail)."""
        return list(dict.fromkeys(path for path in (self.bold_path, self.regular_path, self.mono_path) if path))

    def load(self, size: int, *, bold: bool = False, mono: bool = False) -> Tuple[AnyFont, str]:
        """Load a font at ``size``; returns ``(font, path)``."""
        path = self.mono_path if mono else (self.bold_path if bold else self.regular_path)
        if not path:
            return ImageFont.load_default(), ""
        try:
            return ImageFont.truetype(path, size=size), path
        except OSError as exc:  # pragma: no cover - depends on the host
            self.warnings.append(f"cannot load {path}: {exc}")
            return ImageFont.load_default(), ""


__all__ = [
    "BOLD_CANDIDATES",
    "MONO_CANDIDATES",
    "REGULAR_CANDIDATES",
    "FontBook",
    "first_existing",
]
