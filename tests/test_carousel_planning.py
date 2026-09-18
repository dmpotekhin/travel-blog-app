"""Narrative + slide planning through the service (Phase 3)."""

import json

import pytest

from core.config import Config
from core.database import Database
from core.exceptions import CarouselError, NotFoundError, StateTransitionError
from core.models import (
    CarouselSourceContext,
    CarouselSourceType,
    CarouselVerificationStatus,
    CarouselVertical,
    CodeSnippet,
    SourceFact,
)
from modules.carousels import database_helpers as repo
from modules.carousels import enums as ce
from modules.carousels.service import CarouselFactory
from modules.carousels.sources.mock import MockSourceResolver

ISSUE_URL = "https://github.com/acme/tool/issues/7"
QA_FACTS = [
    "Тест падает только в CI, локально зелёный.",
    "Падает в 3 из 10 прогонов.",
    "Причина оказалась в порядке запуска тестов.",
]
CODE = CodeSnippet(
    language="python",
    code="def isolated_state():\n    return {}",
    caption="фикстура изоляции",
    source_ref="pr_diff:tests/test_state.py",
)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "planning.db"))
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


def factory(db: Database, **overrides) -> CarouselFactory:
    config = Config()
    for key, value in overrides.items():
        setattr(config.carousels, key, value)
    return CarouselFactory(db, config)


def qa_context(facts=None, **overrides) -> CarouselSourceContext:
    lines = list(QA_FACTS if facts is None else facts)
    payload = dict(
        source_type=CarouselSourceType.GITHUB_ISSUE,
        vertical=CarouselVertical.QA,
        source_url=ISSUE_URL,
        canonical_url=ISSUE_URL,
        external_id="acme/tool#7",
        title="Flaky test in CI",
        summary=lines[0] if lines else "",
        facts=lines,
        sourced_facts=[
            SourceFact(text=line, source_excerpt=line, source_ref=f"issue:{index}", verified=True)
            for index, line in enumerate(lines)
        ],
        code_snippets=[CODE],
        confidence=0.85,
    )
    payload.update(overrides)
    return CarouselSourceContext(**payload)


async def researched_job(db: Database, context=None):
    carousel = factory(db)
    job = await carousel.create_job(source_type="github_issue", source_ref=ISSUE_URL)
    assert job.id is not None
    await carousel.research(job.id, resolver=MockSourceResolver(context=context or qa_context()))
    return carousel, job.id


async def test_draft_narrative_saves_ranked_hooks_and_a_logline(db):
    carousel, job_id = await researched_job(db)

    updated = await carousel.draft_narrative(job_id)

    assert updated.status is ce.CarouselStatus.NARRATIVE_DRAFTED
    assert updated.logline
    hooks = await repo.get_hook_candidates(db, job_id)
    assert hooks
    assert [hook.score for hook in hooks] == sorted(
        [hook.score for hook in hooks], reverse=True
    )
    assert all(hook.source_support for hook in hooks)


async def test_draft_narrative_is_honest_about_an_empty_source(db):
    carousel, job_id = await researched_job(db, context=qa_context(facts=[]))

    updated = await carousel.draft_narrative(job_id)

    assert await repo.get_hook_candidates(db, job_id) == []
    assert any("no hook candidates" in warning for warning in updated.warnings)


async def test_draft_narrative_needs_a_researched_source(db):
    carousel = factory(db)
    job = await carousel.create_job(source_type="github_issue", source_ref=ISSUE_URL)

    with pytest.raises(CarouselError):
        await carousel.draft_narrative(job.id)


async def test_plan_slides_persists_six_slides_and_selects_one_hook(db):
    carousel, job_id = await researched_job(db)
    await carousel.draft_narrative(job_id)

    bundle = await carousel.plan_slides(job_id)

    assert bundle.job.status is ce.CarouselStatus.SLIDES_PLANNED
    assert len(bundle.slides) == 6
    assert [slide.order for slide in bundle.slides] == [1, 2, 3, 4, 5, 6]
    assert all(slide.alt_text for slide in bundle.slides)
    assert all(slide.job_id == job_id for slide in bundle.slides)
    assert all(
        slide.verification_status is CarouselVerificationStatus.PENDING
        for slide in bundle.slides
    )

    selected = await repo.get_hook_candidates(db, job_id, selected_only=True)
    assert len(selected) == 1
    assert bundle.job.selected_hook_id == selected[0].id
    assert bundle.slides[0].headline
    hooks = await repo.get_hook_candidates(db, job_id)
    assert bundle.slides[0].headline in {hook.text for hook in hooks} | {hook.source_support for hook in hooks}


async def test_plan_slides_can_take_an_explicit_hook_choice(db):
    carousel, job_id = await researched_job(db)
    await carousel.draft_narrative(job_id)
    hooks = await repo.get_hook_candidates(db, job_id)
    wanted = hooks[-1]

    bundle = await carousel.plan_slides(job_id, hook_id=wanted.id)

    assert bundle.job.selected_hook_id == wanted.id
    assert bundle.slides[0].headline in {wanted.text, wanted.source_support}


async def test_plan_slides_rejects_an_unknown_hook_id(db):
    carousel, job_id = await researched_job(db)
    await carousel.draft_narrative(job_id)

    with pytest.raises(NotFoundError):
        await carousel.plan_slides(job_id, hook_id=999_999)


async def test_plan_slides_keeps_the_source_warnings_visible(db):
    carousel, job_id = await researched_job(db, context=qa_context(facts=[QA_FACTS[0]]))
    await carousel.draft_narrative(job_id)

    bundle = await carousel.plan_slides(job_id)

    assert bundle.job.warnings
    assert json.loads(bundle.job.hashtags_json)


async def test_plan_slides_requires_the_research_step(db):
    carousel = factory(db)
    job = await carousel.create_job(source_type="github_issue", source_ref=ISSUE_URL)

    with pytest.raises(CarouselError):
        await carousel.plan_slides(job.id)