"""CarouselFactory (Phase 1): job creation, lifecycle, approval gate, edits."""

import json

import pytest

from core.config import Config
from core.database import Database
from core.exceptions import (
    CarouselError,
    NotFoundError,
    PublishNotApprovedError,
    StateTransitionError,
)
from modules.carousels import enums as ce
from modules.carousels import models as cm
from modules.carousels import state_machine as sm
from modules.carousels.service import CarouselFactory, looks_like_url

ISSUE_URL = "https://github.com/dmitrypotekhin/travel-blog-app/issues/7"

APPROVAL_PATH = (
    ce.CarouselStatus.RESEARCHING,
    ce.CarouselStatus.RESEARCHED,
    ce.CarouselStatus.NARRATIVE_DRAFTED,
    ce.CarouselStatus.SLIDES_PLANNED,
    ce.CarouselStatus.RENDERING,
    ce.CarouselStatus.RENDERED,
    ce.CarouselStatus.VERIFYING,
    ce.CarouselStatus.VERIFIED,
    ce.CarouselStatus.AWAITING_APPROVAL,
)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "service.db"))
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


def factory(db: Database, **config_overrides) -> CarouselFactory:
    config = Config()
    for key, value in config_overrides.items():
        setattr(config.carousels, key, value)
    return CarouselFactory(db, config)


async def _job_id(carousel: CarouselFactory, **kwargs) -> int:
    job = await carousel.create_job(**kwargs)
    assert job.id is not None
    return job.id


def test_looks_like_url_recognises_real_references():
    assert looks_like_url(ISSUE_URL) is True
    assert looks_like_url("https://habr.com/ru/articles/123456/") is True
    assert looks_like_url("dmpotekhin/travel-blog-app") is False
    assert looks_like_url("Китай за 10 дней") is False


async def test_create_job_from_github_issue_url(db):
    carousel = factory(db)
    job = await carousel.create_job(
        source_type="github_issue", source_ref=ISSUE_URL, created_by="cli"
    )
    assert job.id is not None
    assert job.status is ce.CarouselStatus.PENDING
    assert job.source_type is ce.CarouselSourceType.GITHUB_ISSUE
    assert job.source_url == ISSUE_URL
    assert job.autonomy_mode is ce.CarouselAutonomyMode.SUPERVISED
    assert job.requires_approval is True
    assert job.created_by == "cli"
    assert job.dry_run is True  # dry_run is the config default: no live calls
    assert job.target_platforms == ["tiktok", "instagram"]
    assert job.warnings, "an unresolved source must be flagged, never assumed"

    sources = await db.list_carousel_sources(job_id=job.id)
    assert len(sources) == 1
    assert sources[0].source_ref == ISSUE_URL
    assert sources[0].canonical_url == ISSUE_URL
    assert sources[0].confidence == 0.0
    assert sources[0].warnings == job.warnings


async def test_create_job_applies_default_vertical_and_override(db):
    carousel = factory(db)
    default_job = await carousel.create_job(source_type="url", source_ref="https://example.com/a")
    assert default_job.vertical is ce.CarouselVertical.HYBRID  # config default_vertical

    qa_job = await carousel.create_job(
        source_type="url", source_ref="https://example.com/b", vertical="qa"
    )
    assert qa_job.vertical is ce.CarouselVertical.QA

    for auto in (None, "", "auto", "AUTO"):
        job = await carousel.create_job(
            source_type="url", source_ref="https://example.com/c", vertical=auto
        )
        assert job.vertical is ce.CarouselVertical.HYBRID

    with pytest.raises(CarouselError):
        await carousel.create_job(
            source_type="url", source_ref="https://example.com/d", vertical="nonsense"
        )


