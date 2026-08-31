import shutil
import tempfile
from pathlib import Path

import pytest

from core.config import Config
from core.database import Database
from core.models import City, CityStatus, Platform
from modules.content.engine import ContentEngine
from modules.scanner import Scanner
from tests.test_scanner import build_archive


@pytest.mark.asyncio
async def test_content_pipeline_creates_drafts():
    tmp = Path(tempfile.mkdtemp(prefix="tba_content_"))
    try:
        archive = build_archive(tmp)
        db = Database(str(tmp / "t.db"))
        await db.connect()
        try:
            sc = Scanner(db, str(archive))
            await sc.scan()
            cities = await db.get_all_cities()
            moscow = next(c for c in cities if c.name == "Moscow")

            config = Config()  # app.dry_run defaults True -> mock provider
            engine = ContentEngine(db, config)
            city = await engine.process_city(moscow.id)

            assert city.status == CityStatus.DRAFTED, city.status

            drafts = await db.get_drafts(city_id=moscow.id)
            assert len(drafts) == len(Platform), [d.platform for d in drafts]
            for d in drafts:
                assert d.content.strip() != "", f"empty content for {d.platform}"
                assert d.ai_provider == "mock", d.ai_provider

            # every enabled platform got a draft
            got = {d.platform for d in drafts}
            assert got == {p.value for p in Platform}, got

            # drafts carry the photo payload (photos_json) and a title
            assert all(d.title for d in drafts), "every draft should have a title"

            # the mocked BASE story must mention the city (context preserved there)
            base = await engine.generate_base_story(
                moscow, ["- photo_a.jpg: сцена (объекты: x; настроение: y; текст: -)"]
            )
            assert "Moscow" in base, base
            print("PASS: content pipeline -> DRAFTED with per-platform drafts")
        finally:
            await db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_content_engine_no_photos_marks_drafted():
    tmp = Path(tempfile.mkdtemp(prefix="tba_content_"))
    try:
        db = Database(str(tmp / "t.db"))
        await db.connect()
        try:
            # a city with no usable photos still must not crash the pipeline
            city = await db.add_city(City(name="Vilnius", country="Lithuania", year=2016))
            config = Config()
            engine = ContentEngine(db, config)
            result = await engine.process_city(city.id)
            assert result.status == CityStatus.DRAFTED, result.status
            drafts = await db.get_drafts(city_id=city.id)
            assert drafts == []
            print("PASS: city with no photos -> DRAFTED (empty)")
        finally:
            await db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
