import shutil
import tempfile
from pathlib import Path

import pytest

from core import models as m
from core.config import Config
from core.database import Database
from core.models import DraftStatus, PublicationStatus
from modules.content.engine import ContentEngine
from modules.drafts import DraftManager
from modules.publishers.service import PublishService
from modules.scanner import Scanner
from tests.test_scanner import build_archive


@pytest.fixture
async def manual_prepared(tmp_path):
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
    await mgr.auto_approve()  # approves telegram + vk, leaves zen/instagram/etc pending
    return db, config


def test_manual_to_published_is_legal():
    allowed = m.can_transition(m.PublicationStatus.MANUAL, m._PUBLICATION_TRANSITIONS)
    assert m.PublicationStatus.PUBLISHED in allowed
    assert m.PublicationStatus.DISABLED in allowed
    # a manual post cannot silently become "scheduled" (no fake scheduling)
    assert m.PublicationStatus.SCHEDULED not in allowed


async def test_mark_manual_published_completes_publication(manual_prepared):
    db, config = manual_prepared
    svc = PublishService(db, config)

    draft = (await db.get_drafts(platform="zen"))[0]
    assert draft.status == DraftStatus.PENDING
    await DraftManager(db).approve(draft.id)

    # publish_draft on a manual platform leaves a MANUAL publication (content prepared)
    result = await svc.publish_draft(draft.id)
    assert result["manual"] is True and result["status"] == "manual"
    pub = await db.get_publication_by_platform(draft.city_id, "zen")
    assert pub is not None and pub.status == PublicationStatus.MANUAL

    # human completes it -> PUBLISHED + published_at, draft becomes PUBLISHED
    updated = await svc.mark_manual_published(draft.id)
    assert updated.status == PublicationStatus.PUBLISHED
    assert updated.published_at is not None
    refreshed = (await db.get_drafts(draft_id=draft.id))[0]
    assert refreshed.status == DraftStatus.PUBLISHED


async def test_mark_manual_published_creates_row_when_missing(manual_prepared):
    """If plan() never ran, no MANUAL row exists. Completing should still create one."""
    db, config = manual_prepared
    svc = PublishService(db, config)

    draft = (await db.get_drafts(platform="instagram"))[0]
    await DraftManager(db).approve(draft.id)
    draft = (await db.get_drafts(draft_id=draft.id))[0]
    assert await db.get_publication_by_platform(draft.city_id, "instagram") is None
    assert draft.status == DraftStatus.APPROVED

    pub = await svc.mark_manual_published(draft.id)
    assert pub.status == PublicationStatus.PUBLISHED
    assert pub.published_at is not None
    # a DRAFT row now has a corresponding PUBLISHED publication, and it is not dropped
    assert await db.get_publication_by_platform(draft.city_id, "instagram") is not None
    refreshed = (await db.get_drafts(draft_id=draft.id))[0]
    assert refreshed.status == DraftStatus.PUBLISHED


async def test_mark_manual_published_from_pending(manual_prepared):
    """A PENDING manual draft goes straight to PUBLISHED (PENDING -> PUBLISHED legal).

    The human reviews the post in the dashboard, posts it manually in their account,
    then clicks 'Mark as manually published' — even *before* the auto-approval stage.
    """
    db, config = manual_prepared
    svc = PublishService(db, config)

    draft = (await db.get_drafts(platform="youtube"))[0]
    assert draft.status == DraftStatus.PENDING

    pub = await svc.mark_manual_published(draft.id)
    assert pub.status == PublicationStatus.PUBLISHED
    assert pub.published_at is not None
    refreshed = (await db.get_drafts(draft_id=draft.id))[0]
    assert refreshed.status == DraftStatus.PUBLISHED


async def test_mark_manual_published_is_idempotent(manual_prepared):
    db, config = manual_prepared
    svc = PublishService(db, config)

    draft = (await db.get_drafts(platform="zen"))[0]
    await DraftManager(db).approve(draft.id)
    await svc.publish_draft(draft.id)

    first = await svc.mark_manual_published(draft.id)
    assert first.status == PublicationStatus.PUBLISHED
    # second call is a no-op guard, not a transition error
    again = await svc.mark_manual_published(draft.id)
    assert again.status == PublicationStatus.PUBLISHED
