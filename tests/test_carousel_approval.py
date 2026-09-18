"""Human-in-the-loop approval gate (Phase 5).

The rule under test: nothing reaches TikTok or Instagram without an explicit
approval, and autonomy needs two switches, not one.
"""

import json
from pathlib import Path

import pytest

from core.config import Config
from core.database import Database
from core.exceptions import CarouselError, PublishNotApprovedError, StateTransitionError
from core.models import (
    CarouselSourceContext,
    CarouselSourceType,
    CarouselStatus,
    CarouselVertical,
    PublicationStatus,
    SourceFact,
)
from modules.carousels.publishing import BaseCarouselPublisher, PublishResult
from modules.carousels.service import CarouselFactory
from modules.carousels.sources.mock import MockSourceResolver

ISSUE_URL = "https://github.com/acme/tool/issues/7"
FACTS = [
    "Тест падает только в CI, локально зелёный.",
    "Падает в 3 из 10 прогонов.",
    "Причина оказалась в порядке запуска тестов.",
    "Фикс изолировал состояние между тестами.",
    "Проверяем на 200 прогонах перед мержем.",
    "Добавили чек-лист против флаки-тестов.",
]


class FakePublisher(BaseCarouselPublisher):
    """Pretends the platforms accepted everything (offline)."""

    name = "fake"

    def __init__(
        self,
        status: PublicationStatus = PublicationStatus.PUBLISHED,
        shared_request_id: str = "",
    ):
        self.status = status
        self.shared_request_id = shared_request_id
        self.requests = []

    async def publish(self, request):
        self.requests.append(request)
        return [
            PublishResult(
                platform=platform,
                status=self.status,
                request_id=self.shared_request_id or f"req-{request.job_id}-{platform}",
                external_id=f"ext-{platform}",
                post_url=f"https://example.com/{platform}/{request.job_id}",
                raw_response_json=json.dumps({"success": True, "platform": platform}),
            )
            for platform in request.platforms
        ]

    async def fetch_status(self, request_id):
        raise NotImplementedError


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "approval.db"))
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


def factory(db: Database, tmp_path: Path, **overrides) -> CarouselFactory:
    config = Config()
    config.carousels.output_dir = str(tmp_path / "out")
    for key, value in overrides.items():
        setattr(config.carousels, key, value)
    return CarouselFactory(db, config)


def qa_context() -> CarouselSourceContext:
    return CarouselSourceContext(
        source_type=CarouselSourceType.GITHUB_ISSUE,
        vertical=CarouselVertical.QA,
        source_url=ISSUE_URL,
        title="Флаки-тест в CI",
        summary="Разбор флаки-теста.",
        facts=FACTS,
        sourced_facts=[
            SourceFact(text=fact, source_excerpt=fact, source_ref=f"comment:{index}", verified=True)
            for index, fact in enumerate(FACTS)
        ],
        confidence=0.88,
    )


async def to_verified(factory_: CarouselFactory) -> int:
    """Drive a job from PENDING to VERIFIED (research -> plan -> render -> verify)."""
    job = await factory_.create_job(source_type="github_issue", source_ref=ISSUE_URL)
    job = await factory_.research(job.id, resolver=MockSourceResolver(context=qa_context()))
    job = await factory_.draft_narrative(job.id)
    await factory_.plan_slides(job.id)
    await factory_.render_slides(job.id)
    await factory_.verify_slides(job.id)
    job = await factory_.get_job(job.id)
    assert job.status is CarouselStatus.VERIFIED
    return job.id


async def test_verified_job_waits_for_a_human(db, tmp_path):
    carousel = factory(db, tmp_path)
    job_id = await to_verified(carousel)

    job = await carousel.submit_for_approval(job_id)

    assert job.status is CarouselStatus.AWAITING_APPROVAL
    assert job.approved_by == ""
    queue = await carousel.approval_queue()
    assert [queued.id for queued in queue] == [job_id]


async def test_approve_records_who_and_when(db, tmp_path):
    carousel = factory(db, tmp_path)
    job_id = await to_verified(carousel)
    await carousel.submit_for_approval(job_id)

    approved = await carousel.approve(job_id, approved_by="Братан", note="ок")

    assert approved.status is CarouselStatus.APPROVED
    assert approved.approved_by == "Братан"
    assert approved.approved_at is not None
    assert await carousel.approval_queue() == []


async def test_approve_before_verification_is_refused(db, tmp_path):
    carousel = factory(db, tmp_path)
    job = await carousel.create_job(source_type="github_issue", source_ref=ISSUE_URL)

    with pytest.raises(StateTransitionError, match="verification"):
        await carousel.approve(job.id, approved_by="Братан")


async def test_reject_sends_the_job_back_with_the_reason(db, tmp_path):
    carousel = factory(db, tmp_path)
    job_id = await to_verified(carousel)
    await carousel.submit_for_approval(job_id)

    rejected = await carousel.reject(job_id, reason="Второй слайд врёт про проценты")

    assert rejected.status is CarouselStatus.NEEDS_REVISION
    assert any("Второй слайд врёт" in warning for warning in rejected.warnings)


