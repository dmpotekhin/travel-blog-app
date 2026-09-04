"""P9 Scheduler — plan publication, run due posts, retry failures, recover.

Pure async orchestration logic (ARCHITECTURE §P9):

* ``plan()``        — approved, media-ready drafts -> Publication rows. Auto/partial
                      get a ``scheduled`` row with a real ``scheduled_at``; manual
                      platforms get a ``manual`` row (human must finish them).
* ``run_due()``     — publish every ``scheduled``/``pending`` publication whose
                      ``scheduled_at`` has passed, via :class:`PublishService`.
* ``retry_failed()``— requeue ``failed`` publications as ``pending`` with an
                      exponential backoff, bounded by ``retry.max_attempts``.
* ``tick()``        — one production cycle: plan -> retry -> run_due.

APScheduler wiring is optional and lazily imported so tests never depend on a
running daemon (see :meth:`build_async_scheduler`).
"""

from __future__ import annotations

import datetime as dt
from typing import List, Optional
from zoneinfo import ZoneInfo

from loguru import logger

from core import models as m
from core.database import Database
from modules.publishers.base import PERMANENT_PREFIX
from modules.publishers.registry import MANUAL_PLATFORMS
from modules.publishers.service import PublishService
from modules.publishers.vibecoding import VibeCodingPublisherService
from modules.trip_validator import TripValidator
from modules.vibecoding_validator import VibeCodingValidator

UTC = dt.timezone.utc


