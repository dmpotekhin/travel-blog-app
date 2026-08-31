"""P10 Dashboard — Streamlit view of pipeline KPIs + browser photo upload.

Run: ``streamlit run ui/dashboard.py`` (or via ``./run.sh``).
Opens a fresh SQLite connection on each rerun (single event loop) so aiosqlite
never leaks a dead loop; the connection is closed in a ``finally``.

Tabs:
    Upload  — in-browser photo ingestion into the pipeline queue.
    Posts   — review the generated post content, copy it, and complete a
              MANUAL publication (human posts it to zen/instagram/youtube/trip_com).
    Stats   — read-only KPIs (metrics, by-status charts, platform matrix, recent).
"""
from __future__ import annotations

import os
import sys

# Streamlit runs this script with cwd=ui/, so the project root (where `core`
# and `modules` live) is NOT on sys.path. Bootstrap it before any project import.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import asyncio
from typing import Dict

import pandas as pd
import streamlit as st

from core.config import Config
from core.database import Database
from modules.ingest import ingest_photos
from modules.stats import StatsService
from modules.trip_validator import TripValidator
from ui.vibecoding_page import render as render_vibecoding
from ui.settings_page import render as render_settings
from ui.validation_view import render_validation

_STATUS_COLS = ["published", "scheduled", "pending", "processing", "failed", "manual", "disabled"]
# Platforms that are never auto-published: the pipeline only prepares the content
# and a human must copy the post into their account. See modules/publishers/registry.py.
_MANUAL_PLATFORMS = {"zen", "instagram", "youtube", "trip_com"}


async def _collect():
    db = Database()
    await db.connect()
    try:
        svc = StatsService(db)
        summary = await svc.summary()
        by_status = await svc.by_status()
        by_platform = await svc.by_platform()
        recent = await svc.recent(limit=10)
        return summary, by_status, by_platform, recent
    finally:
        await db.close()


async def _collect_vibe_stats():
    db = Database()
    await db.connect()
    try:
        posts = await db.get_vibecoding_posts(limit=1000)
    finally:
        await db.close()
    total = len(posts)
    published = sum(1 for p in posts if p.status == "published")
    drafts = sum(1 for p in posts if p.status == "draft")
    pending = sum(1 for p in posts if p.status == "pending")
    errors = sum(1 for p in posts if p.status == "error")
    return total, published, drafts, pending, errors


async def _ingest(city_name: str, year: int, files) -> dict:
    db = Database()
    await db.connect()
    try:
        return await ingest_photos(db, Config(), city_name, year, files)
    finally:
        await db.close()


async def _run(action, *args, **kwargs):
    """Open a fresh DB + Config, run ``action(db, config, *args)``, close it."""
    db = Database()
    await db.connect()
    try:
        return await action(db, Config(), *args, **kwargs)
    finally:
        await db.close()


def _call(action, *args, **kwargs):
    """Sync wrapper for st.button callbacks (Streamlit runs synchronously)."""
    try:
        return asyncio.run(_run(action, *args, **kwargs))
    except Exception as exc:  # noqa: BLE001 - surface to the UI
        st.error(f"Action failed: {exc}")
        return None


async def _collect_posts():
    """Join drafts with their publication rows (for the Posts review tab)."""

    async def _inner(db, cfg):
        drafts = await db.get_drafts()
        pubs = await db.get_all_publications()
        # get_all_publications returns newest-first; keep the newest per (city, platform)
        pub_map: Dict = {}
        for p in pubs:
            key = (p.city_id, p.platform)
            if key not in pub_map:
                pub_map[key] = p
        rows = []
        for d in drafts:
            pub = pub_map.get((d.city_id, d.platform))
            rows.append(
                {
                    "id": d.id,
                    "city_id": d.city_id,
                    "platform": d.platform,
                    "status": d.status.value if hasattr(d.status, "value") else str(d.status),
                    "title": d.title,
                    "content": d.content,
                    "pub_id": pub.id if pub else None,
                    "pub_status": pub.status.value if pub else "",
                    "is_manual": d.platform in _MANUAL_PLATFORMS,
                }
            )
        return rows

    return await _run(_inner)


async def _mark_manual(db, cfg, draft_id):
    from modules.publishers.service import PublishService

    pub = await PublishService(db, cfg).mark_manual_published(draft_id)
    return {
        "publication_id": pub.id,
        "status": pub.status.value,
        "published_at": pub.published_at.isoformat() if pub.published_at else None,
    }


async def _validate_trip(db, cfg, draft_id):
    """Run the Trip.com guidelines validator for a travel draft."""
    drafts = await db.get_drafts(draft_id=draft_id)
    if not drafts:
        return None
    draft = drafts[0]
    city = await db.get_city(draft.city_id)
    return TripValidator(cfg).validate(draft, city).to_dict()


def _platform_frame(by_platform: Dict[str, Dict[str, int]]) -> pd.DataFrame:
    rows = []
    for platform, block in sorted(by_platform.items()):
        row = {"platform": platform}
        for col in _STATUS_COLS:
            row[col] = block.get(col, 0)
        rows.append(row)
    return pd.DataFrame(rows)


def _recent_frame(recent) -> pd.DataFrame:
    return pd.DataFrame(recent)


