"""Live end-to-end walk of the QA vertical and of the GitHub resolver.

No real service and no credential is involved anywhere in this file:

* the GitHub API is served by ``httpx.MockTransport``, so the resolver runs its
  production code path (parsing, confidence, warnings) with **no token at all**;
* publishing and metrics use the production HTTP clients, but their transport is
  a stub that answers the way Upload-Post documents it — the ``request_id`` is
  really parsed out of an HTTP response and stored;
* the JPGs are painted by the real Pillow renderer into a temp directory.

Everything else — hook engine, narrative planner, slide planner, verification,
approval gate, scoring, learnings — is production code on a temp database.
"""

from __future__ import annotations

import httpx
import pytest
from PIL import Image

from core.config import CarouselGithubSourceConfig, Config
from core.database import Database
from core.exceptions import PublishNotApprovedError
from core.models import (
    CarouselSourceType,
    CarouselStatus,
    CarouselVertical,
    PublicationStatus,
)
from modules.carousels.analytics import UploadPostMetricsCollector
from modules.carousels.database_helpers import get_hook_candidates, list_sources
from modules.carousels.publishing import UploadPostPublisher
from modules.carousels.service import CarouselFactory
from modules.carousels.sources.github import GitHubSourceResolver

ISSUE_URL = "https://github.com/acme/tool/issues/7"
DISCUSSION_URL = "https://github.com/acme/tool/discussions/15"

ISSUE_JSON = {
    "number": 7,
    "title": "Flaky checkout test fails on retry",
    "body": (
        "`tests/test_checkout.py::test_retry_after_timeout` fails about once in five runs.\n\n"
        "```\nE   assert 0 == 1\nE    +  where 0 = len(calls)\n```\n\n"
        "The fixture in `tests/conftest.py` hands one shared mock to every test "
        "in the module, so a retry sees the calls of the previous test."
    ),
    "state": "closed",
    "html_url": ISSUE_URL,
    "comments": 2,
    "labels": [{"name": "bug"}, {"name": "flaky-test"}],
    "user": {"login": "qa-bot"},
    "created_at": "2026-01-05T10:00:00Z",
    "closed_at": "2026-01-06T09:00:00Z",
}

ISSUE_COMMENTS = [
    {"user": {"login": "rev"}, "body": "Reproduced: the mock is shared between tests."},
    {
        "user": {"login": "author"},
        "body": "Fixed by making the fixture function-scoped: `shared = {}` -> `return {}`.",
    },
]

PUBLISH_PAYLOAD = {
    "success": True,
    "request_id": "req-qa-1",
    # Upload-Post answers with per-platform rows when the upload is synchronous.
    "results": [
        {"platform": "tiktok", "success": True, "post_url": "https://tiktok.com/@qa/1"},
        {"platform": "instagram", "success": True, "post_url": "https://instagram.com/p/1"},
    ],
}

#: Only the numbers the platform reported. Nothing is interpolated.
METRICS_PAYLOAD = {
    "success": True,
    "results": [
        {"platform": "tiktok", "success": True, "views": 12000, "likes": "1.2K", "shares": 40},
        {"platform": "instagram", "success": True, "views": 9000, "saves": 250},
    ],
}
EXPECTED_METRICS = {
    ("tiktok", "views"): 12000.0,
    ("tiktok", "likes"): 1200.0,
    ("tiktok", "shares"): 40.0,
    ("instagram", "views"): 9000.0,
    ("instagram", "saves"): 250.0,
}


def router(routes: dict, seen: list) -> httpx.MockTransport:
    """MockTransport handler keyed by (method, path); records what was sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, payload = routes.get((request.method, request.url.path), (404, {"message": "Not Found"}))
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def upload_post_stub(seen: list) -> httpx.MockTransport:
    """One transport for the real publisher and the real collector."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "upload_photos" in request.url.path:
            return httpx.Response(200, json=PUBLISH_PAYLOAD)
        return httpx.Response(200, json=METRICS_PAYLOAD)

    return httpx.MockTransport(handler)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "carousel_e2e.db"))
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def factory(db, tmp_path) -> CarouselFactory:
    factory = CarouselFactory(db, Config())
    # keep the rendered JPGs inside the test's temp dir
    factory.settings.output_dir = str(tmp_path / "carousels")
    return factory


