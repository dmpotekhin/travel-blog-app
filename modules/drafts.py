"""Draft approval workflow (section 39-45).

Wraps draft state transitions so that:

* nothing can be published before human approval (or an explicit auto-approve
  rule), and
* every state change goes through ``models.draft_transition`` (no illegal jumps).

Auto-approve rules mirror the ARCHITECTURE. §7 "honest automation" decision:
fully-automated platforms (Telegram, VK) may be auto-approved when the profile
is in trusted mode; manual platforms are never auto-approved by default.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from core import models as m
from core.database import Database
from core.exceptions import StateTransitionError


class DraftManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def list_pending(self, platform: Optional[str] = None) -> List[m.Draft]:
        return await self.db.get_drafts(status=m.DraftStatus.PENDING, platform=platform)

    async def get(self, draft_id: int) -> Optional[m.Draft]:
        drafts = await self.db.get_drafts(draft_id=draft_id)
        return drafts[0] if drafts else None

    async def _transition(self, draft: Optional[m.Draft], target: str) -> m.Draft:
        if draft is None:
            raise ValueError(f"draft not found")
        m.draft_transition(draft.status.value, target)
        updated = await self.db.update_draft_status(draft.id, target)
        assert updated is not None, "update_draft_status returned None"
        return updated

    async def approve(self, draft_id: int) -> m.Draft:
        return await self._transition(await self.get(draft_id), m.DraftStatus.APPROVED)

    async def reject(self, draft_id: int) -> m.Draft:
        return await self._transition(await self.get(draft_id), m.DraftStatus.REJECTED)

    async def reset(self, draft_id: int) -> m.Draft:
        """Re-open an approved/rejected draft back to pending (re-edit)."""
        return await self._transition(await self.get(draft_id), m.DraftStatus.PENDING)

    async def auto_approve(
        self,
        platforms: Optional[List[str]] = None,
        *,
        only_platforms: Optional[List[str]] = None,
        factory: bool = True,
    ) -> int:
        """Auto-approve pending drafts for a set of platforms.

        ``factory`` keeps the default safe behaviour: only platforms in the
        "trusted auto" set (telegram, vk) are auto-approved; everything else
        stays pending for a human. Passing ``only_platforms`` overrides the set.
        """
        if only_platforms is None:
            only_platforms = ["telegram", "vk"] if factory else []
        pending = await self.list_pending()
        count = 0
        for d in pending:
            if d.platform in only_platforms:
                try:
                    await self.approve(d.id)
                    count += 1
                except StateTransitionError:
                    continue
        return count

    async def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for status in m.DraftStatus:
            rows = await self.db.get_drafts(status=status)
            out[status.value] = len(rows)
        return out
