"""Renderer (Phase 4): 768x1376 JPG, bottom-safe text, real verification.

Everything runs offline into ``tmp_path``; no Gemini, no network, no uploads.
"""

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from core.config import Config
from core.models import (
    CarouselSlide,
    CarouselSourceContext,
    CarouselSourceType,
    CarouselVertical,
    CodeSnippet,
    ImageAsset,
    Metric,
    SlideType,
    SourceFact,
)
from modules.carousels.render import (
    PillowSlideRenderer,
    SlideVerifier,
    SolidGradientProvider,
    provider_for,
)
from modules.carousels.vertical_profiles import profile_for

WIDTH = 768
HEIGHT = 1376
SAFE = 275  # 20% of 1376


def qa_context() -> CarouselSourceContext:
    facts = [
        "Тест падает только в CI, локально зелёный.",
        "Падает в 3 из 10 прогонов.",
        "Причина оказалась в порядке запуска тестов.",
    ]
    return CarouselSourceContext(
        source_type=CarouselSourceType.GITHUB_PR,
        vertical=CarouselVertical.QA,
        source_url="https://github.com/acme/tool/pull/42",
        title="Флаки-тест в CI",
        summary="Разбор флаки-теста.",
        facts=facts,
        sourced_facts=[
            SourceFact(text=fact, source_excerpt=fact, source_ref=f"comment:{index}", verified=True)
            for index, fact in enumerate(facts)
        ],
        code_snippets=[
            CodeSnippet(
                language="python",
                code="def isolated_state():\n    return {}\n\nassert isolated_state() == {}",
                caption="фикстура изоляции",
                source_ref="pr_diff:tests/test_state.py",
                truncated=False,
            )
        ],
        metrics=[
            Metric(
                name="additions",
                value=12,
                unit="lines",
                raw_value="12",
                source_ref="pr:42",
                is_verified=True,
            )
        ],
    )


def slide_payload(**overrides) -> dict:
    payload = dict(
        order=1,
        slide_type=SlideType.FIX_CODE,
        headline="Тест падает только в CI",
        subheadline="Флаки-тест в CI",
        body_text="Причина оказалась в порядке запуска тестов.",
        code_json=json.dumps(
            {
                "language": "python",
                "code": "def isolated_state():\n    return {}\n\nassert isolated_state() == {}",
                "caption": "фикстура изоляции",
                "source_ref": "pr_diff:tests/test_state.py",
                "truncated": False,
            }
        ),
        source_refs_json=json.dumps(["comment:0", "comment:2"]),
        alt_text="Слайд с фиксом флаки-теста",
        accent_color="#FF3B30",
    )
    payload.update(overrides)
    return payload


def render(slide: CarouselSlide, tmp_path: Path, *, attempt: int = 0, provider=None):
    renderer = PillowSlideRenderer(background_provider=provider or SolidGradientProvider())
    return renderer.render(
        slide,
        profile=profile_for(CarouselVertical.QA),
        width=WIDTH,
        height=HEIGHT,
        safe_zone_pixels=SAFE,
        destination=tmp_path / f"slide_{slide.order:02d}.jpg",
        context=qa_context(),
        attempt=attempt,
    )


def test_renderer_writes_a_768x1376_jpg(tmp_path):
    result = render(CarouselSlide(**slide_payload()), tmp_path)
    assert result.width == WIDTH and result.height == HEIGHT
    assert result.image_format == "jpeg"
    assert result.byte_size > 5000
    with Image.open(result.path) as image:
        assert image.size == (WIDTH, HEIGHT)
        assert (image.format or "").lower() == "jpeg"


def test_text_never_reaches_the_bottom_safe_zone(tmp_path):
    result = render(CarouselSlide(**slide_payload()), tmp_path)
    assert result.text_boxes
    assert result.lowest_text_bottom <= HEIGHT - SAFE
    assert all(box.right <= WIDTH for box in result.text_boxes)


