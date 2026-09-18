"""Analytics (Phase 6): what a platform reports, what we score, what we refuse.

The honesty rule under test: an empty provider answer stays empty. No zeros, no
averages, no "likely" numbers ever enter the metric store.
"""

import httpx
import pytest

from core.database import Database
from core.exceptions import CarouselError
from core.models import (
    CarouselJob,
    CarouselPublication,
    CarouselSourceType,
    CarouselVertical,
    PublicationStatus,
)
from modules.carousels.analytics import (
    BaseMetricsCollector,
    MockMetricsCollector,
    PlatformMetrics,
    UploadPostMetricsCollector,
    as_number,
    cohort_views_baseline,
    score_metrics,
)
from modules.carousels.publishing import UploadPostPublisher
from modules.carousels.service import CarouselFactory


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "analytics.db"))
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


class FakeCollector(BaseMetricsCollector):
    """Returns the metrics it was handed, per platform; nothing else."""

    name = "fake"

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def collect(self, publication):
        self.calls.append(publication.platform)
        metrics = self.payload.get(publication.platform)
        if metrics is None:
            return None
        return PlatformMetrics(
            platform=publication.platform,
            metrics=dict(metrics),
            raw_value={name: str(value) for name, value in metrics.items()},
            source=self.name,
            raw_payload={"fixture": True},
        )


async def _job_with_publications(db, statuses=("tiktok", "instagram")):
    job = await db.create_carousel_job(
        CarouselJob(
            vertical=CarouselVertical.TRAVEL,
            source_type=CarouselSourceType.URL,
            source_url="https://example.com/blog/china",
            title="Китай за 10 дней",
        )
    )
    for index, platform in enumerate(statuses):
        await db.save_carousel_publication(
            CarouselPublication(
                job_id=job.id,
                platform=platform,
                status=PublicationStatus.PUBLISHED,
                request_id=f"req-{index}",
                post_url=f"https://example.com/{platform}/1",
            )
        )
    return job


def test_as_number_expands_documented_shorthand_only():
    assert as_number("1.2K") == 1200.0
    assert as_number("3M") == 3_000_000.0
    assert as_number(7) == 7.0
    assert as_number(True) is None  # a flag is not a measurement
    assert as_number("—") is None
    assert as_number(None) is None


def make_publisher(handler):
    return UploadPostPublisher(
        token="t",
        user="u",
        base_url="https://api.upload-post.com",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.upload-post.com"
        ),
        backoff_seconds=0.0,
    )


async def test_upload_post_collector_reads_the_reported_numbers():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "results": [
                    {"platform": "tiktok", "success": True, "views": 12000, "likes": "1.2K", "shares": 40},
                    {"platform": "instagram", "success": True, "views": 9000, "saves": 250},
                ],
            },
        )

    publisher = make_publisher(handler)
    collector = UploadPostMetricsCollector(publisher)
    publication = CarouselPublication(job_id=1, platform="tiktok", request_id="req-x")

    measured = await collector.collect(publication)
    await publisher.aclose()

    assert measured is not None
    assert measured.metrics == {"views": 12000.0, "likes": 1200.0, "shares": 40.0}
    assert measured.raw_value["likes"] == "1.2K"
    assert measured.source == "upload-post"


async def test_upload_post_collector_keeps_an_empty_answer_empty():
    def handler(request):
        return httpx.Response(
            200, json={"success": True, "results": [{"platform": "tiktok", "success": True}]}
        )

    publisher = make_publisher(handler)
    collector = UploadPostMetricsCollector(publisher)
    measured = await collector.collect(
        CarouselPublication(job_id=1, platform="tiktok", request_id="req-y")
    )
    await publisher.aclose()

    assert measured is not None
    assert measured.is_empty() is True
    assert "no metrics" in measured.note


async def test_mock_collector_is_silent_without_a_fixture():
    collector = MockMetricsCollector()
    assert await collector.collect(CarouselPublication(job_id=1, platform="tiktok")) is None

    with_fixture = MockMetricsCollector({"tiktok": {"views": 10.0}})
    measured = await with_fixture.collect(CarouselPublication(job_id=1, platform="tiktok"))
    assert measured is not None and measured.metrics == {"views": 10.0}
    assert "fixture" in measured.note