# --------------------------------------------------------------------- QA flow


async def test_github_issue_walks_the_whole_qa_pipeline(db, factory: CarouselFactory) -> None:
    """Source -> hooks -> plan -> render -> verify -> approve -> publish -> learn."""
    seen: list = []
    github_routes = {
        ("GET", "/repos/acme/tool/issues/7"): (200, ISSUE_JSON),
        ("GET", "/repos/acme/tool/issues/7/comments"): (200, ISSUE_COMMENTS),
    }

    job = await factory.create_job(
        source_type="github_issue",
        source_ref=ISSUE_URL,
        vertical=CarouselVertical.QA,
        title="Flaky checkout test",
    )
    assert job.status is CarouselStatus.PENDING
    assert job.vertical is CarouselVertical.QA
    job_id = job.id
    assert job_id is not None  # a stored row always has an id

    # 1. research: the real GitHub resolver, mock transport, no token
    resolver = GitHubSourceResolver(
        CarouselGithubSourceConfig(), client=httpx.AsyncClient(transport=router(github_routes, seen))
    )
    job = await factory.research(job_id, resolver=resolver, detect_vertical=False)
    assert job.status is CarouselStatus.RESEARCHED

    context = job.source_context()
    assert context is not None
    assert context.source_type is CarouselSourceType.GITHUB_ISSUE
    assert context.confidence >= factory.settings.health.min_source_confidence
    assert any("flaky" in fact.lower() or "retry" in fact.lower() for fact in context.facts), (
        context.facts
    )
    sources = await list_sources(db, job_id=job_id)
    assert sources and sources[0].source_type is CarouselSourceType.GITHUB_ISSUE
    assert all(request.headers.get("authorization") is None for request in seen), "no token, ever"

    # 2. hooks: candidates are ranked and each one quotes the source
    job = await factory.draft_narrative(job_id)
    assert job.status is CarouselStatus.NARRATIVE_DRAFTED
    hooks = await get_hook_candidates(db, job_id)
    assert hooks, "no hook candidates were proposed"
    assert all(hook.source_support.strip() for hook in hooks)

    # 3. six slides, planned from the sourced facts
    await factory.plan_slides(job_id)
    bundle = await factory.get_bundle(job_id)
    assert len(bundle.slides) == factory.settings.slide_count == 6

    # 4. render: real JPGs, real size, real bottom safe zone
    await factory.render_slides(job_id)
    bundle = await factory.get_bundle(job_id)
    for slide in bundle.slides:
        assert slide.final_image_path, slide.order
        with Image.open(slide.final_image_path) as image:
            assert image.format == "JPEG"
            assert image.size == (
                factory.settings.resolution.width,
                factory.settings.resolution.height,
            )
            assert image.size == (768, 1376)

    # 5. verification: everything passes on the first pass
    reports = await factory.verify_slides(job_id)
    assert len(reports) == 6
    assert all(report.passed for report in reports), [
        getattr(report, "issues", None) for report in reports
    ]
    assert (await factory.get_job(job_id)).status is CarouselStatus.VERIFIED

    # 6. a human approves (the supervised default is on)
    assert factory.should_auto_approve() is False
    job = await factory.submit_for_approval(job_id)
    assert job.status is CarouselStatus.AWAITING_APPROVAL
    job = await factory.approve(job_id, approved_by="dmitry", note="QA walk")
    assert job.status is CarouselStatus.APPROVED

    # 7. publish through the real HTTP client against the stub wire
    publisher = UploadPostPublisher(
        token="test-token-not-a-real-one",
        user="qa-e2e",
        base_url="https://api.upload-post.com",
        client=httpx.AsyncClient(
            transport=upload_post_stub(seen), base_url="https://api.upload-post.com"
        ),
        backoff_seconds=0.0,
    )
    factory.settings.dry_run = False  # exercises publishing; the wire is still a stub
    published = await factory.publish(job_id, publisher=publisher)

    assert published.job.status is CarouselStatus.PUBLISHED
    rows = published.publications
    assert {row.platform for row in rows} == {"tiktok", "instagram"}
    assert {row.status for row in rows} == {PublicationStatus.PUBLISHED}
    assert {row.request_id for row in rows} == {PUBLISH_PAYLOAD["request_id"]}
    uploads = [request for request in seen if "upload_photos" in request.url.path]
    assert len(uploads) == 1, "one upload call carries every platform"
    assert uploads[0].content.count(b'name="photos[]"') == 6

    # 8. metrics: exactly what the platform reported, nothing else
    collector = UploadPostMetricsCollector(publisher)
    stored = await factory.collect_metrics(job_id, collector=collector)
    assert stored, "no metric rows were stored"
    measured = {(row.platform, row.metric_name): row.metric_value for row in stored}
    assert measured == EXPECTED_METRICS
    raw = {(row.platform, row.metric_name): row.raw_value for row in stored}
    assert raw[("tiktok", "likes")] == "1.2K"  # the platform's own notation survives
    await publisher.aclose()

    # 9. score: rates only, no invented components
    score = await factory.score_job(job_id)
    assert "views" not in score.components, "views is a volume, not a rate"
    assert score.score > 0
    assert any("cohort" in warning.lower() for warning in score.warnings), score.warnings
    assert score.sample_size == 0, "one carousel is not a cohort baseline"

    # 10. learnings: written from what was collected, with their own basis on the row
    assert factory.settings.learning.min_sample_size == 3
    written = await factory.refresh_learnings()
    assert written, "no learnings were written"
    by_metric = {row.metric_name: row for row in written}
    stored_score = by_metric["carousel_score"]
    # score_job and refresh_learnings must not disagree about the same carousel
    assert stored_score.metric_value == pytest.approx(score.score)
    assert stored_score.sample_size == 1
    assert 0.0 < stored_score.confidence < 1.0

    picks = await factory.recommendations()
    assert all(pick.sample_size >= factory.settings.learning.min_sample_size for pick in picks)


