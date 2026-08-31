"""Manual publisher: prepares content, never fakes a publish."""
from __future__ import annotations

from typing import List

from core.models import Draft
from modules.publishers.base import BasePublisher, PublishResult


class ManualPublisher(BasePublisher):
    mode = "manual"
    name = "manual"

    def __init__(self, db, config, platform: str) -> None:
        super().__init__(db, config)
        self.name = platform

    async def publish(self, draft: Draft, media_paths: List[str]) -> PublishResult:
        return PublishResult(success=True, manual=True, external_id="", url="",
                             error="")