def test_layout_sidecar_is_written_for_the_verifier(tmp_path):
    result = render(CarouselSlide(**slide_payload()), tmp_path)
    sidecar = Path(result.layout_path)
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["width"] == WIDTH and payload["text_boxes"]


def test_rendering_is_deterministic(tmp_path):
    first = render(CarouselSlide(**slide_payload()), tmp_path / "a")
    second = render(CarouselSlide(**slide_payload()), tmp_path / "b")
    assert first.sha256 == second.sha256
    assert hashlib.sha256(Path(first.path).read_bytes()).hexdigest() == first.sha256


def test_code_is_drawn_verbatim_from_the_source(tmp_path):
    slide = CarouselSlide(**slide_payload())
    result = render(slide, tmp_path)
    code_boxes = [box for box in result.text_boxes if box.kind == "code"]
    assert code_boxes
    assert "def isolated_state():" in code_boxes[0].text
    assert code_boxes[0].text.splitlines() == [line for line in slide.code().code.splitlines() if line.strip()]


def test_long_code_is_truncated_with_a_warning(tmp_path):
    long_code = "\n".join(f"line_{index} = {index}" for index in range(200))
    slide = CarouselSlide(
        **slide_payload(code_json=json.dumps({"language": "python", "code": long_code}))
    )
    result = render(slide, tmp_path)
    assert any("truncated" in warning.lower() for warning in result.warnings)
    assert result.lowest_text_bottom <= HEIGHT - SAFE


def test_attempt_shrinks_the_type_instead_of_overflowing(tmp_path):
    slide = CarouselSlide(
        **slide_payload(
            headline=" ".join(["очень длинный заголовок"] * 8),
            body_text=" ".join(["длинный текст"] * 60),
        )
    )
    first = render(slide, tmp_path / "a", attempt=0)
    second = render(slide, tmp_path / "b", attempt=3)
    assert second.lowest_text_bottom <= HEIGHT - SAFE
    assert min(box.font_size for box in second.text_boxes) <= min(
        box.font_size for box in first.text_boxes
    )


def test_empty_slide_is_reported_not_faked(tmp_path):
    slide = CarouselSlide(order=6, slide_type=SlideType.CTA, alt_text="CTA")
    result = render(slide, tmp_path)
    assert result.warnings
    assert result.lowest_text_bottom <= HEIGHT - SAFE


def test_verifier_passes_a_good_slide(tmp_path):
    slide = CarouselSlide(**slide_payload())
    result = render(slide, tmp_path)
    verifier = SlideVerifier(width=WIDTH, height=HEIGHT, safe_zone_pixels=SAFE)
    report = verifier.verify(slide, path=Path(result.path), context=qa_context())
    assert report.passed, report.issues
    assert report.quality_score == pytest.approx(1.0)
    assert report.checks["bottom_zone"] and report.checks["code_integrity"]


def test_verifier_flags_a_wrong_size(tmp_path):
    slide = CarouselSlide(**slide_payload())
    result = render(slide, tmp_path)
    with Image.open(result.path) as image:
        image.resize((700, 1200)).save(result.path, format="JPEG")
    report = SlideVerifier(width=WIDTH, height=HEIGHT, safe_zone_pixels=SAFE).verify(
        slide, path=Path(result.path), context=qa_context()
    )
    assert not report.passed
    assert any("wrong size" in issue for issue in report.issues)


def test_verifier_flags_text_inside_the_safe_zone(tmp_path):
    slide = CarouselSlide(**slide_payload())
    result = render(slide, tmp_path)
    payload = json.loads(Path(result.layout_path).read_text(encoding="utf-8"))
    payload["text_boxes"].append(
        {"kind": "body", "text": "хвост", "x": 10, "y": HEIGHT - 100, "width": 100, "height": 60, "font_size": 30}
    )
    Path(result.layout_path).write_text(json.dumps(payload), encoding="utf-8")
    report = SlideVerifier(width=WIDTH, height=HEIGHT, safe_zone_pixels=SAFE).verify(
        slide, path=Path(result.path), context=qa_context()
    )
    assert any("safe zone" in issue for issue in report.issues)


