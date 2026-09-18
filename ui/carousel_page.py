"""Carousel Factory page — the ten-stage pipeline with a human in the middle.

Imported by ``ui/dashboard.py`` as a tab's render function. Same rule as the
other pages: aiosqlite is one connection per rerun, so every DB access opens a
fresh ``Database`` inside ``asyncio.run`` and closes it in a ``finally``.

The page never invents numbers: a score/learning that does not exist yet is
shown as "нет данных", not as a zero.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

from core.config import Config, get_secrets
from core.database import Database

#: the order a verified carousel walks through; every entry is a service method
STEPS = (
    ("research", "Прочитать источник (Research)"),
    ("draft_narrative", "Хуки + нарратив (Hook lab)"),
    ("plan_slides", "Спланировать 6 слайдов"),
    ("render_slides", "Отрисовать 768x1376 JPG"),
    ("verify_slides", "Верификация фактов"),
    ("submit_for_approval", "Отправить на согласование"),
)


async def _run(action, *args, **kwargs):
    db = Database()
    await db.connect()
    try:
        return await action(db, Config(), *args, **kwargs)
    finally:
        await db.close()


def _call(action, *args, **kwargs):
    """Run one async action; a failure is shown, never swallowed into a number."""
    try:
        return asyncio.run(_run(action, *args, **kwargs))
    except Exception as exc:  # noqa: BLE001 - the UI must surface the reason
        st.error(f"Не получилось: {exc}")
        return None


def _factory(db, cfg):
    from modules.carousels.service import CarouselFactory

    return CarouselFactory(db, cfg, get_secrets())


# ----------------------------------------------------------------------
# actions (one function per service call, kept thin for testability)
# ----------------------------------------------------------------------


async def _create(db, cfg, source_type, source_ref, vertical, title):
    return await _factory(db, cfg).create_job(
        source_type=source_type,
        source_ref=source_ref,
        vertical=vertical or None,
        title=title,
        created_by="streamlit",
    )


async def _list_jobs(db, cfg, status, limit=50):
    return await _factory(db, cfg).list_jobs(status=status or None, limit=limit)


async def _status(db, cfg, job_id):
    return await _factory(db, cfg).status_report(job_id)


async def _step(db, cfg, job_id, step):
    carousel = _factory(db, cfg)
    return await getattr(carousel, step)(job_id)


async def _queue(db, cfg):
    return await _factory(db, cfg).approval_queue(limit=50)


async def _bundle(db, cfg, job_id):
    return await _factory(db, cfg).get_bundle(job_id)


async def _decide(db, cfg, job_id, approve, who, note):
    carousel = _factory(db, cfg)
    if approve:
        return await carousel.approve(job_id, approved_by=who, note=note)
    return await carousel.reject(job_id, reason=note, rejected_by=who)


async def _publish(db, cfg, job_id):
    return await _factory(db, cfg).publish(job_id)


async def _collect(db, cfg, job_id):
    return await _factory(db, cfg).collect_metrics(job_id)


async def _score(db, cfg, job_id):
    return await _factory(db, cfg).score_job(job_id)


async def _metrics(db, cfg, job_id):
    return await _factory(db, cfg).list_metrics(job_id)


async def _refresh_learnings(db, cfg, vertical):
    return await _factory(db, cfg).refresh_learnings(vertical=vertical or None)


async def _learnings(db, cfg, vertical):
    return await _factory(db, cfg).list_learnings(limit=200)


async def _advice(db, cfg, vertical):
    return await _factory(db, cfg).recommendations(vertical=vertical or None, limit=5)


# ----------------------------------------------------------------------
# views
# ----------------------------------------------------------------------


def _render_hub() -> None:
    st.caption(
        "Источник → 6-слайдовая карусель 768x1376 (JPG, 9:16) → верификация → "
        "человеческое согласование → публикация → метрики → learnings."
    )
    with st.form("carousel_new"):
        cols = st.columns([1, 3])
        source_type = cols[0].selectbox(
            "Тип источника", ["url", "github", "manual", "mock"], key="cf_type"
        )
        source_ref = cols[1].text_input(
            "Ссылка или референс", placeholder="https://example.com/article", key="cf_ref"
        )
        cols2 = st.columns([1, 3])
        vertical = cols2[0].selectbox(
            "Вертикаль", ["auto", "travel", "qa", "vibecoding", "hybrid"], key="cf_vertical"
        )
        title = cols2[1].text_input("Заголовок (необязательно)", key="cf_title")
        submitted = st.form_submit_button("Создать карусель")

    if submitted:
        if not source_ref.strip():
            st.warning("Нужна ссылка на источник — без неё исследовать нечего.")
        else:
            job = _call(_create, source_type, source_ref.strip(), vertical, title)
            if job is not None:
                st.success(f"Задача #{job.id} создана (статус {job.status.value}).")
                st.session_state["cf_job_id"] = int(job.id or 0)


def _render_pipeline() -> None:
    jobs = _call(_list_jobs, "", 50) or []
    if not jobs:
        st.info("Пока нет ни одной карусели — создай её на вкладке Hub.")
        return

    labels = {f"#{job.id} · {job.status.value} · {job.vertical.value} · {job.title or 'без названия'}":
              int(job.id or 0) for job in jobs}
    picked = st.selectbox("Карусель", list(labels.keys()), key="cf_pick")
    job_id = labels[picked]
    st.session_state["cf_job_id"] = job_id

    report = _call(_status, job_id)
    bundle = None
    if report:
        bundle = _call(_bundle, job_id)
        slides = list(getattr(bundle, "slides", []) or [])
        cols = st.columns(4)
        cols[0].metric("Статус", str(report.get("status", "—")))
        cols[1].metric("Вертикаль", str(report.get("vertical", "—")))
        cols[2].metric("Слайдов", str(len(slides)))
        cols[3].metric("Дальше", str(report.get("next") or "—"))
        st.caption(
            f"autonomy={report.get('autonomy_mode')} · "
            f"можно публиковать: {report.get('can_publish')} · "
            f"нужен человек: {report.get('requires_human_action')}"
        )

        for warning in report.get("warnings") or []:
            st.warning(warning)

        if slides:
            columns = st.columns(3)
            for index, slide in enumerate(slides):
                path = Path(str(getattr(slide, "image_path", "") or ""))
                header = f"{index + 1}. {slide.slide_type.value} — {slide.headline}"
                with columns[index % 3]:
                    st.caption(header)
                    if path.is_file():
                        st.image(str(path), use_container_width=True)
                    else:
                        st.info("JPG ещё не отрисован")

    st.divider()
    st.subheader("Шаги пайплайна")
    step_cols = st.columns(3)
    for index, (step, label) in enumerate(STEPS):
        if step_cols[index % 3].button(label, key=f"cf_step_{step}"):
            result = _call(_step, job_id, step)
            if result is not None:
                st.success(f"{label}: готово")
                st.rerun()

    who = st.text_input("Кто согласует", value="dmitry", key="cf_who")
    note = st.text_input("Заметка (или причина отказа)", key="cf_note")
    cols = st.columns(3)
    if cols[0].button("✅ Согласовать", key="cf_approve"):
        if _call(_decide, job_id, True, who, note) is not None:
            st.rerun()
    if cols[1].button("⛔ Отклонить", key="cf_reject"):
        if _call(_decide, job_id, False, who, note) is not None:
            st.rerun()
    if cols[2].button("🚀 Опубликовать", key="cf_publish"):
        result = _call(_publish, job_id)
        if result is not None:
            st.success("Публикация принята (dry_run ничего не грузит наружу).")


def _render_queue() -> None:
    st.caption("Здесь карусели ждут человека. Публикация без согласования запрещена.")
    queue: List[Any] = _call(_queue) or []
    if not queue:
        st.info("Очередь пуста — все карусели уже обработаны.")
        return
    for job in queue:
        cols = st.columns([3, 1, 1])
        cols[0].write(f"#{job.id} · {job.vertical.value} · {job.title or 'без названия'}")
        who = st.text_input("Кто", value="dmitry", key=f"cf_q_who_{job.id}")
        note = st.text_input("Заметка", key=f"cf_q_note_{job.id}")
        if cols[1].button("Согласовать", key=f"cf_q_yes_{job.id}"):
            if _call(_decide, int(job.id or 0), True, who, note) is not None:
                st.rerun()
        if cols[2].button("Отклонить", key=f"cf_q_no_{job.id}"):
            if _call(_decide, int(job.id or 0), False, who, note) is not None:
                st.rerun()


def _render_analytics() -> None:
    job_id = st.session_state.get("cf_job_id")
    if not job_id:
        st.info("Выбери карусель на вкладке Pipeline.")
        return

    st.caption("Метрики только те, что вернула платформа. Пустой ответ так и остаётся пустым.")
    if st.button("Собрать метрики", key="cf_collect"):
        if _call(_collect, job_id) is not None:
            st.rerun()

    rows = _call(_metrics, job_id) or []
    if not rows:
        st.info("Метрик нет: либо пост ещё не опубликован, либо платформа не отдала цифры.")
    else:
        for row in rows:
            st.write(
                f"{row.platform} · {row.metric_name} = {row.metric_value:g} "
                f"(raw {row.raw_value!r}, источник {row.source})"
            )

    score = _call(_score, job_id)
    if score is not None:
        st.metric("carousel_score", f"{score.score:.4f}")
        st.json(score.explain())


def _render_learnings() -> None:
    vertical = st.text_input("Вертикаль (пусто = все)", key="cf_lv")
    if st.button("Пересчитать learnings", key="cf_refresh"):
        written = _call(_refresh_learnings, vertical)
        if written is not None:
            st.success(f"Пересчитано строк: {len(written)}")

    rows = _call(_learnings, vertical) or []
    st.caption(f"Строк learnings: {len(rows)} (минимальная выборка указана в каждой строке)")
    for row in rows[:40]:
        st.write(
            f"{row.scope_type.value}/{row.scope_value} · {row.metric_name} = "
            f"{row.metric_value:.4g} · n={row.sample_size} · confidence {row.confidence:.2f}"
        )

    st.divider()
    picks = _call(_advice, vertical) or []
    if not picks:
        st.info("Рекомендаций нет: выборки ещё малы, и выдумывать их мы не будем.")
        return
    for pick in picks:
        st.write(
            f"• {pick.scope_type}/{pick.scope_value} · {pick.metric_name} = "
            f"{pick.metric_value:.4g} · n={pick.sample_size} · confidence {pick.confidence:.2f}"
        )
        if pick.rationale:
            st.caption(pick.rationale)


def render() -> None:
    st.header("🎠 Carousel Factory")
    tabs = st.tabs(["Hub", "Pipeline", "Approval Queue", "Analytics", "Learnings"])
    with tabs[0]:
        _render_hub()
    with tabs[1]:
        _render_pipeline()
    with tabs[2]:
        _render_queue()
    with tabs[3]:
        _render_analytics()
    with tabs[4]:
        _render_learnings()


if __name__ == "__main__":  # pragma: no cover - manual run
    render()
