"""Telegram publisher — Bot API over httpx. Mode: auto."""

from __future__ import annotations

from typing import List, Optional
from pathlib import Path

import httpx

from core.models import Draft
from modules.publishers.base import BasePublisher, PublishResult


class TelegramPublisher(BasePublisher):
    name = "telegram"
    mode = "auto"
    api_base = "https://api.telegram.org/bot{token}"

    @property
    def key_configured(self) -> bool:
        return bool(self.secrets.telegram_bot_token and self.config.telegram.chat_id)

    def _client(self):
        token = self.secrets.telegram_bot_token
        return self.api_base.format(token=token)

    async def publish(self, draft, media_paths=None):
        if not self.key_configured:
            return PublishResult(success=False, error="telegram not configured (token/chat_id missing)", status_hint="failed")
        chat_id = self.config.telegram.chat_id
        media_paths = media_paths or []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if media_paths:
                    url = self._client() + "/sendPhoto"
                    with open(media_paths[0], "rb") as fh:
                        files = {"photo": (Path(media_paths[0]).name, fh)}
                        r = await client.post(url, data={"chat_id": chat_id, "caption": draft.content[:1024]}, files=files)
                else:
                    url = self._client() + "/sendMessage"
                    r = await client.post(url, json={"chat_id": chat_id, "text": draft.content})
            if r.status_code == 429:
                return PublishResult(success=False, error="rate limited (429)", status_hint="retry")
            if r.status_code >= 400:
                return PublishResult(success=False, error=r.text[:300], status_hint="failed")
            msg = r.json().get("result", {})
            return PublishResult(success=True, external_id=str(msg.get("message_id", "")), url="", status_hint="published")
        except httpx.HTTPError as exc:
            return PublishResult(success=False, error=f"network error: {exc}", status_hint="failed")
