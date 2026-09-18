"""GitHub researcher (Phase 2): REST + GraphQL in, sourced context out.

Offline: every request goes through ``httpx.MockTransport``; the token is only
ever read from the caller (env wiring happens in ``core.config``).
"""

import json

import httpx
import pytest

from core.config import CarouselGithubSourceConfig
from core.exceptions import SourceResolutionError
from core.models import CarouselSourceType, CarouselVertical
from modules.carousels.sources.detect import parse_github_ref
from modules.carousels.sources.github import GitHubSourceResolver

REPO_URL = "https://github.com/acme/tool"
ISSUE_URL = "https://github.com/acme/tool/issues/7"
PR_URL = "https://github.com/acme/tool/pull/42"
DISCUSSION_URL = "https://github.com/acme/tool/discussions/15"
RELEASE_URL = "https://github.com/acme/tool/releases/tag/v0.2.0"

REPO_JSON = {
    "full_name": "acme/tool",
    "description": "Local carousel factory",
    "html_url": REPO_URL,
    "topics": ["ai", "agents"],
    "stargazers_count": 12,
    "forks_count": 3,
    "open_issues_count": 4,
    "language": "Python",
    "default_branch": "main",
    "license": {"spdx_id": "MIT"},
}

README_MD = """# acme/tool

Local carousel factory built in a weekend.

```python
from modules.carousels import CarouselFactory
factory = CarouselFactory(db, config)
```

![pipeline](https://raw.githubusercontent.com/acme/tool/main/docs/pipeline.png)
"""

ISSUE_JSON = {
    "number": 7,
    "title": "Flaky test in CI only",
    "body": "Тест падает только в CI, локально зелёный.",
    "state": "open",
    "labels": [{"name": "bug"}, {"name": "flaky-test"}],
    "comments": 2,
    "user": {"login": "qa-dev"},
    "html_url": ISSUE_URL,
    "created_at": "2026-02-01T09:00:00Z",
}

ISSUE_COMMENTS = [
    {"body": "Падает в 3 из 10 прогонов.", "user": {"login": "qa-dev"}},
    {"body": "Похоже на shared state между тестами.", "user": {"login": "dev"}},
]

PR_JSON = {
    "number": 42,
    "title": "Fix flaky test by isolating state",
    "body": "Изолируем состояние между тестами.",
    "state": "closed",
    "merged": True,
    "additions": 120,
    "deletions": 8,
    "changed_files": 3,
    "comments": 4,
    "user": {"login": "dev"},
    "html_url": PR_URL,
    "created_at": "2026-02-05T09:00:00Z",
}

PR_FILES = [
    {
        "filename": "tests/test_state.py",
        "additions": 10,
        "deletions": 2,
        "patch": "@@ -1,3 +1,4 @@\n- shared = {}\n+ def isolated():\n+     return {}",
    }
]

PR_REVIEWS = [{"state": "APPROVED", "body": "lgtm", "user": {"login": "rev"}}]


