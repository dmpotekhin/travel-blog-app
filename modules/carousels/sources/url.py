"""URL researcher (Phase 2): httpx + stdlib HTML parsing — no browser needed.

Playwright stays an *optional* future adapter behind the same
:class:`BaseSourceResolver`; the MVP path must work with a plain HTTP client so
`dry_run` and CI never need a headless browser.
"""

from typing import List, Optional

import httpx
from loguru import logger

from core.config import CarouselUrlSourceConfig
from core.exceptions import SourceResolutionError
from core.models import (
    URL_SOURCE_TYPES,
    CarouselSourceContext,
    CarouselSourceType,
    CarouselVertical,
    SourceFact,
    dump_json_obj,
)

from .base import BaseSourceResolver, ResolveOptions, confidence_from, merge_warnings
from .detect import detect_content_type, detect_vertical
from .html import ExtractedDocument, canonicalize_url, extract_document, split_sentences

DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
DEFAULT_USER_AGENT_FALLBACK = "travel-blog-app-carousel/1.0"


class UrlSourceResolver(BaseSourceResolver):
    """Turn an article / docs / post URL into sourced context."""

    name = "url"
    source_types = URL_SOURCE_TYPES

    def __init__(
        self,
        settings: Optional[CarouselUrlSourceConfig] = None,
        *,
        client: Optional[httpx.AsyncClient] = None,
        user_agent: str = "",
    ) -> None:
        self.settings = settings or CarouselUrlSourceConfig()
        self._client = client
        self._owns_client = client is None
        self.user_agent = user_agent or self.settings.user_agent or DEFAULT_USER_AGENT_FALLBACK

    # ------------------------------------------------------------------- http

    def _client_or_new(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.timeout_seconds,
                follow_redirects=True,
                max_redirects=self.settings.max_redirects,
            )
            self._owns_client = True
        return self._client

    async def _get(self, url: str) -> httpx.Response:
        client = self._client_or_new()
        try:
            return await client.get(
                url,
                headers={"User-Agent": self.user_agent, "Accept": DEFAULT_ACCEPT},
            )
        except httpx.HTTPError as exc:  # timeouts, DNS, TLS, too many redirects
            raise SourceResolutionError(f"cannot fetch {url}: {exc}") from exc

    async def resolve(
        self, source_ref: str, options: Optional[ResolveOptions] = None
    ) -> CarouselSourceContext:
        """Fetch and extract; raises when the source cannot be read."""
        if not self.settings.enabled:
            raise SourceResolutionError(
                "URL resolver is disabled (carousels.sources.url.enabled = false)"
            )
        url = (source_ref or "").strip()
        if not url:
            raise SourceResolutionError("empty source reference")

        response = await self._get(url)
        if response.status_code >= 400:
            raise SourceResolutionError(f"URL returned {response.status_code} for {url}")
        final_url = str(response.url) if response.url else url
        document = extract_document(
            response.text,
            final_url,
            want_code=self.settings.extract_code_blocks,
            want_images=self.settings.extract_images,
            want_colors=self.settings.extract_brand_colors,
        )
        context = self.build_context(document, options=options)
        context.raw_payload_json = dump_json_obj(
            {
                "http_code": response.status_code,
                "final_url": final_url,
                "content_type_header": response.headers.get("content-type", ""),
                "paragraphs": len(document.paragraphs),
                "headings": len(document.headings),
                "code_blocks": len(document.code_blocks),
                "images": len(document.images),
            }
        )
        logger.debug(
            "url source resolved: {} paragraphs={} facts={} confidence={}",
            canonicalize_url(url),
            len(document.paragraphs),
            len(context.facts),
            context.confidence,
        )
        return context

    # ---------------------------------------------------------------- mapping

    def build_context(
        self,
        document: ExtractedDocument,
        *,
        options: Optional[ResolveOptions] = None,
    ) -> CarouselSourceContext:
        """Extractive mapping: sentences become facts, nothing is paraphrased."""
        options = options or ResolveOptions()
        source_type = options.source_type or CarouselSourceType.URL
        title = document.title or (document.headings[0] if document.headings else "")

        facts: List[str] = []
        sourced: List[SourceFact] = []
        for index, paragraph in enumerate(document.paragraphs):
            for sentence in split_sentences(paragraph):
                if sentence in facts:
                    continue
                facts.append(sentence)
                sourced.append(
                    SourceFact(
                        text=sentence,
                        source_excerpt=sentence,
                        source_ref=f"p:{index}",
                        verified=True,
                    )
                )

        warnings = list(options.extra.get("warnings", []) if options.extra else [])
        if document.is_empty_body():
            warnings.append(
                "no readable body text extracted — the page is likely JS-only or "
                "paywalled: confirm the source manually"
            )
        if any(block.truncated for block in document.code_blocks):
            warnings.append("a code block was truncated — it must be shown as a fragment")
        if not title:
            warnings.append("the page has no title")

        vertical = options.vertical or detect_vertical(
            source_type, url=document.url, title=title
        )
        confidence = confidence_from(
            title=title,
            facts=len(facts),
            paragraphs=len(document.paragraphs),
            verified_code=len(document.code_blocks),
            language=document.lang,
        )
        return CarouselSourceContext(
            source_type=source_type,
            vertical=vertical,
            source_url=document.url,
            external_id=document.canonical_url,
            title=title,
            summary=document.description or (document.paragraphs[0] if document.paragraphs else ""),
            canonical_url=document.canonical_url,
            facts=facts,
            sourced_facts=sourced,
            quotes=list(document.quotes),
            code_snippets=list(document.code_blocks),
            images=list(document.images),
            brand_colors=list(document.brand_colors),
            content_type=detect_content_type(document.url, title),
            language=document.lang,
            author=document.author,
            published_at=document.published_at,
            confidence=confidence,
            warnings=merge_warnings(warnings),
        )

    async def aclose(self) -> None:
        """Close the HTTP client when we created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


__all__ = ["UrlSourceResolver"]
