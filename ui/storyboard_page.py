"""Storyboard page (P13, ADR-106) — the human surface of the Visual Narrative Studio.

Imported by ``ui/dashboard.py`` as a tab's render function. Same single-connection
rule as the other pages: every DB access opens a fresh ``Database`` inside
``asyncio.run`` and closes it in a ``finally``.

The page is deliberately thin: it collects the human decisions (narrative header,
shot order, captions, alt-texts, hero image) and hands them to
:class:`modules.visual_narrative_studio.VisualNarrativeStudio`, which owns the
validation and the state machine — no business logic is duplicated here.
"""

from __future__ import annotations

import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st  # noqa: E402

from core.config import Config, load_config_file  # noqa: E402
from core.database import Database  # noqa: E402
from core.exceptions import TravelBlogError  # noqa: E402
from modules.visual_narrative_studio import (  # noqa: E402
    ShotPatch,
    StoryboardPatch,
    StoryboardUpdateRequest,
    VisualNarrativeStudio,
)


async def _with_db(action):
    """Open a fresh connection, run ``action(db, cfg)``, always close."""
    db = Database()
    await db.connect()
    try:
        return await action(db, load_config_file())
    finally:
        await db.close()


def _call(action):
    try:
        return asyncio.run(_with_db(action))
    except TravelBlogError as exc:
        st.error(f"{exc.code}: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface anything to the UI
        st.error(f"Action failed: {exc}")
    return None


def _studio(db: Database, cfg: Config) -> VisualNarrativeStudio:
    return VisualNarrativeStudio(db, cfg)


# -- actions ---------------------------------------------------------------


async def _load_bundle(db: Database, cfg: Config, city_id: int):
    try:
        return await _studio(db, cfg).bundle_for_city(city_id)
    except TravelBlogError:
        return None


async def _generate(db: Database, cfg: Config, city_id: int, dry_run: bool):
    return await _studio(db, cfg).generate(city_id, dry_run=dry_run)


async def _apply(db: Database, cfg: Config, city_id: int, request: StoryboardUpdateRequest):
    return await _studio(db, cfg).apply_update(city_id, request)


async def _approve(db: Database, cfg: Config, city_id: int, force: bool):
    studio = _studio(db, cfg)
    storyboard = await studio.latest_storyboard(city_id)
    if storyboard is None:
        raise TravelBlogError(f"City {city_id} has no storyboard yet")
    return await studio.approve(storyboard.id or 0, force=force, actor="streamlit")


async def _move(db: Database, cfg: Config, storyboard_id: int, order: list[int]):
    return await _studio(db, cfg).reorder_shots(storyboard_id, order)


# -- rendering -------------------------------------------------------------


def _render_beats(bundle) -> None:
    st.subheader("Сюжетные биты")
    if not bundle.beats:
        st.info("Битов пока нет — сгенерируйте storyboard.")
        return
    st.table(
        [
            {
                "№": beat.order + 1,
                "Тип": beat.beat_type.value,
                "Заголовок": beat.title,
                "Эмоция": beat.emotional_tone,
                "Визуальная цель": beat.visual_goal,
                "Кадров": len(beat.photo_paths),
            }
            for beat in bundle.beats
        ]
    )


def _render_shots(bundle) -> StoryboardUpdateRequest | None:
    """Draw the shot editor; returns the pending update when "save" is pressed."""
    st.subheader("Порядок кадров и подписи")
    if not bundle.shots:
        st.info("В раскадровке нет кадров.")
        return None

    storyboard_id = bundle.storyboard.id or 0
    shot_ids = [shot.id or 0 for shot in bundle.shots]
    patches: list[ShotPatch] = []

    for position, shot in enumerate(bundle.shots):
        hero_marker = " ⭐" if shot.is_hero_image else ""
        with st.expander(f"{position + 1}. {os.path.basename(shot.photo_path)}{hero_marker}", expanded=False):
            if os.path.exists(shot.photo_path):
                st.image(shot.photo_path, width=320)
            else:
                st.caption(f"файл недоступен: {shot.photo_path}")

            caption = st.text_input("Подпись", value=shot.caption, key=f"cap_{storyboard_id}_{shot.id}")
            alt_text = st.text_input(
                "Alt-текст (обязателен)", value=shot.alt_text, key=f"alt_{storyboard_id}_{shot.id}"
            )
            metaphor = st.text_input(
                "Визуальная метафора", value=shot.visual_metaphor, key=f"met_{storyboard_id}_{shot.id}"
            )
            col_meta_1, col_meta_2 = st.columns(2)
            with col_meta_1:
                st.caption(f"кадрирование: {shot.crop_recommendation or '—'}")
            with col_meta_2:
                st.caption(f"фокус: {shot.focus_point or '—'}")
            st.caption(f"вес ритма: {shot.pacing_weight}")

            is_hero = st.checkbox(
                "Hero image (один на историю)",
                value=shot.is_hero_image,
                key=f"hero_{storyboard_id}_{shot.id}",
            )
            order_now = [sid for sid in shot_ids]
            up, down = st.columns(2)
            with up:
                if st.button("⬆ Выше", key=f"up_{storyboard_id}_{shot.id}", disabled=position == 0):
                    new_order = list(order_now)
                    new_order[position - 1], new_order[position] = new_order[position], new_order[position - 1]
                    _call(lambda db, cfg, o=new_order: _move(db, cfg, storyboard_id, o))
                    st.rerun()
            with down:
                if st.button(
                    "⬇ Ниже",
                    key=f"down_{storyboard_id}_{shot.id}",
                    disabled=position == len(shot_ids) - 1,
                ):
                    new_order = list(order_now)
                    new_order[position + 1], new_order[position] = new_order[position], new_order[position + 1]
                    _call(lambda db, cfg, o=new_order: _move(db, cfg, storyboard_id, o))
                    st.rerun()

            patches.append(
                ShotPatch(
                    shot_id=shot.id or 0,
                    caption=caption,
                    alt_text=alt_text,
                    visual_metaphor=metaphor,
                    is_hero_image=is_hero,
                )
            )

    if st.button("💾 Сохранить изменения", type="primary"):
        return StoryboardUpdateRequest(shots=patches, shot_order=shot_ids)
    return None


def _render_header_form(bundle) -> StoryboardPatch | None:
    storyboard = bundle.storyboard
    st.subheader("Нарратив")
    title = st.text_input("Заголовок", value=storyboard.title, key=f"title_{storyboard.id}")
    logline = st.text_input("Логлайн", value=storyboard.logline, key=f"logline_{storyboard.id}")
    arc = st.text_area("Сюжетная арка", value=storyboard.narrative_arc, key=f"arc_{storyboard.id}")
    journey = st.text_area(
        "Эмоциональная дуга", value=storyboard.emotional_journey, key=f"journey_{storyboard.id}"
    )
    theme = st.text_input("Главная тема", value=storyboard.primary_theme, key=f"theme_{storyboard.id}")
    themes = st.text_input(
        "Второстепенные темы (через запятую)",
        value=", ".join(storyboard.secondary_themes),
        key=f"themes_{storyboard.id}",
    )
    st.caption(
        "Платформы: " + (", ".join(storyboard.target_platforms) or "—")
        + f" · статус: {storyboard.status.value} · версия: {storyboard.version}"
    )
    if st.button("💾 Сохранить нарратив"):
        return StoryboardPatch(
            title=title,
            logline=logline,
            narrative_arc=arc,
            emotional_journey=journey,
            primary_theme=theme,
            secondary_themes=[part.strip() for part in themes.split(",") if part.strip()],
        )
    return None


def render() -> None:
    """Streamlit entry point (called from ``ui/dashboard.py``)."""
    st.header("🎬 Visual Narrative Studio")
    st.caption(
        "Слой визуального нарратива между AI-анализом фотографий и генерацией "
        "платформенного контента: арка, раскадровка, порядок кадров, подписи и alt-тексты."
    )

    cfg = load_config_file()
    settings = cfg.visual_narrative
    if not settings.enabled:
        st.warning("Фича выключена: visual_narrative.enabled = false в config.yaml.")
        return
    st.caption(
        f"provider: {settings.provider or cfg.ai.provider} · max_photos: {settings.max_photos} · "
        f"require_alt_text: {settings.require_alt_text} · enforce_narrative_arc: {settings.enforce_narrative_arc}"
    )
    if cfg.app.dry_run:
        st.info("app.dry_run = true: по умолчанию генерация только предпросматривается.")

    cities = _call(lambda db, cfg_: db.get_all_cities())
    if not cities:
        st.info("В архиве пока нет городов — сначала просканируйте фотографии.")
        return

    labels = {f"{city.name} ({city.year or '—'}) · #{city.id}": city.id for city in cities}
    chosen = st.selectbox("Город", list(labels))
    city_id = labels[chosen]

    col_generate, col_dry, col_archive = st.columns([2, 2, 1])
    with col_dry:
        dry_run = st.checkbox("Предпросмотр (dry-run)", value=cfg.app.dry_run)
    with col_generate:
        if st.button("🎬 Сгенерировать storyboard", type="primary"):
            result = _call(lambda db, cfg_: _generate(db, cfg_, city_id, dry_run))
            if result is not None:
                if result.dry_run:
                    st.warning("Предпросмотр: в базе ничего не сохранено.")
                else:
                    st.success(
                        f"Storyboard v{result.storyboard.version}: "
                        f"{len(result.plan.beats)} битов, {len(result.plan.shots)} кадров "
                        f"(provider: {result.plan.provider or settings.provider})."
                    )
                for warning in result.warnings:
                    st.warning(warning)
                if result.plan.degraded:
                    st.error(f"Деградация: {result.plan.degradation_reason}")

    bundle = _call(lambda db, cfg_: _load_bundle(db, cfg_, city_id))
    if bundle is None:
        st.info("У города ещё нет storyboard — сгенерируйте его кнопкой выше.")
        return

    if bundle.issues:
        st.warning("Проблемы перед одобрением:\n" + "\n".join(f"• {issue}" for issue in bundle.issues))
    else:
        st.success("Валидация пройдена: арка на месте, alt-тексты заполнены.")

    storyboard = bundle.storyboard
    st.metric("Версия", storyboard.version, help="каждая генерация создаёт новую версию")
    st.write(f"**Статус:** `{storyboard.status.value}` · **Hero image:** "
             + (os.path.basename(next((s.photo_path for s in bundle.shots if s.is_hero_image), "—"))))

    header_patch = _render_header_form(bundle)
    if header_patch is not None:
        updated = _call(lambda db, cfg_: _apply(db, cfg_, city_id, StoryboardUpdateRequest(storyboard=header_patch)))
        if updated is not None:
            st.success("Нарратив сохранён.")
            st.rerun()

    _render_beats(bundle)

    shot_update = _render_shots(bundle)
    if shot_update is not None:
        updated = _call(lambda db, cfg_: _apply(db, cfg_, city_id, shot_update))
        if updated is not None:
            st.success("Кадры сохранены.")
            st.rerun()

    st.subheader("Одобрение")
    force = False
    if bundle.issues:
        force = st.checkbox(
            "Одобрить несмотря на проблемы (manual override)",
            value=False,
            disabled=not settings.allow_manual_override,
        )
    col_approve, col_archive = st.columns([2, 1])
    with col_approve:
        if st.button("✅ Одобрить storyboard", disabled=storyboard.status.value == "approved"):
            approved = _call(lambda db, cfg_: _approve(db, cfg_, city_id, force))
            if approved is not None:
                st.success("Storyboard одобрен — платформенный контент можно генерировать.")
                st.rerun()

    with st.expander("Порядок кадров для генерации контента (JSON)"):
        st.code(
            "[\n"
            + ",\n".join(
                f'  {{"order": {shot.order}, "photo": "{shot.photo_path}", '
                f'"caption": "{shot.caption}", "hero": {str(shot.is_hero_image).lower()}}}'
                for shot in bundle.shots
            )
            + "\n]",
            language="json",
        )
    if storyboard.accessibility_notes:
        with st.expander("Заметки о доступности"):
            for note in storyboard.accessibility_notes:
                st.write(f"• {note}")
    if storyboard.cultural_sensitivity_notes:
        with st.expander("Культурная чувствительность"):
            for note in storyboard.cultural_sensitivity_notes:
                st.write(f"• {note}")