async def test_collect_metrics_stores_one_row_per_reported_number(db, tmp_path):
    job = await _job_with_publications(db)
    carousel = CarouselFactory(db)
    collector = FakeCollector(
        {"tiktok": {"views": 12000.0, "likes": 1200.0}, "instagram": {"views": 9000.0}}
    )

    rows = await carousel.collect_metrics(job.id, collector=collector)

    assert sorted(collector.calls) == ["instagram", "tiktok"]
    assert sorted((row.platform, row.metric_name) for row in rows) == [
        ("instagram", "views"),
        ("tiktok", "likes"),
        ("tiktok", "views"),
    ]
    assert {row.source for row in rows} == {"fake"}
    stored = await carousel.list_metrics(job.id)
    assert len(stored) == 3
    assert (await carousel.get_job(job.id)).warnings == []


async def test_collect_metrics_warns_instead_of_inventing_numbers(db):
    job = await _job_with_publications(db)
    carousel = CarouselFactory(db)

    rows = await carousel.collect_metrics(job.id, collector=FakeCollector({}))

    assert rows == []
    warnings = (await carousel.get_job(job.id)).warnings
    assert any("не выдумываются" in warning for warning in warnings)


async def test_collect_metrics_needs_a_publication(db):
    carousel = CarouselFactory(db)
    job = await db.create_carousel_job(
        CarouselJob(vertical=CarouselVertical.QA, source_type=CarouselSourceType.URL, source_url="x")
    )
    with pytest.raises(CarouselError, match="публикаций"):
        await carousel.collect_metrics(job.id, collector=FakeCollector({}))


def test_score_uses_rates_and_says_so_without_a_cohort():
    score = score_metrics({"views": 1000.0, "likes": 100.0, "comments": 20.0})

    assert score.basis == "rates_only"
    assert score.components["likes"] == pytest.approx(0.1)
    assert "views" not in score.components  # weight redistributed over the rates
    assert any("cohort" in warning for warning in score.warnings)
    assert 0.0 <= score.score <= 1.0


def test_score_uses_the_cohort_median_when_peers_exist():
    score = score_metrics(
        {"views": 2000.0, "likes": 200.0},
        cohort_views=1000.0,
        cohort_size=5,
        min_sample_size=3,
    )

    assert score.basis == "cohort"
    assert score.components["views"] == pytest.approx(2.0)  # 2000 / 1000
    assert score.warnings == []


def test_rejection_penalty_lowers_the_score():
    plain = score_metrics({"views": 1000.0, "likes": 100.0}, cohort_views=1000.0, cohort_size=5)
    rejected = score_metrics(
        {"views": 1000.0, "likes": 100.0},
        cohort_views=1000.0,
        cohort_size=5,
        approved=False,
        rejected=True,
    )

    assert rejected.score < plain.score
    assert rejected.components["approval"] == 0.0


def test_cohort_baseline_is_the_median_and_none_when_empty():
    assert cohort_views_baseline([100.0, 300.0, 200.0]) == 200.0
    assert cohort_views_baseline([]) is None
    assert cohort_views_baseline([0.0, 0.0]) is None


async def test_scheduler_tick_reports_the_carousel_step(db):
    from core.config import Config
    from modules.scheduler import Scheduler

    report = await Scheduler(db, Config()).sync_carousels()

    # nothing published yet -> nothing asked, nothing learned, nothing invented
    assert report == {"collected": 0, "learnings": 0}


async def test_score_job_needs_collected_metrics(db):
    job = await _job_with_publications(db)
    carousel = CarouselFactory(db)

    with pytest.raises(CarouselError, match="метрик"):
        await carousel.score_job(job.id)


async def test_score_job_returns_an_explainable_score(db):
    job = await _job_with_publications(db)
    carousel = CarouselFactory(db)
    await carousel.collect_metrics(
        job.id, collector=FakeCollector({"tiktok": {"views": 1000.0, "likes": 150.0}})
    )

    score = await carousel.score_job(job.id)

    assert 0.0 <= score.score <= 1.0
    assert score.components["likes"] == pytest.approx(0.15)
    assert "likes=" in score.explain()
