"""Analytics abstraction: read the numbers back, invent none.

A platform reports whatever it reports. If a field is missing, it stays missing —
``PlatformMetrics.is_empty()`` is a legitimate answer and the service turns it
into a warning instead of a plausible-looking number.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

#: Metric names we understand. Anything else stays in ``raw_payload`` only.
CAROUSEL_METRIC_NAMES = (
    "views",
    "likes",
    "comments",
    "shares",
    "saves",
    "reach",
    "impressions",
    "follows",
    "watch_time_ms",
)


@dataclass
class PlatformMetrics:
    """What one platform said about one published carousel."""

    platform: str
    metrics: Dict[str, float] = field(default_factory=dict)
    raw_value: Dict[str, str] = field(default_factory=dict)
    source: str = ""
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def is_empty(self) -> bool:
        return not self.metrics

    def as_metric_pairs(self) -> list[tuple[str, float, str]]:
        """``(name, value, raw_text)`` triples, sorted for determinism."""
        return [
            (name, self.metrics[name], self.raw_value.get(name, str(self.metrics[name])))
            for name in sorted(self.metrics)
        ]


class BaseMetricsCollector(ABC):
    """Pulls metrics for a publication. One implementation per provider."""

    name = "base"

    @abstractmethod
    async def collect(self, publication: Any) -> Optional[PlatformMetrics]:
        """Return the reported metrics, or ``None`` when the provider has none."""

    async def aclose(self) -> None:
        return None
