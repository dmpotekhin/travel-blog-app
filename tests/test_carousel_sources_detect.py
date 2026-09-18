"""Source detection (Phase 2): what kind of reference is this, and which face?"""

import pytest

from core.models import CarouselSourceType, CarouselVertical
from modules.carousels.sources.detect import (
    canonicalize_url,
    detect_source_type,
    detect_vertical,
    parse_github_ref,
)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("https://github.com/acme/tool", CarouselSourceType.GITHUB_REPO),
        ("https://github.com/acme/tool/", CarouselSourceType.GITHUB_REPO),
        ("github.com/acme/tool", CarouselSourceType.GITHUB_REPO),
        ("https://github.com/acme/tool/issues/7", CarouselSourceType.GITHUB_ISSUE),
        ("https://github.com/acme/tool/pull/42", CarouselSourceType.GITHUB_PR),
        ("https://github.com/acme/tool/discussions/15", CarouselSourceType.GITHUB_DISCUSSION),
        (
            "https://github.com/acme/tool/releases/tag/v0.2.0",
            CarouselSourceType.GITHUB_RELEASE,
        ),
        ("https://habr.com/ru/articles/123456/", CarouselSourceType.ARTICLE_URL),
        ("https://dev.to/someone/post", CarouselSourceType.URL),
        ("https://t.me/somechannel/12", CarouselSourceType.TELEGRAM_POST),
        ("https://zen.yandex.ru/media/x/y", CarouselSourceType.ZEN_POST),
        ("Как я собрал карусель-фабрику", CarouselSourceType.MANUAL_TOPIC),
    ],
)
def test_detect_source_type(ref, expected):
    assert detect_source_type(ref) is expected


def test_detect_source_type_on_empty_input_is_manual_topic():
    assert detect_source_type("") is CarouselSourceType.MANUAL_TOPIC
    assert detect_source_type("   ") is CarouselSourceType.MANUAL_TOPIC


def test_canonicalize_url_is_stable_for_equivalent_links():
    first = canonicalize_url("https://GitHub.com/acme/tool/?tab=readme#install")
    second = canonicalize_url("https://github.com/acme/tool")
    assert first == second == "https://github.com/acme/tool"


def test_canonicalize_url_keeps_query_when_it_is_meaningful():
    # GitHub issue links use the path, not the query: those are dropped
    assert canonicalize_url("https://example.com/a?page=2") == "https://example.com/a"


def test_parse_github_ref_returns_none_for_a_bare_topic():
    assert parse_github_ref("Как я собирал карусель") is None


# ------------------------------------------------------------------ vertical


def test_github_issue_and_pr_are_qa():
    assert detect_vertical(CarouselSourceType.GITHUB_ISSUE) is CarouselVertical.QA
    assert detect_vertical(CarouselSourceType.GITHUB_PR) is CarouselVertical.QA


def test_repo_with_ai_topics_is_vibecoding():
    assert (
        detect_vertical(CarouselSourceType.GITHUB_REPO, tags=["ai", "agents"])
        is CarouselVertical.VIBECODING
    )


def test_repo_without_topics_is_still_vibecoding():
    assert detect_vertical(CarouselSourceType.GITHUB_REPO) is CarouselVertical.VIBECODING


def test_travel_article_is_travel():
    assert (
        detect_vertical(
            CarouselSourceType.ARTICLE_URL,
            url="https://example.com/blog/china-route",
            title="Маршрут по Китаю за 10 дней",
        )
        is CarouselVertical.TRAVEL
    )


def test_flaky_test_article_is_qa_not_travel():
    vertical = detect_vertical(
        CarouselSourceType.URL,
        url="https://example.com/blog/flaky-tests",
        title="Как мы ловили flaky test в CI",
    )
    assert vertical is CarouselVertical.QA


def test_prompt_engineering_article_is_vibecoding():
    vertical = detect_vertical(
        CarouselSourceType.URL,
        url="https://example.com/blog/prompts",
        title="Prompt engineering для локальных LLM",
    )
    assert vertical is CarouselVertical.VIBECODING


def test_unknown_article_stays_hybrid():
    vertical = detect_vertical(
        CarouselSourceType.URL, url="https://example.com/x", title="Мысли вслух"
    )
    assert vertical is CarouselVertical.HYBRID
