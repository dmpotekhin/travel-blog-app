"""Publisher abstraction (section 50-56).

Every platform adapter produces a :class:`PublishResult`. The three modes
(mirroring ARCHITECTURE §7) are honest:

* ``auto``   — the platform API is called and the result recorded verbatim;
* ``manual`` — the API cannot be automated (Zen/Trip/Instagram/YouTube), so we
  prepare content and flag it ``manual`` for a human instead of faking success;
* ``partial`` — Facebook: text+photo posted, but scheduling is app-managed.

The ``manual`` flag in the result is never converted to ``published`` by the
caller — see :class:`PublishService`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from core.config import Config, get_secrets
from core.database import Database
from core.models import Draft, Publication


@dataclass
class PublishResult:
    success: bool
    manual: bool = False
    external_id: str = ""
    url: str = ""
    error: str = ""
    status_hint: str = ""
    #: False for "don't retry" errors (e.g. bot config missing, invalid scope): a
    #: future retry will not fix them, so ``retry_failed`` must not burn attempts.
    retryable: bool = True
    #: True when the platform could only honour part of the media/caption — the
    #: post still went out, but degraded (F7). Surfaced by the API + structured log.
    degraded: bool = False


#: Marked as a permanent, non-retryable failure on a Publication.
PERMANENT_PREFIX = "[permanent] "


def plan_media(media_paths, content: str, *, photo_cap: int | None, caption_limit: int | None):
    """Decide which photos a publisher should send and whether that degrades (ADR-104).

    Returns ``(selected_paths, degraded, note)``:
      * ``selected_paths`` — the photos to actually send (honouring ``photo_cap``).
      * ``degraded`` — True when the caption is truncated or not all photos fit
        ``photo_cap``, i.e. the platform only honoured part of the media (F7).
      * ``note`` — a human-readable explanation of what degraded; empty if clean.
    """
    media = list(media_paths or [])
    degraded = False
    reasons = []
    if photo_cap is not None and len(media) > photo_cap:
        selected = media[:photo_cap]
        degraded = True
        reasons.append(f"{len(media)} photos -> {photo_cap}")
    else:
        selected = media
    if caption_limit is not None and len(content) > caption_limit:
        degraded = True
        reasons.append(f"caption {len(content)} -> {caption_limit}")
    return selected, degraded, "; ".join(reasons)


class BasePublisher(ABC):
    #: "auto" | "partial" | "manual"  (see module docstring)
    mode: str = "auto"
    name: str = "base"

    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self.secrets = get_secrets()

    @property
    def enabled(self) -> bool:
        """Whether this platform is enabled in config."""
        flags = getattr(self.config, "publishing", None)
        if flags is None:
            return True
        key = getattr(self, "platform", None) or self.name
        return bool(getattr(flags, key, True))

    @property
    def key_configured(self) -> bool:
        """Whether required credentials are present (implied by subclass)."""
        return True

    @abstractmethod
    async def publish(self, draft: Draft, media_paths: Optional[List[str]] = None) -> PublishResult:
        """Publish an approved draft. Never raises — always returns a result."""
        raise NotImplementedError

    def _manual_result(self, draft: Draft) -> PublishResult:
        return PublishResult(success=True, manual=True, status_hint="manual")
