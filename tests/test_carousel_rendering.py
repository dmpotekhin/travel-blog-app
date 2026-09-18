"""Rendering + verification through the service (Phase 4)."""

import json
from pathlib import Path

import pytest
from PIL import Image

from core.config import Config
from core.database import Database
from core.exceptions import CarouselError
from core.models import (
    CarouselSourceContext,
    CarouselSourceType,
    CarouselStatus,
    CarouselVerificationStatus,
    CarouselVertical,
    CodeSnippet,
    Metric,
    SourceFact,
)
from modules.carousels import database_helpers as repo
from modules.carousels.render import BaseSlideRenderer, RenderedSlide, SolidGradientProvider
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


class BrokenRenderer(BaseSlideRenderer):
    """Writes the wrong size and no layout sidecar — verification must notice."""

    name = "broken"

    def render(
        self,
        slide,
        *,
        profile,
        width,
        height,
        safe_zone_pixels,
        destination,
        context=None,
        attempt=0,
    ) -> RenderedSlide:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (width // 2, height // 2), (12, 12, 12)).save(
            destination, format="JPEG"
        )
        return RenderedSlide(
            order=slide.order,
            path=str(destination),
            layout_path="",
            width=width // 2,
            height=height // 2,
            image_format="jpeg",
            byte_size=destination.stat().st_size,
        )


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "render.db"))
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
        code_snippets=[
            CodeSnippet(
                language="python",
                code="def isolated_state():\n    return {}",
                caption="изоляция состояния",
                source_ref="pr_diff:tests/test_state.py",
                truncated=False,
            )
        ],
        metrics=[
            Metric(
                name="comments",
                value=2,
                unit="comments",
                raw_value="2",
                source_ref="issue:7",
                is_verified=True,
            )
        ],
        confidence=0.88,
    )


async def planned_job(db: Database, tmp_path: Path, **overrides):
    carousel = factory(db, tmp_path, **overrides)
    job = await carousel.create_job(source_type="github_issue", source_ref=ISSUE_URL)
    assert job.id is not None
    await carousel.research(job.id, resolver=MockSourceResolver(context=qa_context()))
    await carousel.draft_narrative(job.id)
    await carousel.plan_slides(job.id)
    return carousel, job.id


async def test_render_slides_writes_six_jpgs(db, tmp_path):
    carousel, job_id = await planned_job(db, tmp_path)
    bundle = await carousel.render_slides(job_id)
    assert bundle.job.status is CarouselStatus.RENDERED
    assert len(bundle.slides) == 6
    for slide in bundle.slides:
        path = Path(slide.final_image_path)
        assert path.is_file()
        assert path.suffix == ".jpg"
        assert path.with_suffix(".layout.json").is_file()
        with Image.open(path) as image:
            assert image.size == (768, 1376)
            assert (image.format or "").lower() == "jpeg"


async def test_verify_slides_marks_a_good_render_verified(db, tmp_path):
    carousel, job_id = await planned_job(db, tmp_path)
    await carousel.render_slides(job_id)
    reports = await carousel.verify_slides(job_id)
    job = await carousel.get_job(job_id)
    assert job.status is CarouselStatus.VERIFIED
    assert len(reports) == 6
    assert all(report.passed for report in reports), [report.issues for report in reports]
    slides = await repo.get_carousel_slides(db, job_id)
    assert all(slide.verification_status is CarouselVerificationStatus.PASSED for slide in slides)
    assert all(slide.quality_score > 0 for slide in slides)


async def test_failed_verification_regenerates_then_asks_for_revision(db, tmp_path):
    carousel, job_id = await planned_job(db, tmp_path)
    await carousel.render_slides(job_id, renderer=BrokenRenderer())
    reports = await carousel.verify_slides(job_id, renderer=BrokenRenderer())
    job = await carousel.get_job(job_id)
    assert job.status is CarouselStatus.NEEDS_REVISION
    assert any("need revision" in warning for warning in job.warnings)
    assert all(not report.passed for report in reports)
    slides = await repo.get_carousel_slides(db, job_id)
    assert all(slide.regeneration_count == carousel.settings.max_regeneration_attempts for slide in slides)
    assert all(slide.verification_status is CarouselVerificationStatus.NEEDS_REVISION for slide in slides)
    issues = json.loads(slides[0].verification_issues_json)
    assert any("layout metadata missing" in issue for issue in issues)


async def test_render_requires_a_plan(db, tmp_path):
    carousel = factory(db, tmp_path)
    job = await carousel.create_job(source_type="github_issue", source_ref=ISSUE_URL)
    with pytest.raises(CarouselError):
        await carousel.render_slides(job.id)


async def test_unknown_renderer_is_refused_not_silently_swapped(db, tmp_path):
    carousel = factory(db, tmp_path, renderer="html")
    with pytest.raises(CarouselError):
        carousel.renderer_for()


async def test_renderer_uses_the_painted_background_in_dry_run(db, tmp_path):
    carousel = factory(db, tmp_path)
    renderer = carousel.renderer_for()
    assert renderer.background_provider is not None
    assert renderer.background_provider.name == SolidGradientProvider.name