def router(routes: dict[str, tuple[int, object]], *, seen: list | None = None):
    """MockTransport handler keyed by (method, path)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        key = (request.method, request.url.path)
        status, payload = routes.get(key, (404, {"message": "Not Found"}))
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    return handler


def resolver(handler, *, token: str = "", **settings) -> GitHubSourceResolver:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GitHubSourceResolver(
        CarouselGithubSourceConfig(**settings), client=client, token=token
    )


REPO_ROUTES = {
    ("GET", "/repos/acme/tool"): (200, REPO_JSON),
    ("GET", "/repos/acme/tool/readme"): (200, README_MD),
    ("GET", "/repos/acme/tool/languages"): (200, {"Python": 1200, "Shell": 40}),
    ("GET", "/repos/acme/tool/releases/latest"): (404, {"message": "Not Found"}),
}


# --------------------------------------------------------------- ref parsing


def test_parse_github_ref_recognises_every_supported_kind():
    assert parse_github_ref(REPO_URL).source_type is CarouselSourceType.GITHUB_REPO
    issue = parse_github_ref(ISSUE_URL)
    assert issue.source_type is CarouselSourceType.GITHUB_ISSUE
    assert (issue.owner, issue.repo, issue.number) == ("acme", "tool", 7)
    assert parse_github_ref(PR_URL).source_type is CarouselSourceType.GITHUB_PR
    assert parse_github_ref(PR_URL).number == 42
    assert (
        parse_github_ref(DISCUSSION_URL).source_type
        is CarouselSourceType.GITHUB_DISCUSSION
    )
    release = parse_github_ref(RELEASE_URL)
    assert release.source_type is CarouselSourceType.GITHUB_RELEASE
    assert release.ref == "v0.2.0"


def test_parse_github_ref_rejects_foreign_urls():
    assert parse_github_ref("https://example.com/blog") is None
    assert parse_github_ref("просто тема") is None


# ------------------------------------------------------------------ no token


async def test_repo_without_token_still_resolves_public_api():
    result = await resolver(router(REPO_ROUTES)).resolve(REPO_URL)
    assert result.source_type == CarouselSourceType.GITHUB_REPO
    assert result.external_id == "acme/tool"
    assert result.title == "acme/tool"
    assert result.vertical == CarouselVertical.VIBECODING


async def test_no_authorization_header_is_sent_without_a_token():
    seen: list[httpx.Request] = []
    await resolver(router(REPO_ROUTES, seen=seen)).resolve(REPO_URL)
    assert seen, "the resolver must actually call the API"
    assert all("authorization" not in request.headers for request in seen)


async def test_token_is_sent_as_bearer_when_configured():
    seen: list[httpx.Request] = []
    await resolver(router(REPO_ROUTES, seen=seen), token="secret-token").resolve(REPO_URL)
    assert all(
        request.headers.get("authorization") == "Bearer secret-token" for request in seen
    )


# ----------------------------------------------------------------------- repo


async def test_repo_metrics_come_from_the_api_not_from_the_model():
    result = await resolver(router(REPO_ROUTES)).resolve(REPO_URL)
    by_name = {metric.name: metric for metric in result.metrics}
    assert by_name["stargazers"].value == 12
    assert by_name["stargazers"].is_verified is True
    assert by_name["stargazers"].raw_value == "12"
    assert by_name["forks"].value == 3
    assert by_name["open_issues"].value == 4


async def test_repo_readme_yields_facts_tags_and_code():
    result = await resolver(router(REPO_ROUTES)).resolve(REPO_URL)
    assert any("carousel factory" in fact.lower() for fact in result.facts)
    assert result.tags == ["ai", "agents"]
    assert [snippet.language for snippet in result.code_snippets] == ["python"]
    assert "CarouselFactory" in result.code_snippets[0].code


async def test_repo_readme_images_are_absolute_and_attributed():
    result = await resolver(router(REPO_ROUTES)).resolve(REPO_URL)
    assert len(result.images) == 1
    assert result.images[0].url_or_path.endswith("docs/pipeline.png")
    assert result.images[0].alt_text == "pipeline"
    assert result.images[0].license_or_origin == REPO_URL


async def test_missing_latest_release_is_a_warning_not_a_failure():
    result = await resolver(router(REPO_ROUTES)).resolve(REPO_URL)
    assert result.metrics
    assert any("release" in warning.lower() for warning in result.warnings)


# ---------------------------------------------------------------------- issue


ISSUE_ROUTES = {
    ("GET", "/repos/acme/tool/issues/7"): (200, ISSUE_JSON),
    ("GET", "/repos/acme/tool/issues/7/comments"): (200, ISSUE_COMMENTS),
}


async def test_issue_is_qa_and_keeps_labels_and_comment_excerpts():
    result = await resolver(router(ISSUE_ROUTES)).resolve(ISSUE_URL)
    assert result.source_type == CarouselSourceType.GITHUB_ISSUE
    assert result.vertical == CarouselVertical.QA
    assert "bug" in result.tags and "flaky-test" in result.tags
    assert any("3 из 10" in fact for fact in result.facts)
    assert all(fact.source_excerpt for fact in result.sourced_facts)


async def test_issue_facts_point_back_at_their_evidence():
    result = await resolver(router(ISSUE_ROUTES)).resolve(ISSUE_URL)
    refs = {fact.source_ref for fact in result.sourced_facts}
    assert any(ref.startswith("issue:") for ref in refs)
    assert any(ref.startswith("comment:") for ref in refs)


# ------------------------------------------------------------------------- pr


PR_ROUTES = {
    ("GET", "/repos/acme/tool/pulls/42"): (200, PR_JSON),
    ("GET", "/repos/acme/tool/pulls/42/files"): (200, PR_FILES),
    ("GET", "/repos/acme/tool/pulls/42/reviews"): (200, PR_REVIEWS),
}


async def test_pr_metrics_are_real_numbers_from_the_api():
    result = await resolver(router(PR_ROUTES)).resolve(PR_URL)
    by_name = {metric.name: metric for metric in result.metrics}
    assert by_name["additions"].value == 120
    assert by_name["additions"].unit == "lines"
    assert by_name["deletions"].value == 8
    assert by_name["changed_files"].value == 3
    assert all(metric.is_verified for metric in result.metrics)


async def test_pr_diff_hunks_become_diff_snippets():
    result = await resolver(router(PR_ROUTES)).resolve(PR_URL)
    assert len(result.code_snippets) == 1
    snippet = result.code_snippets[0]
    assert snippet.language == "diff"
    assert "+ def isolated():" in snippet.code
    assert snippet.source_ref == "pr_diff:tests/test_state.py"
    assert snippet.truncated is False


async def test_pr_reviews_land_in_facts_as_quotes():
    result = await resolver(router(PR_ROUTES)).resolve(PR_URL)
    assert any("APPROVED" in quote or "APPROVED" in fact for quote in result.quotes + result.facts)


# ------------------------------------------------------------------ failures


async def test_missing_issue_raises_instead_of_faking_content():
    with pytest.raises(SourceResolutionError):
        await resolver(router({})).resolve(ISSUE_URL)


async def test_rate_limit_raises_with_a_readable_message():
    routes = {("GET", "/repos/acme/tool"): (403, {"message": "API rate limit exceeded"})}
    with pytest.raises(SourceResolutionError) as excinfo:
        await resolver(router(routes)).resolve(REPO_URL)
    assert "403" in str(excinfo.value) or "rate limit" in str(excinfo.value).lower()


# --------------------------------------------------------------- discussions


async def test_discussion_without_token_is_low_confidence_and_explained():
    result = await resolver(router({}), token="").resolve(DISCUSSION_URL)
    assert result.source_type is CarouselSourceType.GITHUB_DISCUSSION
    assert result.confidence < 0.5
    assert any("token" in warning.lower() for warning in result.warnings)


async def test_discussion_with_token_uses_graphql():
    payload = {
        "data": {
            "repository": {
                "discussion": {
                    "number": 15,
                    "title": "Как вы храните learnings?",
                    "body": "Обсуждаем rolling history и метрики.",
                    "url": DISCUSSION_URL,
                    "comments": {
                        "nodes": [
                            {
                                "body": "Мы храним последние 100 значений.",
                                "author": {"login": "dev"},
                            }
                        ]
                    },
                }
            }
        }
    }
    routes = {("POST", "/graphql"): (200, payload)}
    result = await resolver(router(routes), token="t").resolve(DISCUSSION_URL)
    assert result.confidence >= 0.5
    assert any("100" in fact for fact in result.facts)
    assert any(fact.source_ref.startswith("discussion:") for fact in result.sourced_facts)


def test_api_base_override_is_used():
    cfg = CarouselGithubSourceConfig(api_base="https://ghe.example.com/api")
    assert cfg.api_base == "https://ghe.example.com/api"


async def test_payload_is_serialisable_for_the_audit_row():
    result = await resolver(router(REPO_ROUTES)).resolve(REPO_URL)
    assert json.loads(result.raw_payload_json)
    assert result.raw_payload_json != "{}"
