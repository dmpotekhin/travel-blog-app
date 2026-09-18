"""Carousel persistence: schema idempotency, CRUD, approval gate (Phase 1)."""

import json

import pytest

from core.database import Database
from core.exceptions import StateTransitionError
from modules.carousels import enums as ce
from modules.carousels import models as cm

LEGACY_TABLES = {
    "cities",
    "photos",
    "drafts",
    "published",
    "vibecoding_posts",
    "storyboards",
    "narrative_beats",
    "storyboard_shots",
}

CAROUSEL_TABLES = {
    "carousel_jobs",
    "carousel_sources",
    "carousel_slides",
    "carousel_hook_candidates",
    "carousel_publications",
    "carousel_metrics",
    "carousel_learnings",
    "carousel_templates",
    "qa_artifacts",
    "vibecoding_sessions",
}


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "carousel.db"))
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _tables(db: Database) -> set:
    rows = await db._fetchall("SELECT name FROM sqlite_master WHERE type = 'table'", [])
    return {row["name"] for row in rows}


async def _new_job(db: Database, **overrides) -> cm.CarouselJob:
    payload = dict(
        vertical=ce.CarouselVertical.QA,
        source_type=ce.CarouselSourceType.GITHUB_ISSUE,
        source_url="https://github.com/dmitrypotekhin/travel-blog-app/issues/7",
        title="Flaky test only on Fridays",
        created_by="test",
    )
    payload.update(overrides)
    return await db.create_carousel_job(cm.CarouselJob(**payload))  # type: ignore[arg-type]


async def test_schema_creates_carousel_tables_alongside_legacy_ones(db):
    tables = await _tables(db)
    assert CAROUSEL_TABLES <= tables
    # the existing travel pipeline tables must still be there
    assert LEGACY_TABLES <= tables


async def test_schema_is_idempotent(tmp_path):
    path = str(tmp_path / "twice.db")
    first = Database(path)
    await first.connect()
    await first.close()
    second = Database(path)
    await second.connect()
    try:
        assert CAROUSEL_TABLES <= await _tables(second)
        assert await second.list_carousel_jobs() == []
    finally:
        await second.close()


async def test_indexes_exist(db):
    rows = await db._fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_carousel%'", ()
    )
    names = {row["name"] for row in rows}
    for expected in (
        "idx_carousel_jobs_status",
        "idx_carousel_jobs_vertical",
        "idx_carousel_jobs_source_type",
        "idx_carousel_jobs_created",
        "idx_carousel_slides_job",
        "idx_carousel_hooks_job",
        "idx_carousel_publications_job",
        "idx_carousel_metrics_publication",
        "idx_carousel_metrics_lookup",
        "idx_carousel_learnings_scope",
    ):
        assert expected in names, expected


async def test_create_and_fetch_job(db):
    job = await _new_job(db)
    assert isinstance(job.id, int)
    assert job.status is ce.CarouselStatus.PENDING
    assert job.autonomy_mode is ce.CarouselAutonomyMode.SUPERVISED
    assert job.created_at is not None and job.updated_at is not None

    fetched = await db.get_carousel_job(job.id)
    assert fetched is not None
    assert fetched.title == "Flaky test only on Fridays"
    assert fetched.vertical is ce.CarouselVertical.QA
    assert fetched.source_type is ce.CarouselSourceType.GITHUB_ISSUE


async def test_unknown_job_returns_none(db):
    assert await db.get_carousel_job(4242) is None


async def test_list_jobs_filters_by_status_vertical_and_source_type(db):
    qa = await _new_job(db, title="qa job")
    travel = await _new_job(
        db,
        title="travel job",
        vertical=ce.CarouselVertical.TRAVEL,
        source_type=ce.CarouselSourceType.URL,
    )
    await db.update_carousel_job_status(qa.id, ce.CarouselStatus.RESEARCHING)

    assert {job.id for job in await db.list_carousel_jobs()} == {qa.id, travel.id}
    researching = await db.list_carousel_jobs(status="researching")
    assert [job.id for job in researching] == [qa.id]
    assert [job.id for job in await db.list_carousel_jobs(vertical="travel")] == [travel.id]
    assert [job.id for job in await db.list_carousel_jobs(source_type="github_issue")] == [qa.id]
    assert await db.list_carousel_jobs(vertical="vibecoding") == []
    assert [job.id for job in await db.list_carousel_jobs(limit=1)] == [travel.id]


async def test_illegal_status_transition_is_refused_by_the_database(db):
    job = await _new_job(db)
    with pytest.raises(StateTransitionError):
        await db.update_carousel_job_status(job.id, ce.CarouselStatus.RENDERED)
    still_pending = await db.get_carousel_job(job.id)
    assert still_pending.status is ce.CarouselStatus.PENDING


