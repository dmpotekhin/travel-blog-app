"""Carousel enums, models and slide-plan JSON (Phase 1)."""

import json

import pytest

from core.config import Config
from core.exceptions import CarouselError
from core.models import CarouselSlidePlan
from modules.carousels import enums as ce
from modules.carousels import models as cm


def test_vertical_values_are_stable_strings():
    assert ce.CarouselVertical.TRAVEL.value == "travel"
    assert ce.CarouselVertical.QA.value == "qa"
    assert ce.CarouselVertical.VIBECODING.value == "vibecoding"
    assert ce.CarouselVertical.HYBRID.value == "hybrid"
    # str-Enum: usable as a plain string key (the state machine relies on it)
    assert ce.CarouselVertical.QA == "qa"


def test_source_types_cover_url_and_github_entry_points():
    values = {member.value for member in ce.CarouselSourceType}
    assert {
        "url",
        "github_repo",
        "github_issue",
        "github_pr",
        "github_discussion",
        "github_release",
        "city",
        "manual_topic",
        "qa_artifact",
        "vibecoding_session",
    } <= values


def test_status_machine_members_present():
    values = {member.value for member in ce.CarouselStatus}
    assert "pending" in values and "awaiting_approval" in values and "analyzed" in values


def test_slide_type_aliases_resolve_to_canonical_members():
    assert ce.resolve_slide_type("symptom_list") is ce.SlideType.SYMPTOM
    assert ce.resolve_slide_type("code_fix") is ce.SlideType.FIX_CODE
    assert ce.resolve_slide_type("CODE-BLOCK") is ce.SlideType.CODE_BLOCK
    assert ce.resolve_slide_type(ce.SlideType.CTA) is ce.SlideType.CTA


def test_unknown_slide_type_is_rejected():
    with pytest.raises(CarouselError):
        ce.resolve_slide_type("no_such_slide")


def test_slide_type_aliases_never_drift_from_core():
    """enums.SLIDE_TYPE_ALIASES is built on core.models.SLIDE_TYPE_ALIASES."""
    from core.models import SLIDE_TYPE_ALIASES as core_aliases

    for alias, target in core_aliases.items():
        assert ce.SLIDE_TYPE_ALIASES[alias] == ce.SlideType(target)


def test_source_type_resolution_and_aliases():
    assert ce.resolve_source_type("github_issue") is ce.CarouselSourceType.GITHUB_ISSUE
    assert ce.resolve_source_type("url") is ce.CarouselSourceType.URL
    with pytest.raises(CarouselError):
        ce.resolve_source_type("carrier_pigeon")
    for value in ce.source_type_aliases().values():
        assert isinstance(value, ce.CarouselSourceType)


def test_technical_slide_types_are_marked_for_deterministic_rendering():
    assert ce.is_technical_slide("code_block") is True
    assert ce.is_technical_slide(ce.SlideType.FIX_CODE) is True
    assert ce.is_technical_slide(ce.SlideType.METRICS_COMPARISON) is True
    assert ce.is_technical_slide(ce.SlideType.PHOTO_CARD) is False
    # no slide type may be both technical and purely visual
    assert not (ce.TECHNICAL_SLIDE_TYPES & ce.VISUAL_SLIDE_TYPES)


def test_hook_categories_per_vertical_are_real_members():
    for vertical in ce.CarouselVertical:
        categories = ce.hook_categories_for(vertical)
        assert categories, vertical
        assert all(isinstance(category, ce.HookCategory) for category in categories)


def test_hook_categories_cover_spec_examples():
    qa = {c.value for c in ce.hook_categories_for("qa")}
    vibecoding = {c.value for c in ce.hook_categories_for("vibecoding")}
    travel = {c.value for c in ce.hook_categories_for("travel")}
    assert {"failure", "flaky_test", "root_cause"} <= qa
    assert {"local_ai", "agent_workflow", "evening_build"} <= vibecoding
    assert {"hidden_place", "budget", "route_guide"} <= travel


