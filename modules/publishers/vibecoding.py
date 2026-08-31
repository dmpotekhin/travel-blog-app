"""VibeCoding publisher service (F4).

Publishes a generated VibeCoding post to every enabled platform through the SAME
honest adapters used for travel posts (:class:`build_publisher`). No new
publishing logic is invented here — just a thin orchestration that maps a
VibeCoding post onto the existing ``BasePublisher`` contract:

* auto platforms (telegram / vk / facebook) are published via their adapters
  (``MockPublisher`` under ``app.dry_run``);
* manual platforms (trip_com / zen / instagram) are prepared honestly and
  flagged ``manual`` — the pipeline never fakes a ``published`` status.

The overall post ``status`` mirrors the existing state machine
(:data:`core.models.VibeCodingStatus`): ``published`` if any auto platform
succeeded, ``pending`` if content was prepared for a human, ``error`` if all
platforms failed.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Dict, List, Optional

from core import models as m
from core.config import Config
from core.database import Database
from core.exceptions import NotFoundError
from modules.media import prepare_vibecoding_media
from .registry import build_publisher

UTC = dt.timezone.utc


class VibeCodingPublisherService:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config

    def _title_for(self, topic: str) -> str:
        return f"Вайбкодинг: {topic}"

    async def _prepare_media(self, post_id: int, image_path: str) -> Dict[str, str]:
        """Return ``{platform: absolute_path}`` for every enabled platform."""
        prepared = prepare_vibecoding_media(
            image_path, post_id, self.config.media_presets, self.config
        )
        return {p: str(path) for p, path in prepared.items()}

    async def publish_vibecoding_post(self, post_id: int) -> dict:
        """Publish a VibeCoding post to all enabled platforms; return results."""
        post = await self.db.get_vibecoding_post(post_id)
        if post is None:
            raise NotFoundError(f"VibeCoding post {post_id} not found")

        # Idempotent: an already-published post is returned as-is.
        if post.status == m.VibeCodingStatus.PUBLISHED:
            return {
                "post_id": post_id,
                "status": post.status.value,
                "platform_status": self._load_platform_status(post.platform_status),
            }

        # Bring the post into a state from which publication is legal.
        if post.status.value != m.VibeCodingStatus.PENDING.value:
            await self.db.update_vibecoding_status(post_id, m.VibeCodingStatus.PENDING.value)

        media = await self._prepare_media(post_id, post.image_url)
        title = self._title_for(post.topic)

        results: Dict[str, dict] = {}
        for platform, media_path in media.items():
            publisher = build_publisher(self.db, self.config, platform)
            draft = m.Draft(
                city_id=0,
                platform=platform,
                title=title,
                content=post.generated_text,
                status=m.DraftStatus.APPROVED,
            )
            result = await publisher.publish(draft, [media_path])
            results[platform] = {
                "success": result.success,
                "manual": result.manual,
                "external_id": result.external_id,
                "url": result.url,
                "error": result.error,
            }

        status = self._overall_status(results)
        now = dt.datetime.now(UTC)
        platform_status_json = json.dumps(results, ensure_ascii=False)

        if status == m.VibeCodingStatus.PUBLISHED.value:
            await self.db.update_vibecoding_status(post_id, m.VibeCodingStatus.PUBLISHED.value)
            await self.db.update_vibecoding_post(post_id, published_at=now)
        elif status == m.VibeCodingStatus.ERROR.value:
            await self.db.update_vibecoding_status(post_id, m.VibeCodingStatus.ERROR.value)

        await self.db.update_vibecoding_post(post_id, platform_status=platform_status_json)

        return {
            "post_id": post_id,
            "status": status,
            "platform_status": results,
        }

    @staticmethod
    def _overall_status(results: Dict[str, dict]) -> str:
        """Overall post status from per-platform results (see module docstring)."""
        successes = [r for r in results.values() if r["success"] and not r["manual"]]
        manuals = [r for r in results.values() if r["manual"]]
        errors = [r for r in results.values() if not r["success"] and not r["manual"]]
        if successes:
            return m.VibeCodingStatus.PUBLISHED.value
        if manuals:
            return m.VibeCodingStatus.PENDING.value
        if errors:
            return m.VibeCodingStatus.ERROR.value
        return m.VibeCodingStatus.PENDING.value

    @staticmethod
    def _load_platform_status(raw: str) -> dict:
        try:
            return json.loads(raw or "{}")
        except (ValueError, TypeError):
            return {}