async def test_full_happy_path_walks_to_analyzed(db):
    job = await _new_job(db)
    for status in (
        ce.CarouselStatus.RESEARCHING,
        ce.CarouselStatus.RESEARCHED,
        ce.CarouselStatus.NARRATIVE_DRAFTED,
        ce.CarouselStatus.SLIDES_PLANNED,
        ce.CarouselStatus.RENDERING,
        ce.CarouselStatus.RENDERED,
        ce.CarouselStatus.VERIFYING,
        ce.CarouselStatus.VERIFIED,
        ce.CarouselStatus.AWAITING_APPROVAL,
        ce.CarouselStatus.APPROVED,
        ce.CarouselStatus.PUBLISHING,
        ce.CarouselStatus.PUBLISHED,
        ce.CarouselStatus.ANALYZING,
        ce.CarouselStatus.ANALYZED,
    ):
        job = await db.update_carousel_job_status(job.id, status)
        assert job.status is status


async def test_status_writer_excludes_status_from_generic_updates(db):
    job = await _new_job(db)
    assert job.id is not None
    # ``status`` is not part of the writable set: only the state machine may
    # move a job, so a generic update attempts to bypass it and is rejected.
    with pytest.raises(ValueError):
        await db.update_carousel_job(job.id, status="published", title="renamed")

    renamed = await db.update_carousel_job(job.id, title="renamed")
    assert renamed.title == "renamed"
    assert renamed.status is ce.CarouselStatus.PENDING


async def test_failed_job_records_the_error(db):
    job = await _new_job(db)
    failed = await db.fail_carousel_job(job.id, "resolver exploded")
    assert failed.status is ce.CarouselStatus.FAILED
    assert failed.error_message == "resolver exploded"


async def test_save_and_read_six_slides_in_order(db):
    job = await _new_job(db)
    slides = [
        cm.CarouselSlide(
            job_id=job.id,
            order=index + 1,
            slide_type=ce.SlideType.HERO_HOOK if index == 0 else ce.SlideType.SYMPTOM,
            headline=f"Заголовок {index + 1}",
            bullets_json=json.dumps(["локально зелёный", "в CI красный"]),
            alt_text=f"Слайд {index + 1}",
            source_refs_json=json.dumps(["issue_comment_1"]),
        )
        for index in range(6)
    ]
    stored = await db.save_carousel_slides(job.id, slides)
    assert len(stored) == 6
    assert [slide.order for slide in stored] == [1, 2, 3, 4, 5, 6]

    reloaded = await db.get_carousel_slides(job.id)
    assert len(reloaded) == 6
    assert reloaded[0].slide_type is ce.SlideType.HERO_HOOK
    assert reloaded[0].bullets == ["локально зелёный", "в CI красный"]
    assert reloaded[0].source_refs == ["issue_comment_1"]
    assert reloaded[0].verification_status is ce.CarouselVerificationStatus.PENDING


async def test_saving_slides_replaces_the_previous_plan(db):
    job = await _new_job(db)
    await db.save_carousel_slides(
        job.id, [cm.CarouselSlide(job_id=job.id, order=1, slide_type=ce.SlideType.CTA)]
    )
    await db.save_carousel_slides(
        job.id,
        [
            cm.CarouselSlide(job_id=job.id, order=1, slide_type=ce.SlideType.HERO_HOOK),
            cm.CarouselSlide(job_id=job.id, order=2, slide_type=ce.SlideType.CTA),
        ],
    )
    slides = await db.get_carousel_slides(job.id)
    assert len(slides) == 2
    assert slides[0].slide_type is ce.SlideType.HERO_HOOK


async def test_update_slide_records_verification_issues(db):
    job = await _new_job(db)
    stored = await db.save_carousel_slides(
        job.id, [cm.CarouselSlide(job_id=job.id, order=1, slide_type=ce.SlideType.CODE_BLOCK)]
    )
    slide = stored[0]
    edited = await db.update_carousel_slide(
        slide.id,
        headline="Фикс",
        verification_status=ce.CarouselVerificationStatus.NEEDS_REVISION.value,
        verification_issues_json=json.dumps(["code truncated"]),
        regeneration_count=1,
    )
    assert edited.headline == "Фикс"
    assert edited.verification_status is ce.CarouselVerificationStatus.NEEDS_REVISION
    assert edited.verification_issues == ["code truncated"]
    assert edited.regeneration_count == 1


