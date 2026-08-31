"""VibeCoding feature tests — DB, generator, media, publisher, scheduler.

Runs entirely under dry_run/mock mode (Config defaults to ``app.dry_run=True``),
so no network calls and no API keys are needed: text generation uses the Mock
provider, image generation uses the Pillow placeholder, and auto platforms use
MockPublisher. Manual platforms (trip_com/zen/instagram) stay honestly ``manual``.
"""

import os

import pytest
from PIL import Image

from core.config import Config, MediaPreset
from core.database import Database
from core.models import VibeCodingStatus
from modules.media import prepare_vibecoding_media
from modules.publishers.vibecoding import VibeCodingPublisherService
from modules.scheduler import Scheduler
from modules.vibecoding_generator import VibeCodingGenerator


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "v.db"))
    await d.connect()
    return d


@pytest.fixture
def config():
    """Config with the platform presets the VibeCoding media prep depends on."""
    cfg = Config()
    cfg.media_presets = {
        "trip_cover": MediaPreset(width=1200, height=800, max_mb=5),
        "facebook_post": MediaPreset(width=1200, height=630, max_mb=8),
        "telegram": MediaPreset(width=1280, height=720, max_mb=5),
        "vk_post": MediaPreset(width=1200, height=630, max_mb=8),
        "zen": MediaPreset(width=1200, height=630, max_mb=10),
        "instagram_post": MediaPreset(width=1080, height=1080, max_mb=8),
    }
    return cfg


# -- DB CRUD + state machine ------------------------------------------------
async def test_vibecoding_crud(db):
    row = await db.add_vibecoding_post("Мой пост", "AI-агенты", "текст-промпт", "img-промпт")
    assert row.id is not None
    assert row.status == VibeCodingStatus.DRAFT

    got = await db.get_vibecoding_post(row.id)
    assert got.topic == "AI-агенты"

    upd = await db.update_vibecoding_post(row.id, generated_text="hello", image_url="/tmp/x.png")
    assert upd.generated_text == "hello" and upd.image_url == "/tmp/x.png"

    lst = await db.get_vibecoding_posts(status=VibeCodingStatus.DRAFT.value)
    assert any(p.id == row.id for p in lst)

    await db.update_vibecoding_status(row.id, "pending")
    assert (await db.get_vibecoding_post(row.id)).status.value == "pending"

    assert await db.delete_vibecoding_post(row.id) is True
    assert await db.get_vibecoding_post(row.id) is None


async def test_vibecoding_illegal_transition_rejected(db):
    row = await db.add_vibecoding_post("T", "t")
    await db.update_vibecoding_status(row.id, "published")
    # published is terminal — moving back to draft must raise
    with pytest.raises(Exception):
        await db.update_vibecoding_status(row.id, "draft")


# -- generator (dry_run -> mock text + mock image) --------------------------
async def test_generator_generates_and_saves(db):
    gen = VibeCodingGenerator(db, Config())
    pid = await gen.generate_and_save("Автотесты и вайбкодинг")
    post = await db.get_vibecoding_post(pid)
    assert post.generated_text, "text should be generated"
    assert os.path.exists(post.image_url), "image file should exist"
    assert post.status == VibeCodingStatus.DRAFT


# -- media prep -------------------------------------------------------------
async def test_media_prepare(db, config, tmp_path):
    src = tmp_path / "tile.png"
    Image.new("RGB", (1024, 1024), (10, 20, 30)).save(src)
    out = prepare_vibecoding_media(str(src), 7, config.media_presets, config)
    assert "facebook" in out and "trip_com" in out and "telegram" in out
    with Image.open(out["facebook"]) as im:
        assert im.size == (1200, 630)  # facebook_post preset
    with Image.open(out["trip_com"]) as im:
        assert im.size == (1200, 800)  # trip_cover preset


# -- publisher (dry_run -> mock auto, honest manual) ------------------------
async def test_publish_vibecoding_post(db, config):
    gen = VibeCodingGenerator(db, config)
    pid = await gen.generate_and_save("Публикуем пост")
    svc = VibeCodingPublisherService(db, config)
    res = await svc.publish_vibecoding_post(pid)

    assert res["status"] == "published"
    assert "facebook" in res["platform_status"]
    # auto platforms (facebook/telegram/vk) are mock-published, not manual
    assert res["platform_status"]["facebook"]["manual"] is False
    # manual platforms stay honestly manual
    assert res["platform_status"]["trip_com"]["manual"] is True
    assert res["platform_status"]["zen"]["manual"] is True
    assert res["platform_status"]["instagram"]["manual"] is True

    post = await db.get_vibecoding_post(pid)
    assert post.status == VibeCodingStatus.PUBLISHED
    assert post.published_at is not None


async def test_publish_vibecoding_post_idempotent(db, config):
    gen = VibeCodingGenerator(db, config)
    pid = await gen.generate_and_save("Идемпотентность")
    svc = VibeCodingPublisherService(db, config)
    await svc.publish_vibecoding_post(pid)
    # second publish is a no-op and keeps published
    again = await svc.publish_vibecoding_post(pid)
    assert again["status"] == "published"
    assert (await db.get_vibecoding_post(pid)).status == VibeCodingStatus.PUBLISHED


# -- scheduler --------------------------------------------------------------
async def test_scheduler_respects_auto_publish(db, config):
    gen = VibeCodingGenerator(db, config)
    pid_draft = await gen.generate_and_save("Черновик")

    sched = Scheduler(db, config)
    # auto_publish is False by default -> drafts are NOT published
    res_off = await sched.publish_vibecoding_due()
    assert res_off == []
    assert (await db.get_vibecoding_post(pid_draft)).status == VibeCodingStatus.DRAFT

    # a pending (explicitly scheduled) post IS published
    await db.update_vibecoding_status(pid_draft, "pending")
    res = await sched.publish_vibecoding_due()
    assert any(r["status"] == "published" for r in res)
    assert (await db.get_vibecoding_post(pid_draft)).status.value == "published"

    # disabled feature -> nothing published
    config.vibecoding.enabled = False
    assert await sched.publish_vibecoding_due() == []


async def test_scheduler_publishes_drafts_when_auto_publish(db, config):
    config.vibecoding.auto_publish = True
    gen = VibeCodingGenerator(db, config)
    pid = await gen.generate_and_save("Автопубликация")
    sched = Scheduler(db, config)
    res = await sched.publish_vibecoding_due()
    assert any(r["post_id"] == pid and r["status"] == "published" for r in res)
    assert (await db.get_vibecoding_post(pid)).status == VibeCodingStatus.PUBLISHED
