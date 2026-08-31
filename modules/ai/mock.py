"""Mock AI provider — deterministic, no network, no API key.

Used for development, ``app.dry_run`` mode and tests. Produces a stable
:class:`ImageAnalysis` and a stable story string, both derived from the input so
the pipeline can be exercised end-to-end without spending real quota or needing
credentials.
"""

from __future__ import annotations

import os
from typing import Optional

from .base import BaseAIProvider, ImageAnalysis


class MockProvider(BaseAIProvider):
    name = "mock"

    def _model_name(self) -> str:
        return "mock-model"

    async def analyze_image(self, image_path: str, prompt: str, image_hash: Optional[str] = None) -> ImageAnalysis:
        # Mock path: skip the cache/limit overhead, return deterministic result.
        base = os.path.basename(image_path)
        low = base.lower()
        if image_hash:
            # stable pseudo-subject from the hash (so identical photos → identical analysis)
            seed = sum(image_hash.encode("utf-8"))
        else:
            seed = sum(low.encode("utf-8"))
        subjects = [f"subject{seed % 7}", "people" if seed % 3 == 0 else "landscape"]
        quality_ok = not any(tok in low for tok in ("bad", "corrupt", "lowest", "blur"))
        return ImageAnalysis(
            subjects=subjects,
            scene=f"mocked scene for {base}",
            objects=[f"object{seed % 5}"],
            mood="calm" if seed % 2 else "vivid",
            text="",
            quality_ok=quality_ok,
            quality_reason="" if quality_ok else "mock flagged low quality",
            raw=f"mock:{base}",
        )

    async def _analyze_image_raw(self, image_path: str, prompt: str) -> ImageAnalysis:
        # not used (analyze_image is overridden) but required by the ABC
        return await self.analyze_image(image_path, prompt)

    async def _generate_text_raw(
        self, system: str, user: str, *, max_tokens: Optional[int] = None
    ) -> str:
        # Deterministic story: pull the city/country/year hints out of the prompt.
        city = self._extract(("city: ", "Город: "), user) or "неизвестный город"
        country = self._extract(("country: ", "Страна: "), user) or "неизвестная страна"
        year = self._extract(("year: ", "Год"), user) or "разных лет"
        return (
            f"Мок-история о городе {city} ({country}, {year}). "
            f"Моковый текст генерируется без обращения к модели: он позволяет "
            f"проверить весь конвейер — от отбора фото до черновиков — без "
            f"реального API и без затрат квоты."
        )

    @staticmethod
    def _extract(labels, text: str) -> Optional[str]:
        if isinstance(labels, str):
            labels = (labels,)
        for label in labels:
            if label in text:
                tail = text.split(label, 1)[1]
                stop = tail.find("\n")
                if stop != -1:
                    tail = tail[:stop]
                return tail.strip() or None
        return None
