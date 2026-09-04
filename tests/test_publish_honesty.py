"""ADR-102 — honest publishing: VK photo -> manual, error classes, honoured flags.

* VK photo upload is not automated: it must be flagged ``manual`` (never a fake
  ``published`` and never an endless ``failed`` retry loop).
* A "not configured" (bot token/group missing) error is permanent:
  ``retryable=False`` and the resulting Publication carries a ``[permanent]``
  marker so ``retry_failed`` does not burn attempt budget on it.
* ``config.publishing.<platform>: false`` actually disables a platform in plan().
"""

from types import SimpleNamespace

import pytest

from core import models as m
from core.config import Config
from core.database import Database
from modules.publishers import vk as vk_mod
from modules.publishers.base import PERMANENT_PREFIX, PublishResult
from modules.scheduler import Scheduler


class _Secrets:
    vk_access_token = "tok"


def _vk_config(group_id="1"):
    return SimpleNamespace(vk=SimpleNamespace(group_id=group_id), publishing=None)


async def test_vk_photo_is_manual_not_failed_or_published(monkeypatch):
    monkeypatch.setattr("modules.publishers.base.get_secrets", lambda: _Secrets())
    pub = vk_mod.VKPublisher(None, _vk_config())
    res = await pub.publish(
        m.Draft(city_id=0, platform="vk", title="t", content="c",
                status=m.DraftStatus.APPROVED),
        ["a.jpg"],
    )
    assert res.manual is True
    assert res.status_hint == "manual"
    assert res.success is False


async def test_vk_not_configured_is_permanent(monkeypatch):
    monkeypatch.setattr(
        "modules.publishers.base.get_secrets",
        lambda: SimpleNamespace(vk_access_token=""),
    )
    pub = vk_mod.VKPublisher(None, _vk_config(group_id=None))
    res = await pub.publish(
        m.Draft(city_id=0, platform="vk", title="t", content="c",
                status=m.DraftStatus.APPROVED),
    )
    assert res.success is False
    assert res.retryable is False


@pytest.fixture
async def seeded_db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    city = m.City(name="Moscow", country="", year=2020)
    await db.add_city(city)
    city = await db.get_city_by_name("Moscow", 2020)
    return db, city


async def test_retry_failed_skips_permanent(seeded_db):
    db, city = seeded_db
    permanent = m.Publication(
        city_id=city.id, platform="vk", status=m.PublicationStatus.FAILED,
        error_message=PERMANENT_PREFIX + "bad config", retry_count=0,
    )
    transient = m.Publication(
        city_id=city.id, platform="telegram", status=m.PublicationStatus.FAILED,
        error_message="network down", retry_count=0,
    )
    await db.save_published(permanent)
    await db.save_published(transient)

    requeued = await Scheduler(db, Config()).retry_failed()
    assert len(requeued) == 1
    assert requeued[0].platform == "telegram"
    assert requeued[0].status == m.PublicationStatus.PENDING


async def test_plan_skips_disabled_platform(seeded_db):
    db, city = seeded_db
    await db.add_draft(m.Draft(city_id=city.id, platform="vk", title="t",
                               content="c", status=m.DraftStatus.APPROVED))
    await db.add_draft(m.Draft(city_id=city.id, platform="telegram", title="t",
                               content="c", status=m.DraftStatus.APPROVED))
    cfg = Config.model_validate({"publishing": {"vk": False, "telegram": True}})

    created = await Scheduler(db, cfg).plan(limit=10)
    platforms = {p.platform for p in created}
    assert "telegram" in platforms
    assert "vk" not in platforms


async def test_publish_service_marks_permanent_error(seeded_db, monkeypatch):
    db, city = seeded_db
    await db.add_draft(m.Draft(city_id=city.id, platform="vk", title="t",
                               content="c", status=m.DraftStatus.APPROVED))

    class StubPublisher:
        async def publish(self, draft, media_paths=None):
            return PublishResult(success=False, retryable=False, error="bad scope",
                                 status_hint="failed")

    monkeypatch.setattr(
        "modules.publishers.service.build_publisher",
        lambda db, cfg, platform: StubPublisher(),
    )

    from modules.publishers.service import PublishService
    res = await PublishService(db, Config()).publish_draft(1)
    assert res["status"] == "failed"
    assert res["error"].startswith(PERMANENT_PREFIX)

    pub = await db.get_publication_by_platform(city.id, "vk")
    assert pub is not None
    assert pub.error_message.startswith(PERMANENT_PREFIX)
