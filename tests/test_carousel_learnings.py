"""Learnings store (Phase 6): aggregation, idempotency, honest recommendations."""

import pytest

from core.database import Database
from core.exceptions import CarouselError
from core.models import (
    CarouselHookCandidate,
    CarouselJob,
    CarouselLearning,
    CarouselLearningScope,
    CarouselMetric,
    CarouselPublication,
    CarouselSourceType,
    CarouselVertical,
    PublicationStatus,
)
from modules.carousels.analytics import cohort_for, cohort_views_baseline
from modules.carousels.service import CarouselFactory

#: the legal walk from a fresh job to a published one
TO_PUBLISHED = (
    "researching",
    "researched",
    "narrative_drafted",
    "slides_planned",
    "rendering",
    "rendered",
    "verifying",
    "verified",
    "approved",
    "publishing",
    "published",
)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "learnings.db"))
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _published_job(
    db,
    *,
    hook: str = "number_list",
    views: float = 1000.0,
    likes: float = 100.0,
    platform: str = "tiktok",
    vertical: CarouselVertical = CarouselVertical.TRAVEL,
):
    job = await db.create_carousel_job(
        CarouselJob(
            vertical=vertical,
            source_type=CarouselSourceType.URL,
            source_url="https://example.com/blog/china",
            title="Китай за 10 дней",
        )
    )
    for status in TO_PUBLISHED:
        await db.update_carousel_job_status(job.id, status)
    publication = await db.save_carousel_publication(
        CarouselPublication(
            job_id=job.id,
            platform=platform,
            status=PublicationStatus.PUBLISHED,
            request_id=f"req-{job.id}",
            post_url=f"https://example.com/{platform}/{job.id}",
        )
    )
    await db.save_hook_candidates(
        job.id,
        [
            CarouselHookCandidate(
                job_id=job.id, category=hook, text="Пекин, Сиань, Ченду", is_selected=True
            )
        ],
    )
    for name, value in (("views", views), ("likes", likes)):
        await db.save_carousel_metric(
            CarouselMetric(
                publication_id=publication.id,
                job_id=job.id,
                platform=platform,
                metric_name=name,
                metric_value=value,
                raw_value=str(value),
                source="test",
            )
        )
    return job


async def test_refresh_writes_learnings_for_every_scope(db):
    carousel = CarouselFactory(db)
    for _ in range(3):
        await _published_job(db)

    written = await carousel.refresh_learnings()

    by_key = {(str(row.scope_type.value), row.scope_value, row.metric_name): row for row in written}
    assert ("vertical", "travel", "carousel_score") in by_key
    assert ("hook_category", "travel/number_list", "carousel_score") in by_key
    assert ("source_type", "url", "views") in by_key

    hook_score = by_key[("hook_category", "travel/number_list", "carousel_score")]
    assert hook_score.sample_size == 3
    assert hook_score.confidence == pytest.approx(1.0)  # 3 / min_sample_size 3
    assert 0.0 <= hook_score.metric_value <= 1.0

    views = by_key[("vertical", "travel", "views")]
    assert views.metric_value == pytest.approx(1000.0)


async def test_refresh_is_idempotent(db):
    carousel = CarouselFactory(db)
    for _ in range(3):
        await _published_job(db)

    first = await carousel.refresh_learnings()
    second = await carousel.refresh_learnings()

    assert len(first) == len(second)
    stored = await db.get_carousel_learnings()
    assert len(stored) == len(first)


async def test_jobs_without_metrics_are_not_learned(db):
    carousel = CarouselFactory(db)
    job = await db.create_carousel_job(
        CarouselJob(
            vertical=CarouselVertical.QA,
            source_type=CarouselSourceType.URL,
            source_url="https://example.com/x",
        )
    )
    for status in TO_PUBLISHED:
        await db.update_carousel_job_status(job.id, status)

    assert await carousel.refresh_learnings() == []
    assert job.id is not None


async def test_recommendations_stay_empty_below_min_sample_size(db):
    carousel = CarouselFactory(db)
    await _published_job(db)
    await _published_job(db)
    await carousel.refresh_learnings()

    assert await carousel.recommendations(vertical="travel") == []


