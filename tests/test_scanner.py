import shutil
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from core.database import Database
from core.models import ScanStatus
from modules.scanner import Scanner


def make_jpg(path, color=(200, 0, 0), exif=None):
    im = Image.new("RGB", (120, 80), color)
    if exif is not None:
        e = im.getexif()
        for k, v in exif.items():
            e[k] = v
        im.save(path, exif=e)
    else:
        im.save(path)


def build_archive(root: Path) -> Path:
    archive = root / "archive"
    msk = archive / "Moscow_2020"; msk.mkdir(parents=True)
    make_jpg(msk / "photo_a.jpg", (255, 0, 0), exif={0x0132: "2021:08:15 14:30:00", 0x010F: "Canon", 0x0110: "EOS"})
    make_jpg(msk / "photo_b.jpg", (0, 255, 0))
    make_jpg(msk / "photo_a_copy.jpg", (255, 0, 0), exif={0x0132: "2021:08:15 14:30:00", 0x010F: "Canon", 0x0110: "EOS"})
    (msk / "broken.jpg").write_bytes(b"this is definitely not a jpeg image")

    par = archive / "Paris_2018"; par.mkdir(parents=True)
    make_jpg(par / "paris_a.jpg", (0, 0, 255))

    y = archive / "2015" / "Rome"; y.mkdir(parents=True)
    make_jpg(y / "rome_a.jpg", (10, 10, 10))
    return archive


@pytest.mark.asyncio
async def test_full_scan_lifecycle():
    tmp = Path(tempfile.mkdtemp(prefix="tba_test_"))
    try:
        archive = build_archive(tmp)
        db = Database(str(tmp / "t.db"))
        await db.connect()
        try:
            sc = Scanner(db, str(archive))
            stats = await sc.scan()

            assert stats["new"] == 5 and stats["failed"] == 1 and stats["duplicates"] == 1, stats

            cities = await db.get_all_cities()
            names = {c.name: c for c in cities}
            assert names["Moscow"].year == 2020
            assert names["Paris"].year == 2018
            assert names["Rome"].year == 2015
            assert len(cities) == 3

            photos = []
            for c in cities:
                photos += await db.get_photos_by_city(c.id)
            byfile = {p.filename: p for p in photos}
            assert byfile["photo_a.jpg"].scan_status == ScanStatus.SCANNED
            assert byfile["photo_a_copy.jpg"].scan_status == ScanStatus.DUPLICATE
            assert byfile["broken.jpg"].scan_status == ScanStatus.FAILED
            assert byfile["photo_a.jpg"].taken_at is not None
            assert byfile["photo_a.jpg"].year == 2021
            assert byfile["photo_a.jpg"].city == "Moscow"

            # incremental: unchanged → skipped, nothing new
            stats2 = await sc.scan()
            assert stats2["new"] == 0 and stats2["skipped"] >= 6, stats2

            # deletion detection
            (archive / "Moscow_2020" / "photo_b.jpg").unlink()
            stats3 = await sc.scan()
            after = [p for p in await db.get_photos_by_city(names["Moscow"].id) if p.filename == "photo_b.jpg"]
            assert after and after[0].scan_status == ScanStatus.MISSING
            assert stats3["missing"] >= 1
        finally:
            await db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
