"""Composite carousel score — deterministic, explainable, no magic constants.

The score is a weighted sum of *rates*, not absolute counts: likes-per-view is
comparable between a small account and a large one, raw view counts are not.
When a cohort baseline exists (>= ``min_sample_size`` published carousels of the
same vertical) the view count is measured against the cohort's median; without a
baseline the views weight is redistributed across the engagement rates and the
redistribution is reported in ``warnings`` instead of hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Dict, List, Mapping, Optional, Sequence

ENGAGEMENT_METRICS = ("likes", "comments", "shares", "saves")

DEFAULT_WEIGHTS: Dict[str, float] = {
    "views": 0.20,
    "likes": 0.25,
    "comments": 0.20,
    "shares": 0.15,
    "saves": 0.10,
    "approval": 0.05,
    "rejection_penalty": 0.05,
}


@dataclass
class CarouselScore:
    """A score plus the components that produced it (auditable by design)."""

    score: float = 0.0
    components: Dict[str, float] = field(default_factory=dict)
    basis: str = "rates_only"
    sample_size: int = 0
    warnings: List[str] = field(default_factory=list)

    def explain(self) -> str:
        parts = ", ".join(f"{name}={value:.3f}" for name, value in sorted(self.components.items()))
        return f"{self.score:.3f} ({self.basis}) [{parts}]"


def engagement_rate(metrics: Mapping[str, float]) -> float:
    """Interactions per view (0.0 when the view count is unknown)."""
    views = float(metrics.get("views", 0.0) or 0.0)
    if views <= 0:
        return 0.0
    return sum(float(metrics.get(name, 0.0) or 0.0) for name in ENGAGEMENT_METRICS) / views


def cohort_views_baseline(view_counts: Sequence[float]) -> Optional[float]:
    """Median views of comparable posts, or ``None`` when there is no cohort."""
    values = [float(value) for value in view_counts if float(value) > 0]
    if not values:
        return None
    return float(median(values))


def score_metrics(
    metrics: Mapping[str, float],
    weights: Optional[Mapping[str, float]] = None,
    *,
    cohort_views: Optional[float] = None,
    cohort_size: int = 0,
    min_sample_size: int = 3,
    approved: bool = True,
    rejected: bool = False,
) -> CarouselScore:
    """Weighted score in ``[0, 1]`` with the components that produced it."""
    active_weights = dict(weights or DEFAULT_WEIGHTS)
    warnings: List[str] = []
    views = float(metrics.get("views", 0.0) or 0.0)

    components: Dict[str, float] = {}
    for name in ENGAGEMENT_METRICS:
        components[name] = (
            float(metrics.get(name, 0.0) or 0.0) / views if views > 0 else 0.0
        )

    views_weight = float(active_weights.pop("views", 0.0))
    basis = "rates_only"
    if cohort_views and views > 0 and cohort_size >= max(1, min_sample_size):
        components["views"] = min(views / float(cohort_views), 2.0)
        active_weights["views"] = views_weight
        basis = "cohort"
    elif views_weight > 0:
        # no baseline yet: spread the views weight over the rates we do have
        share = views_weight / max(len(ENGAGEMENT_METRICS), 1)
        for name in ENGAGEMENT_METRICS:
            active_weights[name] = float(active_weights.get(name, 0.0)) + share
        warnings.append(
            "no cohort baseline yet (fewer than "
            f"{max(1, min_sample_size)} comparable carousels): the views weight was "
            "redistributed across engagement rates"
        )

    components["approval"] = 1.0 if approved else 0.0
    penalty = 1.0 if rejected else 0.0

    total_weight = sum(max(value, 0.0) for value in active_weights.values())
    if total_weight <= 0:
        return CarouselScore(
            score=0.0,
            components=components,
            basis=basis,
            sample_size=cohort_size,
            warnings=warnings + ["all score weights are zero — check config"],
        )

    weighted = sum(
        float(active_weights.get(name, 0.0)) * components.get(name, 0.0)
        for name in active_weights
    )
    weighted -= float(active_weights.get("rejection_penalty", 0.0) or 0.0) * penalty
    score = max(0.0, min(1.0, weighted / total_weight))
    return CarouselScore(
        score=score,
        components=components,
        basis=basis,
        sample_size=cohort_size,
        warnings=warnings,
    )
