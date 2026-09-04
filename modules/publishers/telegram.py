"""Telegram publisher — Bot API over httpx. Mode: auto.

Sends up to 10 photos as an album (sendMediaGroup) and flags ``degraded`` when it
had to truncate the caption or drop photos beyond the platform limit (ADR-104).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import httpx

from core.models import Draft
from modules.publishers.base import BasePublisher, PublishResult, plan_media


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
            return PublishResult(
                success=False,
                retryable=False,
                error="telegram not configured (token/chat_id missing)",
                status_hint="failed",
            )
        chat_id = self.config.telegram.chat_id
        media = media_paths or []
        selected, degraded, _note = plan_media(
            media, draft.content, photo_cap=10, caption_limit=1024
        )
        caption = draft.content[:1024]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if selected:
                    if len(selected) == 1:
                        url = self._client() + "/sendPhoto"
                        with open(selected[0], "rb") as fh:
                            r = await client.post(
                                url,
                                data={"chat_id": chat_id, "caption": caption},
                                files={"photo": (Path(selected[0]).name, fh)},
                            )
                    else:
                        url = self._client() + "/sendMediaGroup"
                        handles = [open(p, "rb") for p in selected]
                        try:
                            media_items = []
                            files = {}
                            for i, p in enumerate(selected):
                                media_items.append(
                                    {"type": "photo", "media": f"attach://photo{i}"}
                                )
                                files[f"photo{i}"] = (Path(p).name, handles[i])
                            r = await client.post(
                                url,
                                data={"chat_id": chat_id, "media": json.dumps(media_items)},
                                files=files,
                            )
                        finally:
                            for h in handles:
                                h.close()
                else:
                    url = self._client() + "/sendMessage"
                    r = await client.post(url, json={"chat_id": chat_id, "text": draft.content})
            if r.status_code == 429:
                return PublishResult(success=False, error="rate limited (429)", status_hint="retry")
            if r.status_code >= 400:
                return PublishResult(success=False, error=r.text[:300], status_hint="failed")
            msg = r.json().get("result", {})
            return PublishResult(
                success=True,
                external_id=str(msg.get("message_id", "")),
                url="",
                status_hint="published",
                degraded=degraded,
            )
        except httpx.HTTPError as exc:
            return PublishResult(
                success=False, error=f"network error: {exc}", status_hint="failed"
            )
