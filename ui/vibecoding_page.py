"""VibeCoding page — generate, review, publish and schedule VibeCoding posts.

Imported by ``ui/dashboard.py`` as a tab's render function. The project uses a
single-connection-per-rerun rule for aiosqlite, so every DB access here opens a
fresh ``Database`` inside ``asyncio.run`` and closes it in a ``finally``.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import asyncio

import streamlit as st

from core.config import Config
from core.database import Database
from modules.publishers.vibecoding import VibeCodingPublisherService
from modules.vibecoding_generator import VibeCodingGenerator
from modules.vibecoding_validator import VibeCodingValidator
from ui.validation_view import render_validation


async def _run(action, *args, **kwargs):
    db = Database()
    await db.connect()
    try:
        return await action(db, Config(), *args, **kwargs)
    finally:
        await db.close()


def _call(action, *args, **kwargs):
    try:
        return asyncio.run(_run(action, *args, **kwargs))
    except Exception as exc:  # noqa: BLE001 - surface to the UI
        st.error(f"Action failed: {exc}")
        return None


async def _generate(db, cfg, topic, cp, cip):
    return await VibeCodingGenerator(db, cfg).generate_and_save(topic, cp, cip)


async def _publish(db, cfg, post_id):
    return await VibeCodingPublisherService(db, cfg).publish_vibecoding_post(post_id)


async def _validate_vibe(db, cfg, post_id):
    post = await db.get_vibecoding_post(post_id)
    if not post:
        return None
    return VibeCodingValidator(cfg).validate(post).to_dict()


async def _schedule(db, cfg, post_id):
    await db.update_vibecoding_status(post_id, "pending")
    post = await db.get_vibecoding_post(post_id)
    return post.status.value


async def _list_posts(db, cfg, status):
    return await db.get_vibecoding_posts(status=status, limit=100)


async def _delete(db, cfg, post_id):
    return await db.delete_vibecoding_post(post_id)


def _render_post_meta(post) -> None:
    if post.image_url and os.path.exists(post.image_url):
        st.image(post.image_url, width=320)
    st.caption(f"#id {post.id} · статус: {post.status.value} · создан: {post.created_at}")
    if post.title:
        st.markdown(f"**{post.title}**")
    st.code(post.generated_text, language="markdown")


def render() -> None:
    st.subheader("✨ VibeCoding — генерация и публикация постов")
    st.caption("Тема → текст (DeepSeek) + уникальное изображение → публикация по платформам.")

    # -- creation form ----------------------------------------------------
    with st.form("vc_gen", clear_on_submit=False):
        topic = st.text_input("Тема поста", placeholder="Например: AI-агенты в разработке")
        cp = st.text_area("Промпт для текста (опционально)", height=80)
        cip = st.text_area("Промпт для изображения (опционально)", height=80)
        submitted = st.form_submit_button("🚀 Сгенерировать")

    if submitted and topic.strip():
        with st.spinner("Генерация текста и изображения..."):
            pid = _call(_generate, topic.strip(), cp or None, cip or None)
        if pid is not None:
            st.session_state["vc_last_post_id"] = pid
            st.success(f"Черновик сохранён (#{pid}).")
            st.rerun()

    last_id = st.session_state.get("vc_last_post_id")
    # -- actions on a selected draft -------------------------------------
    if last_id:
        st.markdown("#### Последний черновик")
        selected = _call(_get_post, last_id)
        if selected is not None:
            _render_post_meta(selected)
            c1, c2, c3, c4 = st.columns(4)
            if c1.button("📤 Опубликовать сейчас", key=f"vc_pub_{last_id}"):
                res = _call(_publish, last_id)
                if res:
                    st.success(f"Опубликовано: {res['status']}")
                st.rerun()
            if c2.button("⏰ Запланировать", key=f"vc_sched_{last_id}"):
                status = _call(_schedule, last_id)
                if status:
                    st.info(f"Пост #{last_id} в очереди планировщика (статус: {status}).")
                st.rerun()
            if c3.button("🔍 Проверить", key=f"vc_val_{last_id}"):
                st.session_state[f"vc_val_res_{last_id}"] = _call(_validate_vibe, last_id)
                st.rerun()
            if c4.button("🗑 Удалить", key=f"vc_del_{last_id}"):
                _call(_delete, last_id)
                st.session_state.pop("vc_last_post_id", None)
                st.rerun()
            render_validation(st.session_state.get(f"vc_val_res_{last_id}"))

    # -- list of posts ----------------------------------------------------
    st.markdown("#### Все посты")
    status = st.selectbox("Фильтр по статусу", ["all", "draft", "pending", "published", "error"],
                          index=0)
    posts = _call(_list_posts, None if status == "all" else status) or []
    if not posts:
        st.info("Пока нет постов. Сгенерируй первый выше.")
        return

    for post in posts:
        with st.expander(f"#{post.id} · {post.title or post.topic} · {post.status.value}"):
            _render_post_meta(post)
            b1, b2, b3, b4 = st.columns(4)
            if b1.button("📤 Опубликовать", key=f"pl_{post.id}"):
                res = _call(_publish, post.id)
                if res:
                    st.success(f"Опубликовано: {res['status']}")
                st.rerun()
            if b2.button("⏰ Запланировать", key=f"pls_{post.id}"):
                _call(_schedule, post.id)
                st.rerun()
            if b3.button("🔄 Перегенерировать", key=f"plg_{post.id}"):
                pid = _call(_generate, post.topic, post.prompt_text or None, post.image_prompt or None)
                if pid is not None:
                    st.session_state["vc_last_post_id"] = pid
                st.rerun()
            if b4.button("🗑 Удалить", key=f"pld_{post.id}"):
                _call(_delete, post.id)
                st.rerun()
            if st.button("🔍 Проверить гайдлайны", key=f"pval_{post.id}"):
                st.session_state[f"pval_res_{post.id}"] = _call(_validate_vibe, post.id)
            render_validation(st.session_state.get(f"pval_res_{post.id}"))


async def _get_post(db, _cfg, post_id):
    return await db.get_vibecoding_post(post_id)
