"""Behavioural tests for the Visual Narrative Studio (ADR-106).

Covers the whole feature surface: deterministic mock generation, persistence in
SQLite, dry-run, photo budget, shot reordering, alt-text validation, the
storyboard state machine, and backward compatibility of the city pipeline.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from core import models as m
from core.config import Config
from core.database import Database
from core.exceptions import StateTransitionError, StoryboardValidationError
from modules import narrative_heuristics as heuristics
from modules.ai.base import ImageAnalysis
from modules.ai.mock import MockProvider
from modules.content.engine import ContentEngine
from modules.scanner import Scanner
from modules.visual_narrative_studio import (
    ShotPatch,
    StoryboardPatch,
    StoryboardUpdateRequest,
    VisualNarrativeStudio,
    storyboard_issues,
)
from tests.test_scanner import build_archive


def _config(**overrides: object) -> Config:
    """Config with the studio enabled, no dry-run and the mock provider."""
    cfg = Config()
    cfg.app.dry_run = False
    cfg.visual_narrative.provider = "mock"
    for key, value in overrides.items():
        setattr(cfg.visual_narrative, key, value)
    return cfg


async def _city_with_photos(db: Database, count: int = 5, city_name: str = "Moscow") -> m.City:
    city = await db.add_city(m.City(name=city_name, country="Russia", year=2019))
    for index in range(1, count + 1):
        await db.add_photo(
            m.Photo(
                city_id=city.id,
                path=f"/archive/{city_name}_2019/photo_{index}.jpg",
                filename=f"photo_{index}.jpg",
                sha256=f"sha{index}",
                scan_status=m.ScanStatus.SCANNED,
            )
        )
    return city


async def _studio(db: Database, config: Optional[Config] = None) -> VisualNarrativeStudio:
    return VisualNarrativeStudio(db, config or _config())


# -- generation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_plan_is_deterministic_and_covers_every_photo() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 5)
        studio = await _studio(db)

        context = await studio.load_context(city.id)
        first = await studio.provider.generate_visual_narrative_plan(context)
        second = await studio.provider.generate_visual_narrative_plan(
            await studio.load_context(city.id)
        )

        assert first.model_dump() == second.model_dump()  # same input ⇒ same plan
        assert first.provider == "mock"
        assert {shot.photo_path for shot in first.shots} == {photo.path for photo in context.photos}
        beat_types = [beat.beat_type.value for beat in first.beats]
        assert beat_types[0] == "setup"
        assert "climax" in beat_types
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_generate_persists_storyboard_beats_and_shots() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 6)
        result = await (await _studio(db)).generate(city.id)

        assert result.dry_run is False
        storyboard = result.storyboard
        assert storyboard.id is not None
        assert storyboard.version == 1
        assert storyboard.status is m.StoryboardStatus.DRAFT
        assert storyboard.title and storyboard.logline

        beats = await db.get_beats(storyboard.id or 0)
        shots = await db.get_shots(storyboard.id or 0)
        assert [beat.order for beat in beats] == list(range(len(beats)))
        assert [shot.order for shot in shots] == list(range(len(shots)))
        assert len(shots) == 6
        assert all(shot.alt_text for shot in shots)
        assert sum(1 for shot in shots if shot.is_hero_image) == 1
        assert storyboard.accessibility_notes
        assert storyboard.cultural_sensitivity_notes
        assert storyboard_issues(storyboard, beats, shots) == []

        # reading it back through the API-facing read model
        bundle = await (await _studio(db, _config())).bundle_for_id(storyboard.id or 0)
        assert len(bundle.shots) == 6 and bundle.issues == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dry_run_writes_nothing() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 4)
        result = await (await _studio(db)).generate(city.id, dry_run=True)

        assert result.dry_run is True
        assert result.storyboard.id is None
        assert result.storyboard.version == 0
        assert await db.get_city_storyboard(city.id) is None
        assert len(result.plan.shots) == 4  # the preview is still complete
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_max_photos_budget_is_respected() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 8)
        studio = await _studio(db, _config(max_photos=3))

        result = await studio.generate(city.id, dry_run=True)

        assert len(result.plan.shots) == 3
        assert len(result.plan.selected_photos) == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_generate_creates_a_new_version_and_keeps_history() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 3)
        studio = await _studio(db)

        first = await studio.generate(city.id)
        second = await studio.generate(city.id)

        assert first.storyboard.version == 1
        assert second.storyboard.version == 2
        versions = [storyboard.version for storyboard in await db.list_storyboards(city_id=city.id)]
        assert versions == [2, 1]  # newest first, old versions stay as history
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_degraded_plan_when_provider_fails() -> None:
    class _BrokenProvider(MockProvider):
        async def generate_visual_narrative_plan(self, context, *, max_photos: int = 12):
            raise RuntimeError("provider exploded")

    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 3)
        config = _config()
        studio = VisualNarrativeStudio(db, config, provider=_BrokenProvider(db, config))

        result = await studio.generate(city.id)

        assert result.plan.degraded is True
        assert result.plan.provider == "local_fallback"
        assert "provider exploded" in result.plan.degradation_reason
        assert result.warnings and result.storyboard.id is not None  # still a usable storyboard
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_city_without_photos_degrades_without_persisting() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await db.add_city(m.City(name="Empty", country="Nowhere"))
        result = await (await _studio(db)).generate(city.id)

        assert result.plan.degraded is True
        assert result.storyboard.id is None
        assert await db.get_city_storyboard(city.id) is None
    finally:
        await db.close()


# -- editing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_reorder_shots_normalizes_order_and_rejects_wrong_sets() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 4)
        studio = await _studio(db)
        storyboard = (await studio.generate(city.id)).storyboard
        shot_ids = [shot.id or 0 for shot in await db.get_shots(storyboard.id or 0)]

        reordered = await studio.reorder_shots(storyboard.id or 0, list(reversed(shot_ids)))

        assert [shot.order for shot in reordered] == [0, 1, 2, 3]
        assert [shot.id for shot in reordered] == list(reversed(shot_ids))

        with pytest.raises(StoryboardValidationError):
            await studio.reorder_shots(storyboard.id or 0, shot_ids[:-1])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_hero_image_stays_unique() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 4)
        studio = await _studio(db)
        storyboard = (await studio.generate(city.id)).storyboard
        shots = await db.get_shots(storyboard.id or 0)

        await studio.set_hero_image(shots[0].id or 0)

        after = await db.get_shots(storyboard.id or 0)
        heroes = [shot for shot in after if shot.is_hero_image]
        assert len(heroes) == 1
        assert heroes[0].id == shots[0].id
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_manual_edit_demotes_an_approved_storyboard() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 3)
        studio = await _studio(db)
        storyboard = (await studio.generate(city.id)).storyboard
        approved = await studio.approve(storyboard.id or 0)
        assert approved.storyboard.status is m.StoryboardStatus.APPROVED

        bundle = await studio.update_storyboard(
            storyboard.id or 0, StoryboardPatch(title="Новая Москва", logline="Другая история")
        )

        assert bundle.storyboard.title == "Новая Москва"
        assert bundle.storyboard.status is m.StoryboardStatus.DRAFT
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_apply_update_handles_header_shots_and_order_together() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 3)
        studio = await _studio(db)
        storyboard = (await studio.generate(city.id)).storyboard
        shots = await db.get_shots(storyboard.id or 0)

        request = StoryboardUpdateRequest(
            storyboard=StoryboardPatch(primary_theme="городской ритм"),
            shots=[
                ShotPatch(shot_id=shots[0].id or 0, caption="Рассвет", alt_text="Красная площадь на рассвете"),
            ],
            shot_order=[shot.id or 0 for shot in reversed(shots)],
        )
        bundle = await studio.apply_update(city.id, request)

        assert bundle.storyboard.primary_theme == "городской ритм"
        assert bundle.shots[0].id == shots[-1].id
        assert bundle.shots[0].caption == "Рассвет" or bundle.shots[-1].caption == "Рассвет"
        assert bundle.issues == []
    finally:
        await db.close()


# -- validation and approval ----------------------------------------------


@pytest.mark.asyncio
async def test_alt_text_is_required_before_approval() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 3)
        studio = await _studio(db)
        storyboard = (await studio.generate(city.id)).storyboard
        shots = await db.get_shots(storyboard.id or 0)

        await studio.update_shot(shots[0].id or 0, ShotPatch(shot_id=shots[0].id or 0, alt_text="   "))

        with pytest.raises(StoryboardValidationError) as excinfo:
            await studio.approve(storyboard.id or 0)
        assert any(issue.startswith("alt_text_missing") for issue in excinfo.value.issues)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_force_approval_bypasses_issues_only_when_allowed() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 3)
        studio = await _studio(db, _config(allow_manual_override=False))
        storyboard = (await studio.generate(city.id)).storyboard
        shots = await db.get_shots(storyboard.id or 0)
        await studio.update_shot(shots[0].id or 0, ShotPatch(shot_id=shots[0].id or 0, alt_text=""))

        with pytest.raises(StoryboardValidationError):  # overrides not allowed
            await studio.approve(storyboard.id or 0, force=True)
    finally:
        await db.close()

    db2 = Database(str(Path(tempfile.mkdtemp(prefix="tba_vns_")) / "t2.db"))
    await db2.connect()
    try:
        city = await _city_with_photos(db2, 3, city_name="Kazan")
        studio = await _studio(db2, _config(allow_manual_override=True))
        storyboard = (await studio.generate(city.id)).storyboard
        shots = await db2.get_shots(storyboard.id or 0)
        await studio.update_shot(shots[0].id or 0, ShotPatch(shot_id=shots[0].id or 0, alt_text=""))

        bundle = await studio.approve(storyboard.id or 0, force=True, actor="tester")

        assert bundle.storyboard.status is m.StoryboardStatus.APPROVED
        assert bundle.issues  # the issues are reported, not hidden
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_narrative_arc_is_enforced() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 4)
        studio = await _studio(db)
        storyboard = (await studio.generate(city.id)).storyboard
        beats = await db.get_beats(storyboard.id or 0)
        shots = await db.get_shots(storyboard.id or 0)
        climax = next(beat for beat in beats if beat.beat_type is m.NarrativeBeatType.CLIMAX)

        # simulate a human deleting the climax beat straight in the repository
        async with db.transaction() as conn:
            await conn.execute("DELETE FROM narrative_beats WHERE id = ?", (climax.id,))
        remaining = await db.get_beats(storyboard.id or 0)

        issues = storyboard_issues(storyboard, remaining, shots)
        assert "missing_beat:climax" in issues
        with pytest.raises(StoryboardValidationError):
            await studio.approve(storyboard.id or 0)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_illegal_transitions_are_refused() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 3)
        studio = await _studio(db)
        storyboard = (await studio.generate(city.id)).storyboard

        await studio.approve(storyboard.id or 0)
        with pytest.raises(StateTransitionError):  # approved -> approved
            await studio.approve(storyboard.id or 0)

        await studio.archive(storyboard.id or 0)
        with pytest.raises(StateTransitionError):  # archived is terminal
            await studio.archive(storyboard.id or 0)
    finally:
        await db.close()


def test_storyboard_issues_reports_every_structural_problem() -> None:
    storyboard = m.Storyboard(city_id=1, title="", logline="")
    shots = [
        m.StoryboardShot(photo_path="/a.jpg", order=0, alt_text="", is_hero_image=True),
        m.StoryboardShot(photo_path="/a.jpg", order=0, alt_text="ok", is_hero_image=True),
    ]

    issues = storyboard_issues(storyboard, [], shots)

    assert "missing_title: у визуальной истории нет заголовка" in issues
    assert "duplicate_photos: одна фотография попала в раскадровку дважды" in issues
    assert "shot_order_duplicated: у кадров совпадают номера порядка" in issues
    assert "multiple_hero_images: hero image должен быть только один" in issues
    assert any(issue.startswith("alt_text_missing") for issue in issues)
    assert "missing_beat:climax" in issues
    assert storyboard_issues(storyboard, [], [], require_alt_text=False, enforce_narrative_arc=False) == [
        "no_shots: в раскадровке нет ни одного кадра",
        "missing_title: у визуальной истории нет заголовка",
        "missing_logline: логлайн не заполнен",
    ]


# -- heuristics ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "alt_text_missing"),
        ("ok", "alt_text_too_short"),
        ("photo_1.jpg", "alt_text_is_filename"),
        ("здесь фото", "alt_text_placeholder"),
        ("ж" * 300, "alt_text_too_long"),
    ],
)
def test_alt_text_rules(text: str, expected: str) -> None:
    assert expected in heuristics.alt_text_issues(text, "/x/photo_1.jpg")


def test_valid_alt_text_has_no_issues() -> None:
    assert heuristics.alt_text_issues("Красная площадь на рассвете, пустая", "/x/a.jpg") == []


def test_arc_helpers_are_deterministic() -> None:
    photos = [
        m.NarrativePhoto(path=f"/p{i}.jpg", filename=f"p{i}.jpg", scene=f"scene {i}", mood="спокойствие")
        for i in range(1, 7)
    ]

    assert heuristics.canonical_arc(0) == []
    assert heuristics.canonical_arc(1) == ["setup"]
    assert heuristics.canonical_arc(2) == ["setup", "climax"]
    assert set(heuristics.REQUIRED_BEATS) <= set(heuristics.canonical_arc(6))

    beats = heuristics.build_beats(photos)
    shots = heuristics.build_shots(photos, beats)
    assert len(shots) == 6
    assert [shot.order for shot in shots] == list(range(6))
    assert sum(1 for shot in shots if shot.is_hero_image) == 1
    assert all(not heuristics.alt_text_issues(shot.alt_text, shot.photo_path) for shot in shots)
    # the same photos always produce the same shot list
    assert [shot.photo_path for shot in heuristics.build_shots(photos, heuristics.build_beats(photos))] == [
        shot.photo_path for shot in shots
    ]


def test_ensure_arc_normalizes_a_thin_plan() -> None:
    photos = [m.NarrativePhoto(path=f"/p{i}.jpg", filename=f"p{i}.jpg", scene="вид") for i in range(1, 4)]
    plan = m.VisualNarrativePlan(
        city_id=1,
        beats=[m.NarrativeBeat(beat_type=m.NarrativeBeatType.CLIMAX, order=5)],
        shots=[
            m.StoryboardShot(photo_path="/p1.jpg", order=9, alt_text=""),
            m.StoryboardShot(photo_path="/p1.jpg", order=3, alt_text="вид города"),
            m.StoryboardShot(photo_path="/p9.jpg", order=0, alt_text="чужой кадр"),
        ],
    )

    fixed = heuristics.ensure_arc(plan, photos)

    assert [shot.order for shot in fixed.shots] == list(range(len(fixed.shots)))
    assert {shot.photo_path for shot in fixed.shots} == {"/p1.jpg"}  # unknown photos dropped
    assert all(shot.alt_text for shot in fixed.shots)
    assert sum(1 for shot in fixed.shots if shot.is_hero_image) == 1
    present = {beat.beat_type.value for beat in fixed.beats}
    assert set(heuristics.REQUIRED_BEATS) <= present
    assert plan.shots[0].order == 9  # the input plan is not mutated


# -- pipeline integration --------------------------------------------------


class _RecordingMock(MockProvider):
    """MockProvider that remembers the prompts, so the integration can be asserted."""

    def __init__(self, db: Database, config: Config) -> None:
        super().__init__(db, config)
        self.user_prompts: List[str] = []

    async def generate_text(self, system: str, user: str, *, max_tokens: Optional[int] = None) -> str:
        self.user_prompts.append(user)
        return await super().generate_text(system, user, max_tokens=max_tokens)


@pytest.mark.asyncio
async def test_pipeline_creates_a_storyboard_and_feeds_the_base_story() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    archive = build_archive(tmp)
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        await Scanner(db, str(archive)).scan()
        city = next(c for c in await db.get_all_cities() if c.name == "Moscow")
        config = _config()
        provider = _RecordingMock(db, config)
        engine = ContentEngine(db, config, provider=provider)

        processed = await engine.process_city(city.id or 0)

        assert processed.status is m.CityStatus.DRAFTED  # the city machine is untouched
        drafts = await db.get_drafts(city_id=city.id)
        assert drafts, "platform drafts must still be generated"
        storyboard = await db.get_city_storyboard(city.id or 0)
        assert storyboard is not None
        assert storyboard.status is m.StoryboardStatus.DRAFT
        assert any("ВИЗУАЛЬНЫЙ НАРРАТИВ" in prompt for prompt in provider.user_prompts)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pipeline_without_the_feature_stays_unchanged() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    archive = build_archive(tmp)
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        await Scanner(db, str(archive)).scan()
        city = next(c for c in await db.get_all_cities() if c.name == "Moscow")
        config = _config(enabled=False)
        provider = _RecordingMock(db, config)
        engine = ContentEngine(db, config, provider=provider)

        processed = await engine.process_city(city.id or 0)

        assert processed.status is m.CityStatus.DRAFTED
        assert await db.get_city_storyboard(city.id or 0) is None
        assert not any("ВИЗУАЛЬНЫЙ НАРРАТИВ" in prompt for prompt in provider.user_prompts)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_require_approval_gate_blocks_platform_content() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    archive = build_archive(tmp)
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        await Scanner(db, str(archive)).scan()
        city = next(c for c in await db.get_all_cities() if c.name == "Moscow")
        config = _config(require_approval=True)
        engine = ContentEngine(db, config, provider=_RecordingMock(db, config))

        with pytest.raises(StoryboardValidationError):
            await engine.process_city(city.id or 0)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_narrative_block_renders_the_storyboard_for_the_prompt() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 4)
        studio = await _studio(db)
        assert await studio.narrative_block(city.id) == ""  # nothing generated yet

        await studio.generate(city.id)

        block = await studio.narrative_block(city.id)
        assert "ВИЗУАЛЬНЫЙ НАРРАТИВ" in block
        assert "setup" in block and "climax" in block
        assert "photo_1.jpg" in block
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_load_context_reuses_provided_analyses() -> None:
    """The pipeline already analysed the photos; the studio must not re-ask."""

    class _CountingMock(MockProvider):
        calls = 0

        async def analyze_image(self, image_path: str, prompt: str, image_hash: Optional[str] = None):
            _CountingMock.calls += 1
            return await super().analyze_image(image_path, prompt, image_hash)

    tmp = Path(tempfile.mkdtemp(prefix="tba_vns_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        city = await _city_with_photos(db, 3)
        config = _config()
        provider = _CountingMock(db, config)
        studio = VisualNarrativeStudio(db, config, provider=provider)
        photos = await studio.load_photos(city.id)
        analyses: Dict[str, ImageAnalysis] = {
            photo.path: ImageAnalysis(scene=f"готовый кадр {index}") for index, photo in enumerate(photos)
        }

        context = await studio.load_context(city.id, analyses=analyses)

        assert _CountingMock.calls == 0
        assert [photo.scene for photo in context.photos] == [
            f"готовый кадр {index}" for index in range(len(photos))
        ]
    finally:
        await db.close()
