"""Google Gemini provider (image + text) via the REST GenerateContent API.

Implemented directly over httpx (already a dependency, pure-Python) instead of
the ``google-genai`` SDK — avoids native build / install issues on this machine
while keeping the same endpoint. See ARCHITECTURE.md decision log (§77, D4.1).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
from typing import Optional

import httpx

from core.exceptions import AIProviderError, ConfigurationError, RateLimitError
from .base import BaseAIProvider, ImageAnalysis

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

ANALYSIS_PROMPT = (
    "Analyze this travel photo. Return ONLY a JSON object with these keys exactly: "
    '"subjects" (array of strings, main people/objects), '
    '"scene" (one sentence), '
    '"objects" (array of strings, landmarks/details), '
    '"mood" (short emotional tone), '
    '"text" (any visible signs/captions/watermarks, else empty string), '
    '"quality_ok" (boolean), '
    '"quality_reason" (string). No markdown, no prose outside the JSON.'
)


class GeminiProvider(BaseAIProvider):
    name = "gemini"

    def _model_name(self) -> str:
        return self.config.gemini.model

    def _verify_credentials(self) -> None:
        if not self.secrets.gemini_api_key:
            raise ConfigurationError("GEMINI_API_KEY is not set (add it to .env)")

    # -- abstract raw calls ----------------------------------------------

    async def _analyze_image_raw(self, image_path: str, prompt: str) -> ImageAnalysis:
        data_url, mime = await self._to_thread(_read_image_b64, image_path)
        body = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": mime, "data": data_url}},
                    {"text": prompt or ANALYSIS_PROMPT},
                ],
            }],
            "generationConfig": {
                "temperature": self.config.gemini.temperature,
                "maxOutputTokens": 1024,
            },
            "responseMimeType": "application/json",
        }
        text = await self._generate_content(body)
        analysis = self._parse_analysis(text)
        analysis.raw = text
        await self.limiter.record_success()
        return analysis

    async def _generate_text_raw(
        self, system: str, user: str, *, max_tokens: Optional[int] = None
    ) -> str:
        body = {
            "contents": [{
                "parts": [{"text": (system + "\n\n" + user) if system else user}],
            }],
            "generationConfig": {
                "temperature": self.config.gemini.temperature,
                "maxOutputTokens": max_tokens or 2048,
            },
        }
        text = await self._generate_content(body)
        await self.limiter.record_success()
        return text

    # -- internals --------------------------------------------------------

    async def _generate_content(self, body: dict) -> str:
        self._verify_credentials()
        model = self.config.gemini.model
        url = f"{_GEMINI_BASE}/{model}:generateContent"
        headers = {"x-goog-api-key": self.secrets.gemini_api_key}
        max_retries = max(1, self.config.gemini.max_retries)
        delay = self.config.gemini.retry_429_seconds

        async with httpx.AsyncClient(timeout=120.0) as client:
            for attempt in range(max_retries + 1):
                try:
                    resp = await client.post(url, headers=headers, json=body)
                except httpx.HTTPError as exc:
                    await self.limiter.record_server_error()
                    raise AIProviderError("Gemini request failed", cause=exc, code="network_error")

                if resp.status_code == 200:
                    return self._extract_text(resp.json())

                if resp.status_code == 429:
                    await self.limiter.record_rate_limit()
                    if attempt == max_retries:
                        raise RateLimitError("Gemini rate limit exceeded", cause=_http_cause(resp))
                    retry_after = _retry_after_seconds(resp, default=delay)
                    await asyncio.sleep(retry_after * (2 ** attempt))
                    continue

                if resp.status_code in (500, 502, 503, 504):
                    await self.limiter.record_server_error()
                    if attempt == max_retries:
                        raise AIProviderError(f"Gemini server error {resp.status_code}", cause=_http_cause(resp), code="server_error")
                    await asyncio.sleep(2 ** attempt)
                    continue

                # 400/401/403 → configuration/permission problem, do not retry
                if resp.status_code in (400, 401, 403):
                    raise ConfigurationError(f"Gemini rejected the request ({resp.status_code}): {resp.text[:300]}")
                raise AIProviderError(f"Gemini returned {resp.status_code}", cause=_http_cause(resp), code="bad_status")

        raise AIProviderError("Gemini request failed after retries", code="exhausted")

    def _parse_analysis(self, text: str) -> ImageAnalysis:
        payload = _extract_json(text)
        if payload and isinstance(payload, dict):
            try:
                return ImageAnalysis.model_validate(payload)
            except Exception:
                pass
        # fallback: keep the raw provider text for diagnostics
        return ImageAnalysis(scene=text[:500].strip(), quality_ok=True, raw=text)

    @staticmethod
    def _extract_text(payload: dict) -> str:
        candidates = payload.get("candidates") or []
        if not candidates:
            raise AIProviderError("Gemini returned no candidates", code="empty_response")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text:
            raise AIProviderError("Gemini returned empty text", code="empty_response")
        return text


def _read_image_b64(path: str) -> tuple[str, str]:
    """Read an image, re-encode to JPEG when possible (handles HEIC/RAW-ish)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except Exception:
        mime, _ = mimetypes.guess_type(path)
        with open(path, "rb") as fh:
            raw = fh.read()
        return base64.b64encode(raw).decode("ascii"), (mime or "image/jpeg")


def _extract_json(text: str) -> Optional[dict]:
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except Exception:
            return None
    # tolerate a fenced ```json ... ``` block
    if "```" in text:
        try:
            block = text.split("```")[1]
            if block.startswith("json"):
                block = block[4:]
            return json.loads(block.strip())
        except Exception:
            return None
    return None


def _retry_after_seconds(resp: httpx.Response, default: int) -> int:
    value = resp.headers.get("Retry-After")
    if value:
        try:
            return max(1, int(float(value)))
        except ValueError:
            pass
    return default


def _http_cause(resp: httpx.Response) -> Exception:
    return RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
