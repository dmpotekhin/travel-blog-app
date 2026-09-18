"""Carousel analytics (Phase 6): metrics in, score and learnings out."""

from __future__ import annotations

from typing import Any, Optional

from core.config import CarouselConfig
from modules.carousels.publishing.uploadpost import UploadPostPublisher

from .base import CAROUSEL_METRIC_NAMES, BaseMetricsCollector, PlatformMetrics
from .collectors import (
    MockMetricsCollector,
    UploadPostMetricsCollector,
    as_number,
    extract_metrics,
)
from .learnings import JobOutcome, LearningStore, Recommendation, cohort_for, latest_metrics_per_platform
from .scoring import (
    DEFAULT_WEIGHTS,
    ENGAGEMENT_METRICS,
    CarouselScore,
    cohort_views_baseline,
    engagement_rate,
    score_metrics,
)

__all__ = [
    "CAROUSEL_METRIC_NAMES",
    "DEFAULT_WEIGHTS",
    "ENGAGEMENT_METRICS",
    "BaseMetricsCollector",
    "CarouselScore",
    "JobOutcome",
    "LearningStore",
    "MockMetricsCollector",
    "PlatformMetrics",
    "Recommendation",
    "UploadPostMetricsCollector",
    "as_number",
    "cohort_for",
    "cohort_views_baseline",
    "collector_for",
    "engagement_rate",
    "extract_metrics",
    "latest_metrics_per_platform",
    "score_metrics",
]


def collector_for(
    settings: CarouselConfig,
    secrets: Any = None,
    *,
    fixtures: Optional[dict] = None,
    dry_run: Optional[bool] = None,
) -> BaseMetricsCollector:
    """Pick a metrics collector: the live provider, or the silent offline one.

    ``dry_run`` (the default) never touches the network and never fabricates a
    measurement — an offline collector reports nothing unless a fixture is passed.
    """
    use_dry_run = settings.dry_run if dry_run is None else dry_run
    upload = getattr(settings, "upload_post", None)
    token = getattr(secrets, "uploadpost_token", "") if secrets is not None else ""
    user = getattr(secrets, "uploadpost_user", "") if secrets is not None else ""
    if use_dry_run or not getattr(upload, "enabled", False) or not token or not user:
        return MockMetricsCollector(fixtures=fixtures)
    publisher = UploadPostPublisher(
        token=token,
        user=user,
        base_url=getattr(upload, "base_url", "https://api.upload-post.com"),
        timeout_seconds=getattr(upload, "timeout_seconds", 120),
        max_retries=getattr(upload, "max_retries", 2),
    )
    return UploadPostMetricsCollector(publisher)
