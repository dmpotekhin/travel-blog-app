"""Slide planner (Phase 3): six slides that only say what the source said."""

from typing import List

from core.models import (
    CarouselHookCandidate,
    CarouselSourceContext,
    CarouselSourceType,
    CarouselVertical,
    CodeSnippet,
    HookCategory,
    Metric,
    SourceFact,
)
from modules.carousels.narrative import SlidePlanner
from modules.carousels.vertical_profiles import profile_for

QA_FACTS = [
    "Тест падает только в CI, локально зелёный.",
    "Падает в 3 из 10 прогонов.",
    "Похоже на shared state между тестами.",
    "Причина оказалась в порядке запуска тестов.",
]
TRAVEL_FACTS = [
    "Поезд Пекин — Сиань идёт около пяти часов.",
    "Мы приехали в закрытый сезон и половина маршрута не работала.",
    "Местные никогда не платят чаевые в чайных.",
]
VIBE_FACTS = [
    "Собрал агента за вечер на DeepSeek.",
    "Пайплайн делает посты из фотоархива автоматически.",
    "Локальная модель отвечает за черновики.",
]

CODE = CodeSnippet(
    language="python",
    code="def isolated_state():\n    return {}",
    caption="фикстура изоляции",
    source_ref="pr_diff:tests/test_state.py",
    truncated=False,
)
METRIC = Metric(
    name="additions",
    value=12,
    unit="lines",
    raw_value="12",
    source_ref="pr:42",
    is_verified=True,
)


def make_context(
    vertical: CarouselVertical,
    facts: List[str],
    *,
    code_snippets=None,
    metrics=None,
    images=None,
) -> CarouselSourceContext:
    return CarouselSourceContext(
        source_type=CarouselSourceType.GITHUB_ISSUE,
        vertical=vertical,
        source_url="https://github.com/acme/tool/issues/7",
        canonical_url="https://github.com/acme/tool/issues/7",
        title="Тест падает только в CI",
        facts=list(facts),
        sourced_facts=[
            SourceFact(text=fact, source_excerpt=fact, source_ref=f"issue:{i}", verified=True)
            for i, fact in enumerate(facts)
        ],
        code_snippets=list(code_snippets or []),
        metrics=list(metrics or []),
        images=list(images or []),
        confidence=0.9,
    )


def plan(vertical: CarouselVertical, facts: List[str], **kwargs):
    context = make_context(vertical, facts, **kwargs)
    return SlidePlanner().plan(context, vertical=vertical, job_id=7)


def test_plan_has_six_slides_following_the_vertical_profile():
    result = plan(CarouselVertical.QA, QA_FACTS)
    profile = profile_for(CarouselVertical.QA)

    assert len(result.slides) == 6
    assert [slide.order for slide in result.slides] == [1, 2, 3, 4, 5, 6]
    assert [slide.slide_type for slide in result.slides] == list(profile.slide_sequence)
    assert result.vertical is CarouselVertical.QA
    assert result.job_id == 7


def test_every_text_block_is_supported_by_the_source():
    context = make_context(CarouselVertical.QA, QA_FACTS)
    result = SlidePlanner().plan(context, vertical=CarouselVertical.QA)

    from modules.carousels.fact_guard import FactGuard

    guard = FactGuard(context)
    for slide in result.slides:
        if slide.slide_type.value == "cta":
            continue
        for text in [slide.headline, slide.subheadline, slide.body_text, *slide.bullets]:
            if text:
                assert guard.is_supported(text), f"{slide.slide_type} says unsupported: {text!r}"


def test_technical_slides_carry_real_code_and_metrics():
    qa_result = plan(CarouselVertical.QA, QA_FACTS, code_snippets=[CODE])
    fix_slide = next(slide for slide in qa_result.slides if slide.slide_type.value == "fix_code")
    assert fix_slide.code() is not None
    assert fix_slide.code().language == "python"
    assert "isolated_state" in fix_slide.code().code
    assert fix_slide.code().source_ref == "pr_diff:tests/test_state.py"

    vibe_result = plan(CarouselVertical.VIBECODING, VIBE_FACTS, metrics=[METRIC])
    metrics_slide = next(
        slide for slide in vibe_result.slides if slide.slide_type.value == "metrics_comparison"
    )
    assert [metric.name for metric in metrics_slide.metrics()] == ["additions"]
    assert metrics_slide.metrics()[0].is_verified is True


def test_metrics_are_never_invented():
    result = plan(CarouselVertical.VIBECODING, VIBE_FACTS)
    metrics_slide = next(
        slide for slide in result.slides if slide.slide_type.value == "metrics_comparison"
    )

    assert metrics_slide.metrics() == []
    assert any("metric" in warning.lower() for warning in result.warnings)


def test_cta_slide_makes_no_factual_claims():
    result = plan(CarouselVertical.TRAVEL, TRAVEL_FACTS)
    cta = result.slides[-1]

    assert cta.slide_type.value == "cta"
    assert cta.bullets == []
    assert cta.source_refs == []
    assert cta.headline


def test_plan_is_deterministic():
    first = plan(CarouselVertical.QA, QA_FACTS, code_snippets=[CODE])
    second = plan(CarouselVertical.QA, QA_FACTS, code_snippets=[CODE])

    assert first.model_dump() == second.model_dump()


def test_slides_get_alt_text_and_source_refs():
    result = plan(CarouselVertical.QA, QA_FACTS, code_snippets=[CODE])

    for slide in result.slides:
        assert slide.alt_text, f"slide {slide.order} has no alt text"
    content_slides = [slide for slide in result.slides if slide.slide_type.value != "cta"]
    assert any(slide.source_refs for slide in content_slides)


def test_bottom_zone_budget_is_respected():
    profile = profile_for(CarouselVertical.QA)
    result = plan(CarouselVertical.QA, QA_FACTS, code_snippets=[CODE])

    for slide in result.slides:
        assert len(slide.headline) <= profile.max_headline_chars
        assert len(slide.body_text) <= profile.max_body_chars
        assert len(slide.bullets) <= profile.bullets_max


def test_hero_hook_line_does_not_come_back_on_a_later_slide():
    """A repeated sentence makes a carousel read as a loop — regression guard."""
    facts = [
        "Поезд Пекин — Сиань идёт около пяти часов.",
        "Мы приехали в закрытый сезон, половина маршрута не работала.",
        "Местные не платят чаевые в чайных, это не принято.",
        "Сиань даёт терракотовую армию и ночной рынок.",
        "Чэнду — это панды и медленный ритм города.",
    ]
    context = make_context(CarouselVertical.TRAVEL, facts)
    hook = CarouselHookCandidate(
        pattern="travel_closed_season",
        category=HookCategory.SEASONALITY.value,
        text=facts[1],
        source_support=facts[1],
        score=0.62,
    )
    result = SlidePlanner().plan(
        context, vertical=CarouselVertical.TRAVEL, hook=hook, job_id=7
    )
    assert result.slides[0].headline == facts[1]
    later_blocks = []
    for slide in result.slides[1:]:
        later_blocks.extend([slide.headline, slide.subheadline, slide.body_text])
        later_blocks.extend(slide.bullets)
    assert facts[1] not in later_blocks


def test_thin_source_is_flagged_not_padded():
    result = plan(CarouselVertical.QA, [QA_FACTS[0]])

    assert len(result.slides) == 6
    assert result.warnings
