import shutil
import tempfile
from pathlib import Path

import pytest

from core.database import Database
from core.models import City, CityStatus, Photo, ScanStatus, StateTransitionError
from modules.queue import CityQueue
from modules.scanner import Scanner
from tests.test_scanner import build_archive


@pytest.fixture
async def db_and_queue():
    tmp = Path(tempfile.mkdtemp(prefix="tba_q_"))
    db = Database(str(tmp / "t.db"))
    await db.connect()
    try:
        archive = build_archive(tmp)
        sc = Scanner(db, str(archive))
        await sc.scan()
        queue = CityQueue(db)
        yield db, queue
    finally:
        await db.close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_queue_ordering_by_priority(db_and_queue):
    db, queue = db_and_queue
    cities = await db.get_all_cities()
    by_name = {c.name: c for c in cities}

    await db.update_city_priority(by_name["Moscow"].id, 100)
    await db.update_city_priority(by_name["Rome"].id, 50)
    await db.update_city_priority(by_name["Paris"].id, 0)

    ready = await queue.next(batch_size=10)
    # high priority first, then oldest year (Rome 2015 < Paris 2018)
    assert [c.name for c in ready] == ["Moscow", "Rome", "Paris"]
    assert all(c.status == CityStatus.QUEUED for c in ready)
    print("PASS: queue ordering (priority desc, year asc)")


@pytest.mark.asyncio
async def test_claim_and_illegal_transition(db_and_queue):
    db, queue = db_and_queue
    city = (await db.get_all_cities())[0]

    claimed = await queue.claim(city.id)
    assert claimed.status == CityStatus.PROCESSING

    with pytest.raises(StateTransitionError):
        await queue.claim(city.id)  # processing -> processing is illegal
    print("PASS: claim + illegal transition rejected")


@pytest.mark.asyncio
async def test_draft_requeue_error_recovery(db_and_queue):
    db, queue = db_and_queue
    city = (await db.get_all_cities())[0]

    await queue.claim(city.id)
    await queue.mark_drafted(city.id)
    assert (await db.get_city(city.id)).status == CityStatus.DRAFTED

    await queue.requeue(city.id)
    assert (await db.get_city(city.id)).status == CityStatus.QUEUED

    await queue.mark_error(city.id, "test")
    assert (await db.get_city(city.id)).status == CityStatus.ERROR

    n = await queue.re_enqueue_errors()
    assert n == 1
    assert (await db.get_city(city.id)).status == CityStatus.QUEUED
    print("PASS: drafted -> requeue, error -> requeue recovery")


@pytest.mark.asyncio
async def test_require_photos_excludes_photo_less_cities(db_and_queue):
    db, queue = db_and_queue
    # a queued city with NO scanned photo (only a failed one) must be excluded
    city = await db.add_city(City(name="Nowhere", country="X", year=2020))
    await db.add_photo(Photo(
        city_id=city.id, path="/nowhere/a.jpg", filename="a.jpg", size=1, modified_at=None,
        sha256="deadbeef", scan_status=ScanStatus.FAILED, city="Nowhere", country="X", year=2020,
    ))

    ready = await queue.next(batch_size=100)
    assert "Nowhere" not in [c.name for c in ready]

    # require_photos=False returns it
    all_q = await queue.next(batch_size=100, require_photos=False)
    assert "Nowhere" in [c.name for c in all_q]
    print("PASS: require_photos gating")


@pytest.mark.asyncio
async def test_counts(db_and_queue):
    db, queue = db_and_queue
    counts = await queue.counts()
    assert counts["queued"] == 3
    assert counts["processing"] == 0
    print("PASS: counts by status")
