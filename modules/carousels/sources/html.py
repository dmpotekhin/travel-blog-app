"""Deterministic HTML → document extraction (Phase 2, stdlib only).

No model runs here: the parser reports what the page literally says (headings,
paragraphs, quotes, code, images, colours). Everything downstream may only use
these extracted blocks, which is what makes the fact-guard possible.
"""

import re
from html import unescape
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from pydantic import BaseModel, Field

from core.models import CodeSnippet, ImageAsset

#: A single code block longer than this is cut and flagged ``truncated``.
MAX_CODE_CHARS = 1200

HEADING_TAGS = {"h1", "h2", "h3"}
PARAGRAPH_TAGS = {"p"}
QUOTE_TAGS = {"blockquote"}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
TRACKED_TAGS = HEADING_TAGS | PARAGRAPH_TAGS | QUOTE_TAGS

_HEX_COLOR = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_LANG_CLASS = re.compile(r"(?:language|lang|brush)[-:]([A-Za-z0-9+#_-]+)")


class ExtractedDocument(BaseModel):
    """What one HTML page actually contains (verbatim blocks only)."""

    url: str = ""
    canonical_url: str = ""
    title: str = ""
    description: str = ""
    lang: str = ""
    author: str = ""
    published_at: str = ""
    headings: List[str] = Field(default_factory=list)
    paragraphs: List[str] = Field(default_factory=list)
    quotes: List[str] = Field(default_factory=list)
    code_blocks: List[CodeSnippet] = Field(default_factory=list)
    images: List[ImageAsset] = Field(default_factory=list)
    brand_colors: List[str] = Field(default_factory=list)
    text: str = ""

    def is_empty_body(self) -> bool:
        """True when the page gave us no readable prose at all."""
        return not self.paragraphs and not self.quotes and not self.headings


def collapse(text: str) -> str:
    """Collapse HTML whitespace into single spaces."""
    return " ".join(unescape(text or "").split())


def absolute_url(base: str, link: str) -> str:
    """Absolute-ise ``link`` against ``base``; drop data:/javascript: links."""
    link = (link or "").strip()
    if not link or link.startswith(("data:", "javascript:", "mailto:")):
        return ""
    if link.startswith("//"):
        return "https:" + link
    return urljoin(base, link)


