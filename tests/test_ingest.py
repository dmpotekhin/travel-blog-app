import io
from pathlib import Path

import pytest
from PIL import Image

from core.config import Config
from core.database import Database
from modules import ingest as ingest_mod
from modules.ingest import ingest_photos


def _jpg(color):
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), color).save(buf, format="JPEG")
    return buf.getvalue()


def _png(color):
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_ingest_creates_city_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_mod, "_archive_base", lambda cfg: tmp_path)

    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    try:
        cfg = Config()
        files = [("a.jpg", _jpg((1, 1, 1))), ("b.png", _png((2, 2, 2))), ("bad.txt", b"not an image")]

        res = await ingest_photos(db, cfg, "Rome", 2019, files)
        assert "error" not in res
        assert res["added"] == 2 and res["rejected"] == 1 and res["duplicates"] == 0

        city = await db.get_city_by_name("Rome", 2019)
        assert city is not None and city.id == res["city_id"]
        assert city.status.value == "queued"

        photos = await db.get_photos_by_city(city.id)
        assert len(photos) == 2
        assert all(p.scan_status.value == "scanned" for p in photos)

        # duplicate re-ingest is skipped by sha
        res2 = await ingest_photos(db, cfg, "Rome", 2019, [("a.jpg", _jpg((1, 1, 1)))])
        assert res2["added"] == 0 and res2["duplicates"] == 1

        # same city+year upserts onto the same row
        res3 = await ingest_photos(db, cfg, "Rome", 2019, [("c.jpg", _jpg((3, 3, 3)))])
        assert res3["city_id"] == city.id and res3["added"] == 1
    finally:
        await db.close()
