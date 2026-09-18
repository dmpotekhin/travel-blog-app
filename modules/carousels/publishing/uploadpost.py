"""Upload-Post publisher (https://api.upload-post.com).

Wire contract, as documented for the upload endpoints:

* ``POST /api/upload_photos`` — multipart, ``Authorization: Apikey <token>``,
  repeated ``photos[]`` files, ``user``, ``title``, repeated ``platform[]``.
* ``GET /api/uploadposts/status?request_id=...`` — per-platform result of an
  upload that the API accepted asynchronously.

The API is the only thing that decides what a platform reported: this module
never invents a post URL, a metric, or a success. If the response cannot be
read, the result is FAILED/PROCESSING with the raw body kept for the auditor.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from core.exceptions import CarouselError
from core.models import PublicationStatus

from .base import BaseCarouselPublisher, PublishRequest, PublishResult, PublishStatusReport

UPLOAD_PATH = "/api/upload_photos"
STATUS_PATH = "/api/uploadposts/status"

SUCCESS_WORDS = frozenset({"published", "success", "ok", "completed", "done"})


class UploadPostPublisher(BaseCarouselPublisher):
    """Publishes a carousel to TikTok and Instagram through Upload-Post."""

    name = "upload-post"

    def __init__(
        self,
        *,
        token: str,
        user: str,
        base_url: str = "https://api.upload-post.com",
        timeout_seconds: int = 120,
        auto_add_music: bool = True,
        privacy_level: str = "PUBLIC_TO_EVERYONE",
        async_upload: bool = True,
        send_platform_options: bool = True,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        upload_path: str = UPLOAD_PATH,
        status_path: str = STATUS_PATH,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.token = (token or "").strip()
        self.user = (user or "").strip()
        self.auto_add_music = auto_add_music
        self.privacy_level = privacy_level
        self.async_upload = async_upload
        self.send_platform_options = send_platform_options
        self.max_retries = max(0, int(max_retries))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.upload_path = upload_path
        self.status_path = status_path
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(float(timeout_seconds))
        )
        self._owns_client = client is None

    # -- public API ----------------------------------------------------

    async def publish(self, request: PublishRequest) -> List[PublishResult]:
        self._require_credentials()
        platforms = [str(item).strip().lower() for item in request.platforms if str(item).strip()]
        if not platforms:
            raise CarouselError("У публикации нет платформ: нечего отправлять.")

        missing = request.missing_slides()
        if missing:
            return [
                self._failed(platform, f"slide file(s) missing: {', '.join(missing)}")
                for platform in platforms
            ]

        response, transport_error = await self._post(
            self.upload_path, data=self._form_data(request, platforms), files=self._file_parts(request)
        )
        if response is None:
            return [self._failed(platform, transport_error) for platform in platforms]

        payload = self._payload(response)
        raw = json.dumps(payload, ensure_ascii=False)[:4000]
        if response.status_code >= 400:
            message = self._error_message(response, payload)
            return [
                self._failed(platform, message, raw) for platform in platforms
            ]
        return self._results_from_payload(platforms, payload, raw)

    async def fetch_status(self, request_id: str) -> PublishStatusReport:
        self._require_credentials()
        response = await self._client.get(
            self.status_path, params={"request_id": request_id}, headers=self._headers()
        )
        payload = self._payload(response)
        success = payload.get("success")
        if success is None:
            success = response.is_success
        return PublishStatusReport(
            request_id=request_id,
            success=bool(success),
            platforms=self._platform_map(payload),
            raw=payload,
        )

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # pragma: no cover - closing must never raise
            pass

    # -- request building ----------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Apikey {self.token}"}

    def _form_data(self, request: PublishRequest, platforms: Sequence[str]) -> Dict[str, Any]:
        title = request.title.strip() or request.full_caption
        data: Dict[str, Any] = {
            "user": self.user,
            "title": title[:220],
            "platform[]": list(platforms),
        }
        if self.send_platform_options:
            data["async_upload"] = "true" if self.async_upload else "false"
            data["auto_add_music"] = "true" if self.auto_add_music else "false"
            data["privacy_level"] = self.privacy_level
        return data

    def _file_parts(self, request: PublishRequest) -> List[Tuple[str, Tuple[str, bytes, str]]]:
        parts: List[Tuple[str, Tuple[str, bytes, str]]] = []
        for index, path in enumerate(request.slide_paths, start=1):
            slide_path = Path(path)
            name = slide_path.name or f"slide_{index:02d}.jpg"
            parts.append(("photos[]", (name, slide_path.read_bytes(), "image/jpeg")))
        return parts

    # -- transport -----------------------------------------------------

    async def _post(self, path: str, *, data, files) -> Tuple[Optional[httpx.Response], str]:
        """POST with retries on transport errors and 5xx only."""
        attempt = 0
        last_error = ""
        while attempt <= self.max_retries:
            try:
                response = await self._client.post(path, data=data, files=files, headers=self._headers())
            except httpx.HTTPError as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
                attempt += 1
                await self._sleep(attempt)
                continue
            if response.status_code >= 500 and attempt < self.max_retries:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                attempt += 1
                await self._sleep(attempt)
                continue
            return response, ""
        return None, last_error

    async def _sleep(self, attempt: int) -> None:
        if self.backoff_seconds:
            await asyncio.sleep(self.backoff_seconds * attempt)

    # -- response parsing ----------------------------------------------

    @staticmethod
    def _payload(response: httpx.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            return {"raw_text": response.text[:2000]}
        if isinstance(payload, dict):
            return payload
        return {"data": payload}

    @staticmethod
    def _error_message(response: httpx.Response, payload: Dict[str, Any]) -> str:
        for key in ("message", "error", "detail", "errors"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:400]
            if isinstance(value, (list, dict)) and value:
                return json.dumps(value, ensure_ascii=False)[:400]
        text = (response.text or "").strip()
        return f"HTTP {response.status_code}: {text[:300] or 'no body'}"

    def _results_from_payload(
        self, platforms: Sequence[str], payload: Dict[str, Any], raw: str
    ) -> List[PublishResult]:
        request_id = str(payload.get("request_id") or payload.get("id") or "").strip()
        reported = self._platform_map(payload)
        results: List[PublishResult] = []
        for platform in platforms:
            info = reported.get(platform) or {}
            success = self._reported_success(info)
            if success is True:
                results.append(
                    PublishResult(
                        platform=platform,
                        status=PublicationStatus.PUBLISHED,
                        request_id=request_id,
                        external_id=str(info.get("id") or info.get("post_id") or ""),
                        post_url=str(info.get("post_url") or info.get("url") or ""),
                        raw_response_json=raw,
                    )
                )
            elif success is False:
                message = str(info.get("error") or info.get("message") or "platform reported failure")
                results.append(self._failed(platform, message, raw, request_id=request_id))
            elif request_id:
                results.append(
                    PublishResult(
                        platform=platform,
                        status=PublicationStatus.PROCESSING,
                        request_id=request_id,
                        note="upload accepted; the platform result arrives later",
                        raw_response_json=raw,
                    )
                )
            else:
                results.append(
                    PublishResult(
                        platform=platform,
                        status=PublicationStatus.PROCESSING,
                        note="API accepted the upload but returned no request_id — status cannot be tracked",
                        raw_response_json=raw,
                    )
                )
        return results

    @staticmethod
    def _platform_map(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        raw = payload.get("results") or payload.get("platforms") or payload.get("data") or []
        mapping: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw, dict):
            for name, info in raw.items():
                if isinstance(info, dict):
                    mapping[str(name).lower()] = info
        elif isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = item.get("platform") or item.get("platform_name") or item.get("name")
                if name:
                    mapping[str(name).lower()] = item
        return mapping

    @staticmethod
    def _reported_success(info: Dict[str, Any]) -> Optional[bool]:
        if not info:
            return None
        success = info.get("success")
        if isinstance(success, bool):
            return success
        status_value = str(info.get("status") or "").strip().lower()
        if status_value:
            return status_value in SUCCESS_WORDS
        return None

    def _failed(
        self, platform: str, message: str, raw: str = "{}", *, request_id: str = ""
    ) -> PublishResult:
        return PublishResult(
            platform=platform,
            status=PublicationStatus.FAILED,
            request_id=request_id,
            error_message=message[:400],
            raw_response_json=raw,
        )

    def _require_credentials(self) -> None:
        if not self.token or not self.user:
            raise CarouselError(
                "Нет credentials Upload-Post: задайте UPLOADPOST_TOKEN и UPLOADPOST_USER в .env."
            )
