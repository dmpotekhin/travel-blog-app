"""AI response cache (section 26).

Keyed by ``(image_hash, provider, model, prompt_hash)``. Idempotent so the same
photo is never sent to the model twice for the same prompt — this is both a cost
and an API-quota saver.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from core import models as m
from core.database import Database


class AICache:
    def __init__(self, db: Database, provider: str, model: str) -> None:
        self.db = db
        self.provider = provider
        self.model = model

    def _prompt_hash(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    async def get(self, image_hash: str, prompt: str):
        from .base import ImageAnalysis  # lazy to avoid circular import
        entry = await self.db.get_ai_cache(
            image_hash, self.provider, self.model, self._prompt_hash(prompt)
        )
        if entry is None or not entry.response:
            return None
        try:
            return ImageAnalysis.model_validate_json(entry.response)
        except Exception:
            # corrupt / incompatible cached payload — treat as a miss
            return None

    async def put(self, image_hash: str, prompt: str, analysis: ImageAnalysis) -> None:
        await self.db.save_ai_cache(
            m.AICacheEntry(
                image_hash=image_hash,
                provider=self.provider,
                model=self.model,
                prompt_hash=self._prompt_hash(prompt),
                response=analysis.model_dump_json(),
            )
        )
