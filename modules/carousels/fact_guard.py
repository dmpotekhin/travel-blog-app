"""Fact guard (Phase 3): the honesty layer between source and slides.

Every headline, bullet, code block and metric that reaches a slide must trace
back to something the resolver actually read. Nothing here uses a model: it is
plain normalised substring containment, so it is deterministic and explainable
in a code review.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

from core.models import CarouselSourceContext

_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Case/punctuation-insensitive form used for containment checks."""
    lowered = (text or "").lower().replace("ё", "е")
    return _WHITESPACE.sub(" ", _NON_WORD.sub(" ", lowered)).strip()


class FactGuard:
    """Answers one question: does the source actually say this?"""

    def __init__(self, context: CarouselSourceContext) -> None:
        self.context = context
        self._sources: Tuple[str, ...] = tuple(self._collect(context))
        self._normalized: Tuple[str, ...] = tuple(
            normalized for normalized in (normalize(source) for source in self._sources) if normalized
        )

    @staticmethod
    def _collect(context: CarouselSourceContext) -> List[str]:
        """Everything the source literally said, in one flat list."""
        sources: List[str] = [context.title, context.summary]
        sources.extend(context.facts)
        for fact in context.sourced_facts:
            sources.append(fact.text)
            sources.append(fact.source_excerpt)
        sources.extend(context.quotes)
        for snippet in context.code_snippets:
            sources.append(snippet.source_excerpt)
        for metric in context.metrics:
            sources.append(metric.source_excerpt)
        for image in context.images:
            sources.append(image.alt_text)
        return [source for source in sources if source and source.strip()]

    @property
    def sources(self) -> Tuple[str, ...]:
        """Read-only view of the material a claim may be checked against."""
        return self._sources

    def is_supported(self, claim: str) -> bool:
        """True when the claim is contained in something the source said."""
        needle = normalize(claim)
        if not needle:
            return False
        return any(needle in source for source in self._normalized)

    def unsupported_claims(self, claims: Iterable[str]) -> List[str]:
        """The subset of ``claims`` the source does not back (verbatim order)."""
        return [claim for claim in claims if claim and not self.is_supported(claim)]

    def split(self, claims: Iterable[str]) -> Tuple[List[str], List[str]]:
        """``(supported, unsupported)`` — the planner drops the second list."""
        supported: List[str] = []
        unsupported: List[str] = []
        for claim in claims:
            if not claim:
                continue
            (supported if self.is_supported(claim) else unsupported).append(claim)
        return supported, unsupported


__all__ = ["FactGuard", "normalize"]
