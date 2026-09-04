"""PublishService: approved draft -> platform -> Publication record."""
from __future__ import annotations

import datetime as dt
import json
from typing import List, Optional

from core import models as m
from core.database import Database
from modules.publishers.base import PERMANENT_PREFIX
from modules.publishers.registry import build_publisher


class PublishService:
    def __init__(self, db: Database, config) -> None:
        self.db = db
        self.config = config

    async def _media_paths(self, draft: m.Draft) -> List[str]:
        if not draft.photos_json:
            return []
        try:
            rows = json.loads(draft.photos_json)
        except (ValueError, TypeError):
            return []
        out = []
        for r in rows:
            if isinstance(r, str):
                out.append(r)
            elif isinstance(r, dict):
                out.append(r.get("path") or r.get("asset_path"))
        return [p for p in out if p]

    async def publish_draft(self, draft_id: int) -> dict:
        drafts = await self.db.get_drafts(draft_id=draft_id)
        if not drafts:
            raise ValueError(f"draft {draft_id} not found")
        draft = drafts[0]
        if draft.status != m.DraftStatus.APPROVED:
            raise ValueError(f"draft {draft_id} is not approved (status={draft.status})")

        publisher = build_publisher(self.db, self.config, draft.platform)
        media = await self._media_paths(draft)
        result = await publisher.publish(draft, media)

        now = dt.datetime.now(dt.timezone.utc)
        pub = m.Publication(city_id=draft.city_id, platform=draft.platform)
        status, external_id, url, err = m.PublicationStatus.PUBLISHED, "", "", ""
        if result.success and not result.manual:
            external_id, url = result.external_id, result.url
            pub.published_at = now
            m.draft_transition(draft.status.value, m.DraftStatus.PUBLISHED)
        elif result.manual:
            status = m.PublicationStatus.MANUAL
        else:
            status = m.PublicationStatus.FAILED
            err = result.error
            if not result.retryable:
                # Permanent error: tell retry_failed not to requeue this one.
                err = PERMANENT_PREFIX + result.error

        existing = await self.db.get_publication_by_platform(draft.city_id, draft.platform)
        if existing is not None:
            # Upsert: the scheduler may have pre-created a pending/scheduled row
            # for this (city, platform). Progress it instead of inserting a duplicate.
            kwargs: dict = {
                "status": status.value,
                "external_id": external_id,
                "url": url,
                "error_message": err,
            }
            if result.success and not result.manual:
                kwargs["published_at"] = now
            saved = await self.db.update_publication(existing.id, **kwargs)
        else:
            pub.status = status
            pub.external_id = external_id
            pub.url = url
            pub.error_message = err
            saved = await self.db.save_published(pub)
        if result.success and not result.manual:
            await self.db.update_draft_status(draft_id, m.DraftStatus.PUBLISHED)

        return {
            "publication_id": saved.id if saved else None,
            "platform": draft.platform,
            "status": status.value,
            "manual": result.manual,
            "external_id": external_id,
            "url": url,
            "error": err,
        }

    async def mark_manual_published(self, draft_id: int) -> m.Publication:
        """Complete a manual-platform post when a human has placed it.

        Manual platforms (zen/instagram/youtube/trip_com) are never auto-published:
        the pipeline only prepares the content and leaves the publication in
        ``MANUAL`` (or creates none if plan() never ran). After the user copies the
        post into their account and posts it, this marks the publication
        ``PUBLISHED`` (with ``published_at``) and transitions the underlying
        APPROVED draft to ``PUBLISHED`` so the stats stay honest.

        Works whether or not a MANUAL publication row exists yet:
        - exists and not published  -> progress it to PUBLISHED (MANUAL is legal now);
        - missing                   -> create a PUBLISHED publication for (city, platform).

        Idempotent: an already-PUBLISHED publication is returned unchanged.
        """
        drafts = await self.db.get_drafts(draft_id=draft_id)
        if not drafts:
            raise ValueError(f"draft {draft_id} not found")
        draft = drafts[0]

        now = dt.datetime.now(dt.timezone.utc)
        existing = await self.db.get_publication_by_platform(draft.city_id, draft.platform)
        if existing is None:
            saved = await self.db.save_published(
                m.Publication(
                    city_id=draft.city_id,
                    platform=draft.platform,
                    status=m.PublicationStatus.PUBLISHED,
                    published_at=now,
                    content_version=draft.content_version,
                )
            )
            pub = saved
        else:
            if existing.status.value != m.PublicationStatus.PUBLISHED.value:
                await self.db.update_publication_status(
                    existing.id, m.PublicationStatus.PUBLISHED.value
                )
            pub = await self.db.update_publication(existing.id, published_at=now)

        # PENDING and APPROVED drafts both transition straight to PUBLISHED
        # (PENDING -> PUBLISHED is legal); PUBLISHED is skipped for idempotency.
        if draft.status.value != m.DraftStatus.PUBLISHED.value:
            await self.db.update_draft_status(draft.id, m.DraftStatus.PUBLISHED.value)

        return pub