async def test_hook_candidates_and_selection(db):
    job = await _new_job(db)
    candidates = [
        cm.CarouselHookCandidate(
            job_id=job.id,
            category=ce.HookCategory.FAILURE,
            pattern="failure",
            text="Этот тест выглядел невинно, пока не сломал релиз",
            score=0.82,
            expected_emotion="frustration",
            rationale="прямая потеря времени",
            source_support="issue body",
            scores_json=json.dumps({"curiosity": 0.8, "pain": 0.9}),
        ),
        cm.CarouselHookCandidate(
            job_id=job.id,
            category=ce.HookCategory.NUMBER_LIST,
            text="5 причин, почему тест падает только в CI",
            score=0.7,
        ),
    ]
    stored = await db.save_hook_candidates(job.id, candidates)
    assert len(stored) == 2
    assert all(candidate.is_selected is False for candidate in stored)

    chosen = await db.select_hook_candidate(job.id, stored[1].id)
    assert chosen.is_selected is True
    refreshed = await db.get_hook_candidates(job.id)
    assert [candidate.is_selected for candidate in refreshed] == [False, True]
    only_selected = await db.get_hook_candidates(job.id, selected_only=True)
    assert [candidate.id for candidate in only_selected] == [stored[1].id]

    job_after = await db.get_carousel_job(job.id)
    assert job_after.selected_hook_id == stored[1].id
    selected = await db.get_selected_hook(job.id)
    assert selected is not None and selected.text.startswith("5 причин")


async def test_publication_saving_is_idempotent_per_request_id(db):
    job = await _new_job(db)
    first = await db.save_carousel_publication(
        cm.CarouselPublication(job_id=job.id, platform="tiktok", request_id="req-abc")
    )
    again = await db.save_carousel_publication(
        cm.CarouselPublication(job_id=job.id, platform="tiktok", request_id="req-abc")
    )
    assert first.id == again.id
    assert await db.carousel_published_count(job.id) == 0

    found = await db.get_publication_by_request_id("req-abc")
    assert found is not None and found.status is ce.PublicationStatus.PENDING

    updated = await db.update_carousel_publication(
        first.id,
        status=ce.PublicationStatus.PUBLISHED.value,
        post_url="https://www.tiktok.com/@user/video/1",
        external_id="vid-1",
    )
    assert updated.status is ce.PublicationStatus.PUBLISHED
    assert await db.carousel_published_count(job.id) == 1

    both = await db.list_carousel_publications(job_id=job.id, platform="tiktok")
    assert [row.id for row in both] == [first.id]


async def test_publications_for_two_platforms_are_kept_apart(db):
    job = await _new_job(db)
    await db.save_carousel_publication(
        cm.CarouselPublication(job_id=job.id, platform="tiktok", request_id="req-tt")
    )
    await db.save_carousel_publication(
        cm.CarouselPublication(job_id=job.id, platform="instagram", request_id="req-ig")
    )
    rows = await db.list_carousel_publications(job_id=job.id)
    assert {row.platform for row in rows} == {"tiktok", "instagram"}


async def test_metrics_are_stored_with_source_and_raw_payload(db):
    job = await _new_job(db)
    publication = await db.save_carousel_publication(
        cm.CarouselPublication(job_id=job.id, platform="tiktok", request_id="req-m")
    )
    await db.save_carousel_metrics(
        [
            cm.CarouselMetric(
                publication_id=publication.id,
                job_id=job.id,
                platform="tiktok",
                metric_name="views",
                metric_value=1234.0,
                raw_value="1.2K",
                source="upload-post",
                raw_payload_json=json.dumps({"views": "1.2K"}),
            ),
            cm.CarouselMetric(
                publication_id=publication.id,
                job_id=job.id,
                platform="tiktok",
                metric_name="likes",
                metric_value=42.0,
                source="upload-post",
            ),
        ]
    )
    metrics = await db.list_carousel_metrics(job_id=job.id)
    assert {metric.metric_name for metric in metrics} == {"views", "likes"}
    views = await db.list_carousel_metrics(job_id=job.id, metric_name="views")
    assert views[0].metric_value == 1234.0
    assert views[0].raw_value == "1.2K"
    assert json.loads(views[0].raw_payload_json)["views"] == "1.2K"
    assert await db.list_carousel_metrics(platform="instagram") == []


async def test_learning_upsert_replaces_the_same_key_and_keeps_samples(db):
    learning = cm.CarouselLearning(
        scope_type=ce.CarouselLearningScope.HOOK_CATEGORY,
        scope_value="failure",
        metric_name="carousel_score",
        metric_value=0.4,
        sample_size=1,
        confidence=0.2,
    )
    await db.upsert_carousel_learning(learning)

    learning.metric_value = 0.8
    learning.sample_size = 4
    learning.confidence = 0.5
    await db.upsert_carousel_learning(learning)

    stored = await db.get_carousel_learnings(scope_type=ce.CarouselLearningScope.HOOK_CATEGORY)
    assert len(stored) == 1
    assert stored[0].metric_value == 0.8
    assert stored[0].sample_size == 4

    await db.upsert_carousel_learning(
        cm.CarouselLearning(
            scope_type=ce.CarouselLearningScope.VERTICAL,
            scope_value="qa",
            metric_name="carousel_score",
            metric_value=0.6,
            sample_size=3,
        )
    )
    assert len(await db.get_carousel_learnings()) == 2
    assert len(await db.get_carousel_learnings(scope_value="qa")) == 1
    strong = await db.get_carousel_learnings(min_sample_size=3)
    assert {row.scope_value for row in strong} == {"qa", "failure"}
    # a stricter threshold still keeps the row with the bigger sample
    only_failure = await db.get_carousel_learnings(min_sample_size=4)
    assert {row.scope_value for row in only_failure} == {"failure"}


