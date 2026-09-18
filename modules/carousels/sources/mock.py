"""Mock source resolver (Phase 2) — for ``dry_run`` and tests.

Honest by construction: with no fixture it returns an **empty** context flagged
with a warning, so a dry run can never put invented facts on a slide. Tests and
UI previews pass a fixture context when they want to see the pipeline move.
"""

import json
from typing import Optional

from core.models import CarouselSourceContext, CarouselSourceType, CarouselVertical

from ..enums import resolve_source_type
from .base import BaseSourceResolver, ResolveOptions, merge_warnings

MOCK_WARNING = (
    "dry-run: the source was not fetched (mock resolver) — no facts were invented"
)


class MockSourceResolver(BaseSourceResolver):
    """Deterministic resolver: fixture when given, empty context otherwise."""

    name = "mock"
    source_types = frozenset(CarouselSourceType)

    def __init__(
        self,
        *,
        context: Optional[CarouselSourceContext] = None,
        source_type: CarouselSourceType = CarouselSourceType.URL,
        vertical: CarouselVertical = CarouselVertical.HYBRID,
    ) -> None:
        self._context = context
        self.source_type = resolve_source_type(source_type)
        self.vertical = vertical

    async def resolve(
        self, source_ref: str, options: Optional[ResolveOptions] = None
    ) -> CarouselSourceContext:
        """Return the fixture, or a clearly-labelled empty context."""
        options = options or ResolveOptions()
        if self._context is not None:
            context = self._context.model_copy(deep=True)
            if not context.source_url:
                context.source_url = source_ref
            context.warnings = merge_warnings(context.warnings, [MOCK_WARNING])
            return context
        return CarouselSourceContext(
            source_type=options.source_type or self.source_type,
            vertical=options.vertical or self.vertical,
            source_url=source_ref,
            external_id="",
            title="",
            summary="",
            facts=[],
            sourced_facts=[],
            quotes=[],
            code_snippets=[],
            metrics=[],
            images=[],
            tags=[],
            confidence=0.0,
            warnings=[MOCK_WARNING],
            raw_payload_json=json.dumps(
                {"mock": True, "source_ref": source_ref}, ensure_ascii=False
            ),
        )


__all__ = ["MOCK_WARNING", "MockSourceResolver"]