def canonicalize_url(url: str) -> str:
    """Lower-case the host, drop the fragment and the query, trim the slash.

    The carousel engine never needs tracking parameters, and two links to the
    same article must dedupe to one source.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    return urlunparse(
        (
            parsed.scheme.lower() or "https",
            parsed.netloc.lower(),
            path,
            "",
            "",
            "",
        )
    )


class _DocumentParser(HTMLParser):
    """Streaming extractor — keeps only the blocks we can cite."""

    def __init__(self, url: str, *, want_code: bool = True, want_images: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.url = url
        self.want_code = want_code
        self.want_images = want_images
        self.title = ""
        self.description = ""
        self.lang = ""
        self.author = ""
        self.published_at = ""
        self.canonical = ""
        self.headings: List[str] = []
        self.paragraphs: List[str] = []
        self.quotes: List[str] = []
        self.code_blocks: List[CodeSnippet] = []
        self.images: List[ImageAsset] = []
        self.brand_colors: List[str] = []
        self._skip_depth = 0
        self._stack: List[tuple] = []
        self._in_title = False
        self._pre_depth = 0
        self._code_lang = ""
        self._code_buf: List[str] = []

    # ---------------------------------------------------------------- helpers

    def _add_color(self, value: str) -> None:
        for match in _HEX_COLOR.findall(value or ""):
            color = match.lower()
            if color not in self.brand_colors and len(self.brand_colors) < 6:
                self.brand_colors.append(color)

    def _flush(self, tag: str, text: str) -> None:
        clean = collapse(text)
        if not clean:
            return
        if tag in HEADING_TAGS:
            self.headings.append(clean)
        elif tag in QUOTE_TAGS:
            self.quotes.append(clean)
        else:
            self.paragraphs.append(clean)

    # ------------------------------------------------------------ HTMLParser

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "html" and attributes.get("lang"):
            self.lang = attributes["lang"].strip()
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (attributes.get("name") or attributes.get("property") or "").lower()
            content = attributes.get("content", "")
            if name in {"description", "og:description"} and not self.description:
                self.description = collapse(content)
            elif name in {"author", "article:author"} and not self.author:
                self.author = collapse(content)
            elif name in {"article:published_time", "date", "pubdate", "datepublished"}:
                if not self.published_at:
                    self.published_at = collapse(content)
            elif name in {"theme-color", "msapplication-tilecolor"}:
                self._add_color(content)
        elif tag == "link":
            rel = attributes.get("rel", "").lower()
            if "canonical" in rel and attributes.get("href"):
                self.canonical = absolute_url(self.url, attributes["href"])
        elif tag == "img":
            if self.want_images and self._pre_depth == 0:
                source = absolute_url(self.url, attributes.get("src", ""))
                if source:
                    self.images.append(
                        ImageAsset(
                            url_or_path=source,
                            alt_text=collapse(attributes.get("alt", "")),
                            source_type="image",
                            license_or_origin=self.url,
                            is_real_photo=True,
                            confidence=1.0,
                        )
                    )
        elif tag == "pre":
            if self.want_code:
                self._pre_depth += 1
                self._code_lang = ""
                self._code_buf = []
        elif tag == "code" and self._pre_depth:
            match = _LANG_CLASS.search(attributes.get("class", ""))
            if match:
                self._code_lang = match.group(1).lower()

        if attributes.get("style"):
            self._add_color(attributes["style"])
        if tag in TRACKED_TAGS:
            self._stack.append((tag, []))

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == "pre" and self._pre_depth:
            self._pre_depth -= 1
            self._emit_code()
        if tag in TRACKED_TAGS and self._stack:
            for index in range(len(self._stack) - 1, -1, -1):
                open_tag, buffer = self._stack[index]
                if open_tag == tag:
                    self._stack.pop(index)
                    self._flush(tag, "".join(buffer))
                    break
            else:  # pragma: no cover - malformed markup
                self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._pre_depth:
            self._code_buf.append(data)
            return
        if self._in_title:
            self.title += data
        for _, buffer in self._stack:
            buffer.append(data)

    def _emit_code(self) -> None:
        code = "".join(self._code_buf).strip("\n")
        self._code_buf = []
        if not code.strip():
            return
        truncated = len(code) > MAX_CODE_CHARS
        if truncated:
            code = code[:MAX_CODE_CHARS].rstrip()
        self.code_blocks.append(
            CodeSnippet(
                language=self._code_lang,
                code=code,
                caption="",
                source_excerpt=collapse(code[:200]),
                source_ref=self.url,
                truncated=truncated,
            )
        )


def extract_document(
    html_text: str,
    url: str = "",
    *,
    want_code: bool = True,
    want_images: bool = True,
    want_colors: bool = True,
) -> ExtractedDocument:
    """Parse ``html_text`` into cited blocks (never raises on broken markup)."""
    parser = _DocumentParser(url, want_code=want_code, want_images=want_images)
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:  # pragma: no cover - defensive: broken markup is data
        pass
    if not want_colors:
        parser.brand_colors = []
    canonical = parser.canonical or canonicalize_url(url)
    text = "\n\n".join(parser.headings + parser.paragraphs + parser.quotes)
    return ExtractedDocument(
        url=url,
        canonical_url=canonical,
        title=collapse(parser.title),
        description=parser.description,
        lang=parser.lang,
        author=parser.author,
        published_at=parser.published_at,
        headings=parser.headings,
        paragraphs=parser.paragraphs,
        quotes=parser.quotes,
        code_blocks=parser.code_blocks,
        images=parser.images,
        brand_colors=parser.brand_colors,
        text=text,
    )


def split_sentences(text: str, *, min_length: int = 30, max_length: int = 400) -> List[str]:
    """Deterministic sentence split for fact extraction (extractive, no LLM)."""
    out: List[str] = []
    for chunk in re.split(r"(?<=[.!?…])\s+", collapse(text)):
        candidate = chunk.strip()
        if min_length <= len(candidate) <= max_length and candidate not in out:
            out.append(candidate)
    return out


def first_lines(text: str, limit: int = 12, *, min_length: int = 25) -> List[str]:
    """Non-empty lines of a markdown/plain text body (README, PR body…)."""
    out: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("<!--", "---")):
            continue
        cleaned = collapse(re.sub(r"^[>#*\-\s]+", "", line))
        if len(cleaned) >= min_length and cleaned not in out:
            out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def markdown_images(text: str) -> List[ImageAsset]:
    """Pull ``![alt](url)`` images out of markdown (README / PR body)."""
    out: List[ImageAsset] = []
    for match in re.finditer(r"!\[([^\]]*)\]\(([^)\s]+)", text or ""):
        url = absolute_url("", match.group(2))
        if url:
            out.append(ImageAsset(url_or_path=url, alt_text=collapse(match.group(1))))
    return out


def markdown_code_blocks(text: str, *, base_url: str = "") -> List[CodeSnippet]:
    """Pull fenced ```code``` blocks out of markdown, verbatim."""
    out: List[CodeSnippet] = []
    for match in re.finditer(r"```([A-Za-z0-9+#_-]*)\s*\n(.*?)```", text or "", re.DOTALL):
        language = (match.group(1) or "").lower()
        code = match.group(2).strip("\n")
        if not code.strip():
            continue
        truncated = len(code) > MAX_CODE_CHARS
        if truncated:
            code = code[:MAX_CODE_CHARS].rstrip()
        out.append(
            CodeSnippet(
                language=language,
                code=code,
                source_excerpt=collapse(code[:200]),
                source_ref=base_url,
                truncated=truncated,
            )
        )
    return out


def find_optional(pattern: str, text: str) -> Optional[str]:
    """First group of ``pattern`` in ``text`` (None when absent)."""
    match = re.search(pattern, text or "", re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


__all__ = [
    "MAX_CODE_CHARS",
    "ExtractedDocument",
    "absolute_url",
    "canonicalize_url",
    "collapse",
    "extract_document",
    "find_optional",
    "first_lines",
    "markdown_code_blocks",
    "markdown_images",
    "split_sentences",
]
