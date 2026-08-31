"""DeepSeek provider — text generation (content phase).

DeepSeek is text-only (the analysis of photos is done by Gemini), so this
provider implements only ``_generate_text_raw`` via the OpenAI-compatible
``/chat/completions`` endpoint. Uses httpx directly (pure-Python).
"""

from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from core.exceptions import AIProviderError, ConfigurationError, RateLimitError
from .base import BaseAIProvider


class DeepSeekProvider(BaseAIProvider):
    name = "deepseek"

    def _model_name(self) -> str:
        return self.config.deepseek.model

    def _verify_credentials(self) -> None:
        if not self.secrets.deepseek_api_key:
            raise ConfigurationError("DEEPSEEK_API_KEY is not set (add it to .env)")

    async def _analyze_image_raw(self, image_path: str, prompt: str):
        # DeepSeek cannot analyze images — raise clearly, never fake a result.
        raise AIProviderError(
            "DeepSeek is text-only; image analysis must use a vision-capable provider (gemini)",
            code="no_vision",
        )

    async def _generate_text_raw(
        self, system: str, user: str, *, max_tokens: Optional[int] = None
    ) -> str:
        self._verify_credentials()
        base = self.config.deepseek.base_url.rstrip("/")
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.secrets.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.config.deepseek.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.deepseek.temperature,
            "max_tokens": max_tokens or 2048,
        }
        max_retries = max(1, self.config.gemini.max_retries)

        async with httpx.AsyncClient(timeout=120.0) as client:
            for attempt in range(max_retries + 1):
                try:
                    resp = await client.post(url, headers=headers, json=body)
                except httpx.HTTPError as exc:
                    await self.limiter.record_server_error()
                    raise AIProviderError("DeepSeek request failed", cause=exc, code="network_error")

                if resp.status_code == 200:
                    data = resp.json()
                    text = (data["choices"][0]["message"]["content"] or "").strip()
                    if not text:
                        raise AIProviderError("DeepSeek returned empty content", code="empty_response")
                    await self.limiter.record_success()
                    return text

                if resp.status_code == 429:
                    await self.limiter.record_rate_limit()
                    if attempt == max_retries:
                        raise RateLimitError("DeepSeek rate limit exceeded")
                    await asyncio.sleep(60 * (2 ** attempt))
                    continue

                if resp.status_code in (500, 502, 503, 504):
                    await self.limiter.record_server_error()
                    if attempt == max_retries:
                        raise AIProviderError(f"DeepSeek server error {resp.status_code}", code="server_error")
                    await asyncio.sleep(2 ** attempt)
                    continue

                if resp.status_code in (400, 401, 403):
                    raise ConfigurationError(f"DeepSeek rejected the request ({resp.status_code}): {resp.text[:300]}")
                raise AIProviderError(f"DeepSeek returned {resp.status_code}", code="bad_status")

        raise AIProviderError("DeepSeek request failed after retries", code="exhausted")