class Scheduler:
    def __init__(
        self,
        db: Database,
        config,
        publish_service: Optional[PublishService] = None,
    ) -> None:
        self.db = db
        self.config = config
        self.publish_service = publish_service or PublishService(db, config)
        self.schedule = getattr(config, "schedule", None)

        tz_name = getattr(self.schedule, "timezone", None) or getattr(
            getattr(config, "app", None), "timezone", "UTC"
        )
        try:
            self.tz = ZoneInfo(tz_name)
        except Exception:  # invalid tz -> fall back to UTC
            self.tz = ZoneInfo("UTC")

    # -- time helpers -----------------------------------------------------
    @property
    def posts_per_day(self) -> int:
        try:
            return max(1, int(getattr(self.schedule, "posts_per_day", 2) or 2))
        except (TypeError, ValueError):
            return 2

    def _publish_time(self) -> dt.time:
        raw = getattr(self.schedule, "publish_time", "08:00") or "08:00"
        try:
            hh, mm = str(raw).strip().split(":")
            return dt.time(int(hh), int(mm))
        except (ValueError, TypeError):
            return dt.time(8, 0)

    def _today_base_utc(self, now: dt.datetime) -> dt.datetime:
        local = now.astimezone(self.tz)
        base_local = dt.datetime.combine(local.date(), self._publish_time(), tzinfo=self.tz)
        return base_local.astimezone(UTC)

    def _slot_for(self, k: int, now: dt.datetime) -> dt.datetime:
        """Return the k-th future publish slot (UTC-aware), spread across days.

        ``k`` counts from 0.  ``posts_per_day`` is honored: the first N slots land
        on the current publish day, the next N on the following day, and so on.
        """
        interval = dt.timedelta(hours=24.0 / self.posts_per_day)
        base = self._today_base_utc(now)
        day, intra = divmod(k, self.posts_per_day)
        slot = base + dt.timedelta(days=day) + interval * intra
        # roll forward until the slot is in the future
        while slot <= now:
            slot += dt.timedelta(days=1)
        return slot

    # -- planning ---------------------------------------------------------
    def _platform_enabled(self, platform: str) -> bool:
        """Whether ``platform`` is enabled in ``config.publishing`` (F2).

        ``publishing.*`` flags were dead: the engine generated drafts for all
        platforms and plan() scheduled every one of them. Now plan() skips a
        platform that is explicitly disabled (``publishing.<platform>: false``).
        """
        guard = getattr(self.config, "publishing", None)
        if guard is None:
            return True
        # Reuse the same semantics BasePublisher.enabled uses: missing flag -> on.
        return bool(getattr(guard, platform, True))

    async def plan(self, limit: int = 20) -> List[m.Publication]:
        """Create publication rows for approved drafts that have none yet."""
        now = dt.datetime.now(UTC)
        drafts = await self.db.get_drafts(status=m.DraftStatus.APPROVED.value)
        # how many slots are already taken today (so new ones get later slots)
        today_slots = await self._scheduled_today_count(now)

        created: List[m.Publication] = []
        k = today_slots
        for draft in drafts:
            if await self.db.get_publication_by_platform(draft.city_id, draft.platform):
                continue  # already planned
            if not self._platform_enabled(draft.platform):
                continue  # publishing.<platform> is false — never auto-publish it (F2)
            if draft.platform in MANUAL_PLATFORMS:
                status = m.PublicationStatus.MANUAL
                scheduled_at = None
            else:
                status = m.PublicationStatus.SCHEDULED
                scheduled_at = self._slot_for(k, now)
                k += 1
            pub = m.Publication(
                city_id=draft.city_id,
                platform=draft.platform,
                status=status,
                scheduled_at=scheduled_at,
            )
            saved = await self.db.save_published(pub)
            created.append(saved)
            if len(created) >= limit:
                break
        return created

    async def _scheduled_today_count(self, now: dt.datetime) -> int:
        """Number of publications already scheduled on ``now``'s local date."""
        local = now.astimezone(self.tz)
        day_start = dt.datetime.combine(
            local.date(), dt.time.min, tzinfo=self.tz
        ).astimezone(UTC)
        day_end = dt.datetime.combine(
            local.date() + dt.timedelta(days=1), dt.time.min, tzinfo=self.tz
        ).astimezone(UTC)
        count = 0
        for pub in await self.db.get_publications_by_status(
            m.PublicationStatus.SCHEDULED.value
        ):
            if pub.scheduled_at and day_start <= pub.scheduled_at < day_end:
                count += 1
        return count

    # -- execution --------------------------------------------------------
    async def run_due(self, limit: int = 20) -> List[m.Publication]:
        """Publish every due waiting publication; return the resulting rows."""
        now = dt.datetime.now(UTC)
        due = await self.db.get_due_publications(now, limit=limit)
        results: List[m.Publication] = []
        for pub in due:
            drafts = await self.db.get_drafts(
                city_id=pub.city_id,
                platform=pub.platform,
                status=m.DraftStatus.APPROVED.value,
            )
            if not drafts:
                continue
            draft = drafts[0]
            # trip.com compliance gate (soft by default; blocks when configured)
            if pub.platform == "trip_com":
                gate = await self._trip_gate(draft)
                if gate["blocked"]:
                    logger.bind(validator="trip_com", draft_id=draft.id) \
                        .warning("Post blocked by Trip guidelines: {}", gate["summary"])
                    continue
            await self.publish_service.publish_draft(draft.id)
            updated = await self.db.get_publication_by_platform(
                pub.city_id, pub.platform
            )
            if updated is not None:
                results.append(updated)
        return results

    # -- content compliance gates -----------------------------------------
    async def _trip_gate(self, draft) -> dict:
        """Validate a travel draft against Trip.com guidelines before publish."""
        tg = getattr(self.config, "trip_guidelines", None)
        if tg is None or not bool(getattr(tg, "enabled", False)):
            return {"compliant": True, "blocked": False, "summary": "trip_guidelines disabled"}
        city = await self.db.get_city(draft.city_id)
        result = TripValidator(self.config).validate(draft, city)
        blocked = (not result.compliant) and bool(getattr(tg, "block_non_compliant", False))
        return {"compliant": result.compliant, "blocked": blocked, "summary": result.summary()}

    async def _vibe_gate(self, post) -> dict:
        """Validate a VibeCoding post against the guidelines before publish."""
        vg = getattr(self.config, "vibecoding_guidelines", None)
        if vg is None or not bool(getattr(vg, "enabled", False)):
            return {"compliant": True, "blocked": False, "summary": "vibecoding_guidelines disabled"}
        result = VibeCodingValidator(self.config).validate(post)
        blocked = (not result.compliant) and bool(getattr(vg, "block_non_compliant", False))
        return {"compliant": result.compliant, "blocked": blocked, "summary": result.summary()}

    async def retry_failed(self) -> List[m.Publication]:
        """Requeue failed publications as pending with exponential backoff."""
        now = dt.datetime.now(UTC)
        retry = getattr(self.config, "retry", None)
        max_attempts = int(getattr(retry, "max_attempts", 3) or 3)
        base_delay = int(getattr(retry, "initial_delay_seconds", 60) or 60)
        exp = bool(getattr(retry, "exponential_backoff", True))

        requeued: List[m.Publication] = []
        for pub in await self.db.get_publications_by_status(
            m.PublicationStatus.FAILED.value
        ):
            if pub.retry_count >= max_attempts:
                continue
            if pub.error_message.startswith(PERMANENT_PREFIX):
                # Permanent error (bad config, invalid scope): a retry will not fix
                # it. Leave FAILED and never burn attempt budget on it (ADR-102).
                continue
            delay = base_delay * (2 ** pub.retry_count) if exp else base_delay
            next_at = now + dt.timedelta(seconds=delay)
            updated = await self.db.update_publication(
                pub.id,
                retry_count=pub.retry_count + 1,
                status=m.PublicationStatus.PENDING.value,
                scheduled_at=next_at,
            )
            requeued.append(updated)
        return requeued

    async def tick(self) -> dict:
        """One production cycle: plan new work, retry failures, publish due."""
        planned = await self.plan()
        requeued = await self.retry_failed()
        published = await self.run_due()
        return {
            "planned": len(planned),
            "requeued": len(requeued),
            "published": len(published),
        }

    # -- VibeCoding -------------------------------------------------------
    async def publish_vibecoding_due(self, limit: int = 10) -> List[dict]:
        """Publish due VibeCoding posts (queued by the 'Schedule' button).

        Honors ``config.vibecoding.enabled``. Posts are picked in priority
        order: ``pending`` (explicitly scheduled) first, then ``draft`` — but
        only when ``vibecoding.auto_publish`` is enabled. An already-published
        post is skipped (idempotent). Returns ``[{post_id, status}, ...]``.
        """
        vib = getattr(self.config, "vibecoding", None)
        if vib is None or not bool(getattr(vib, "enabled", False)):
            return []

        svc = VibeCodingPublisherService(self.db, self.config)
        statuses = [m.VibeCodingStatus.PENDING.value]
        if bool(getattr(vib, "auto_publish", False)):
            statuses.append(m.VibeCodingStatus.DRAFT.value)

        results: List[dict] = []
        for status in statuses:
            if len(results) >= limit:
                break
            posts = await self.db.get_vibecoding_posts(status=status, limit=limit)
            for post in posts:
                if len(results) >= limit:
                    break
                gate = await self._vibe_gate(post)
                if gate["blocked"]:
                    logger.bind(validator="vibecoding", post_id=post.id) \
                        .warning("Post blocked by VibeCoding guidelines: {}", gate["summary"])
                    continue
                out = await svc.publish_vibecoding_post(post.id)
                results.append({"post_id": post.id, "status": out["status"]})
        return results

    # -- optional APScheduler wiring (lazy) -------------------------------
    def build_async_scheduler(self):
        """Return a configured AsyncIOScheduler, or None if apscheduler is absent.

        Imported lazily so an import failure never breaks module load. The
        daemon should be started by the app entrypoint (P12), not the tests.
        """
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger
        except Exception:
            return None
        scheduler = AsyncIOScheduler(timezone=self.tz)
        publish_time = self._publish_time()
        scheduler.add_job(
            self.tick,
            CronTrigger(
                hour=publish_time.hour,
                minute=publish_time.minute,
                timezone=self.tz,
            ),
            id="publish-tick",
            replace_existing=True,
        )

        # VibeCoding: publish queued posts at its own schedule time/day(s).
        vib = getattr(self.config, "vibecoding", None)
        if vib is not None and bool(getattr(vib, "enabled", False)):
            vib_h, vib_m = 12, 0
            try:
                raw = str(getattr(vib, "schedule_time", "12:00") or "12:00").strip()
                vh, vm = raw.split(":")
                vib_h, vib_m = int(vh), int(vm)
            except (ValueError, TypeError):
                pass
            days = getattr(vib, "schedule_days", None) or [0, 1, 2, 3, 4, 5, 6]
            try:
                doy = ",".join(str(int(d)) for d in days)
            except (TypeError, ValueError):
                doy = "0,1,2,3,4,5,6"
            scheduler.add_job(
                self.publish_vibecoding_due,
                CronTrigger(hour=vib_h, minute=vib_m, day_of_week=doy, timezone=self.tz),
                id="vibecoding-publish",
                replace_existing=True,
            )
        return scheduler
