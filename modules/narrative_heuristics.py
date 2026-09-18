"""Deterministic visual-narrative helpers for the Visual Narrative Studio (ADR-106).

Two callers must never diverge, so the *same* code drives both:

* :class:`modules.ai.mock.MockProvider` — deterministic plans for tests/dry-runs;
* :class:`modules.visual_narrative_studio.VisualNarrativeStudio` — the offline
  fallback used when a real provider fails, so a broken AI call degrades the
  result (and reports it as degraded) instead of blocking the city.

Nothing in here touches the network, the database or the clock: the same input
always produces the same plan. Text is produced in Russian, matching the rest of
the pipeline.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from core import models as m
from modules.narrative_prompts import BEAT_GUIDE

#: Minimal arc every storyboard must satisfy when ``enforce_narrative_arc`` is on.
REQUIRED_BEATS: Tuple[str, ...] = (
    m.NarrativeBeatType.SETUP.value,
    m.NarrativeBeatType.CLIMAX.value,
    m.NarrativeBeatType.RESOLUTION.value,
)

#: Canonical arc order — beats are sorted by this rank, then by their own order.
BEAT_ORDER: Tuple[str, ...] = tuple(bt.value for bt in m.NarrativeBeatType)

#: Visual rhythm: the edit breathes before the climax and settles after it.
PACING_BY_BEAT: Dict[str, float] = {
    m.NarrativeBeatType.SETUP.value: 1.0,
    m.NarrativeBeatType.CONFLICT.value: 1.2,
    m.NarrativeBeatType.DEVELOPMENT.value: 1.0,
    m.NarrativeBeatType.CLIMAX.value: 1.5,
    m.NarrativeBeatType.RESOLUTION.value: 0.9,
    m.NarrativeBeatType.REFLECTION.value: 0.8,
}

#: Accessibility constraints for alt-text (mirrors ``visual_narrative.require_alt_text``).
MIN_ALT_TEXT_LENGTH = 3
MAX_ALT_TEXT_LENGTH = 250
_FILENAME_SUFFIXES = (".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff", ".arw", ".cr2")
_FORBIDDEN_ALT_TOKENS = ("здесь фото", "на фото", "фото", "картинка", "image", "photo", "alt", "img", "фотография")

#: Crop heuristics used until a VLM returns real framing data.
_CROP_WITH_TEXT = "center_crop_16_9"
_CROP_WITH_OBJECTS = "rule_of_thirds_4_5"
_CROP_DEFAULT = "full_frame_3_2"


def beat_rank(beat_type: str) -> int:
    """Position of a beat type in the canonical arc (unknown types go last)."""
    try:
        return BEAT_ORDER.index(beat_type)
    except ValueError:
        return len(BEAT_ORDER)


def canonical_arc(photo_count: int) -> List[str]:
    """Beat types for ``photo_count`` photos — short trips get a short arc."""
    if photo_count <= 0:
        return []
    if photo_count == 1:
        return [m.NarrativeBeatType.SETUP.value]
    if photo_count == 2:
        return [m.NarrativeBeatType.SETUP.value, m.NarrativeBeatType.CLIMAX.value]
    if photo_count == 3:
        return list(REQUIRED_BEATS)
    if photo_count == 4:
        return [
            m.NarrativeBeatType.SETUP.value,
            m.NarrativeBeatType.DEVELOPMENT.value,
            m.NarrativeBeatType.CLIMAX.value,
            m.NarrativeBeatType.RESOLUTION.value,
        ]
    if photo_count == 5:
        return [
            m.NarrativeBeatType.SETUP.value,
            m.NarrativeBeatType.CONFLICT.value,
            m.NarrativeBeatType.DEVELOPMENT.value,
            m.NarrativeBeatType.CLIMAX.value,
            m.NarrativeBeatType.RESOLUTION.value,
        ]
    return [bt for bt in BEAT_ORDER]


def energy(photo: m.NarrativePhoto) -> float:
    """Cheap, explainable "how much is happening in this frame" score."""
    return (
        float(len(photo.objects))
        + (2.0 if photo.text else 0.0)
        + (1.0 if photo.scene else 0.0)
        - (2.0 if not photo.quality_ok else 0.0)
    )


def pick_climax_index(photos: Sequence[m.NarrativePhoto]) -> int:
    """Pick the emotional peak: strongest frame in the dramatic 30-90% window."""
    if not photos:
        return -1
    total = len(photos)
    low = int(total * 0.3)
    high = max(int(total * 0.9), low + 1)
    candidates = [i for i in range(low, high) if 0 <= i < total] or list(range(total))
    return max(candidates, key=lambda i: (energy(photos[i]), -abs(i - (total - 1) * 0.7), -i))


def _split_indices(total: int, buckets: int) -> List[List[int]]:
    """Order-preserving contiguous split of ``total`` indices into ``buckets``."""
    if buckets <= 0:
        return []
    base, extra = divmod(total, buckets)
    out: List[List[int]] = []
    cursor = 0
    for bucket in range(buckets):
        size = base + (1 if bucket < extra else 0)
        out.append(list(range(cursor, cursor + size)))
        cursor += size
    return out


def _clip(text: str, limit: int = MAX_ALT_TEXT_LENGTH) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip(" ,.;:") + "…"


def alt_text_issues(text: str, photo_path: str = "") -> List[str]:
    """Deterministic alt-text validation (used by the studio *and* the UI)."""
    issues: List[str] = []
    clean = (text or "").strip()
    if not clean:
        return ["alt_text_missing"]
    if len(clean) < MIN_ALT_TEXT_LENGTH:
        issues.append("alt_text_too_short")
    if len(clean) > MAX_ALT_TEXT_LENGTH:
        issues.append("alt_text_too_long")
    lowered = clean.lower()
    if lowered.endswith(_FILENAME_SUFFIXES) or (photo_path and clean == photo_path.rsplit("/", 1)[-1]):
        issues.append("alt_text_is_filename")
    if lowered in _FORBIDDEN_ALT_TOKENS:
        issues.append("alt_text_placeholder")
    return issues


def describe_photo(photo: m.NarrativePhoto) -> str:
    """One-sentence description derived only from stored facts."""
    parts: List[str] = []
    if photo.scene:
        parts.append(photo.scene.strip())
    if photo.objects:
        parts.append("объекты: " + ", ".join(photo.objects[:4]))
    if photo.text:
        parts.append(f"видимый текст: {photo.text.strip()}")
    if not parts:
        parts.append(f"кадр из {photo.filename or 'архива'}")
    return _clip(". ".join(p[0].upper() + p[1:] if p else p for p in parts) + ".")


def build_alt_text(photo: m.NarrativePhoto) -> str:
    """Alt-text always derived from facts, never from the filename."""
    text = describe_photo(photo)
    if not alt_text_issues(text, photo.path):
        return text
    fallback = f"Городской кадр: {photo.scene or photo.mood or 'вид места'}."
    return _clip(fallback)


def crop_recommendation(photo: m.NarrativePhoto) -> str:
    if photo.text:
        return _CROP_WITH_TEXT
    if photo.objects:
        return _CROP_WITH_OBJECTS
    return _CROP_DEFAULT


def focus_point(photo: m.NarrativePhoto) -> str:
    """Normalized "x,y" focus hint for the crop tool (deterministic)."""
    return "0.50,0.45" if photo.text else "0.50,0.50"


def visual_metaphor(photo: m.NarrativePhoto, beat_type: str) -> str:
    guide = BEAT_GUIDE.get(beat_type, {})
    base = guide.get("metaphor", "кадр-связка")
    anchor = (photo.objects[0] if photo.objects else photo.scene) or photo.mood
    return f"{base}: {anchor}" if anchor else base


def build_beats(photos: Sequence[m.NarrativePhoto]) -> List[m.NarrativeBeat]:
    """Assign photos to a canonical arc — deterministic given the same photos."""
    types = canonical_arc(len(photos))
    if not types:
        return []
    groups = _split_indices(len(photos), len(types))
    beats: List[m.NarrativeBeat] = []
    for order, (beat_type, indices) in enumerate(zip(types, groups)):
        guide = BEAT_GUIDE.get(beat_type, {})
        paths = [photos[i].path for i in indices]
        captions = [describe_photo(photos[i]) for i in indices]
        beats.append(
            m.NarrativeBeat(
                beat_type=beat_type,
                order=order,
                title=_beat_title(beat_type, photos, indices),
                description=" ".join(captions) or guide.get("goal", ""),
                emotional_tone=guide.get("tone", ""),
                visual_goal=guide.get("goal", ""),
                photo_paths_json=m.dump_json_list(paths),
            )
        )
    return beats


def _beat_title(
    beat_type: str, photos: Sequence[m.NarrativePhoto], indices: Sequence[int]
) -> str:
    labels = {
        m.NarrativeBeatType.SETUP.value: "Начало",
        m.NarrativeBeatType.CONFLICT.value: "Препятствие",
        m.NarrativeBeatType.DEVELOPMENT.value: "Погружение",
        m.NarrativeBeatType.CLIMAX.value: "Кульминация",
        m.NarrativeBeatType.RESOLUTION.value: "Развязка",
        m.NarrativeBeatType.REFLECTION.value: "Послевкусие",
    }
    title = labels.get(beat_type, beat_type)
    if indices:
        anchor = photos[indices[0]]
        place = anchor.scene or anchor.mood
        if place:
            title = f"{title}: {_clip(place, 60)}"
    return title


def build_shots(
    photos: Sequence[m.NarrativePhoto], beats: Sequence[m.NarrativeBeat]
) -> List[m.StoryboardShot]:
    """One shot per photo, ordered by the beats they belong to."""
    by_path = {photo.path: photo for photo in photos}
    beat_of: Dict[str, str] = {}
    for beat in beats:
        for path in beat.photo_paths:
            beat_of.setdefault(path, beat.beat_type.value)
    ordered = [path for beat in beats for path in beat.photo_paths]
    ordered += [photo.path for photo in photos if photo.path not in ordered]
    climax_index = pick_climax_index(photos)
    hero_path = photos[climax_index].path if climax_index >= 0 else ""

    shots: List[m.StoryboardShot] = []
    for order, path in enumerate(ordered):
        photo = by_path.get(path)
        if photo is None:
            continue
        beat_type = beat_of.get(path, m.NarrativeBeatType.DEVELOPMENT.value)
        shots.append(
            m.StoryboardShot(
                photo_path=path,
                order=order,
                caption=_clip(photo.scene or photo.mood or photo.filename, 120),
                alt_text=build_alt_text(photo),
                crop_recommendation=crop_recommendation(photo),
                focus_point=focus_point(photo),
                visual_metaphor=visual_metaphor(photo, beat_type),
                pacing_weight=PACING_BY_BEAT.get(beat_type, 1.0),
                is_hero_image=path == hero_path,
            )
        )
    return shots


def _placeholder_beat(beat_type: str, photos: Sequence[m.NarrativePhoto]) -> m.NarrativeBeat:
    """Synthesize a beat the source material cannot provide (arc invariant)."""
    guide = BEAT_GUIDE.get(beat_type, {})
    beat = m.NarrativeBeat(
        beat_type=m.NarrativeBeatType(beat_type),
        title=_arc_label(beat_type),
        description=guide.get("goal", ""),
        emotional_tone=guide.get("tone", ""),
        visual_goal=guide.get("goal", ""),
    )
    if photos:
        beat.set_photo_paths([photos[-1].path])
    return beat


def ensure_arc(
    plan: m.VisualNarrativePlan, photos: Sequence[m.NarrativePhoto]
) -> m.VisualNarrativePlan:
    """Normalize any plan (AI or local) into a publishable shape.

    Guarantees, in order: beats sorted by the canonical arc, the required beats
    present, shots restricted to the known photos, ordered, unique, with valid
    alt-text, and exactly one hero image.
    """
    out = plan.model_copy(deep=True)
    photos_by_path = {photo.path: photo for photo in photos}
    ordered_photos = [photos_by_path.get(shot.photo_path) for shot in out.shots]
    ordered_photos = [p for p in ordered_photos if p is not None]
    if not ordered_photos:
        ordered_photos = list(photos)

    beats = sorted(out.beats, key=lambda b: (beat_rank(b.beat_type.value), b.order))
    for index, beat in enumerate(beats):
        beat.order = index
        if not beat.visual_goal:
            beat.visual_goal = BEAT_GUIDE.get(beat.beat_type.value, {}).get("goal", "")
        if not beat.emotional_tone:
            beat.emotional_tone = BEAT_GUIDE.get(beat.beat_type.value, {}).get("tone", "")

    present = {beat.beat_type.value for beat in beats}
    missing = [bt for bt in REQUIRED_BEATS if bt not in present]
    if missing:
        source = ordered_photos or list(photos)
        if source:
            beats.extend(beat for beat in build_beats(source) if beat.beat_type.value in missing)
        # Source material may simply not afford a beat (a one-photo plan cannot
        # produce a resolution): the arc invariant still has to hold, so the
        # missing required beats are synthesized explicitly.
        still_missing = [
            beat_type
            for beat_type in REQUIRED_BEATS
            if beat_type not in {beat.beat_type.value for beat in beats}
        ]
        beats.extend(_placeholder_beat(beat_type, source) for beat_type in still_missing)
        beats.sort(key=lambda b: (beat_rank(b.beat_type.value), b.order))
        for index, beat in enumerate(beats):
            beat.order = index
    out.beats = beats

    shots = list(out.shots)
    seen: set[str] = set()
    unique: List[m.StoryboardShot] = []
    for shot in shots:
        if shot.photo_path in seen:
            continue
        if photos_by_path and shot.photo_path not in photos_by_path:
            continue  # a storyboard may only use photographs we actually analysed
        seen.add(shot.photo_path)
        unique.append(shot)
    for photo in ordered_photos:
        if photo.path in seen:
            continue
        seen.add(photo.path)
        unique.append(
            m.StoryboardShot(
                photo_path=photo.path,
                caption=_clip(photo.scene or photo.mood or photo.filename, 120),
                alt_text=build_alt_text(photo),
                crop_recommendation=crop_recommendation(photo),
                focus_point=focus_point(photo),
                visual_metaphor=visual_metaphor(photo, m.NarrativeBeatType.DEVELOPMENT.value),
                pacing_weight=1.0,
            )
        )
    for order, shot in enumerate(unique):
        shot.order = order
        if not shot.alt_text.strip() and (photo := photos_by_path.get(shot.photo_path)):
            shot.alt_text = build_alt_text(photo)
        else:
            shot.alt_text = _clip(shot.alt_text)
        if not shot.crop_recommendation:
            direct = photos_by_path.get(shot.photo_path)
            shot.crop_recommendation = crop_recommendation(direct) if direct else _CROP_DEFAULT
        if not shot.focus_point:
            shot.focus_point = "0.50,0.50"
    climax_path = _climax_path(out.beats)
    hero = _hero_shot(unique, climax_path)
    for shot in unique:
        shot.is_hero_image = shot is hero
    out.shots = unique
    out.selected_photos = [shot.photo_path for shot in unique]
    if not out.accessibility_notes:
        out.accessibility_notes = default_accessibility_notes(len(unique))
    if not out.cultural_sensitivity_notes:
        out.cultural_sensitivity_notes = default_cultural_notes()
    return out


def _climax_path(beats: Sequence[m.NarrativeBeat]) -> str:
    for beat in beats:
        if beat.beat_type.value == m.NarrativeBeatType.CLIMAX.value and beat.photo_paths:
            return beat.photo_paths[0]
    return ""


def _hero_shot(
    shots: Sequence[m.StoryboardShot], climax_path: str
) -> Optional[m.StoryboardShot]:
    """Exactly one hero image: the climax frame, else the heaviest pacing weight."""
    if not shots:
        return None
    for shot in shots:
        if climax_path and shot.photo_path == climax_path:
            return shot
    return max(shots, key=lambda s: (s.pacing_weight, -s.order))


def default_accessibility_notes(photo_count: int) -> List[str]:
    return [
        f"alt-текст обязателен для всех {photo_count} кадров (без имён файлов и «на фото»)",
        "подписи должны читаться вне контекста и не дублировать alt-текст",
        "проверить контраст подписей на светлых и пересвеченных кадрах",
    ]


def default_cultural_notes() -> List[str]:
    return [
        "проверить топонимы, имена людей и названия заведений перед публикацией",
        "убедиться, что люди на кадрах согласны на публикацию",
    ]


def local_plan(
    context: m.NarrativeContext,
    *,
    provider: str = "local_fallback",
    degraded: bool = True,
    degradation_reason: str = "",
) -> m.VisualNarrativePlan:
    """Build a complete plan offline — used by MockProvider and as the fallback."""
    photos = list(context.photos)
    beats = build_beats(photos)
    shots = build_shots(photos, beats)
    arc = " → ".join(_arc_label(b.beat_type.value) for b in beats) or "(нет кадров)"
    journey = " → ".join(b.emotional_tone for b in beats if b.emotional_tone)
    themes = _themes(photos, context)
    plan = m.VisualNarrativePlan(
        city_id=context.city_id,
        beats=beats,
        shots=shots,
        title=_title(context),
        logline=_logline(context, shots),
        narrative_arc=arc,
        emotional_journey=journey,
        primary_theme=themes[0] if themes else "",
        secondary_themes=themes[1:3],
        hook=beats[0].description if beats else "",
        climax=next(
            (b.description for b in beats if b.beat_type.value == m.NarrativeBeatType.CLIMAX.value),
            "",
        ),
        ending=beats[-1].description if beats else "",
        provider=provider,
        model="",
        degraded=degraded,
        degradation_reason=degradation_reason,
    )
    return ensure_arc(plan, photos)


def _arc_label(beat_type: str) -> str:
    labels = {
        m.NarrativeBeatType.SETUP.value: "завязка",
        m.NarrativeBeatType.CONFLICT.value: "конфликт",
        m.NarrativeBeatType.DEVELOPMENT.value: "развитие",
        m.NarrativeBeatType.CLIMAX.value: "кульминация",
        m.NarrativeBeatType.RESOLUTION.value: "развязка",
        m.NarrativeBeatType.REFLECTION.value: "рефлексия",
    }
    return labels.get(beat_type, beat_type)


def _title(context: m.NarrativeContext) -> str:
    year = f" {context.year}" if context.year else ""
    return f"{context.city}{year}: визуальная история"


def _logline(context: m.NarrativeContext, shots: Sequence[m.StoryboardShot]) -> str:
    if not shots:
        return f"Визуальная история {context.city} без кадров — нужен минимум один кадр."
    start = shots[0].caption or "первый кадр"
    end = shots[-1].caption or "последний кадр"
    return _clip(f"{len(shots)} кадров, которые ведут от «{start}» к «{end}».", 300)


def _themes(photos: Sequence[m.NarrativePhoto], context: m.NarrativeContext) -> List[str]:
    """Most frequent concrete objects become the themes (deterministic, ties by name)."""
    counts: Dict[str, int] = {}
    for photo in photos:
        for obj in photo.objects:
            token = " ".join(obj.split()).strip().lower()
            if token:
                counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    themes = [name for name, _ in ranked[:3]]
    if not themes and context.city.strip():
        themes = [context.city.strip().lower()]
    return themes
