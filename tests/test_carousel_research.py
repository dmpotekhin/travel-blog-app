"""Research phase (Phase 2): the service turns a source ref into audited context."""

import json

import pytest

from core.config import Config
from core.database import Database
from core.exceptions import SourceResolutionError, StateTransitionError
from core.models import (
    CarouselSourceContext,
    CarouselSourceType,
    CarouselVertical,
    SourceFact,
)
from modules.carousels import enums as ce
from modules.carousels.service import CarouselFactory
from modules.carousels.sources.mock import MockSourceResolver

ISSUE_URL = "https://github.com/acme/tool/issues/7"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "research.db"))
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


def qa_context(**overrides) -> CarouselSourceContext:
    payload = dict(
        source_type=CarouselSourceType.GITHUB_ISSUE,
        vertical=CarouselVertical.QA,
        source_url=ISSUE_URL,
        external_id="acme/tool#7",
        title="Flaky test in CI",
        summary="Тест падает только в CI.",
        facts=["Тест падает в 3 из 10 прогонов."],
        sourced_facts=[
            SourceFact(
                text="Тест падает в 3 из 10 прогонов.",
                source_excerpt="Падает в 3 из 10 прогонов.",
                source_ref="comment:0",
                verified=True,
            )
        ],
        confidence=0.82,
    )
    payload.update(overrides)
    return CarouselSourceContext(**payload)


async def research_job(db: Database, context: CarouselSourceContext, **overrides):
    carousel = factory(db)
    job = await carousel.create_job(source_type="github_issue", source_ref=ISSUE_URL)
    assert job.id is not None
    updated = await carousel.research(job.id, resolver=MockSourceResolver(context=context))
    return carousel, job.id, updated


async def test_research_marks_the_job_researched(db):
    _, job_id, updated = await research_job(db, qa_context())
    assert updated.status is ce.CarouselStatus.RESEARCHED
    assert updated.confidence == pytest.approx(0.82)


async def test_research_stores_an_audit_row_with_the_context(db):
    _, job_id, _ = await research_job(db, qa_context())
    rows = await db.list_carousel_sources(job_id=job_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.resolver == "mock"
    assert row.canonical_url == ISSUE_URL
    assert row.confidence == pytest.approx(0.82)
    payload = json.loads(row.context_json)
    assert payload["facts"] == ["Тест падает в 3 из 10 прогонов."]
    assert payload["sourced_facts"][0]["source_ref"] == "comment:0"


async def test_research_adopts_the_detected_vertical_when_the_job_has_none(db):
    _, _, updated = await research_job(db, qa_context())
    assert updated.vertical is ce.CarouselVertical.QA


async def test_explicit_vertical_wins_over_detection(db):
    carousel = factory(db)
    job = await carousel.create_job(
        source_type="github_issue", source_ref=ISSUE_URL, vertical="travel"
    )
    assert job.id is not None
    updated = await carousel.research(
        job.id, resolver=MockSourceResolver(context=qa_context())
    )
    assert updated.vertical is ce.CarouselVertical.TRAVEL


async def test_low_confidence_source_is_flagged_for_manual_confirmation(db):
    thin = qa_context(facts=[], sourced_facts=[], confidence=0.2)
    _, _, updated = await research_job(db, thin)
    assert any("low confidence" in warning.lower() for warning in updated.warnings)


async def test_research_failure_marks_the_job_failed_and_reraises(db):
    carousel = factory(db)
    job = await carousel.create_job(source_type="github_issue", source_ref=ISSUE_URL)
    assert job.id is not None

    class Broken(MockSourceResolver):
        name = "broken"

        async def resolve(self, source_ref: str) -> CarouselSourceContext:
            raise SourceResolutionError("GitHub returned 404 for acme/tool#7")

    with pytest.raises(SourceResolutionError):
        await carousel.research(job.id, resolver=Broken())

    failed = await carousel.get_job(job.id)
    assert failed.status is ce.CarouselStatus.FAILED
    assert "404" in failed.error_message


async def test_research_twice_is_refused_by_the_state_machine(db):
    carousel, job_id, _ = await research_job(db, qa_context())
    with pytest.raises(StateTransitionError):
        await carousel.research(job_id, resolver=MockSourceResolver(context=qa_context()))


async def test_dry_run_uses_the_mock_resolver_and_invents_nothing(db):
    carousel = factory(db)  # dry_run=True by default
    job = await carousel.create_job(source_type="github_issue", source_ref=ISSUE_URL)
    assert job.id is not None
    updated = await carousel.research(job.id)
    assert updated.status is ce.CarouselStatus.RESEARCHED
    assert json.loads(updated.source_context_json)["facts"] == []
    assert any("dry-run" in warning.lower() for warning in updated.warnings)
    rows = await db.list_carousel_sources(job_id=job.id)
    assert rows and rows[0].resolver == "mock"
    assert json.loads(rows[0].raw_payload_json)["mock"] is True


async def test_status_report_exposes_the_warnings_after_research(db):
    carousel, job_id, _ = await research_job(db, qa_context(facts=[], confidence=0.1))
    report = await carousel.status_report(job_id)
    assert report["needs_manual_input"] is True
    assert report["warnings"]
