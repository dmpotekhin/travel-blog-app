"""URL researcher (Phase 2): HTML in, sourced context out — fully offline.

The resolver takes an ``httpx.AsyncClient``, so the tests inject
``httpx.MockTransport`` and never touch the network.
"""

import asyncio

import httpx
import pytest

from core.config import CarouselUrlSourceConfig
from core.exceptions import SourceResolutionError
from core.models import CarouselSourceType, CarouselVertical
from modules.carousels.sources import html as htmlext
from modules.carousels.sources.detect import (
    canonicalize_url,
    detect_content_type,
)
from modules.carousels.sources.url import UrlSourceResolver

URL = "https://example.com/blog/china-route"
CANONICAL = "https://example.com/blog/china-route-canonical"

ARTICLE_HTML = """<!doctype html>
<html lang="ru">
<head>
  <title>Маршрут по Китаю за 10 дней</title>
  <link rel="canonical" href="/blog/china-route-canonical">
  <meta name="description" content="Пекин, Сиань, Чэнду: как не потерять дни.">
  <meta name="author" content="Дмитрий Потехин">
  <meta name="theme-color" content="#c8102e">
  <meta property="article:published_time" content="2026-03-04T10:00:00Z">
</head>
<body style="background:#0f172a">
  <article>
    <h1>Маршрут по Китаю за 10 дней</h1>
    <p>Пекин стоит первым: трёх дней хватает на главное без спешки.</p>
    <p>Сиань даёт терракотовую армию и ночной рынок в центре города.</p>
    <blockquote>Поезд Пекин — Сиань идёт около пяти часов.</blockquote>
    <h2>Бюджет</h2>
    <pre><code class="language-python">trip = {"days": 10, "cities": ["Пекин", "Сиань"]}</code></pre>
    <img src="/img/beijing.jpg" alt="Пекин на рассвете">
  </article>
</body>
</html>"""

SCRIPT_ONLY_HTML = """<!doctype html>
<html><head><title>Loading…</title></head>
<body><div id="app"></div><script>window.__DATA__ = {}</script></body></html>"""


