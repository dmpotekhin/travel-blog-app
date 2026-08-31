"""Request rate limiter for AI calls (section 26).

Mix of:

* a hard minimum interval between calls (prevents bursting a free tier),
* an in-memory per-minute sliding window,
* a daily cap persisted in ``gemini_stats`` (so it survives restarts and is
  visible on the dashboard).

Before each call it increments ``requests_count``; providers additionally bump
``rate_limit_errors`` / ``server_errors`` / ``successful_requests`` so the
dashboard reflects real provider health.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Deque, Optional

from core.config import Config, get_secrets
from core.database import Database
from core.exceptions import TravelBlogError


class RateLimitExceeded(TravelBlogError):
    """Raised when a quota is exhausted (caller should back off / skip)."""


class RateLimiter:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        g = config.gemini
        self.min_interval = max(0.0, float(g.min_interval_seconds))
        self.per_minute = max(1, int(g.requests_per_minute))
        self.per_day = max(1, int(g.requests_per_day))
        self._last_call: float = 0.0
        self._minute_window: Deque[float] = deque()

    async def _daily_used(self) -> int:
        stats = await self.db.get_gemini_stats()
        return stats.requests_count if stats else 0

    async def acquire(self) -> None:
        """Block until allowed to make a request. Raises RateLimitExceeded on quota."""
        # daily cap
        used = await self._daily_used()
        if used >= self.per_day:
            raise RateLimitExceeded(
                f"daily AI request cap reached ({used}/{self.per_day})"
            )

        # minimum interval
        now = time.monotonic()
        wait = self.min_interval - (now - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)

        # per-minute sliding window
        self._prune_window()
        if len(self._minute_window) >= self.per_minute:
            oldest = self._minute_window[0]
            await asyncio.sleep(max(0.1, (oldest + 60.0) - time.monotonic()))
            self._prune_window()
            if len(self._minute_window) >= self.per_minute:
                raise RateLimitExceeded(
                    f"per-minute AI request cap reached ({self.per_minute}/min)"
                )

        # record the request + update counters
        self._last_call = time.monotonic()
        self._minute_window.append(self._last_call)
        await self.db.increment_gemini_counter(None, "requests_count", 1)

    def _prune_window(self) -> None:
        cutoff = time.monotonic() - 60.0
        while self._minute_window and self._minute_window[0] < cutoff:
            self._minute_window.popleft()

    # -- accounting helpers used by providers ----------------------------

    async def record_success(self) -> None:
        await self.db.increment_gemini_counter(None, "successful_requests", 1)

    async def record_rate_limit(self) -> None:
        await self.db.increment_gemini_counter(None, "rate_limit_errors", 1)

    async def record_server_error(self) -> None:
        await self.db.increment_gemini_counter(None, "server_errors", 1)

    async def record_failure(self) -> None:
        await self.db.increment_gemini_counter(None, "failed_requests", 1)
