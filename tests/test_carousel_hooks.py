"""Hook engine + fact guard (Phase 3): ranked hooks, never invented."""

from typing import List

import pytest

from core.models import (
    CarouselSourceContext,
    CarouselSourceType,
    CarouselVertical,
    SourceFact,
)
from modules.carousels.enums import hook_categories_for
from modules.carousels.fact_guard import FactGuard
from modules.carousels.hooks import HookEngine
from modules.carousels.vertical_profiles import profile_for

TRAVEL_FACTS = [
    "Поезд Пекин — Сиань идёт около пяти часов.",
    "Мы приехали в закрытый сезон и половина маршрута не работала.",
    "Местные никогда не платят чаевые в чайных.",
]
QA_FACTS = [
    "Тест падает только в CI, локально зелёный.",
    "Падает в 3 из 10 прогонов.",
    "Похоже на shared state между тестами.",
]
VIBE_FACTS = [
    "Собрал агента за вечер на DeepSeek.",
    "Пайплайн делает посты из фотоархива автоматически.",
    "Локальная модель отвечает за черновики.",
]


def make_context(
    vertical: CarouselVertical,
    facts: List[str],
    *,
    title: str = "",
    code_snippets=None,
    metrics=None,
) -> CarouselSourceContext:
    return CarouselSourceContext(
        source_type=CarouselSourceType.URL,
        vertical=vertical,
        source_url="https://example.com/post",
        canonical_url="https://example.com/post",
        title=title or "Источник",
        facts=list(facts),
        sourced_facts=[
            SourceFact(text=fact, source_excerpt=fact, source_ref=f"p:{i}", verified=True)
            for i, fact in enumerate(facts)
        ],
        code_snippets=list(code_snippets or []),
        metrics=list(metrics or []),
        confidence=0.9,
    )


def test_hook_engine_returns_ranked_candidates_with_source_support():
    candidates = HookEngine().generate(
        make_context(CarouselVertical.QA, QA_FACTS), vertical=CarouselVertical.QA
    )

    assert candidates
    assert all(candidate.source_support for candidate in candidates)
    assert all(candidate.source_support in QA_FACTS for candidate in candidates)
    scores = [candidate.score for candidate in candidates]
    assert scores == sorted(scores, reverse=True)


def test_hook_engine_never_invents_when_the_source_is_empty():
    engine = HookEngine()

    assert engine.generate(make_context(CarouselVertical.QA, []), vertical=CarouselVertical.QA) == []
    assert engine.generate(make_context(CarouselVertical.TRAVEL, []), vertical=CarouselVertical.TRAVEL) == []


@pytest.mark.parametrize(
    "vertical, facts",
    [
        (CarouselVertical.TRAVEL, TRAVEL_FACTS),
        (CarouselVertical.QA, QA_FACTS),
        (CarouselVertical.VIBECODING, VIBE_FACTS),
    ],
)
def test_hook_categories_stay_inside_the_vertical_family(vertical, facts):
    profile = profile_for(vertical)
    allowed = {category.value for category in hook_categories_for(vertical)}

    candidates = HookEngine().generate(make_context(vertical, facts), vertical=vertical)

    assert candidates
    assert {candidate.category for candidate in candidates} <= allowed
    assert set(profile.hook_families) <= {category for category in hook_categories_for(vertical)}


def test_hook_engine_respects_the_limit_and_explains_the_score():
    engine = HookEngine()

    candidates = engine.generate(
        make_context(CarouselVertical.VIBECODING, VIBE_FACTS),
        vertical=CarouselVertical.VIBECODING,
        limit=2,
    )

    assert len(candidates) <= 2
    assert all(candidate.score_breakdown for candidate in candidates)
    assert all(candidate.rationale for candidate in candidates)
    assert all(candidate.expected_emotion for candidate in candidates)


def test_fact_guard_accepts_only_what_the_source_actually_says():
    guard = FactGuard(make_context(CarouselVertical.QA, QA_FACTS))

    assert guard.is_supported("Тест падает только в CI")
    assert guard.is_supported("Падает в 3 из 10 прогонов.")
    assert not guard.is_supported("Тест падал в 40% прогонов")
    assert guard.unsupported_claims(["Падает в 3 из 10 прогонов.", "Починили за 5 минут"]) == [
        "Починили за 5 минут"
    ]


def test_profiles_describe_six_slides_per_vertical():
    for vertical in (
        CarouselVertical.TRAVEL,
        CarouselVertical.QA,
        CarouselVertical.VIBECODING,
        CarouselVertical.HYBRID,
    ):
        profile = profile_for(vertical)
        assert len(profile.slide_sequence) == 6
        assert len(set(profile.slide_sequence)) == 6
        assert profile.hook_families
        assert profile.visual_style
        assert profile.tone


def test_technical_profiles_put_precision_before_beauty():
    assert profile_for(CarouselVertical.QA).precision_first is True
    assert profile_for(CarouselVertical.VIBECODING).precision_first is True
    assert profile_for(CarouselVertical.TRAVEL).precision_first is False
    assert profile_for(CarouselVertical.QA).banned_claims