async def test_source_records_are_an_audit_trail(db):
    job = await _new_job(db)
    await db.add_carousel_source(
        cm.CarouselSourceRecord(
            job_id=job.id,
            source_type=ce.CarouselSourceType.GITHUB_ISSUE,
            vertical=ce.CarouselVertical.QA,
            source_ref="https://github.com/dmitrypotekhin/travel-blog-app/issues/7",
            canonical_url="https://github.com/dmitrypotekhin/travel-blog-app/issues/7",
            external_id="7",
            title="Flaky test only on Fridays",
            content_type="github_issue",
            confidence=0.66,
            warnings_json=json.dumps(["comments truncated"]),
            resolver="github",
        )
    )
    sources = await db.list_carousel_sources(job_id=job.id)
    assert len(sources) == 1
    assert sources[0].resolver == "github"
    assert sources[0].warnings == ["comments truncated"]


async def test_qa_artifacts_and_vibecoding_sessions_round_trip(db):
    artifact = await db.create_qa_artifact(
        cm.QAArtifact(
            source_type=ce.CarouselSourceType.GITHUB_ISSUE,
            external_id="7",
            title="Flaky test only on Fridays",
            summary="падает только в CI по пятницам",
            severity="high",
            symptoms_json=json.dumps(["зелёный локально", "красный в CI"]),
            root_cause="shared fixture state",
            root_cause_verified=True,
            fix_description="изолировать состояние теста",
            code_snippet="def test_x(): ...",
            code_language="python",
            tags_json=json.dumps(["flaky", "ci"]),
        )
    )
    assert isinstance(artifact.id, int)
    assert artifact.severity == "high"
    assert artifact.symptoms == ["зелёный локально", "красный в CI"]
    assert artifact.root_cause_verified is True
    assert await db.get_qa_artifact(artifact.id) is not None
    assert len(await db.list_qa_artifacts(severity="high")) == 1
    assert await db.list_qa_artifacts(severity="low") == []

    session = await db.create_vibecoding_session(
        cm.VibecodingSession(
            title="Собрал пайплайн за вечер",
            goal="архив фото -> посты без копипаста",
            prompts_json=json.dumps(["сделай сканер архива"]),
            tools_used_json=json.dumps(["hermes", "deepseek"]),
            duration_minutes=180,
            tests_before="131 passed",
            tests_after="138 passed",
            local_only=True,
            outcome="работает",
        )
    )
    stored = await db.get_vibecoding_session(session.id)
    assert stored.tools_used == ["hermes", "deepseek"]
    assert stored.local_only is True
    assert [row.id for row in await db.list_vibecoding_sessions()] == [session.id]


async def test_template_seeding_is_idempotent(db):
    templates = [
        cm.CarouselTemplate(
            vertical=ce.CarouselVertical.QA,
            name="qa_incident_detective_checklist",
            description="hook -> симптом -> цена -> расследование -> фикс -> чек-лист",
            slide_sequence_json=json.dumps([s.value for s in (
                ce.SlideType.HERO_HOOK,
                ce.SlideType.SYMPTOM,
                ce.SlideType.AGITATION,
                ce.SlideType.INVESTIGATION,
                ce.SlideType.FIX_CODE,
                ce.SlideType.PREVENTION_CHECKLIST,
            )]),
            hook_categories_json=json.dumps([ce.HookCategory.FAILURE.value]),
            visual_style="technical_clean",
        )
    ]
    first = await db.seed_carousel_templates(templates)
    second = await db.seed_carousel_templates(templates)
    assert len(first) == 1 and len(second) == 1
    assert first[0].id == second[0].id
    stored = await db.list_carousel_templates()
    assert len(stored) == 1
    assert stored[0].slide_sequence[0] == ce.SlideType.HERO_HOOK.value
    assert len(await db.list_carousel_templates(vertical="qa")) == 1
    assert await db.list_carousel_templates(vertical="travel") == []


async def test_enum_values_written_to_sqlite_are_plain_strings(db):
    job = await _new_job(db)
    rows = await db._fetchall("SELECT status, vertical, source_type FROM carousel_jobs", [])
    assert rows[0]["status"] == "pending"
    assert rows[0]["vertical"] == "qa"
    assert rows[0]["source_type"] == "github_issue"
    assert job.status == "pending"
