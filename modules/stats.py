"""P10 Stats — read-only KPI/aggregation queries for the dashboard.

Pure async queries over the Database; no Streamlit dependency so the metrics
are unit-testable. ``summary()``, ``by_status()``, ``by_platform()`` and
``recent()`` return plain JSON-serialisable dicts / lists.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

from core.database import Database


def _group(rows, attr: str) -> Dict[str, int]:
    counter: Counter = Counter()
    for row in rows:
        counter[getattr(row, attr)] += 1
    return dict(counter)


class StatsService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def summary(self) -> Dict[str, int]:
        cities = await self.db.get_all_cities()
        drafts = await self.db.get_drafts()
        pubs = await self.db.get_all_publications()
        return {
            "cities_total": len(cities),
            "photos_total": await self.db.count_photos(),
            "drafts_total": len(drafts),
            "publications_total": len(pubs),
            "scheduled": sum(1 for p in pubs if p.status == "scheduled"),
            "published": sum(1 for p in pubs if p.status == "published"),
            "pending_approval": sum(1 for d in drafts if d.status == "pending"),
            "approved": sum(1 for d in drafts if d.status == "approved"),
            "errors": sum(1 for c in cities if c.status == "error")
            + sum(1 for d in drafts if d.status == "error")
            + sum(1 for p in pubs if p.status == "failed"),
        }

    async def by_status(self) -> Dict[str, Dict[str, int]]:
        cities = await self.db.get_all_cities()
        drafts = await self.db.get_drafts()
        pubs = await self.db.get_all_publications()
        return {
            "cities": _group(cities, "status"),
            "drafts": _group(drafts, "status"),
            "publications": _group(pubs, "status"),
        }

    async def by_platform(self) -> Dict[str, Dict[str, int]]:
        pubs = await self.db.get_all_publications()
        per_platform: Dict[str, Dict[str, int]] = {}
        for p in pubs:
            block = per_platform.setdefault(p.platform, {})
            block[p.status] = block.get(p.status, 0) + 1
        return per_platform

    async def recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        pubs = await self.db.get_all_publications()
        # newest first by created_at (fall back stable to id)
        pubs = sorted(pubs, key=lambda p: (p.created_at or "", p.id), reverse=True)
        return [
            {
                "id": p.id,
                "city_id": p.city_id,
                "platform": p.platform,
                "status": p.status,
                "external_id": p.external_id,
                "url": p.url,
                "published_at": p.published_at,
            }
            for p in pubs[:limit]
        ]
