import shutil
import tempfile
from pathlib import Path

import pytest

from core.config import Config
from core.database import Database
from core.exceptions import StateTransitionError
from core.models import DraftStatus, Platform
from modules.content.engine import ContentEngine
from modules.drafts import DraftManager
from modules.scanner import Scanner
from tests.test_scanner import build_archive


@pytest.mark.asyncio
async def test_approve_reject_reset_workflow(tmp_path):
    archive = build_archive(tmp_path)
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    try:
        sc = Scanner(db, str(archive))
        await sc.scan()
        msk = next(c for c in await db.get_all_cities() if c.name == "Moscow")
        engine = ContentEngine(db, Config())
        await engine.process_city(msk.id)

        dm = DraftManager(db)
        pending = await dm.list_pending()
        assert len(pending) == len(Platform)

        d0 = pending[0]
        assert d0.status == DraftStatus.PENDING

        approved = await dm.approve(d0.id)
        assert approved.status == DraftStatus.APPROVED

        d1 = pending[1]
        rejected = await dm.reject(d1.id)
        assert rejected.status == DraftStatus.REJECTED

        reopened = await dm.reset(d1.id)
        assert reopened.status == DraftStatus.PENDING

        with pytest.raises(StateTransitionError):
            await dm.approve(d0.id)  # already APPROVED -> illegal
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_approve_only_trusted_platforms(tmp_path):
    archive = build_archive(tmp_path)
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    try:
        sc = Scanner(db, str(archive))
        await sc.scan()
        msk = next(c for c in await db.get_all_cities() if c.name == "Moscow")
        engine = ContentEngine(db, Config())
        await engine.process_city(msk.id)

        dm = DraftManager(db)
        count = await dm.auto_approve()  # factory default: telegram, vk
        assert count == 2, count

        still_pending = await dm.list_pending()
        assert not any(d.platform in ("telegram", "vk") for d in still_pending)
        assert any(d.platform == "instagram" for d in still_pending)
    finally:
        await db.close()
