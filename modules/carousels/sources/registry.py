"""Resolver registry (Phase 2): pick the adapter for a source type.

This is the single place the rest of the codebase asks for "a resolver that
handles this reference". ``dry_run`` always short-circuits to the mock adapter,
which is why a dry run can never hit the network or invent facts.
"""

import os
from typing import Optional

import httpx

from core.config import CarouselGithubSourceConfig, CarouselSourcesConfig, CarouselUrlSourceConfig
from core.exceptions import SourceResolutionError
from core.models import GITHUB_SOURCE_TYPES, URL_SOURCE_TYPES, CarouselSourceType

from ..enums import resolve_source_type
from .base import BaseSourceResolver
from .github import GitHubSourceResolver
from .mock import MockSourceResolver
from .url import UrlSourceResolver

#: Environment variables consulted for the GitHub token (first non-empty wins).
GITHUB_TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")


def github_token_from_env(environ: Optional[dict] = None) -> str:
    """Read the GitHub token from the environment — never from a file we log."""
    source = environ if environ is not None else os.environ
    for name in GITHUB_TOKEN_ENV_VARS:
        value = str(source.get(name, "") or "").strip()
        if value:
            return value
    return ""


def source_resolver_for(
    source_type,
    config,
    *,
    client: Optional[httpx.AsyncClient] = None,
    token: Optional[str] = None,
    force_mock: bool = False,
    dry_run: bool = False,
) -> BaseSourceResolver:
    """Build the resolver that handles ``source_type``.

    ``dry_run`` (config ``carousels.dry_run`` or the job flag) short-circuits to
    :class:`MockSourceResolver` so nothing leaves the machine.
    """
    kind = resolve_source_type(source_type)
    if force_mock or dry_run or bool(getattr(config, "dry_run", False)):
        return MockSourceResolver(source_type=kind)

    sources = getattr(config, "sources", None) or CarouselSourcesConfig()
    if kind in GITHUB_SOURCE_TYPES:
        settings = getattr(sources, "github", None) or CarouselGithubSourceConfig()
        if not settings.enabled:
            raise SourceResolutionError(
                "GitHub resolver is disabled (carousels.sources.github.enabled = false)"
            )
        return GitHubSourceResolver(
            settings,
            client=client,
            token=github_token_from_env() if token is None else token,
        )
    if kind in URL_SOURCE_TYPES:
        settings = getattr(sources, "url", None) or CarouselUrlSourceConfig()
        if not settings.enabled:
            raise SourceResolutionError(
                "URL resolver is disabled (carousels.sources.url.enabled = false)"
            )
        return UrlSourceResolver(settings, client=client)
    # Manual topics have no remote source: the mock resolver keeps the contract.
    return MockSourceResolver(source_type=kind)


__all__ = [
    "GITHUB_TOKEN_ENV_VARS",
    "github_token_from_env",
    "source_resolver_for",
]
