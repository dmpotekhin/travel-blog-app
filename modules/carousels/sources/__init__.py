"""Source adapters (Phase 2): URL / GitHub → sourced ``CarouselSourceContext``.

Everything external lives behind :class:`BaseSourceResolver`, so the pipeline
never imports ``httpx`` directly and a Playwright adapter can be added later
without touching the service.
"""

from .base import BaseSourceResolver, ResolveOptions, confidence_from, merge_warnings
from .detect import (
    GithubRef,
    canonicalize_url,
    detect_content_type,
    detect_source_type,
    detect_vertical,
    looks_like_url,
    parse_github_ref,
)
from .github import GitHubSourceResolver
from .mock import MOCK_WARNING, MockSourceResolver
from .registry import github_token_from_env, source_resolver_for
from .url import UrlSourceResolver

__all__ = [
    "BaseSourceResolver",
    "GithubRef",
    "GitHubSourceResolver",
    "MOCK_WARNING",
    "MockSourceResolver",
    "ResolveOptions",
    "UrlSourceResolver",
    "canonicalize_url",
    "confidence_from",
    "detect_content_type",
    "detect_source_type",
    "detect_vertical",
    "github_token_from_env",
    "looks_like_url",
    "merge_warnings",
    "parse_github_ref",
    "source_resolver_for",
]
