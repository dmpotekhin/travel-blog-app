import json
import shutil
import tempfile
from pathlib import Path

import pytest

from core.config import Config
from core.database import Database
from core.models import DraftStatus, Platform
from modules.content.engine import ContentEngine
from modules.drafts import DraftManager
from modules.publishers.registry import build_publisher
from modules.publishers.service import PublishService
from modules.publishers.manual import ManualPublisher
from modules.scanner import Scanner
from tests.test_scanner import build_archive


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


async def test_auto_platform_publishes_and_marks_draft(
    prepared,
):
    db, config = prepared
    svc = PublishService(db, config)
    draft = (await db.get_drafts(platform="telegram"))[0]
    result = await svc.publish_draft(draft.id)
    assert result["status"] == "published"
    assert not result["manual"]
    updated = (await db.get_drafts(draft_id=draft.id))[0]
    assert updated.status == DraftStatus.PUBLISHED
    pub = await db.get_publication_by_platform(draft.city_id, "telegram")
    assert pub is not None and pub.status == "published"


async def test_manual_platform_stays_approved(prepared):
    db, config = prepared
    mgr = DraftManager(db)
    svc = PublishService(db, config)
    draft = (await db.get_drafts(platform="zen"))[0]
    await mgr.approve(draft.id)
    result = await svc.publish_draft(draft.id)
    assert result["manual"] is True
    updated = (await db.get_drafts(draft_id=draft.id))[0]
    assert updated.status == DraftStatus.APPROVED


async def test_cannot_publish_unapproved(prepared):
    db, config = prepared
    svc = PublishService(db, config)
    drafts = await db.get_drafts(platform="vk")
    if not drafts:
        pytest.skip("no vk draft")
    draft = drafts[0]
    await db.update_draft_status(draft.id, DraftStatus.PENDING)
    with pytest.raises(ValueError):
        await svc.publish_draft(draft.id)


def test_registry_modes():
    from core.config import Config
    class _Cfg:
        dry_run = False
    assert build_publisher(None, _Cfg(), "zen") is not None