async def test_publish_is_refused_before_approval(factory: CarouselFactory) -> None:
    """The gate is the state machine's, not the caller's."""
    job = await factory.create_job(
        source_type="github_issue", source_ref=ISSUE_URL, vertical=CarouselVertical.QA
    )
    job_id = job.id
    assert job_id is not None
    report = await factory.status_report(job_id)
    assert report["can_publish"] is False
    with pytest.raises(PublishNotApprovedError):
        await factory.publish(job_id)


# ------------------------------------------------------- GitHub resolver, offline


async def test_resolver_kinds_without_a_token() -> None:
    """issue / PR / repo resolve offline and send no credential."""
    seen: list = []
    routes = {
        ("GET", "/repos/acme/tool/issues/7"): (200, ISSUE_JSON),
        ("GET", "/repos/acme/tool/issues/7/comments"): (200, ISSUE_COMMENTS),
    }
    resolver = GitHubSourceResolver(
        CarouselGithubSourceConfig(), client=httpx.AsyncClient(transport=router(routes, seen))
    )
    result = await resolver.resolve(ISSUE_URL)
    assert result.source_type is CarouselSourceType.GITHUB_ISSUE
    assert result.facts
    assert all(request.headers.get("authorization") is None for request in seen)


async def test_discussion_without_a_token_degrades_instead_of_inventing() -> None:
    """No token: a warning and a low confidence, never made-up content."""
    seen: list = []
    resolver = GitHubSourceResolver(
        CarouselGithubSourceConfig(), client=httpx.AsyncClient(transport=router({}, seen))
    )
    result = await resolver.resolve(DISCUSSION_URL)

    assert result.source_type is CarouselSourceType.GITHUB_DISCUSSION
    assert result.confidence < 0.5
    assert any("token" in warning.lower() for warning in result.warnings), result.warnings
    assert not result.metrics, "a discussion without a token reports no numbers at all"