async def test_create_job_marks_non_url_references_without_faking_a_url(db):
    carousel = factory(db)
    job = await carousel.create_job(
        source_type="manual_topic", source_ref="Китай за 10 дней без туроператоров"
    )
    assert job.source_url == ""
    assert job.id is not None
    sources = await db.list_carousel_sources(job_id=job.id)
    assert sources[0].source_ref == "Китай за 10 дней без туроператоров"
    assert sources[0].canonical_url == ""


async def test_create_job_rejects_unknown_source_type(db):
    carousel = factory(db)
    with pytest.raises(CarouselError):
        await carousel.create_job(source_type="pigeon_post", source_ref="coo")


async def test_disabled_factory_refuses_to_create_jobs(db):
    carousel = factory(db, enabled=False)
    with pytest.raises(CarouselError):
        await carousel.create_job(source_type="url", source_ref="https://example.com")


async def test_create_job_honours_explicit_dry_run_flag(db):
    carousel = factory(db)
    live = await carousel.create_job(
        source_type="url", source_ref="https://example.com/live", dry_run=False
    )
    assert live.dry_run is False


async def test_get_job_and_bundle(db):
    carousel = factory(db)
    job_id = await _job_id(carousel, source_type="github_issue", source_ref=ISSUE_URL)
    await db.save_carousel_slides(
        job_id,
        [
            cm.CarouselSlide(job_id=job_id, order=1, slide_type=ce.SlideType.HERO_HOOK, headline="1"),
            cm.CarouselSlide(job_id=job_id, order=2, slide_type=ce.SlideType.SYMPTOM, headline="2"),
        ],
    )
    await db.save_hook_candidates(
        job_id, [cm.CarouselHookCandidate(job_id=job_id, text="Хук", score=0.5)]
    )

    bundle = await carousel.get_bundle(job_id)
    assert bundle.job.id == job_id
    assert len(bundle.slides) == 2
    assert len(bundle.hooks) == 1
    assert bundle.publications == []
    # Phase 1 has no verification pipeline yet: the only issues are the source
    # warnings recorded at creation time (never silently dropped).
    assert bundle.issues == bundle.job.warnings


async def test_get_bundle_surfaces_slide_verification_issues(db):
    carousel = factory(db)
    job_id = await _job_id(carousel, source_type="url", source_ref="https://example.com/x")
    slides = [
        cm.CarouselSlide(job_id=job_id, order=index + 1, slide_type=ce.SlideType.CTA, headline="Ок")
        for index in range(3)
    ]
    # a verification issue a human must fix before approval (Phase 4 writes
    # these; here we assert the bundle aggregates them per slide)
    slides[1].verification_issues_json = json.dumps(
        ["headline is empty"], ensure_ascii=False
    )
    await db.save_carousel_slides(job_id, slides)

    bundle = await carousel.get_bundle(job_id)
    assert "slide 2: headline is empty" in bundle.issues
    assert bundle.job.warnings[0] in bundle.issues


async def test_unknown_job_raises_not_found(db):
    carousel = factory(db)
    with pytest.raises(NotFoundError):
        await carousel.get_job(999)


async def test_walk_to_awaiting_approval_and_approve(db):
    carousel = factory(db)
    job_id = await _job_id(carousel, source_type="github_pr", source_ref=ISSUE_URL)
    job = await carousel.get_job(job_id)
    for status in APPROVAL_PATH:
        job = await carousel.transition(job_id, status)
    assert job.status is ce.CarouselStatus.AWAITING_APPROVAL
    assert ce.CarouselStatus.AWAITING_APPROVAL.value in sm.HUMAN_ACTION_STATUSES

    approved = await carousel.approve(job_id, approved_by="Братан")
    assert approved.status is ce.CarouselStatus.APPROVED
    assert approved.approved_by == "Братан"
    assert approved.approved_at is not None
    await carousel.ensure_publishable(job_id)  # no exception once approved
    assert sm.can_publish(approved) is True


