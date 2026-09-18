"""Publishing abstraction for carousel bundles.

Every external channel (Upload-Post today, anything tomorrow) sits behind
``BaseCarouselPublisher``: the factory never builds HTTP requests itself, so a
dry run, a test, or a new provider is a swap of one object.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Sequence

from core.models import PublicationStatus

#: The two faces of a carousel post (spec §7: TikTok + Instagram, 9:16 JPG).
CAROUSEL_PLATFORMS: tuple = ("tiktok", "instagram")


@dataclass
class PublishRequest:
    """Everything a publisher needs — no database, no config objects."""

    job_id: int
    title: str = ""
    caption: str = ""
    platforms: Sequence[str] = CAROUSEL_PLATFORMS
    slide_paths: Sequence[str] = ()
    hashtags: Sequence[str] = ()
    privacy_level: str = "PUBLIC_TO_EVERYONE"
    auto_add_music: bool = True
    async_upload: bool = True
    send_platform_options: bool = True
    dry_run: bool = False

    def missing_slides(self) -> List[str]:
        """Slide files that are not on disk (a publish must never half-run)."""
        return [path for path in self.slide_paths if not Path(path).is_file()]

    @property
    def full_caption(self) -> str:
        """Caption plus hashtags, trimmed to the platform-friendly form."""
        tags = [tag if tag.startswith("#") else f"#{tag}" for tag in self.hashtags if tag.strip()]
        parts = [self.caption.strip()]
        if tags:
            parts.append(" ".join(tags))
        return "\n\n".join(part for part in parts if part)


@dataclass
class PublishResult:
    """Outcome of one platform of one carousel."""

    platform: str
    status: PublicationStatus
    request_id: str = ""
    external_id: str = ""
    post_url: str = ""
    error_message: str = ""
    note: str = ""
    raw_response_json: str = "{}"


@dataclass
class PublishStatusReport:
    """Normalised answer of a status poll (raw payload kept for the audit trail)."""

    request_id: str
    success: bool = False
    platforms: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


class BaseCarouselPublisher(ABC):
    """One publishing channel for a carousel bundle."""

    name = "base"

    @abstractmethod
    async def publish(self, request: PublishRequest) -> List[PublishResult]:
        """Publish one carousel; one result per requested platform."""

    @abstractmethod
    async def fetch_status(self, request_id: str) -> PublishStatusReport:
        """Ask the channel what happened to a previously accepted upload."""

    async def aclose(self) -> None:
        """Release network resources (no-op for offline publishers)."""
        return None
