import datetime as dt
import shutil
import tempfile
from pathlib import Path

import pytest

from core.config import Config
from core.database import Database
from core.models import CityStatus, DraftStatus, Platform, PublicationStatus
from modules.content.engine import ContentEngine
from modules.drafts import DraftManager
from modules.scheduler import Scheduler
from modules.scanner import Scanner
from tests.test_scanner import build_archive


@pytest.mark.asyncio
async def test_full_pipeline_scan_to_publish():
    tmp = Path(tempfile.mkdtemp(prefix="tba_e2e_"))
    try:
        archive = build_archive(tmp)
        db = Database(str(tmp / "t.db"))
        await db.connect()
        try:
            config = Config()  # app.dry_run True -> mock AI + mock publishers

            # 1. SCAN -> cities queued with photos
            await Scanner(db, str(archive)).scan()
            cities = await db.get_all_cities()
            moscow = next(c for c in cities if c.name == "Moscow")
            assert moscow.status == CityStatus.QUEUED, moscow.status

            # 2. CONTENT -> per-platform drafts (mock AI)
            engine = ContentEngine(db, config)
            processed = await engine.process_city(moscow.id)
            assert processed.status == CityStatus.DRAFTED, processed.status
            drafts = await db.get_drafts(city_id=moscow.id)
            assert len(drafts) == len(Platform)
            assert all(d.ai_provider == "mock" for d in drafts)

            # 3. HUMAN APPROVAL (auto rule: telegram+vk)
            await DraftManager(db).auto_approve()
            approved = await db.get_drafts(city_id=moscow.id, status=DraftStatus.APPROVED)
            assert {d.platform for d in approved} == {"telegram", "vk"}, [d.platform for d in approved]

            # 4. SCHEDULE -> SCHEDULED rows for approved auto platforms only
            sched = Scheduler(db, config)
            planned = await sched.plan()
            sched_pubs = [p for p in planned if p.status == PublicationStatus.SCHEDULED]
            assert {p.platform for p in sched_pubs} == {"telegram", "vk"}, [p.platform for p in planned]
            assert all(p.scheduled_at is not None for p in sched_pubs)

            # 5. PUBLISH due posts (force them into the past so they are due now)
            for p in sched_pubs:
                await db.update_publication(
                    p.id, scheduled_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
                )
            results = await sched.run_due()
            assert len(results) >= 1, results

            tel = await db.get_publication_by_platform(moscow.id, "telegram")
            vk = await db.get_publication_by_platform(moscow.id, "vk")
            assert tel.status == PublicationStatus.PUBLISHED, tel.status
            assert vk.status == PublicationStatus.PUBLISHED, vk.status
            assert tel.external_id and vk.external_id  # real-ish external handles from mock

            # drafts that were published are now PUBLISHED
            pub_drafts = await db.get_drafts(city_id=moscow.id, status=DraftStatus.PUBLISHED)
            assert {d.platform for d in pub_drafts} == {"telegram", "vk"}, [d.platform for d in pub_drafts]

            print("PASS: full pipeline scan -> content -> approve -> schedule -> publish")
        finally:
            await db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
