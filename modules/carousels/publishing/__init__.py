"""Carousel publishing channel (Phase 5): one abstraction, three implementations."""

from __future__ import annotations

from typing import Optional

from core.config import CarouselConfig
from core.exceptions import CarouselError
from core.models import PublicationStatus

from .base import (
    CAROUSEL_PLATFORMS,
    BaseCarouselPublisher,
    PublishRequest,
    PublishResult,
    PublishStatusReport,
)
from .mock import MockCarouselPublisher
from .uploadpost import STATUS_PATH, UPLOAD_PATH, UploadPostPublisher

__all__ = [
    "CAROUSEL_PLATFORMS",
    "BaseCarouselPublisher",
    "MockCarouselPublisher",
    "PublishRequest",
    "PublishResult",
    "PublishStatusReport",
    "PublicationStatus",
    "STATUS_PATH",
    "UPLOAD_PATH",
    "UploadPostPublisher",
    "publisher_for",
]


def publisher_for(
    settings: CarouselConfig,
    secrets,
    *,
    dry_run: Optional[bool] = None,
) -> BaseCarouselPublisher:
    """Pick a publisher for the current configuration.

    ``dry_run`` (the default) always wins: it returns the offline publisher so
    nothing can leave the machine by accident. Real credentials are required
    only for a real publish, and a missing token is an error — never a silent
    downgrade to something that looks published.
    """
    upload = settings.upload_post
    is_dry = settings.dry_run if dry_run is None else dry_run
    if is_dry or not upload.enabled:
        return MockCarouselPublisher()

    token = getattr(secrets, "uploadpost_token", "") or ""
    user = getattr(secrets, "uploadpost_user", "") or ""
    if not token or not user:
        raise CarouselError(
            "Публикация включена, но credentials Upload-Post пусты: "
            "задайте UPLOADPOST_TOKEN и UPLOADPOST_USER в .env."
        )
    return UploadPostPublisher(
        token=token,
        user=user,
        base_url=upload.base_url,
        timeout_seconds=upload.timeout_seconds,
        auto_add_music=upload.auto_add_music,
        privacy_level=upload.privacy_level,
        async_upload=upload.async_upload,
        max_retries=upload.max_retries,
    )