async def test_approve_requires_the_approval_state(db):
    carousel = factory(db)
    job_id = await _job_id(carousel, source_type="url", source_ref="https://example.com/y")
    # a pending job cannot jump straight to approved: the state machine rules
    with pytest.raises(StateTransitionError):
        await carousel.approve(job_id)


async def test_publishing_is_refused_before_approval(db):
    carousel = factory(db)
    job = await carousel.create_job(source_type="github_issue", source_ref=ISSUE_URL)
    assert sm.can_publish(job) is False
    assert job.id is not None
    with pytest.raises(PublishNotApprovedError):
        await carousel.ensure_publishable(job.id)


async def test_reject_sends_the_job_back_for_revision(db):
    carousel = factory(db)
    job_id = await _job_id(carousel, source_type="url", source_ref="https://example.com/z")
    for status in APPROVAL_PATH:
        await carousel.transition(job_id, status)

    rejected = await carousel.reject(job_id, reason="второй слайд не читается")
    assert rejected.status is ce.CarouselStatus.NEEDS_REVISION
    assert rejected.error_message == "второй слайд не читается"
    assert sm.can_publish(rejected) is False
    # revision may go back to rendering, planning or approval
    assert sm.next_states(rejected.status) == {
        "rendering",
        "slides_planned",
        "awaiting_approval",
        "failed",
    }


async def test_edit_slide_applies_human_changes(db):
    carousel = factory(db)
    job_id = await _job_id(carousel, source_type="github_issue", source_ref=ISSUE_URL)
    stored = await db.save_carousel_slides(
        job_id,
        [
            cm.CarouselSlide(
                job_id=job_id, order=1, slide_type=ce.SlideType.HERO_HOOK, headline="Один flaky test"
            )
        ],
    )
    edited = await carousel.edit_slide(
        stored[0].id,
        headline="Один flaky test стоил команде 2 дня",
        bullets_json=json.dumps(["локально зелёный", "в CI красный"]),
    )
    assert edited.headline.endswith("2 дня")
    assert edited.bullets == ["локально зелёный", "в CI красный"]

    with pytest.raises(NotFoundError):
        await carousel.edit_slide(4242, headline="нет такого слайда")


async def test_update_job_and_list_jobs(db):
    carousel = factory(db)
    qa_id = await _job_id(
        carousel, source_type="github_issue", source_ref=ISSUE_URL, title="QA"
    )
    travel_id = await _job_id(
        carousel, source_type="url", source_ref="https://example.com/t", vertical="travel"
    )
    await carousel.transition(qa_id, ce.CarouselStatus.RESEARCHING)
    await carousel.update_job(qa_id, logline="Разбор flaky test")

    assert (await carousel.get_job(qa_id)).logline == "Разбор flaky test"
    assert [job.id for job in await carousel.list_jobs(vertical="travel")] == [travel_id]
    assert [job.id for job in await carousel.list_jobs(status="researching")] == [qa_id]
    assert {job.id for job in await carousel.list_jobs(limit=10)} == {qa_id, travel_id}
    assert await carousel.list_jobs(status="published") == []


async def test_status_report_exposes_progress_and_next_step(db):
    carousel = factory(db)
    job = await carousel.create_job(source_type="github_issue", source_ref=ISSUE_URL)
    assert job.id is not None
    report = await carousel.status_report(job.id)
    assert report["job_id"] == job.id
    assert report["status"] == "pending"
    assert report["next"] == "researching"
    assert report["can_publish"] is False
    assert report["requires_human_action"] is False


async def test_supervised_config_never_allows_auto_approval(db):
    carousel = factory(db)
    assert carousel.autonomy_mode == "supervised"
    assert carousel.should_auto_approve() is False
    assert carousel.require_human_approval is True

    autonomous = factory(db, mode="full_autonomous", require_human_approval=False)
    assert autonomous.should_auto_approve() is True

    # full_autonomous alone is not enough: approval is still required by default
    half_autonomous = factory(db, mode="full_autonomous")
    assert half_autonomous.should_auto_approve() is False
