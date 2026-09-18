"""AI provider factory.

Picks the implementation by ``config.ai.provider``, but forces the mock provider
in ``app.dry_run`` mode unless a mock is explicitly requested — so the platform
runs out-of-the-box without API keys during development.
"""

from __future__ import annotations

from typing import Optional

from core.config import Config
from core.database import Database
from core.exceptions import ConfigurationError
from .base import BaseAIProvider
from .mock import MockProvider


def build_provider(
    db: Database, config: Config, name: Optional[str] = None
) -> BaseAIProvider:
    name = (name or config.ai.provider or "").lower()
    if name == "mock" or (config.app.dry_run and name not in ("mock",)):
        return MockProvider(db, config)
    if name == "local_vlm":
        # Extension point (ADR-106): a local vision-language model plugs in by
        # implementing the same BaseAIProvider contract, nothing else changes.
        try:
            from .local_vlm import LocalVLMProvider
        except ImportError as exc:
            raise ConfigurationError(
                "AI provider 'local_vlm' is not wired yet: add modules/ai/local_vlm.py "
                "implementing BaseAIProvider (see LOCAL_VLM_BASE_URL / LOCAL_VLM_MODEL "
                "in .env.example)"
            ) from exc
        return LocalVLMProvider(db, config)
    if name == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider(db, config)
    if name == "deepseek":
        from .deepseek import DeepSeekProvider
        return DeepSeekProvider(db, config)
    raise ConfigurationError(f"Unknown AI provider: {name!r}")
