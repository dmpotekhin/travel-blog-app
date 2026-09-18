"""Offline publisher: records the intent, uploads nothing, invents nothing.

Used by ``dry_run`` (the default) and by tests. Every result is
``PublicationStatus.MANUAL`` with the payload we *would* have sent written into
the audit column — a dry run must never look like a real post.
"""

from __future__ import annotations

import json
from typing import List

from core.models import PublicationStatus

from .base import BaseCarouselPublisher, PublishRequest, PublishResult, PublishStatusReport


class MockCarouselPublisher(BaseCarouselPublisher):
    """Deterministic, network-free publisher for dry runs and tests."""

    name = "mock"

    async def publish(self, request: PublishRequest) -> List[PublishResult]:
        payload = {
            "dry_run": True,
            "job_id": request.job_id,
            "title": request.title,
            "caption": request.full_caption,
            "platforms": list(request.platforms),
            "photos": [str(path) for path in request.slide_paths],
            "note": "dry run: nothing was uploaded to TikTok or Instagram",
        }
        raw = json.dumps(payload, ensure_ascii=False)
        return [
            PublishResult(
                platform=platform,
                status=PublicationStatus.MANUAL,
                request_id="",
                external_id="",
                post_url="",
                error_message="",
                note="dry run — publication prepared, not uploaded",
                raw_response_json=raw,
            )
            for platform in request.platforms
        ]

    async def fetch_status(self, request_id: str) -> PublishStatusReport:
        return PublishStatusReport(
            request_id=request_id,
            success=False,
            platforms={},
            raw={"dry_run": True, "request_id": request_id},
        )
