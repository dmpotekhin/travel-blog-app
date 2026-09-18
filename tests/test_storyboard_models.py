"""Model-level tests for the Visual Narrative Studio (ADR-106).

Pure domain behaviour: value objects, JSON helpers, the storyboard state machine
and row→model coercion (SQLite stores booleans as 0/1).
"""

from __future__ import annotations

import pytest

from core import models as m
from core.exceptions import StateTransitionError


def test_storyboard_defaults() -> None:
    storyboard = m.Storyboard(city_id=1)

    assert storyboard.id is None
    assert storyboard.status is m.StoryboardStatus.DRAFT
    assert storyboard.version == 1
    assert storyboard.secondary_themes == []
    assert storyboard.target_platforms == []
    assert storyboard.accessibility_notes == []
    assert storyboard.cultural_sensitivity_notes == []


def test_json_list_helpers_roundtrip_and_tolerate_garbage() -> None:
    assert m.dump_json_list(["еда", "люди"]) == '["еда", "люди"]'
    assert m.load_json_list('["a", "b"]') == ["a", "b"]
    assert m.load_json_list(None) == []
    assert m.load_json_list("{not json") == []
    assert m.load_json_list('{"a": 1}') == []

    storyboard = m.Storyboard(
        city_id=1,
        secondary_themes_json='["архитектура", "еда"]',
        accessibility_notes_json='["alt-тексты обязательны"]',
    )
    assert storyboard.secondary_themes == ["архитектура", "еда"]
    assert storyboard.accessibility_notes == ["alt-тексты обязательны"]


def test_beat_photo_paths_roundtrip() -> None:
    beat = m.NarrativeBeat(storyboard_id=1, beat_type=m.NarrativeBeatType.CLIMAX, order=2)

    beat.set_photo_paths(["/a.jpg", "/b.jpg"])

    assert beat.photo_paths == ["/a.jpg", "/b.jpg"]
    assert beat.beat_type is m.NarrativeBeatType.CLIMAX
    assert m.NarrativeBeat(**beat.model_dump()).photo_paths == ["/a.jpg", "/b.jpg"]


def test_beat_type_accepts_the_wire_value() -> None:
    beat = m.NarrativeBeat(beat_type="reflection")  # type: ignore[arg-type]

    assert beat.beat_type is m.NarrativeBeatType.REFLECTION


def test_shot_row_values_are_coerced() -> None:
    # SQLite has no boolean type: the hero flag comes back as 0/1.
    shot = m.StoryboardShot(
        **{
            "id": 3,
            "storyboard_id": 1,
            "photo_path": "/x.jpg",
            "order": 0,
            "is_hero_image": 1,
            "pacing_weight": 1.5,
        }
    )

    assert shot.is_hero_image is True
    assert shot.pacing_weight == 1.5
    assert shot.alt_text == ""  # defaults keep old rows readable


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("draft", "approved"),
        ("draft", "archived"),
        ("approved", "draft"),
        ("approved", "archived"),
    ],
)
def test_storyboard_transitions_allowed(current: str, target: str) -> None:
    m.storyboard_transition(current, target)  # must not raise


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("archived", "draft"),
        ("archived", "approved"),
        ("draft", "draft"),
        ("approved", "approved"),
    ],
)
def test_storyboard_transitions_rejected(current: str, target: str) -> None:
    with pytest.raises(StateTransitionError):
        m.storyboard_transition(current, target)


def test_narrative_photo_and_context() -> None:
    photo = m.NarrativePhoto(path="/archive/Moscow_2019/IMG_1.jpg", filename="IMG_1.jpg")

    assert photo.stem == "IMG_1"
    assert photo.quality_ok is True
    assert m.NarrativePhoto(path="/a/IMG_2.JPG").stem == "IMG_2"

    context = m.NarrativeContext(city_id=7, city="Moscow", year=2019)
    assert context.photos == []
    assert context.language == "ru"


def test_plan_bundle_and_result_shapes() -> None:
    plan = m.VisualNarrativePlan(city_id=1, title="Moscow")
    storyboard = m.Storyboard(city_id=1)

    result = m.VisualNarrativeResult(city_id=1, storyboard=storyboard, plan=plan)
    bundle = m.StoryboardBundle(storyboard=storyboard)

    assert plan.dry_run is False
    assert plan.degraded is False
    assert result.created is True
    assert bundle.beats == [] and bundle.shots == [] and bundle.issues == []
