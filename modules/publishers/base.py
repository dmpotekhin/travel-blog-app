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
        key = self.name
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
