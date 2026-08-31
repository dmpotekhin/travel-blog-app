"""Facebook publisher — Graph API over httpx. Mode: partial.

Text+photo are posted to the Page; scheduled posting is app-managed. Honors the
honest-automation rule: if credentials are missing the result is ``manual``,
never a fake ``published``.
"""

from __future__ import annotations

from typing import List, Optional

import httpx

from core.models import Draft
from modules.publishers.base import BasePublisher, PublishResult


class FacebookPublisher(BasePublisher):
    name = "facebook"
    mode = "partial"

    @property
    def key_configured(self) -> bool:
        return bool(self.secrets.facebook_access_token and self.secrets.facebook_page_id)

    async def publish(self, draft, media_paths=None):
        if not self.key_configured:
            return PublishResult(success=False, manual=True, error="facebook not configured (token/page missing)", status_hint="manual")
        token = self.secrets.facebook_access_token
        page_id = self.secrets.facebook_page_id
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if media_paths:
                    # upload the first photo, then publish it on the page
                    r = await client.post(
                        f"https://graph.facebook.com/{page_id}/photos",
                        params={"access_token": token},
                        files={"source": (media_paths[0], open(media_paths[0], "rb"))},
                        data={"message": draft.content[:5000]},
                    )
                else:
                    r = await client.post(
                        f"https://graph.facebook.com/{page_id}/feed",
                        params={"access_token": token},
                        data={"message": draft.content},
                    )
        except httpx.HTTPError as exc:
            return PublishResult(success=False, error=f"network error: {exc}", status_hint="failed")
        try:
            data = r.json()
        except ValueError:
            return PublishResult(success=False, error=r.text[:300], status_hint="failed")
        if "error" in data:
            err = data["error"].get("message", str(data["error"]))
            if "session has expired" in err or "invalid" in err.lower():
                return PublishResult(success=False, manual=True, error=err, status_hint="manual")
            return PublishResult(success=False, error=err, status_hint="failed")
        pid = data.get("id", "")
        return PublishResult(success=True, external_id=str(pid), url="", status_hint="published")
