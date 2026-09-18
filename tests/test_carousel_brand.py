"""Brand strategy wiring (config ``carousels.brand``): mix, builder angle, CTA.

The factory is a brand surface: the hook ranking reads the content mix, a travel
carousel must keep a builder angle (warning, never a block, in supervised mode)
and the CTA strings come from ``cta_funnel`` instead of being hardcoded.
"""

import json
from typing import List

import pytest

from core.config import CarouselBrandConfig, load_config_file
from core.models import (
    CarouselSourceContext,
    CarouselSourceType,
    CarouselVertical,
    SlideType,
    SourceFact,
)
from modules.carousels.hooks import HookEngine
from modules.carousels.hooks.engine import FAMILY_BY_PATTERN
from modules.carousels.narrative import SlidePlanner
from modules.carousels.vertical_profiles import profile_for

#: Primary face: an automated pipeline, i.e. the builder identity.
VIBE_FACT = "Локальный агент собрал контент-фабрику из архива за 12 вечеров."
#: Travel, but framed as a system (builder angle keywords from the config).
BUILDER_TRAVEL_FACT = "Маршрут собран как система: GPS и EXIF-датасет на 12 дней."
#: Travel as pure lifestyle — no builder angle keyword anywhere in it.
PURE_TRAVEL_FACT = "Местные никогда не платят чаевые в чайных, так принято."

