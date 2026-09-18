"""GitHub researcher (Phase 2): repo / issue / PR / discussion / release.

Every number and every code block on a QA or vibecoding slide must come from
here — straight out of the REST (or GraphQL) payload — never from a model.

Token handling: the caller passes it in (``carousels.sources.github`` reads
``GITHUB_TOKEN``/``GH_TOKEN`` from the environment). Without a token the public
REST API still works for public repos; discussions need GraphQL, so they come
back flagged as low confidence instead of guessed.
"""

from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from core.config import CarouselGithubSourceConfig
from core.exceptions import SourceResolutionError
from core.models import (
    GITHUB_SOURCE_TYPES,
    CarouselSourceContext,
    CarouselSourceType,
    CarouselVertical,
    CodeSnippet,
    Metric,
    SourceFact,
    dump_json_obj,
)

from .base import BaseSourceResolver, ResolveOptions, confidence_from, merge_warnings
from .detect import GithubRef, detect_vertical, parse_github_ref
from .html import (
    MAX_CODE_CHARS,
    collapse,
    markdown_code_blocks,
    markdown_images,
    split_sentences,
)

DISCUSSION_WITHOUT_TOKEN = (
    "GitHub discussions are only reachable through GraphQL: set GITHUB_TOKEN "
    "and re-research, the source is not readable without it"
)
GRAPHQL_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      title
      body
      url
      comments(first: 50) { nodes { body author { login } } }
    }
  }
}
"""


class GitHubSourceResolver(BaseSourceResolver):
    """Resolve GitHub references into sourced context."""

    name = "github"
    source_types = GITHUB_SOURCE_TYPES

    def __init__(
        self,
        settings: Optional[CarouselGithubSourceConfig] = None,
        *,
        client: Optional[httpx.AsyncClient] = None,
        token: str = "",
    ) -> None:
        self.settings = settings or CarouselGithubSourceConfig()
        self.api_base = self.settings.api_base.rstrip("/")
        self.token = (token or "").strip()
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------- http

    def _client_or_new(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.timeout_seconds, follow_redirects=True
            )
            self._owns_client = True
        return self._client

    def _headers(self, accept: str) -> Dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self.settings.user_agent
            if hasattr(self.settings, "user_agent")
            else "travel-blog-app-carousel/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/vnd.github+json",
        json_body: Optional[dict] = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        client = self._client_or_new()
        try:
            response = await client.request(
                method, url, headers=self._headers(accept), json=json_body
            )
        except httpx.HTTPError as exc:
            raise SourceResolutionError(f"cannot reach GitHub ({path}): {exc}") from exc
        if response.status_code >= 400:
            message = ""
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    message = str(payload.get("message", ""))
            except Exception:  # pragma: no cover - non-JSON error body
                message = response.text[:200]
            raise SourceResolutionError(
                f"GitHub returned {response.status_code} for {path}: {message}".strip()
            )
        return response

    async def _get_json(self, path: str) -> Any:
        response = await self._request("GET", path)
        try:
            return response.json()
        except ValueError as exc:  # pragma: no cover - GitHub always returns JSON
            raise SourceResolutionError(f"GitHub returned invalid JSON for {path}") from exc

    # ---------------------------------------------------------------- resolve

    async def resolve(
        self, source_ref: str, options: Optional[ResolveOptions] = None
    ) -> CarouselSourceContext:
        """Resolve repo / issue / PR / discussion / release references."""
        options = options or ResolveOptions()
        if not self.settings.enabled:
            raise SourceResolutionError(
                "GitHub resolver is disabled (carousels.sources.github.enabled = false)"
            )
        parsed = parse_github_ref(source_ref)
        if parsed is None:
            raise SourceResolutionError(f"not a GitHub reference: {source_ref!r}")

        if parsed.source_type is CarouselSourceType.GITHUB_DISCUSSION:
            return await self._resolve_discussion(parsed, options)
        if parsed.source_type is CarouselSourceType.GITHUB_ISSUE:
            return await self._resolve_issue(parsed, options)
        if parsed.source_type is CarouselSourceType.GITHUB_PR:
            return await self._resolve_pull_request(parsed, options)
        return await self._resolve_repo(parsed, options)

    # ------------------------------------------------------------------- repo

    async def _resolve_repo(
        self, parsed: GithubRef, options: ResolveOptions
    ) -> CarouselSourceContext:
        repo = await self._get_json(f"/repos/{parsed.slug}")
        facts: List[str] = []
        sourced: List[SourceFact] = []
        warnings: List[str] = []
        code: List[CodeSnippet] = []
        images = []
        metrics: List[Metric] = []

        description = collapse(str(repo.get("description") or ""))
        if description:
            facts.append(description)
            sourced.append(
                SourceFact(text=description, source_excerpt=description, source_ref="repo", verified=True)
            )

        if self.settings.include_readme:
            readme = await self._fetch_readme(parsed)
            if readme:
                for line in self._readme_lines(readme):
                    if line in facts:
                        continue
                    facts.append(line)
                    sourced.append(
                        SourceFact(text=line, source_excerpt=line, source_ref="readme", verified=True)
                    )
                code = markdown_code_blocks(readme, base_url=parsed.url)
                for snippet in code:
                    snippet.source_ref = "readme"
                images = markdown_images(readme)
                for image in images:
                    image.license_or_origin = parsed.url
                    image.source_type = "readme"
                    image.is_real_photo = True

        metrics.extend(self._repo_metrics(repo))
        if self.settings.include_readme:
            metrics.extend(await self._language_metrics(parsed))

        if self.settings.include_releases:
            release = await self._latest_release(parsed, warnings)
            if release:
                name = collapse(str(release.get("name") or release.get("tag_name") or ""))
                body = collapse(str(release.get("body") or ""))[:400]
                text = f"release {name}: {body}" if body else f"release {name}"
                if name:
                    facts.append(text)
                    sourced.append(
                        SourceFact(
                            text=text,
                            source_excerpt=text,
                            source_ref=f"release:{release.get('tag_name', '')}",
                            verified=True,
                        )
                    )

        tags = [str(topic) for topic in (repo.get("topics") or [])]
        language = str(repo.get("language") or "")
        title = str(repo.get("full_name") or parsed.slug)
        return CarouselSourceContext(
            source_type=CarouselSourceType.GITHUB_REPO,
            vertical=options.vertical
            or detect_vertical(CarouselSourceType.GITHUB_REPO, url=parsed.url, title=title, tags=tags),
            source_url=str(repo.get("html_url") or parsed.url),
            external_id=title,
            title=title,
            summary=description,
            facts=facts,
            sourced_facts=sourced,
            code_snippets=code,
            metrics=metrics,
            images=images,
            tags=tags,
            content_type="repository",
            language=language,
            confidence=confidence_from(
                title=title,
                facts=len(facts),
                verified_metrics=len(metrics),
                verified_code=len(code),
                language=language,
            ),
            warnings=merge_warnings(warnings, options.extra.get("warnings", [])),
            raw_payload_json=dump_json_obj(
                {
                    "kind": "repo",
                    "full_name": title,
                    "topics": tags,
                    "stargazers_count": repo.get("stargazers_count"),
                    "default_branch": repo.get("default_branch"),
                }
            ),
        )

    async def _fetch_readme(self, parsed: GithubRef) -> str:
        response = await self._request(
            "GET", f"/repos/{parsed.slug}/readme", accept="application/vnd.github.raw"
        )
        return response.text or ""

    def _readme_lines(self, readme: str) -> List[str]:
        out: List[str] = []
        for index, raw in enumerate(readme.splitlines()):
            line = collapse(raw.lstrip("#>-* ").strip())
            if len(line) < 25 or line.startswith("!["):
                continue
            if line not in out:
                out.append(line)
            if len(out) >= 12:
                break
            del index
        return out

    def _repo_metrics(self, repo: Dict[str, Any]) -> List[Metric]:
        specs = (
            ("stargazers", "stargazers_count", "stars"),
            ("forks", "forks_count", "forks"),
            ("open_issues", "open_issues_count", "issues"),
        )
        metrics: List[Metric] = []
        for name, key, unit in specs:
            value = repo.get(key)
            if value is None:
                continue
            metrics.append(
                Metric(
                    name=name,
                    value=float(value),
                    unit=unit,
                    raw_value=str(value),
                    source_excerpt=f"{key}={value}",
                    source_ref="repo",
                    is_verified=True,
                )
            )
        return metrics

    async def _language_metrics(self, parsed: GithubRef) -> List[Metric]:
        payload = await self._get_json(f"/repos/{parsed.slug}/languages")
        metrics: List[Metric] = []
        if isinstance(payload, dict):
            for language, size in list(payload.items())[:5]:
                metrics.append(
                    Metric(
                        name=f"bytes_{str(language).lower()}",
                        value=float(size),
                        unit="bytes",
                        raw_value=str(size),
                        source_excerpt=f"{language}={size}",
                        source_ref="languages",
                        is_verified=True,
                    )
                )
        return metrics

    async def _latest_release(self, parsed: GithubRef, warnings: List[str]) -> Optional[dict]:
        try:
            payload = await self._get_json(f"/repos/{parsed.slug}/releases/latest")
        except SourceResolutionError as exc:
            warnings.append(f"latest release is not available ({exc})")
            return None
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------------ issue

    async def _resolve_issue(
        self, parsed: GithubRef, options: ResolveOptions
    ) -> CarouselSourceContext:
        issue = await self._get_json(f"/repos/{parsed.slug}/issues/{parsed.number}")
        facts, sourced = self._text_facts(
            str(issue.get("title") or ""), str(issue.get("body") or ""), f"issue:{parsed.number}"
        )
        comments: List[dict] = []
        if self.settings.include_issues:
            comments = await self._comments(parsed)
        for index, comment in enumerate(comments):
            for sentence in split_sentences(str(comment.get("body") or ""), min_length=20):
                if sentence in facts:
                    continue
                facts.append(sentence)
                sourced.append(
                    SourceFact(
                        text=sentence,
                        source_excerpt=sentence,
                        source_ref=f"comment:{index}",
                        verified=True,
                    )
                )

        labels = [
            str(label.get("name"))
            for label in (issue.get("labels") or [])
            if isinstance(label, dict) and label.get("name")
        ]
        metrics = self._count_metric("comments", issue.get("comments"), f"issue:{parsed.number}")
        title = f"#{parsed.number} {collapse(str(issue.get('title') or ''))}".strip()
        return CarouselSourceContext(
            source_type=CarouselSourceType.GITHUB_ISSUE,
            vertical=options.vertical or CarouselVertical.QA,
            source_url=str(issue.get("html_url") or parsed.url),
            external_id=f"{parsed.slug}#{parsed.number}",
            title=title,
            summary=collapse(str(issue.get("body") or ""))[:400],
            facts=facts,
            sourced_facts=sourced,
            quotes=[collapse(str(comment.get("body") or "")) for comment in comments[:5]],
            metrics=metrics,
            tags=labels,
            content_type="issue",
            published_at=str(issue.get("created_at") or ""),
            confidence=confidence_from(
                title=title, facts=len(facts), verified_metrics=len(metrics)
            ),
            warnings=merge_warnings(options.extra.get("warnings", [])),
            raw_payload_json=dump_json_obj(
                {
                    "kind": "issue",
                    "number": parsed.number,
                    "state": issue.get("state"),
                    "labels": labels,
                    "comments": issue.get("comments"),
                }
            ),
        )

    # --------------------------------------------------------------------- pr

    async def _resolve_pull_request(
        self, parsed: GithubRef, options: ResolveOptions
    ) -> CarouselSourceContext:
        pull = await self._get_json(f"/repos/{parsed.slug}/pulls/{parsed.number}")
        facts, sourced = self._text_facts(
            str(pull.get("title") or ""), str(pull.get("body") or ""), f"pr:{parsed.number}"
        )
        metrics = [
            self._metric("additions", pull.get("additions"), "lines", "pull"),
            self._metric("deletions", pull.get("deletions"), "lines", "pull"),
            self._metric("changed_files", pull.get("changed_files"), "files", "pull"),
            self._metric("comments", pull.get("comments"), "comments", "pull"),
        ]
        metrics = [metric for metric in metrics if metric is not None]

        code: List[CodeSnippet] = []
        warnings: List[str] = []
        if self.settings.include_prs:
            files = await self._pr_files(parsed)
            for entry in files:
                patch = str(entry.get("patch") or "")
                if not patch.strip():
                    continue
                truncated = len(patch) > MAX_CODE_CHARS
                code.append(
                    CodeSnippet(
                        language="diff",
                        code=patch[:MAX_CODE_CHARS].rstrip() if truncated else patch,
                        caption=f"{entry.get('filename', '')} (+{entry.get('additions', 0)}/-{entry.get('deletions', 0)})",
                        source_excerpt=collapse(patch[:200]),
                        source_ref=f"pr_diff:{entry.get('filename', '')}",
                        truncated=truncated,
                    )
                )
            if any(snippet.truncated for snippet in code):
                warnings.append("a diff hunk was truncated — it must be shown as a fragment")
            quotes = await self._pr_review_quotes(parsed)
        else:
            quotes = []

        merged = bool(pull.get("merged"))
        title = f"PR #{parsed.number}: {collapse(str(pull.get('title') or ''))}".strip()
        return CarouselSourceContext(
            source_type=CarouselSourceType.GITHUB_PR,
            vertical=options.vertical or CarouselVertical.QA,
            source_url=str(pull.get("html_url") or parsed.url),
            external_id=f"{parsed.slug}#{parsed.number}",
            title=title,
            summary=collapse(str(pull.get("body") or ""))[:400],
            facts=facts,
            sourced_facts=sourced,
            quotes=quotes,
            code_snippets=code,
            metrics=metrics,
            tags=["merged"] if merged else [],
            content_type="pull_request",
            published_at=str(pull.get("created_at") or ""),
            confidence=confidence_from(
                title=title,
                facts=len(facts),
                verified_metrics=len(metrics),
                verified_code=len(code),
            ),
            warnings=merge_warnings(warnings, options.extra.get("warnings", [])),
            raw_payload_json=dump_json_obj(
                {
                    "kind": "pull_request",
                    "number": parsed.number,
                    "state": pull.get("state"),
                    "merged": merged,
                    "additions": pull.get("additions"),
                    "deletions": pull.get("deletions"),
                    "changed_files": pull.get("changed_files"),
                }
            ),
        )

    # ------------------------------------------------------------ discussions

    async def _resolve_discussion(
        self, parsed: GithubRef, options: ResolveOptions
    ) -> CarouselSourceContext:
        if not self.token:
            return CarouselSourceContext(
                source_type=CarouselSourceType.GITHUB_DISCUSSION,
                vertical=options.vertical or CarouselVertical.QA,
                source_url=parsed.url,
                external_id=f"{parsed.slug}#{parsed.number}",
                title=f"Discussion #{parsed.number}",
                content_type="discussion",
                confidence=0.0,
                warnings=merge_warnings([DISCUSSION_WITHOUT_TOKEN], options.extra.get("warnings", [])),
                raw_payload_json=dump_json_obj({"kind": "discussion", "fetched": False}),
            )

        payload = await self._request(
            "POST",
            "/graphql",
            json_body={
                "query": GRAPHQL_QUERY,
                "variables": {
                    "owner": parsed.owner,
                    "name": parsed.repo,
                    "number": parsed.number,
                },
            },
        )
        try:
            discussion = (
                payload.json().get("data", {}).get("repository", {}) or {}
            ).get("discussion") or {}
        except ValueError as exc:  # pragma: no cover - GraphQL always answers JSON
            raise SourceResolutionError("GitHub GraphQL returned invalid JSON") from exc
        if not discussion:
            raise SourceResolutionError(
                f"discussion {parsed.slug}#{parsed.number} is not readable with the current token"
            )

        ref = f"discussion:{parsed.number}"
        facts, sourced = self._text_facts(
            str(discussion.get("title") or ""), str(discussion.get("body") or ""), ref
        )
        comments = ((discussion.get("comments") or {}).get("nodes")) or []
        quotes: List[str] = []
        for index, comment in enumerate(comments):
            body = collapse(str(comment.get("body") or ""))
            if body:
                quotes.append(body)
            for sentence in split_sentences(body, min_length=20):
                if sentence in facts:
                    continue
                facts.append(sentence)
                sourced.append(
                    SourceFact(
                        text=sentence,
                        source_excerpt=sentence,
                        source_ref=f"comment:{index}",
                        verified=True,
                    )
                )
        metrics = self._count_metric("comments", len(comments), ref)
        title = f"Discussion: {collapse(str(discussion.get('title') or ''))}".strip()
        return CarouselSourceContext(
            source_type=CarouselSourceType.GITHUB_DISCUSSION,
            vertical=options.vertical or CarouselVertical.QA,
            source_url=str(discussion.get("url") or parsed.url),
            external_id=f"{parsed.slug}#{parsed.number}",
            title=title,
            summary=collapse(str(discussion.get("body") or ""))[:400],
            facts=facts,
            sourced_facts=sourced,
            quotes=quotes,
            metrics=metrics,
            content_type="discussion",
            confidence=confidence_from(
                title=title, facts=len(facts), verified_metrics=len(metrics)
            ),
            warnings=merge_warnings(options.extra.get("warnings", [])),
            raw_payload_json=dump_json_obj(
                {"kind": "discussion", "number": parsed.number, "comments": len(comments)}
            ),
        )

    # ----------------------------------------------------------------- helpers

    async def _comments(self, parsed: GithubRef) -> List[dict]:
        payload = await self._get_json(
            f"/repos/{parsed.slug}/issues/{parsed.number}/comments"
            f"?per_page={int(self.settings.max_comments)}"
        )
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    async def _pr_files(self, parsed: GithubRef) -> List[dict]:
        payload = await self._get_json(f"/repos/{parsed.slug}/pulls/{parsed.number}/files")
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    async def _pr_review_quotes(self, parsed: GithubRef) -> List[str]:
        payload = await self._get_json(f"/repos/{parsed.slug}/pulls/{parsed.number}/reviews")
        quotes: List[str] = []
        if isinstance(payload, list):
            for review in payload:
                if not isinstance(review, dict):
                    continue
                state = collapse(str(review.get("state") or ""))
                body = collapse(str(review.get("body") or ""))
                quotes.append(f"{state}: {body}".strip(": "))
        return quotes

    @staticmethod
    def _text_facts(title: str, body: str, ref: str) -> tuple:
        """Title + body sentences become facts, each pointing at its excerpt."""
        facts: List[str] = []
        sourced: List[SourceFact] = []
        clean_title = collapse(title)
        if clean_title:
            facts.append(clean_title)
            sourced.append(
                SourceFact(
                    text=clean_title,
                    source_excerpt=clean_title,
                    source_ref=ref,
                    verified=True,
                )
            )
        for sentence in split_sentences(collapse(body), min_length=20):
            if sentence in facts:
                continue
            facts.append(sentence)
            sourced.append(
                SourceFact(
                    text=sentence, source_excerpt=sentence, source_ref=ref, verified=True
                )
            )
        return facts, sourced

    @staticmethod
    def _metric(name: str, value: Any, unit: str, ref: str) -> Optional[Metric]:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return Metric(
            name=name,
            value=number,
            unit=unit,
            raw_value=str(value),
            source_excerpt=f"{name}={value}",
            source_ref=ref,
            is_verified=True,
        )

    @classmethod
    def _count_metric(cls, name: str, value: Any, ref: str) -> List[Metric]:
        metric = cls._metric(name, value, name, ref)
        return [metric] if metric is not None else []

    async def aclose(self) -> None:
        """Close the HTTP client when we created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


__all__ = ["GitHubSourceResolver"]
