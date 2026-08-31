import datetime as dt
import shutil
import tempfile
from pathlib import Path

import pytest

from core.config import Config
from core.database import Database
from core.models import DraftStatus, Publication, PublicationStatus
from modules.content.engine import ContentEngine
from modules.drafts import DraftManager
from modules.scheduler import Scheduler
from modules.scanner import Scanner
from tests.test_scanner import build_archive

UTC = dt.timezone.utc


@pytest.fixture
async def prepared(tmp_path):
    archive = build_archive(tmp_path)
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    sc = Scanner(db, str(archive))
    await sc.scan()
    city = next((c for c in await db.get_all_cities() if c.name == "Moscow"), None)
    if city is None:
        city = (await db.get_all_cities())[0]
    config = Config()
    await ContentEngine(db, config).process_city(city.id)
    mgr = DraftManager(db)
    await mgr.auto_approve()
    return db, config


async def test_plan_schedules_auto_and_stays_idempotent(prepared):
    db, config = prepared
    scheduler = Scheduler(db, config)
    planned = await scheduler.plan()
    auto = [p for p in planned if p.status == PublicationStatus.SCHEDULED]
    assert len(auto) >= 2, "telegram+vk should be scheduled"
    assert all(p.scheduled_at is not None for p in auto)
    times = sorted(p.scheduled_at for p in auto)
    assert all(t > dt.datetime.now(UTC) for t in times)
    # second plan creates nothing new
    assert await scheduler.plan() == []


async def test_plan_marks_manual_platforms_manual(prepared):
    db, config = prepared
    mgr = DraftManager(db)
    zen = (await db.get_drafts(platform="zen"))[0]
    await mgr.approve(zen.id)
    scheduler = Scheduler(db, config)
    planned = await scheduler.plan()
    manual = [p for p in planned if p.status == PublicationStatus.MANUAL]
    assert any(p.platform == "zen" for p in manual)
    for p in manual:
        assert p.scheduled_at is None


async def test_plan_respects_posts_per_day(prepared):
    db, config = prepared
    config.schedule.posts_per_day = 2
    scheduler = Scheduler(db, config)
    planned = await scheduler.plan()
    auto = [p for p in planned if p.status == PublicationStatus.SCHEDULED]
    assert len(auto) <= 2
    times = sorted(p.scheduled_at for p in auto)
    # consecutive slots are @interval apart
    if len(times) >= 2:
        interval = dt.timedelta(hours=24 / 2)
        assert abs((times[1] - times[0]) - interval) < dt.timedelta(seconds=1)


async def test_run_due_publishes_scheduled(prepared):
    db, config = prepared
    draft = (await db.get_drafts(platform="vk"))[0]
    city_id = draft.city_id
    # force a due scheduled publication for vk
    await db.save_published(
        Publication(
            city_id=city_id,
            platform="vk",
            status=PublicationStatus.SCHEDULED,
            scheduled_at=dt.datetime.now(UTC) - dt.timedelta(hours=1),
        )
    )
    scheduler = Scheduler(db, config)
    await scheduler.run_due()
    updated = await db.get_publication_by_platform(city_id, "vk")
    assert updated is not None and updated.status == PublicationStatus.PUBLISHED
    draft_after = (await db.get_drafts(draft_id=draft.id))[0]
    assert draft_after.status == DraftStatus.PUBLISHED


async def test_retry_failed_requeues_with_backoff(prepared):
    db, config = prepared
    draft = (await db.get_drafts(platform="vk"))[0]
    city_id = draft.city_id
    await db.save_published(
        Publication(
            city_id=city_id,
            platform="facebook",
            status=PublicationStatus.FAILED,
            error_message="boom",
            retry_count=0,
        )
    )
    scheduler = Scheduler(db, config)
    now = dt.datetime.now(UTC)
    requeued = await scheduler.retry_failed()
    assert len(requeued) == 1
    updated = await db.get_publication_by_platform(city_id, "facebook")
    assert updated.status == PublicationStatus.PENDING
    assert updated.retry_count == 1
    assert updated.scheduled_at > now
