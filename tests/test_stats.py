import shutil
import tempfile
from pathlib import Path

import pytest

from core.database import Database
from core.models import City, Draft, DraftStatus, Photo, Publication, PublicationStatus
from modules.stats import StatsService


@pytest.mark.asyncio
async def test_stats_summary_and_groups():
    tmp = Path(tempfile.mkdtemp())
    try:
        db = Database(tmp / "stats.db")
        await db.connect()
        city = await db.add_city(
            City(name="Test City", country="RU", year=2020, folder_path="/tmp/tc")
        )
        await db.add_photo(
            Photo(
                city_id=city.id,
                path="/tmp/tc/photo1.jpg",
                filename="photo1.jpg",
                size=100,
                sha256="abc",
                scan_status="scanned",
            )
        )
        await db.add_photo(
            Photo(city_id=city.id, path="/tmp/tc/photo2.jpg", filename="photo2.jpg", sha256="def")
        )
        # drafts: one approved, one pending, one error
        await db.add_draft(
            Draft(city_id=city.id, platform="telegram", title="Tg", content="x", status=DraftStatus.APPROVED)
        )
        await db.add_draft(
            Draft(city_id=city.id, platform="vk", title="Vk", content="y", status=DraftStatus.PENDING)
        )
        await db.add_draft(
            Draft(city_id=city.id, platform="zen", title="Zen", content="z", status=DraftStatus.REJECTED)
        )
        # publications: scheduled, published, failed
        await db.save_published(
            Publication(city_id=city.id, platform="telegram", status=PublicationStatus.SCHEDULED)
        )
        await db.save_published(
            Publication(city_id=city.id, platform="vk", status=PublicationStatus.PUBLISHED)
        )
        await db.save_published(
            Publication(city_id=city.id, platform="facebook", status=PublicationStatus.FAILED)
        )

        svc = StatsService(db)
        summary = await svc.summary()
        assert summary["cities_total"] == 1
        assert summary["photos_total"] == 2
        assert summary["drafts_total"] == 3
        assert summary["publications_total"] == 3
        assert summary["published"] == 1
        assert summary["scheduled"] == 1
        assert summary["pending_approval"] == 1
        assert summary["approved"] == 1
        # errors: 1 failed publication + 0 error drafts/0 error city
        assert summary["errors"] == 1

        by_status = await svc.by_status()
        assert by_status["cities"].get("queued") == 1
        assert by_status["drafts"]["approved"] == 1
        assert by_status["drafts"]["pending"] == 1
        assert by_status["publications"]["published"] == 1
        assert by_status["publications"]["failed"] == 1

        by_platform = await svc.by_platform()
        assert by_platform["telegram"]["scheduled"] == 1
        assert by_platform["vk"]["published"] == 1
        assert by_platform["facebook"]["failed"] == 1

        recent = await svc.recent(limit=2)
        assert len(recent) == 2
        assert all(r["status"] in ("failed", "published", "scheduled") for r in recent)

        await db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