TRAVEL_FACTS = [
    "Поезд Пекин — Сиань идёт около пяти часов.",
    "Мы приехали в закрытый сезон и половина маршрута не работала.",
    PURE_TRAVEL_FACT,
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
FACTS_BY_VERTICAL = {
    CarouselVertical.TRAVEL: TRAVEL_FACTS,
    CarouselVertical.QA: QA_FACTS,
    CarouselVertical.VIBECODING: VIBE_FACTS,
}


def brand() -> CarouselBrandConfig:
    """The brand strategy as it is actually loaded from config.yaml."""
    return load_config_file().carousels.brand


def make_context(
    vertical: CarouselVertical,
    facts: List[str],
    *,
    title: str = "Источник",
) -> CarouselSourceContext:
    return CarouselSourceContext(
        source_type=CarouselSourceType.URL,
        vertical=vertical,
        source_url="https://example.com/post",
        canonical_url="https://example.com/post",
        title=title,
        facts=list(facts),
        sourced_facts=[
            SourceFact(text=fact, source_excerpt=fact, source_ref=f"p:{index}", verified=True)
            for index, fact in enumerate(facts)
        ],
        confidence=0.9,
    )


def test_brand_config_loaded():
    config = load_config_file()
    brand_config = config.carousels.brand

    assert isinstance(brand_config, CarouselBrandConfig)
    assert "vibecoder" in brand_config.primary_identity
    assert brand_config.secondary_identity and brand_config.trust_layer

    # the content mix is what the hook ranking multiplies with
    assert brand_config.content_mix.vibecoding == 0.60
    assert brand_config.content_mix.travel_as_case_study == 0.25
    assert brand_config.content_mix.qa_trust == 0.10
    assert brand_config.content_mix.personal_lifestyle == 0.05

    # travel is a case study of the AI stack, not a lifestyle feed
    assert brand_config.travel_rules.require_builder_angle is True
    assert brand_config.travel_rules.warning_only is True
    assert brand_config.travel_rules.max_pure_travel_percentage == 30
    assert "пайплайн" in brand_config.travel_rules.builder_angle_keywords
    assert brand_config.travel_rules.has_builder_angle(BUILDER_TRAVEL_FACT) is True
    assert brand_config.travel_rules.has_builder_angle(PURE_TRAVEL_FACT) is False

    # QA is the trust layer, not a fourth niche
    assert brand_config.qa_rules.role == "trust_layer"
    assert "invented_metrics" in brand_config.qa_rules.forbidden
    assert "flaky_test" in brand_config.qa_rules.reliability_categories
    assert brand_config.vibecoding_rules.role == "primary_brand"

    # the funnel: every vertical has its own CTA, and it comes from the config
    funnel = brand_config.cta_funnel
    assert funnel.destination == "telegram_lead_magnet"
    for vertical in (CarouselVertical.TRAVEL, CarouselVertical.QA, CarouselVertical.VIBECODING):
        assert funnel.cta_for(vertical) == funnel.default_cta_by_vertical[vertical.value]

    # the new section does not disturb the existing carousels.* keys
    assert config.carousels.mode == "supervised"
    assert config.carousels.require_human_approval is True
    assert config.carousels.slide_count == 6


def test_travel_builder_angle_warning():
    brand_config = brand()

    pure = SlidePlanner(brand=brand_config).plan(
        make_context(CarouselVertical.TRAVEL, TRAVEL_FACTS),
        vertical=CarouselVertical.TRAVEL,
    )

    missing = [warning for warning in pure.warnings if warning.startswith("TRAVEL_BUILDER_ANGLE_MISSING")]
    assert len(missing) == 1
    assert "pure lifestyle" in missing[0]
    # supervised mode: this is a warning, never a block — six slides still ship
    assert len(pure.slides) == 6

    framed = SlidePlanner(brand=brand_config).plan(
        make_context(CarouselVertical.TRAVEL, [BUILDER_TRAVEL_FACT] + TRAVEL_FACTS),
        vertical=CarouselVertical.TRAVEL,
    )
    assert not [w for w in framed.warnings if w.startswith("TRAVEL_BUILDER_ANGLE_MISSING")]

    # a QA or vibecoding plan never gets the travel guard
    qa_plan = SlidePlanner(brand=brand_config).plan(
        make_context(CarouselVertical.QA, QA_FACTS), vertical=CarouselVertical.QA
    )
    assert not [w for w in qa_plan.warnings if w.startswith("TRAVEL_BUILDER_ANGLE_MISSING")]

    # without a brand config the guard stays silent (pre-brand behaviour)
    plain = SlidePlanner().plan(
        make_context(CarouselVertical.TRAVEL, TRAVEL_FACTS),
        vertical=CarouselVertical.TRAVEL,
    )
    assert not [w for w in plain.warnings if w.startswith("TRAVEL_BUILDER_ANGLE_MISSING")]


def test_hook_brand_fit_vibecoding_bonus():
    brand_config = brand()
    context = make_context(
        CarouselVertical.HYBRID, [VIBE_FACT, BUILDER_TRAVEL_FACT, PURE_TRAVEL_FACT]
    )

    plain = {
        candidate.source_support: candidate
        for candidate in HookEngine().generate(context, vertical=CarouselVertical.HYBRID, limit=10)
    }
    branded_list = HookEngine(brand=brand_config).generate(
        context, vertical=CarouselVertical.HYBRID, limit=10
    )
    branded = {candidate.source_support: candidate for candidate in branded_list}

    assert branded and set(plain) == set(branded)  # same input, same assignment

    def fit(candidate) -> float:
        return json.loads(candidate.scores_json)["brand_fit"]

    faces = {fact: FAMILY_BY_PATTERN[candidate.pattern] for fact, candidate in branded.items()}
    assert CarouselVertical.VIBECODING in faces.values()

    for fact, candidate in branded.items():
        if faces[fact] is CarouselVertical.VIBECODING:
            assert fit(candidate) == pytest.approx(1.15)  # primary face, top of the band
            assert candidate.score >= plain[fact].score
        elif faces[fact] is CarouselVertical.TRAVEL:
            if brand_config.travel_rules.has_builder_angle(fact):
                assert fit(candidate) == pytest.approx(1.06, abs=0.01)  # case study
            else:
                assert fit(candidate) == pytest.approx(0.85)  # pure lifestyle: smallest slot
                assert candidate.score < plain[fact].score
    assert fit(branded[PURE_TRAVEL_FACT]) < fit(branded[VIBE_FACT])

    # the factor ranks: the gap between the primary face and pure lifestyle widens
    def best_vibe(candidates) -> float:
        return max(
            candidate.score
            for candidate in candidates
            if FAMILY_BY_PATTERN[candidate.pattern] is CarouselVertical.VIBECODING
        )

    plain_gap = best_vibe(plain.values()) - plain[PURE_TRAVEL_FACT].score
    branded_gap = best_vibe(branded.values()) - branded[PURE_TRAVEL_FACT].score
    assert branded_gap > plain_gap

    # no brand configured → no brand_fit term at all (old ranking untouched)
    assert all("brand_fit" not in json.loads(candidate.scores_json) for candidate in plain.values())


def test_cta_default_by_vertical():
    brand_config = brand()

    for vertical, facts in FACTS_BY_VERTICAL.items():
        result = SlidePlanner(brand=brand_config).plan(
            make_context(vertical, facts), vertical=vertical
        )
        cta = next((slide for slide in result.slides if slide.slide_type is SlideType.CTA), None)
        if cta is None:
            # The QA sequence ends with prevention_checklist and carries no CTA
            # slide, so its funnel entry is configured but not rendered yet.
            assert vertical is CarouselVertical.QA
            assert brand_config.cta_funnel.cta_for(vertical)
            continue

        assert cta.headline == brand_config.cta_funnel.cta_for(vertical)
        # the config wins over the hardcoded profile default
        assert cta.headline != profile_for(vertical).cta_style

    # an operator's own CTA wins over the configured one
    own = SlidePlanner(brand=brand_config, cta_override="Мой CTA, не из конфига")
    own_plan = own.plan(
        make_context(CarouselVertical.VIBECODING, VIBE_FACTS),
        vertical=CarouselVertical.VIBECODING,
    )
    assert own_plan.slides[-1].headline == "Мой CTA, не из конфига"

    # without a brand config, the profile's own CTA is still the fallback
    plain = SlidePlanner().plan(
        make_context(CarouselVertical.VIBECODING, VIBE_FACTS),
        vertical=CarouselVertical.VIBECODING,
    )
    assert plain.slides[-1].headline == profile_for(CarouselVertical.VIBECODING).cta_style