async def test_recommendations_rank_the_better_hook_first(db):
    carousel = CarouselFactory(db)
    # richer carousels use the number_list hook, weaker ones the mystery hook
    for _ in range(3):
        await _published_job(db, hook="number_list", views=5000.0, likes=900.0)
    for _ in range(3):
        await _published_job(db, hook="mystery", views=500.0, likes=10.0)

    await carousel.refresh_learnings()
    picks = await carousel.recommendations(vertical="travel")

    assert picks, "three samples per hook must be enough to recommend"
    hook_picks = [pick for pick in picks if pick.scope_type == "hook_category"]
    assert hook_picks, "the hook is what the next carousel actually chooses"
    assert hook_picks[0].scope_value == "number_list"
    assert hook_picks[0].sample_size >= 1
    assert "carousel_score" in hook_picks[0].rationale

    scored = {pick.scope_value: pick.metric_value for pick in hook_picks}
    assert scored["number_list"] > scored["mystery"]


async def test_recommendations_are_scoped_to_the_vertical(db):
    carousel = CarouselFactory(db)
    for _ in range(3):
        await _published_job(db, hook="number_list", vertical=CarouselVertical.QA)

    await carousel.refresh_learnings()
    picks = await carousel.recommendations(vertical="qa")

    assert any(pick.scope_value == "number_list" for pick in picks)
    assert all(not pick.scope_value.startswith("qa/") for pick in picks)


async def test_latest_metric_value_per_platform_wins(db):
    job = await _published_job(db, views=1000.0)
    publication = (await db.list_carousel_publications(job_id=job.id))[0]
    await db.save_carousel_metric(
        CarouselMetric(
            publication_id=publication.id,
            job_id=job.id,
            platform="tiktok",
            metric_name="views",
            metric_value=2500.0,
            raw_value="2.5K",
            source="test",
        )
    )

    carousel = CarouselFactory(db)
    outcomes = await carousel.learning_store().outcomes(vertical="travel")

    assert [outcome.metrics["views"] for outcome in outcomes] == [2500.0]


async def test_refresh_respects_the_config_switch(db):
    carousel = CarouselFactory(db)
    carousel.settings.learning.enabled = False

    with pytest.raises(CarouselError, match="Learnings"):
        await carousel.refresh_learnings()


def test_learning_handles_scope_enum_values():
    assert CarouselLearningScope.HOOK_CATEGORY.value == "hook_category"
    learning = CarouselLearning(scope_type=CarouselLearningScope.POSTING_TIME, scope_value="travel/09")
    assert learning.metric_name == ""


async def test_a_carousel_never_uses_its_own_numbers_as_the_baseline(db):
    """A post is never one of its own peers."""
    await _published_job(db, views=5000.0, likes=900.0)
    await _published_job(db, views=500.0, likes=10.0)
    await _published_job(db, views=200.0, likes=4.0)
    carousel = CarouselFactory(db)
    outcomes = await carousel.learning_store().outcomes(vertical="travel")

    for outcome in outcomes:
        peers = [peer for peer in outcomes if peer.job_id != outcome.job_id]
        cohort_views, cohort_size = cohort_for(outcomes, outcome.job_id)

        assert cohort_size == len(outcomes) - 1, "a post is never its own peer"
        assert cohort_views == cohort_views_baseline([peer.metrics["views"] for peer in peers])

    # including itself would drag the baseline towards the best post
    whole_store = cohort_views_baseline([outcome.metrics["views"] for outcome in outcomes])
    assert whole_store == pytest.approx(500.0)


async def test_refresh_stores_the_very_same_score_the_endpoint_reports(db):
    job_ids = [
        (await _published_job(db, views=5000.0, likes=900.0)).id,
        (await _published_job(db, views=500.0, likes=10.0)).id,
        (await _published_job(db, views=900.0, likes=90.0)).id,
    ]
    carousel = CarouselFactory(db)

    await carousel.refresh_learnings()
    rows = await db.get_carousel_learnings(
        scope_type=CarouselLearningScope.VERTICAL.value, scope_value="travel"
    )
    stored = {row.metric_name: row.metric_value for row in rows}
    direct = [(await carousel.score_job(job_id)).score for job_id in job_ids]

    # one score per carousel: the stored aggregate is exactly the mean of them
    assert stored["carousel_score"] == pytest.approx(sum(direct) / len(direct))
    assert stored["views"] == pytest.approx((5000.0 + 500.0 + 900.0) / 3)


async def test_scoped_learnings_filter_by_min_sample_size(db):
    await _published_job(db, views=1000.0, likes=100.0)
    await _published_job(db, views=1000.0, likes=100.0)
    carousel = CarouselFactory(db)

    await carousel.refresh_learnings()
    loose = await carousel.list_learnings(min_sample_size=1, limit=200)
    strict = await carousel.list_learnings(min_sample_size=3, limit=200)

    assert any(row.sample_size == 2 for row in loose)
    assert all(row.sample_size >= 3 for row in strict)
