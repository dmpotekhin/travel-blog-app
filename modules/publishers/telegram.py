"""Telegram publisher — Bot API over httpx. Mode: auto.

Sends up to 10 photos as an album (sendMediaGroup) and flags ``degraded`` when it
had to truncate the caption or drop photos beyond the platform limit (ADR-104).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from core.models import Draft
from modules.publishers.base import BasePublisher, PublishResult, plan_media


class TelegramPublisher(BasePublisher):
    name = "telegram"
    mode = "auto"
    api_base = "https://api.telegram.org/bot{token}"

    PHOTO_CAP = 10          # sendMediaGroup: up to 10 photos per album
    CAPTION_LIMIT = 1024    # album/caption length limit (media branches)
    TEXT_LIMIT = 4096       # sendMessage text limit (text-only branch)

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

        if media:
            selected, degraded, note = plan_media(
                media, draft.content, photo_cap=self.PHOTO_CAP,
                caption_limit=self.CAPTION_LIMIT,
            )
            caption = draft.content[: self.CAPTION_LIMIT]
        else:
            # Text-only branch: sendMessage takes up to TEXT_LIMIT chars, so the
            # caption limit of the media branches does not apply here — otherwise
            # a long text-only post would be (falsely) flagged as degraded.
            selected = []
            text = draft.content
            degraded = len(text) > self.TEXT_LIMIT
            caption = text[: self.TEXT_LIMIT]
            note = f"caption {len(text)} -> {self.TEXT_LIMIT}" if degraded else ""

        url_base = self._client()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if not selected:
                    r = await client.post(
                        url_base + "/sendMessage",
                        json={"chat_id": chat_id, "text": caption},
                    )
                elif len(selected) == 1:
                    with open(selected[0], "rb") as fh:
                        r = await client.post(
                            url_base + "/sendPhoto",
                            data={"chat_id": chat_id, "caption": caption},
                            files={"photo": (Path(selected[0]).name, fh)},
                        )
                else:
                    r = await self._post_album(
                        client, url_base, chat_id, selected, caption
                    )
        except httpx.HTTPError as exc:
            return PublishResult(
                success=False, error=f"network error: {exc}", status_hint="failed"
            )

        if r.status_code == 429:
            return PublishResult(success=False, error="rate limited (429)", status_hint="retry")
        if r.status_code >= 400:
            return PublishResult(success=False, error=r.text[:300], status_hint="failed")
        res = r.json().get("result", {})
        # sendMediaGroup returns an ARRAY of Messages; sendMessage/sendPhoto return
        # a single Message object. Normalize so we never hit 'list'.get(...).
        if isinstance(res, list):
            first = res[0] if res else {}
        else:
            first = res if isinstance(res, dict) else {}
        return PublishResult(
            success=True,
            external_id=str(first.get("message_id", "")),
            url="",
            status_hint="published",
            degraded=degraded,
            degraded_reason=note,
        )

    async def _post_album(self, client, url_base, chat_id, selected, caption):
        """Send 2-10 photos as a media group.

        The caption rides on the first media item — Telegram shows it as the
        album caption. Omit it entirely (as the old code did) and the album goes
        out with no text, a silent drop ADR-104 exists to prevent.
        """
        # Open files defensively: if a later open() raises, close the ones already
        # opened so we don't leak file handles (partial-failure safety).
        handles: list = []
        try:
            for p in selected:
                handles.append(open(p, "rb"))
            media_items = []
            files = {}
            for i, p in enumerate(selected):
                media_items.append({"type": "photo", "media": f"attach://photo{i}"})
                files[f"photo{i}"] = (Path(p).name, handles[i])
            if caption:
                media_items[0]["caption"] = caption
            return await client.post(
                url_base + "/sendMediaGroup",
                data={"chat_id": chat_id, "media": json.dumps(media_items)},
                files=files,
            )
        finally:
            for h in handles:
                h.close()
