"""VK publisher — VK API ``wall.post`` over httpx. Mode: auto (text).

Photo upload via the VK Photos API is a multi-step flow (get upload server →
upload → save). Until that is wired, a draft with media returns an honest
``manual`` result instead of a fake success.
"""

from __future__ import annotations

from typing import List, Optional

import httpx

from core.models import Draft
from modules.publishers.base import BasePublisher, PublishResult


class VKPublisher(BasePublisher):
    name = "vk"
    mode = "auto"

    @property
    def key_configured(self) -> bool:
        return bool(self.secrets.vk_access_token and self.config.vk.group_id)

    async def _post(self, params):
        url = "https://api.vk.com/method/wall.post"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, data={"access_token": self.secrets.vk_access_token, **params})
        return r

    async def publish(self, draft, media_paths=None):
        if not self.key_configured:
            return PublishResult(success=False, error="vk not configured (token/group_id missing)", status_hint="failed")
        if media_paths:
            return PublishResult(success=False, error="vk photo upload not automated yet; manual", status_hint="manual")
        owner = f"-{self.config.vk.group_id}"
        params = {
            "owner_id": owner,
            "message": draft.content,
            "from_group": 1,
            "v": self.config.vk.api_version or "5.199",
        }
        try:
            r = await self._post(params)
        except httpx.HTTPError as exc:
            return PublishResult(success=False, error=f"network error: {exc}", status_hint="failed")
        try:
            data = r.json()
        except ValueError:
            return PublishResult(success=False, error=r.text[:300], status_hint="failed")
        if data.get("error"):
            return PublishResult(success=False, error=str(data["error"]), status_hint="failed")
        post_id = data.get("response", {}).get("post_id", "")
        return PublishResult(success=True, external_id=str(post_id), url="", status_hint="published")
