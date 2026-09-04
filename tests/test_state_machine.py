"""ADR-103 — atomic claim (no double publish) + state-machine enforcement.

* ``claim_publication`` is a compare-and-swap on the status column: the first
  concurrent tick wins and turns a SCHEDULED/PENDING row into ``processing``;
  the second call returns None so the same publication is never published twice.
* ``update_publication`` validates every status write against the Publication
  state machine (illegal transitions raise), so no path can silently skip ahead.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core import models as m
from core.config import Config
from core.database import Database
from core.exceptions import StateTransitionError

UTC = timezone.utc


@pytest.fixture
async def seeded(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    city = m.City(name="Moscow", country="", year=2020)
    await db.add_city(city)
    city = await db.get_city_by_name("Moscow", 2020)
    yield db, city


def _pub(city, platform="vk", status=m.PublicationStatus.SCHEDULED):
    return m.Publication(
        city_id=city.id,
        platform=platform,
        status=status,
        scheduled_at=datetime.now(UTC) + timedelta(minutes=5),
    )


async def test_claim_is_compare_and_swap(seeded):
    db, city = seeded
    await db.save_published(_pub(city))
    saved = await db.get_publication_by_platform(city.id, "vk")

    first = await db.claim_publication(saved.id)
    assert first is not None
    assert first.status == m.PublicationStatus.PROCESSING

    second = await db.claim_publication(saved.id)
    assert second is None  # already claimed by another tick (F6)


async def test_update_publication_rejects_illegal_transition(seeded):
    db, city = seeded
    await db.save_published(_pub(city))
    saved = await db.get_publication_by_platform(city.id, "vk")

    with pytest.raises(StateTransitionError):
        await db.update_publication(saved.id, status=m.PublicationStatus.MANUAL.value)
    # SCHEDULED -> PUBLISHED is legal
    ok = await db.update_publication(saved.id, status=m.PublicationStatus.PUBLISHED.value)
    assert ok.status == m.PublicationStatus.PUBLISHED


async def test_processing_to_manual_allowed(seeded):
    db, city = seeded
    await db.save_published(_pub(city, status=m.PublicationStatus.PENDING))
    saved = await db.get_publication_by_platform(city.id, "vk")

    await db.claim_publication(saved.id)  # PENDING -> processing
    updated = await db.update_publication(
        saved.id, status=m.PublicationStatus.MANUAL.value
    )
    assert updated.status == m.PublicationStatus.MANUAL


async def test_retry_requeue_is_legal_transition(seeded):
    db, city = seeded
    await db.save_published(
        _pub(city, status=m.PublicationStatus.FAILED)
    )
    saved = await db.get_publication_by_platform(city.id, "vk")
    # FAILED -> PENDING (what retry_failed does) stays legal under enforcement
    updated = await db.update_publication(
        saved.id, status=m.PublicationStatus.PENDING.value, retry_count=1
    )
    assert updated.status == m.PublicationStatus.PENDING