def test_carousel_model_helpers_round_trip_json():
    job = cm.CarouselJob(
        vertical=ce.CarouselVertical.QA,
        source_type=ce.CarouselSourceType.GITHUB_ISSUE,
        target_platforms_json=json.dumps(["tiktok", "instagram"]),
        hashtags_json=json.dumps(["#qa"]),
        warnings_json=json.dumps(["source not resolved yet"]),
    )
    assert job.target_platforms == ["tiktok", "instagram"]
    assert job.hashtags == ["#qa"]
    assert job.warnings == ["source not resolved yet"]
    assert job.requires_approval is True
    assert job.source_context() is None


def test_supervised_is_the_default_autonomy_mode():
    job = cm.CarouselJob()
    assert job.autonomy_mode is ce.CarouselAutonomyMode.SUPERVISED
    assert job.dry_run is False
    assert job.confidence == 0.0
    assert job.status is ce.CarouselStatus.PENDING


def test_source_context_keeps_facts_quotes_and_confidence():
    context = cm.CarouselSourceContext(
        source_type=ce.CarouselSourceType.GITHUB_ISSUE,
        vertical=ce.CarouselVertical.QA,
        source_url="https://github.com/dmitrypotekhin/travel-blog-app/issues/7",
        title="Flaky test only on Fridays",
        facts=["runs green locally"],
        quotes=["we lost two days"],
        code_snippets=[cm.CodeSnippet(language="python", code="def test_x(): pass")],
        metrics=[cm.Metric(name="ci_duration", value=20.0, unit="min", is_verified=True)],
        images=[cm.ImageAsset(url_or_path="ci.png", alt_text="CI log", is_real_photo=False)],
        confidence=0.7,
        warnings=["release notes truncated"],
    )
    payload = json.loads(context.model_dump_json())
    assert payload["facts"] == ["runs green locally"]
    assert payload["code_snippets"][0]["language"] == "python"
    assert payload["metrics"][0]["is_verified"] is True
    assert payload["images"][0]["is_real_photo"] is False
    assert payload["confidence"] == 0.7


def test_slide_plan_json_round_trips():
    plan = CarouselSlidePlan(
        job_id=1,
        vertical=ce.CarouselVertical.QA,
        title="Flaky test, который стоил релиза",
        caption="Разбор flaky test",
        hashtags=["#qa", "#testing"],
        slides=[
            cm.CarouselSlide(order=1, slide_type=ce.SlideType.HERO_HOOK, headline="Один flaky test"),
            cm.CarouselSlide(order=2, slide_type=ce.SlideType.SYMPTOM, headline="Симптомы"),
        ],
    )
    restored = CarouselSlidePlan.model_validate(json.loads(plan.model_dump_json()))
    assert len(restored.slides) == 2
    assert restored.slides[1].slide_type is ce.SlideType.SYMPTOM
    assert restored.hashtags == ["#qa", "#testing"]


def test_generate_request_defaults_to_url_and_both_platforms():
    request = cm.CarouselGenerateRequest(source_ref="https://example.com/post")
    assert request.source_type is ce.CarouselSourceType.URL
    assert request.platforms == ["tiktok", "instagram"]
    assert request.vertical is None
    assert request.dry_run is None


def test_config_carousel_section_matches_the_six_slide_contract():
    settings = Config().carousels
    assert settings.slide_count == 6
    assert (settings.resolution.width, settings.resolution.height) == (768, 1376)
    assert settings.format == "jpg"
    assert settings.bottom_safe_zone_percent == 20.0
    assert settings.bottom_safe_zone_pixels == 275
    assert settings.mode == "supervised"
    assert settings.publishing_requires_approval is True
    assert settings.is_full_autonomous is False


def test_format_validator_rejects_png():
    from pydantic import ValidationError

    from core.config import CarouselConfig

    with pytest.raises(ValidationError):
        CarouselConfig(format="png")
