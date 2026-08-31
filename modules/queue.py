"""CITY QUEUE (section 20-24).

The city queue decides WHICH city gets processed next. Cities are created by
the Scanner in ``queued`` status; the queue:

* selects ready cities (status=queued AND ≥1 scanned photo), ordered by
  priority (high first), then oldest trip year first;
* claims a city (queued -> processing) before content generation;
* requeues / errors a city through the centralized state machine;
* reports per-status counts for the dashboard.

Every transition goes through ``models.city_transition`` so no illegal city
transition can bypass the state machine.
"""

from __future__ import annotations

from typing import List, Optional

from loguru import logger

from core.config import Config
from core.database import Database
from core.exceptions import StateTransitionError, TravelBlogError
from core.models import City, CityStatus, city_transition
from core.config import get_settings

_CITY_STATUSES = [
    CityStatus.QUEUED, CityStatus.PROCESSING, CityStatus.DRAFTED,
    CityStatus.APPROVED, CityStatus.PUBLISHING, CityStatus.PUBLISHED,
    CityStatus.ERROR,
]


class CityQueueError(TravelBlogError):
    """Raised on an invalid queue operation."""


class CityQueue:
    def __init__(self, db: Database, settings: Optional[Config] = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.sync_after_scan = bool(getattr(self.settings.queue, "sync_after_scan", True))
        self.direct_schedule = bool(getattr(self.settings.queue, "direct_schedule", True))

    async def next(
        self, batch_size: int = 1, offset: int = 0, require_photos: bool = True
    ) -> List[City]:
        """Return up to ``batch_size`` ready cities (queued, with scanned photos)."""
        if batch_size < 1:
            raise CityQueueError(f"batch_size must be >= 1, got {batch_size}")
        return await self.db.get_queued_cities(batch_size, offset, require_photos)

    async def claim(self, city_id: int) -> City:
        """Reserve a queued city for processing (queued -> processing)."""
        city = await self.db.get_city(city_id)
        if city is None:
            raise CityQueueError(f"city {city_id} not found")
        await self._transition(city, CityStatus.PROCESSING)
        logger.info("queued city claimed for processing: {} #{}", city.name, city.id)
        return await self.db.get_city(city_id)

    async def mark_drafted(self, city_id: int) -> City:
        """processing -> drafted (drafts for all platforms were queued)."""
        return await self._by_id_transition(city_id, CityStatus.DRAFTED)

    async def mark_error(self, city_id: int, reason: str = "") -> City:
        """Push a city into the error state (recovery via re-queue)."""
        city = await self.db.get_city(city_id)
        if city is None:
            raise CityQueueError(f"city {city_id} not found")
        await self._transition(city, CityStatus.ERROR)
        logger.warning("city marked error: {} #{} ({})", city.name, city.id, reason)
        return await self.db.get_city(city_id)

    async def requeue(self, city_id: int) -> City:
        """Recover a city: error -> queued (or drafted/approved -> queued)."""
        return await self._by_id_transition(city_id, CityStatus.QUEUED)

    async def re_enqueue_errors(self) -> int:
        """Put every errored city back in the queue. Returns how many."""
        errored = await self.db.get_cities_by_status(CityStatus.ERROR.value)
        n = 0
        for city in errored:
            try:
                await self._transition(city, CityStatus.QUEUED)
                n += 1
            except StateTransitionError:
                # already handled elsewhere
                continue
        if n:
            logger.info("re-enqueued {} errored cities", n)
        return n

    async def set_priority(self, city_id: int, priority: int) -> City:
        return await self.db.update_city_priority(city_id, priority)

    async def counts(self) -> dict:
        return {
            s.value: await self.db.count_cities_by_status(s.value)
            for s in _CITY_STATUSES
        }

    # -- internals --------------------------------------------------------

    async def _by_id_transition(self, city_id: int, target: CityStatus) -> City:
        city = await self.db.get_city(city_id)
        if city is None:
            raise CityQueueError(f"city {city_id} not found")
        await self._transition(city, target)
        return await self.db.get_city(city_id)

    async def _transition(self, city: City, target: CityStatus) -> None:
        # centralized guard — illegal transitions raise StateTransitionError
        city_transition(city.status.value, target.value)
        await self.db.update_city_status(city.id, target.value)