def serve(body: str, status_code: int = 200, headers: dict | None = None):
    """A one-route MockTransport handler that records the outgoing request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body, headers=headers or {})

    return handler


def resolver(handler, **settings) -> UrlSourceResolver:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return UrlSourceResolver(CarouselUrlSourceConfig(**settings), client=client)


# ---------------------------------------------------------------- html layer


def test_extract_document_reads_the_head_and_the_body():
    doc = htmlext.extract_document(ARTICLE_HTML, URL)
    assert doc.title == "Маршрут по Китаю за 10 дней"
    assert doc.description == "Пекин, Сиань, Чэнду: как не потерять дни."
    assert doc.lang == "ru"
    assert doc.author == "Дмитрий Потехин"
    assert doc.published_at.startswith("2026-03-04")
    assert doc.headings == ["Маршрут по Китаю за 10 дней", "Бюджет"]
    assert doc.paragraphs[0].startswith("Пекин стоит первым")
    assert doc.quotes == ["Поезд Пекин — Сиань идёт около пяти часов."]


def test_extract_document_absolutises_the_canonical_url():
    doc = htmlext.extract_document(ARTICLE_HTML, URL)
    assert doc.canonical_url == CANONICAL


def test_code_blocks_are_kept_verbatim_with_their_language():
    doc = htmlext.extract_document(ARTICLE_HTML, URL)
    assert len(doc.code_blocks) == 1
    snippet = doc.code_blocks[0]
    assert snippet.language == "python"
    assert snippet.code == 'trip = {"days": 10, "cities": ["Пекин", "Сиань"]}'
    assert snippet.source_ref == URL
    assert snippet.truncated is False


def test_a_huge_code_block_is_flagged_as_truncated():
    body = "<p>" + ("текст " * 40) + "</p><pre><code>" + ("x = 1\n" * 900) + "</code></pre>"
    doc = htmlext.extract_document(f"<html><body>{body}</body></html>", URL)
    snippet = doc.code_blocks[0]
    assert snippet.truncated is True
    assert len(snippet.code) < len("x = 1\n" * 900)


def test_images_are_absolute_and_keep_their_alt_text():
    doc = htmlext.extract_document(ARTICLE_HTML, URL)
    assert len(doc.images) == 1
    assert doc.images[0].url_or_path == "https://example.com/img/beijing.jpg"
    assert doc.images[0].alt_text == "Пекин на рассвете"
    assert doc.images[0].is_real_photo is True


def test_brand_colors_come_from_meta_and_inline_styles():
    doc = htmlext.extract_document(ARTICLE_HTML, URL)
    assert "#c8102e" in doc.brand_colors
    assert "#0f172a" in doc.brand_colors


# -------------------------------------------------------------- url resolver


async def test_resolver_builds_a_sourced_context():
    result = await resolver(serve(ARTICLE_HTML)).resolve(URL)
    assert result.source_type == CarouselSourceType.URL
    assert result.canonical_url == CANONICAL
    assert result.title == "Маршрут по Китаю за 10 дней"
    assert result.content_type == "article"
    assert result.language == "ru"
    assert result.confidence >= 0.5
    assert result.is_low_confidence is False


async def test_every_fact_carries_its_excerpt_and_ref():
    result = await resolver(serve(ARTICLE_HTML)).resolve(URL)
    assert result.facts, "an article with real paragraphs must yield facts"
    assert len(result.sourced_facts) == len(result.facts)
    for fact in result.sourced_facts:
        assert fact.source_excerpt, "a fact without an excerpt is unverifiable"
        assert fact.source_ref
        assert fact.verified is True
        assert fact.is_hypothesis is False


async def test_quotes_and_metrics_are_recorded_separately():
    result = await resolver(serve(ARTICLE_HTML)).resolve(URL)
    assert result.quotes == ["Поезд Пекин — Сиань идёт около пяти часов."]


async def test_script_only_page_is_low_confidence_and_flagged():
    result = await resolver(serve(SCRIPT_ONLY_HTML)).resolve(URL)
    assert result.confidence < 0.5
    assert result.is_low_confidence is True
    assert any("body" in warning for warning in result.warnings)


async def test_http_error_raises_instead_of_inventing_content():
    with pytest.raises(SourceResolutionError):
        await resolver(serve("<html></html>", status_code=500)).resolve(URL)


async def test_configured_user_agent_is_sent():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, text=ARTICLE_HTML)

    await resolver(handler, user_agent="carousel-test/9").resolve(URL)
    assert seen == ["carousel-test/9"]


async def test_raw_payload_is_kept_for_audit():
    result = await resolver(serve(ARTICLE_HTML)).resolve(URL)
    assert result.raw_payload_json != "{}"
    assert '"status"' not in result.raw_payload_json


# ------------------------------------------------------------------- helpers


def test_canonicalize_url_drops_noise():
    assert (
        canonicalize_url("https://Example.com/blog/x/?utm_source=tg#top")
        == "https://example.com/blog/x"
    )


def test_detect_content_type_knows_the_supported_kinds():
    assert detect_content_type("https://example.com/blog/post") == "article"
    assert detect_content_type("https://example.com/docs/setup") == "docs"
    assert detect_content_type("https://github.com/acme/tool/releases") == "release_notes"
    assert detect_content_type("https://t.me/somechannel/12") == "social_post"
    assert detect_content_type("https://habr.com/ru/articles/123456/") == "article"


def test_vertical_defaults_to_hybrid_when_nothing_is_known():
    """Extraction carries no vertical: detection decides, and stays honest."""
    document = htmlext.extract_document("<html><body><p>текст</p></body></html>", URL)
    assert not hasattr(document, "vertical")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body><p>текст</p></body></html>")

    resolved = asyncio.run(
        resolver(handler).resolve("https://example.com/2026/notes")
    )
    assert resolved.vertical is CarouselVertical.HYBRID
