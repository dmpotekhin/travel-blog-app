"""Learnings store: published results -> advice the next carousel can use.

Aggregation rules (deliberately boring, no ML, no invented numbers):

* only ``published`` jobs with collected metrics take part;
* a metric that was collected twice keeps the newest value per (platform, metric);
* every learning row is ``scope_type`` + ``scope_value`` + ``metric_name`` with the
  group mean, the group size and a confidence of ``min(1, n / min_sample_size)``;
* non-vertical scopes store ``"<vertical>/<value>"`` as ``scope_value`` so a hook
  or a posting hour can be learned *per vertical* without a schema change;
* a group smaller than ``min_sample_size`` is still written (it is a real
  observation) but is never served as a recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean
from typing import Dict, List, Optional, Sequence, Tuple

from core.models import (
    CarouselJob,
    CarouselLearning,
    CarouselLearningScope,
    CarouselMetric,
)

from .scoring import (
    ENGAGEMENT_METRICS,
    cohort_views_baseline,
    engagement_rate,
    score_metrics,
)

#: metric_name written for each group
LEARNED_METRICS = ("carousel_score", "engagement_rate") + ENGAGEMENT_METRICS + ("views",)

#: scopes that are aggregated per vertical (``"<vertical>/<value>"``)
PER_VERTICAL_SCOPES = (
    CarouselLearningScope.HOOK_CATEGORY,
    CarouselLearningScope.PLATFORM,
    CarouselLearningScope.POSTING_TIME,
    CarouselLearningScope.DAY_OF_WEEK,
)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class JobOutcome:
    """One published carousel reduced to the numbers the store aggregates."""

    job_id: int
    vertical: str
    source_type: str
    platform: str
    hook_category: str
    metrics: Dict[str, float] = field(default_factory=dict)
    collected_at: Optional[datetime] = None
    score: float = 0.0
    score_basis: str = "rates_only"


@dataclass
class Recommendation:
    """A ranked suggestion with the evidence attached."""

    scope_type: str
    scope_value: str
    metric_name: str
    metric_value: float
    sample_size: int
    confidence: float
    rationale: str = ""


def latest_metrics_per_platform(
    rows: Sequence[CarouselMetric],
) -> Tuple[Dict[str, float], Optional[datetime]]:
    """Merge collected rows: newest value per (platform, metric_name).

    Count-like numbers are **summed across platforms** (this carousel reached that
    many people in total); ratio metrics keep the newest value. Both sides of
    ``engagement_rate`` scale the same way, so the rate stays comparable.
    """
    seen: Dict[Tuple[str, str], CarouselMetric] = {}
    for row in rows:
        key = (row.platform, row.metric_name)
        current = seen.get(key)
        stamp = row.collected_at
        if current is None or (stamp and current.collected_at and stamp > current.collected_at):
            seen[key] = row
    merged: Dict[str, float] = {}
    for (platform, name), row in seen.items():
        if name in ENGAGEMENT_METRICS or name in ("views", "reach", "impressions", "follows"):
            merged[name] = merged.get(name, 0.0) + float(row.metric_value)
        else:
            merged.setdefault(name, float(row.metric_value))
    newest = max((row.collected_at for row in rows if row.collected_at), default=None)
    return merged, newest


def cohort_for(
    outcomes: Sequence[JobOutcome], job_id: int
) -> Tuple[Optional[float], int]:
    """Peer cohort for one carousel: everyone else, never the carousel itself.

    Single source of truth for "what am I being compared against" — the score
    endpoint and the learnings refresh must agree, otherwise the same carousel
    would carry two different scores.
    """
    peers = [outcome for outcome in outcomes if outcome.job_id != job_id]
    return cohort_views_baseline([peer.metrics.get("views", 0.0) for peer in peers]), len(peers)


class LearningStore:
    """Reads job history, writes aggregated learnings, answers recommendations."""

    def __init__(self, db, *, min_sample_size: int = 3, limit: int = 200) -> None:
        self.db = db
        self.min_sample_size = max(1, int(min_sample_size))
        self.limit = int(limit)

    async def outcomes(self, *, vertical: Optional[str] = None) -> List[JobOutcome]:
        """Published carousels with their collected metrics, newest first."""
        jobs: List[CarouselJob] = await self.db.list_carousel_jobs(
            status="published", vertical=vertical, limit=self.limit
        )
        outcomes: List[JobOutcome] = []
        for job in jobs:
            metrics, collected_at = latest_metrics_per_platform(
                await self.db.list_carousel_metrics(job_id=job.id)
            )
            if not metrics:
                continue
            publications = await self.db.list_carousel_publications(job_id=job.id)
            hook = await self.db.get_selected_hook(job.id)
            outcomes.append(
                JobOutcome(
                    job_id=int(job.id or 0),
                    vertical=str(getattr(job.vertical, "value", job.vertical)),
                    source_type=str(getattr(job.source_type, "value", job.source_type)),
                    platform=publications[0].platform if publications else "",
                    hook_category=(hook.category if hook else ""),
                    metrics=metrics,
                    collected_at=collected_at,
                )
            )
        return outcomes

    async def refresh(
        self, *, vertical: Optional[str] = None, weights: Optional[Dict[str, float]] = None
    ) -> List[CarouselLearning]:
        """Recompute every learning row from the current history (idempotent)."""
        outcomes = await self.outcomes(vertical=vertical)
        if not outcomes:
            return []

        cohort_views = cohort_views_baseline(
            [outcome.metrics.get("views", 0.0) for outcome in outcomes]
        )
        groups: Dict[Tuple[CarouselLearningScope, str], List[Dict[str, float]]] = {}
        for outcome in outcomes:
            job_cohort_views, job_cohort_size = cohort_for(outcomes, outcome.job_id)
            score = score_metrics(
                outcome.metrics,
                weights,
                cohort_views=job_cohort_views,
                cohort_size=job_cohort_size,
                min_sample_size=self.min_sample_size,
            )
            outcome.score = score.score
            outcome.score_basis = score.basis
            sample = dict(outcome.metrics)
            sample["carousel_score"] = score.score
            sample["engagement_rate"] = engagement_rate(outcome.metrics)

            stamp = outcome.collected_at or datetime.min
            keys: List[Tuple[CarouselLearningScope, str]] = [
                (CarouselLearningScope.VERTICAL, outcome.vertical),
                (CarouselLearningScope.SOURCE_TYPE, outcome.source_type),
            ]
            if outcome.hook_category:
                keys.append(
                    (
                        CarouselLearningScope.HOOK_CATEGORY,
                        f"{outcome.vertical}/{outcome.hook_category}",
                    )
                )
            if outcome.platform:
                keys.append(
                    (CarouselLearningScope.PLATFORM, f"{outcome.vertical}/{outcome.platform}")
                )
            local_hour = stamp.hour if stamp != datetime.min else None
            if local_hour is not None:
                keys.append(
                    (
                        CarouselLearningScope.POSTING_TIME,
                        f"{outcome.vertical}/{local_hour:02d}",
                    )
                )
                keys.append(
                    (
                        CarouselLearningScope.DAY_OF_WEEK,
                        f"{outcome.vertical}/{WEEKDAYS[stamp.weekday()]}",
                    )
                )
            for key in keys:
                groups.setdefault(key, []).append(sample)

        written: List[CarouselLearning] = []
        for (scope_type, scope_value), samples in groups.items():
            size = len(samples)
            confidence = min(1.0, size / self.min_sample_size)
            available = [name for name in LEARNED_METRICS if any(name in s for s in samples)]
            for metric_name in available:
                values = [s[metric_name] for s in samples if metric_name in s]
                learning = CarouselLearning(
                    scope_type=scope_type,
                    scope_value=scope_value,
                    metric_name=metric_name,
                    metric_value=float(mean(values)),
                    sample_size=size,
                    confidence=confidence,
                )
                written.append(await self.db.upsert_carousel_learning(learning))
        return written

    async def recommendations(
        self,
        *,
        vertical: Optional[str] = None,
        metric_name: str = "carousel_score",
        limit: int = 5,
    ) -> List[Recommendation]:
        """Ranked advice; a group below ``min_sample_size`` is never served."""
        rows = await self.db.get_carousel_learnings(
            min_sample_size=self.min_sample_size, limit=max(50, self.limit)
        )
        prefix = f"{vertical}/" if vertical else ""
        picks: List[Recommendation] = []
        for row in rows:
            if row.metric_name != metric_name:
                continue
            scope_type = str(getattr(row.scope_type, "value", row.scope_type))
            if prefix:
                if not row.scope_value.startswith(prefix):
                    continue
                value = row.scope_value[len(prefix) :]
            else:
                value = row.scope_value
            picks.append(
                Recommendation(
                    scope_type=scope_type,
                    scope_value=value,
                    metric_name=row.metric_name,
                    metric_value=float(row.metric_value),
                    sample_size=int(row.sample_size),
                    confidence=float(row.confidence),
                    rationale=(
                        f"{scope_type}={value}: средний {metric_name} "
                        f"{row.metric_value:.3f} по {row.sample_size} каруселям "
                        f"(confidence {row.confidence:.2f})"
                    ),
                )
            )
        picks.sort(key=lambda item: (-item.metric_value, item.scope_value))
        return picks[: max(1, limit)]
