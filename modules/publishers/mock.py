"""Mock publisher — simulated success for dry_run / tests. No network."""

from __future__ import annotations

from typing import List, Optional

from core.models import Draft
from modules.publishers.base import BasePublisher, PublishResult


class MockPublisher(BasePublisher):
    name = "mock"
    mode = "auto"

    def __init__(self, db, config, platform: str = "") -> None:
        super().__init__(db, config)
        self.platform = platform or "mock"

    async def publish(self, draft, media_paths=None):
        return PublishResult(
            success=True,
            external_id=f"mock-{self.platform}-{draft.id}",
            status_hint="published",
        )