async def test_publishing_without_approval_is_blocked(db, tmp_path):
    carousel = factory(db, tmp_path)
    job_id = await to_verified(carousel)
    await carousel.submit_for_approval(job_id)
    publisher = FakePublisher()

    with pytest.raises(PublishNotApprovedError):
        await carousel.publish(job_id, publisher=publisher)

    assert publisher.requests == []


async def test_dry_run_publishes_nothing_but_records_the_intent(db, tmp_path):
    carousel = factory(db, tmp_path)
    job_id = await to_verified(carousel)
    await carousel.submit_for_approval(job_id)
    await carousel.approve(job_id, approved_by="Братан")

    bundle = await carousel.publish(job_id)

    assert bundle.job.status is CarouselStatus.APPROVED  # nothing was uploaded
    assert {p.status for p in bundle.publications} == {PublicationStatus.MANUAL}
    assert {p.platform for p in bundle.publications} == {"tiktok", "instagram"}
    assert all(json.loads(p.raw_response_json)["dry_run"] is True for p in bundle.publications)
    assert any("dry" in warning.lower() for warning in bundle.job.warnings)


async def test_publish_records_real_results_and_moves_the_job(db, tmp_path):
    carousel = factory(db, tmp_path, dry_run=False)
    job_id = await to_verified(carousel)
    await carousel.submit_for_approval(job_id)
    await carousel.approve(job_id, approved_by="Братан")
    publisher = FakePublisher()

    bundle = await carousel.publish(job_id, publisher=publisher)

    assert publisher.requests and len(publisher.requests) == 1
    assert publisher.requests[0].platforms == ["tiktok", "instagram"]
    assert len(publisher.requests[0].slide_paths) == 6
    assert bundle.job.status is CarouselStatus.PUBLISHED
    by_platform = {p.platform: p for p in bundle.publications}
    assert by_platform["tiktok"].status is PublicationStatus.PUBLISHED
    assert by_platform["tiktok"].post_url.endswith(f"/tiktok/{job_id}")
    assert by_platform["tiktok"].request_id == f"req-{job_id}-tiktok"


async def test_publish_is_idempotent_for_the_same_job(db, tmp_path):
    carousel = factory(db, tmp_path, dry_run=False)
    job_id = await to_verified(carousel)
    await carousel.submit_for_approval(job_id)
    await carousel.approve(job_id, approved_by="Братан")
    publisher = FakePublisher()
    await carousel.publish(job_id, publisher=publisher)

    bundle = await carousel.publish(job_id, publisher=publisher)
    again = await carousel.list_publications(job_id)

    assert len(again) == 2  # no duplicate rows per platform
    assert bundle.job.status is CarouselStatus.PUBLISHED


async def test_manual_outcome_keeps_the_job_publishing_with_a_warning(db, tmp_path):
    carousel = factory(db, tmp_path, dry_run=False)
    job_id = await to_verified(carousel)
    await carousel.submit_for_approval(job_id)
    await carousel.approve(job_id, approved_by="Братан")

    bundle = await carousel.publish(
        job_id, publisher=FakePublisher(status=PublicationStatus.MANUAL)
    )

    assert bundle.job.status is CarouselStatus.PUBLISHING
    assert any("manual" in warning.lower() for warning in bundle.job.warnings)


async def test_full_autonomy_needs_two_switches(db, tmp_path):
    """mode=full_autonomous alone must NOT skip the human."""
    carousel = factory(db, tmp_path, mode="full_autonomous", require_human_approval=True)
    job_id = await to_verified(carousel)

    job = await carousel.submit_for_approval(job_id)

    assert job.status is CarouselStatus.AWAITING_APPROVAL
    assert job.approved_by == ""


async def test_full_autonomy_with_both_switches_auto_approves(db, tmp_path):
    carousel = factory(db, tmp_path, mode="full_autonomous", require_human_approval=False)
    job_id = await to_verified(carousel)

    job = await carousel.submit_for_approval(job_id)

    assert job.status is CarouselStatus.APPROVED
    assert job.approved_by.startswith("auto")
    assert job.approved_at is not None
    assert any("autonomous" in warning.lower() for warning in job.warnings)


async def test_one_shared_request_id_gives_one_row_per_platform(db, tmp_path):
    """Regression (Phase 5 E2E): a single multi-platform upload reports one
    request_id, so both platforms must still get their own row and keep it."""
    carousel = factory(db, tmp_path, dry_run=False)
    job_id = await to_verified(carousel)
    await carousel.submit_for_approval(job_id)
    await carousel.approve(job_id, approved_by="Братан")
    publisher = FakePublisher(status=PublicationStatus.PROCESSING, shared_request_id="req-one")

    bundle = await carousel.publish(job_id, publisher=publisher)

    by_platform = {p.platform: p for p in bundle.publications}
    assert set(by_platform) == {"tiktok", "instagram"}
    assert by_platform["instagram"].request_id == "req-one"
    assert by_platform["tiktok"].request_id == "req-one"
    assert by_platform["tiktok"].id != by_platform["instagram"].id
    assert len(await carousel.list_publications(job_id)) == 2
