"""Facebook publisher — Graph API over httpx. Mode: partial.

Text+photo are posted to the Page; scheduled posting is app-managed. Honors the
honest-automation rule: if credentials are missing the result is ``manual``,
never a fake ``published``.
"""

from __future__ import annotations

import httpx

from core.models import Draft
from modules.publishers.base import BasePublisher, PublishResult, plan_media


class FacebookPublisher(BasePublisher):
    name = "facebook"
    mode = "partial"

    PHOTO_CAP = 1         # Graph /photos accepts a single source
    CAPTION_LIMIT = 5000  # photo-caption / feed-message length ceiling used here

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
                selected, degraded, note = plan_media(
                    media_paths or [], draft.content,
                    photo_cap=self.PHOTO_CAP, caption_limit=self.CAPTION_LIMIT,
                )
                if selected:
                    # FB Graph /photos accepts a single source; post the first photo
                    # and flag degraded if there were more to send (F7/ADR-104).
                    with open(selected[0], "rb") as fh:
                        r = await client.post(
                            f"https://graph.facebook.com/{page_id}/photos",
                            params={"access_token": token},
                            files={"source": (selected[0], fh)},
                            data={"message": draft.content[: self.CAPTION_LIMIT]},
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
        return PublishResult(success=True, external_id=str(pid), url="", status_hint="published", degraded=degraded, degraded_reason=note)