def test_verifier_flags_a_slide_without_alt_text(tmp_path):
    slide = CarouselSlide(**slide_payload(alt_text=""))
    result = render(slide, tmp_path)
    report = SlideVerifier(width=WIDTH, height=HEIGHT, safe_zone_pixels=SAFE).verify(
        slide, path=Path(result.path), context=qa_context()
    )
    assert any("alt-text" in issue for issue in report.issues)


def test_verifier_flags_a_missing_sidecar(tmp_path):
    slide = CarouselSlide(**slide_payload())
    result = render(slide, tmp_path)
    Path(result.layout_path).unlink()
    report = SlideVerifier(width=WIDTH, height=HEIGHT, safe_zone_pixels=SAFE).verify(
        slide, path=Path(result.path), context=qa_context()
    )
    assert any("layout metadata missing" in issue for issue in report.issues)


def test_verifier_flags_code_that_no_longer_matches_the_source(tmp_path):
    slide = CarouselSlide(**slide_payload())
    result = render(slide, tmp_path)
    tampered = CarouselSlide(
        **slide_payload(code_json=json.dumps({"language": "python", "code": "print('другой код')"}))
    )
    report = SlideVerifier(width=WIDTH, height=HEIGHT, safe_zone_pixels=SAFE).verify(
        tampered, path=Path(result.path), context=qa_context()
    )
    assert any("code on the slide does not match" in issue for issue in report.issues)


def test_verifier_flags_unsupported_text(tmp_path):
    slide = CarouselSlide(**slide_payload(headline="Мы ускорили CI на 40%"))
    result = render(slide, tmp_path)
    report = SlideVerifier(width=WIDTH, height=HEIGHT, safe_zone_pixels=SAFE).verify(
        slide, path=Path(result.path), context=qa_context()
    )
    assert any("not supported by the source" in issue for issue in report.issues)
    assert report.checks["facts"] is False


def test_unverified_metrics_are_not_rendered(tmp_path):
    slide = CarouselSlide(
        **slide_payload(
            metrics_json=json.dumps(
                [{"name": "views", "value": 9999, "unit": "views", "is_verified": False}]
            )
        )
    )
    result = render(slide, tmp_path)
    assert not [box for box in result.text_boxes if box.kind == "metric"]


def test_source_image_provider_only_uses_a_real_local_file(tmp_path):
    context = qa_context()
    context.images = [ImageAsset(url_or_path="https://example.com/remote.jpg", alt_text="remote")]
    from modules.carousels.render import SourceImageProvider

    provider = SourceImageProvider()
    import asyncio

    result = asyncio.run(
        provider.background(
            slide=CarouselSlide(**slide_payload()),
            profile=profile_for(CarouselVertical.QA),
            width=WIDTH,
            height=HEIGHT,
            context=context,
            destination_dir=tmp_path,
        )
    )
    assert not result.available
    assert "local image file" in result.reason


def test_gemini_provider_reports_a_missing_key_instead_of_faking(tmp_path):
    from modules.carousels.render import GeminiBackgroundProvider

    provider = GeminiBackgroundProvider(api_key="")
    import asyncio

    result = asyncio.run(
        provider.background(
            slide=CarouselSlide(**slide_payload(background_prompt="cinematic dark blue")),
            profile=profile_for(CarouselVertical.QA),
            width=WIDTH,
            height=HEIGHT,
            destination_dir=tmp_path,
        )
    )
    assert not result.available
    assert "GEMINI_API_KEY" in result.reason


def test_provider_for_uses_painted_background_in_dry_run():
    config = Config().carousels
    provider = provider_for(config.gemini, dry_run=True)
    assert provider.name == "solid_gradient"