def _render_upload_tab() -> None:
    """📤 Upload photos via the browser into the pipeline queue."""
    with st.expander("Upload photos", expanded=True):
        form = st.form("upload", clear_on_submit=True)
        with form:
            c1, c2 = st.columns(2)
            up_city = c1.text_input("City")
            up_year = c2.number_input("Year", min_value=1900, max_value=2100, value=2020, step=1)
            up_files = st.file_uploader(
                "Photo files",
                type=["jpg", "jpeg", "png", "webp", "heic", "heif", "bmp", "tif", "tiff"],
                accept_multiple_files=True,
            )
            submitted = st.form_submit_button("Upload → queue")

        if submitted:
            if not up_city.strip() or not up_files:
                st.warning("Enter a city name and pick at least one photo.")
            else:
                payload = [(f.name, f.getvalue()) for f in up_files]
                try:
                    res = asyncio.run(_ingest(up_city.strip(), int(up_year), payload))
                except Exception as exc:  # noqa: BLE001 - surface to the user
                    st.error(f"Ingest failed: {exc}")
                else:
                    if "error" in res:
                        st.error(res["error"])
                    else:
                        st.success(
                            f"Added {res['added']} photo(s) to {res['city']} ({res['year']}) — "
                            f"duplicates: {res['duplicates']}, rejected: {res['rejected']}."
                        )
                        st.caption(f"Stored under: {res['folder']}")
    st.caption("Photos are validated and queued (city QUEUED, photos SCANNED) for content generation.")


def _render_posts_tab() -> None:
    """📝 Review generated posts, copy them, and complete MANUAL publications."""
    st.subheader("Posts — review, copy, publish manually")
    st.caption("The block has a copy button (top-right). Manual platforms stay 'manual' until you post them yourself.")

    rows = asyncio.run(_collect_posts())
    if not rows:
        st.info("No posts yet. Generate content first (content stage for a city).")
        return

    manual = [r for r in rows if r["is_manual"]]
    if manual:
        st.markdown("#### ⏳ Manual platforms — awaiting you")
        st.caption("Скопируй текст, размести пост в аккаунте и отметь готовым.")
        for r in manual:
            with st.expander(f"⏳ {r['platform']} · city {r['city_id']} · {r['pub_status']}"):
                if r["title"]:
                    st.markdown(f"**{r['title']}**")
                st.code(r["content"], language="markdown")
                if r["platform"] == "trip_com":
                    if st.button("🔍 Проверить гайдлайны Trip", key=f"vt_{r['id']}"):
                        res = _call(_validate_trip, r["id"])
                        st.session_state[f"vt_res_{r['id']}"] = res
                    res = st.session_state.get(f"vt_res_{r['id']}")
                    if res:
                        render_validation(res)
                if st.button("Mark as manually published", key=f"mp_{r['id']}"):
                    st.write(_call(_mark_manual, r["id"]))
                    st.rerun()

    rest = [r for r in rows if not r["is_manual"]]
    if rest:
        st.markdown("#### 📋 All generated posts")
        for r in rest:
            with st.expander(f"{r['platform']} · city {r['city_id']} · {r['status']}"):
                if r["title"]:
                    st.markdown(f"**{r['title']}**")
                st.code(r["content"], language="markdown")


def _render_stats_tab() -> None:
    """📊 Read-only pipeline KPIs."""
    summary, by_status, by_platform, recent = asyncio.run(_collect())

    st.subheader("Key metrics")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Cities", summary["cities_total"])
    c2.metric("Photos", summary["photos_total"])
    c3.metric("Drafts", summary["drafts_total"])
    c4.metric("Scheduled", summary["scheduled"])
    c5.metric("Published", summary["published"])
    c6.metric("Errors", summary["errors"])

    total_vibe, pub_vibe, draft_vibe, pend_vibe, err_vibe = asyncio.run(_collect_vibe_stats())
    st.subheader("✨ VibeCoding")
    v1, v2, v3, v4, v5 = st.columns(5)
    v1.metric("Всего", total_vibe)
    v2.metric("Опубликовано", pub_vibe)
    v3.metric("Черновики", draft_vibe)
    v4.metric("В очереди", pend_vibe)
    v5.metric("Ошибки", err_vibe)

    st.subheader("Pipeline by status")
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.markdown("**Cities**")
        st.bar_chart(by_status.get("cities", {}))
    with col_b:
        st.markdown("**Drafts**")
        st.bar_chart(by_status.get("drafts", {}))
    with col_c:
        st.markdown("**Publications**")
        st.bar_chart(by_status.get("publications", {}))

    st.subheader("Publications by platform")
    frame = _platform_frame(by_platform)
    if frame.empty:
        st.info("No publications yet.")
    else:
        st.dataframe(frame)

    st.subheader("Recent publications")
    recent_frame = _recent_frame(recent)
    if recent_frame.empty:
        st.info("No recent publications.")
    else:
        st.dataframe(recent_frame)

    st.caption("Read-only. Source: travel_blog.db")


def main() -> None:
    st.set_page_config(page_title="Travel Blog Automation", layout="wide")
    st.title("🌍 Travel Blog Automation — Dashboard")

    tab_upload, tab_posts, tab_stats, tab_vibe, tab_settings = st.tabs(
        ["📤 Upload", "📝 Posts", "📊 Stats", "✨ VibeCoding", "⚙️ Settings"]
    )

    with tab_upload:
        _render_upload_tab()
    with tab_posts:
        _render_posts_tab()
    with tab_stats:
        _render_stats_tab()
    with tab_vibe:
        render_vibecoding()
    with tab_settings:
        render_settings()


if __name__ == "__main__":
    main()
