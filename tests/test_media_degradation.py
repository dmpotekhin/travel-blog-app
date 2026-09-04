"""ADR-104 — media path is honest: no silent 1-photo/truncated-caption posts.

* ``plan_media`` decides how many photos a platform can take and whether the
  post degrades (caption truncated or photos dropped).
* Telegram now sends a media group (up to 10) instead of always 1 photo.
* PublishService surfaces ``degraded`` in the result and logs it loudly (F7).
"""

import asyncio

import pytest

from core import models as m
from modules.publishers.base import PublishResult, plan_media
from modules.publishers import telegram as tg


class _Secrets:
    telegram_bot_token = "tok"


class _TelegramCfg:
    class telegram:
        chat_id = "@c"


def _draft(content="caption"):
    return m.Draft(city_id=0, platform="telegram", title="t", content=content, status=m.DraftStatus.APPROVED)


# --- plan_media -----------------------------------------------------------

def test_plan_media_no_media_no_degradation():
    sel, degraded, note = plan_media(None, "hello", photo_cap=10, caption_limit=1024)
    assert sel == [] and degraded is False and note == ""


def test_plan_media_drops_photos_and_flags():
    sel, degraded, note = plan_media(["p1.jpg", "p2.jpg"], "x", photo_cap=1, caption_limit=5000)
    assert sel == ["p1.jpg"] and degraded is True and "photos -> 1" in note


def test_plan_media_truncated_caption_flags():
    sel, degraded, note = plan_media(["p1.jpg"], "x" * 2000, photo_cap=10, caption_limit=1024)
    assert degraded is True and "caption" in note


def test_plan_media_clean_when_fit():
    sel, degraded, note = plan_media(["a.jpg", "b.jpg"], "short", photo_cap=10, caption_limit=1024)
    assert sel == ["a.jpg", "b.jpg"] and degraded is False and note == ""


# --- telegram -------------------------------------------------------------

class _FakeResp:
    status_code = 200

    def json(self):
        return {"result": {"message_id": 7}}


class _FakeClient:
    def __init__(self, *a, **k):
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        self.posts.append((url, kw))
        return _FakeResp()


def test_telegram_sends_media_group_and_flags_truncated_caption(tmp_path, monkeypatch):
    from modules.publishers import base as base_mod

    monkeypatch.setattr("modules.publishers.base.get_secrets", lambda: _Secrets())
    cli = _FakeClient()
    monkeypatch.setattr("modules.publishers.telegram.httpx.AsyncClient", lambda *a, **k: cli)

    p1 = tmp_path / "a.jpg"; p1.write_bytes(b"x")
    p2 = tmp_path / "b.jpg"; p2.write_bytes(b"y")
    pub = tg.TelegramPublisher(None, _TelegramCfg())
    draft = _draft(content="x" * 2000)  # > 1024 -> degraded
    res = asyncio.run(pub.publish(draft, [str(p1), str(p2)]))

    assert res.success is True
    assert res.degraded is True
    assert cli.posts and cli.posts[0][0].endswith("/sendMediaGroup")
