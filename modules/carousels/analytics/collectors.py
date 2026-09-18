"""Concrete metrics collectors: the live provider and an offline stand-in."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .base import CAROUSEL_METRIC_NAMES, BaseMetricsCollector, PlatformMetrics

_SHORTHAND = {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}


def as_number(value: Any) -> Optional[float]:
    """Convert a reported value to a float, or ``None`` if it is not a number.

    Booleans are not numbers here, and a shorthand like ``"1.2K"`` is expanded
    only because the provider documents that shorthand.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace(" ", "").replace(" ", "")
        if not text:
            return None
        multiplier = 1.0
        suffix = text[-1:].lower()
        if suffix in _SHORTHAND:
            multiplier = _SHORTHAND[suffix]
            text = text[:-1]
        try:
            return float(text) * multiplier
        except ValueError:
            return None
    return None


def extract_metrics(info: Dict[str, Any]) -> Tuple[Dict[str, float], Dict[str, str]]:
    """Split a provider result block into known metrics + their raw text."""
    metrics: Dict[str, float] = {}
    raw: Dict[str, str] = {}
    for key, value in info.items():
        name = str(key).strip().lower()
        if name not in CAROUSEL_METRIC_NAMES:
            continue
        number = as_number(value)
        if number is None:
            continue
        metrics[name] = number
        raw[name] = str(value)
    return metrics, raw


class UploadPostMetricsCollector(BaseMetricsCollector):
    """Reads the numbers Upload-Post reports for a finished upload.

    The wire source is the upload-status endpoint — the same per-platform result
    block the publisher polls. Fields that are missing or non-numeric are simply
    not metrics; an answer without numbers comes back empty on purpose.
    """

    name = "upload-post"

    def __init__(self, publisher: Any) -> None:
        self._publisher = publisher

    async def collect(self, publication: Any) -> Optional[PlatformMetrics]:
        request_id = getattr(publication, "request_id", "") or ""
        platform = getattr(publication, "platform", "") or ""
        if not request_id or not platform:
            return None
        report = await self._publisher.fetch_status(request_id)
        info = (report.platforms or {}).get(platform)
        if not isinstance(info, dict):
            return None
        metrics, raw = extract_metrics(info)
        return PlatformMetrics(
            platform=platform,
            metrics=metrics,
            raw_value=raw,
            source=self.name,
            raw_payload=info,
            note="" if metrics else "provider reported no metrics for this post yet",
        )

    async def aclose(self) -> None:
        await self._publisher.aclose()


class MockMetricsCollector(BaseMetricsCollector):
    """Offline collector: reports nothing unless a fixture was handed in.

    Deliberately silent by default — a fabricated baseline would poison every
    score and learning that reads from it.
    """

    name = "mock"

    def __init__(self, fixtures: Optional[Dict[str, Dict[str, float]]] = None) -> None:
        self._fixtures = dict(fixtures or {})

    async def collect(self, publication: Any) -> Optional[PlatformMetrics]:
        platform = getattr(publication, "platform", "") or ""
        fixture = self._fixtures.get(platform)
        if not fixture:
            return None
        return PlatformMetrics(
            platform=platform,
            metrics={name: float(value) for name, value in fixture.items()},
            raw_value={name: str(value) for name, value in fixture.items()},
            source=self.name,
            raw_payload={"fixture": True},
            note="fixture data, not a real measurement",
        )
