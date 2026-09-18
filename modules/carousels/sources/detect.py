"""Source detection (Phase 2): what kind of reference is this, and which face?

Pure functions, no I/O — this is what the CLI/API/UI call *before* any network
request, so a wrong guess costs nothing and a manual override always wins.
"""

from typing import Iterable, Optional, Sequence, Union

from pydantic import BaseModel

from core.models import CarouselSourceType, CarouselVertical

from ..enums import resolve_source_type
from .html import canonicalize_url

GITHUB_HOSTS = ("github.com", "www.github.com")
TELEGRAM_HOSTS = ("t.me", "telegram.me", "telegram.org")
ZEN_HOSTS = ("zen.yandex.ru", "dzen.ru", "zen.yandex.com")

TRAVEL_KEYWORDS = (
    "travel",
    "trip",
    "route",
    "itinerary",
    "маршрут",
    "путешеств",
    "поездк",
    "отпуск",
    "туризм",
    "город",
    "city",
    "hotel",
    "hostel",
    "beach",
    "island",
    "остров",
    "китай",
    "билет",
    "гид",
    "guide",
)
QA_KEYWORDS = (
    "flaky",
    "bug",
    "баг",
    "ci",
    "postmortem",
    "post-mortem",
    "regression",
    "incident",
    "инцидент",
    "test",
    "тест",
    "qa",
    "sre",
    "severity",
    "code review",
    "ревью",
    "assert",
)
VIBECODING_KEYWORDS = (
    "ai",
    "llm",
    "agent",
    "агент",
    "prompt",
    "промпт",
    "vibecod",
    "вайбкод",
    "automation",
    "автоматизац",
    "openai",
    "gpt",
    "claude",
    "модель",
    "pipeline",
    "пайплайн",
    "stack",
    "стек",
)

_DOCS_MARKERS = ("/docs", "/documentation", "/reference", "/api-reference", "/guide")
_RELEASE_MARKERS = ("/releases", "/release-notes", "release-notes", "/changelog", "changelog")


class GithubRef(BaseModel):
    """One parsed GitHub reference."""

    source_type: CarouselSourceType
    owner: str
    repo: str
    number: Optional[int] = None
    ref: str = ""
    url: str = ""

    @property
    def slug(self) -> str:
        """``owner/repo`` — what the REST API paths need."""
        return f"{self.owner}/{self.repo}"


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.netloc or "").lower().split(":")[0]


def looks_like_url(value: str) -> bool:
    """True when the string is a link (scheme-prefixed or host-like)."""
    text = (value or "").strip()
    if not text or " " in text:
        return False
    if text.startswith(("http://", "https://")):
        return True
    host = _host_of(text)
    return "." in host


def parse_github_ref(ref: str) -> Optional[GithubRef]:
    """Parse a GitHub URL into its kind/owner/repo/number, or None."""
    text = (ref or "").strip()
    if not text or " " in text:
        return None
    if not text.startswith(("http://", "https://")):
        if not _host_of(text).endswith("github.com"):
            return None
        text = "https://" + text.lstrip("/")
    host = _host_of(text)
    if host not in GITHUB_HOSTS:
        return None
    from urllib.parse import urlparse

    parts = [part for part in urlparse(text).path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    base = GithubRef(
        source_type=CarouselSourceType.GITHUB_REPO,
        owner=owner,
        repo=repo,
        url=f"https://github.com/{owner}/{repo}",
    )
    if len(parts) >= 4:
        section, value = parts[2], parts[3]
        if section == "issues" and value.isdigit():
            return base.model_copy(
                update={"source_type": CarouselSourceType.GITHUB_ISSUE, "number": int(value)}
            )
        if section in {"pull", "pulls"} and value.isdigit():
            return base.model_copy(
                update={"source_type": CarouselSourceType.GITHUB_PR, "number": int(value)}
            )
        if section == "discussions" and value.isdigit():
            return base.model_copy(
                update={
                    "source_type": CarouselSourceType.GITHUB_DISCUSSION,
                    "number": int(value),
                }
            )
        if section == "releases" and value == "tag" and len(parts) >= 5:
            return base.model_copy(
                update={"source_type": CarouselSourceType.GITHUB_RELEASE, "ref": parts[4]}
            )
    return base


def detect_source_type(ref: str) -> CarouselSourceType:
    """Classify a reference without touching the network."""
    text = (ref or "").strip()
    if not text:
        return CarouselSourceType.MANUAL_TOPIC
    github = parse_github_ref(text)
    if github is not None:
        return github.source_type
    if not looks_like_url(text):
        return CarouselSourceType.MANUAL_TOPIC
    host = _host_of(text)
    if any(host == item or host.endswith("." + item) for item in TELEGRAM_HOSTS):
        return CarouselSourceType.TELEGRAM_POST
    if any(host == item or host.endswith("." + item) for item in ZEN_HOSTS):
        return CarouselSourceType.ZEN_POST
    if host == "habr.com" or host.endswith(".habr.com"):
        return CarouselSourceType.ARTICLE_URL
    return CarouselSourceType.URL


def detect_content_type(url: str, title: str = "") -> str:
    """``article`` / ``docs`` / ``release_notes`` / ``social_post``."""
    text = (url or "").lower()
    host = _host_of(url)
    lowered_title = (title or "").lower()
    if any(host == item or host.endswith("." + item) for item in TELEGRAM_HOSTS + ZEN_HOSTS):
        return "social_post"
    if any(marker in text for marker in _RELEASE_MARKERS) or "release notes" in lowered_title:
        return "release_notes"
    if any(marker in text for marker in _DOCS_MARKERS):
        return "docs"
    return "article"


def _hits(haystack: str, keywords: Sequence[str]) -> int:
    return sum(1 for keyword in keywords if keyword in haystack)


def detect_vertical(
    source_type: Union[str, CarouselSourceType],
    *,
    url: str = "",
    title: str = "",
    tags: Iterable[str] = (),
    language: str = "",
) -> CarouselVertical:
    """Pick the face (travel / qa / vibecoding / hybrid) for a source."""
    kind = resolve_source_type(source_type)
    haystack = " ".join([url or "", title or "", " ".join(str(tag) for tag in tags or ())]).lower()

    if kind is CarouselSourceType.GITHUB_ISSUE or kind is CarouselSourceType.GITHUB_PR:
        return CarouselVertical.QA
    if kind is CarouselSourceType.GITHUB_DISCUSSION:
        return CarouselVertical.QA
    if kind is CarouselSourceType.GITHUB_REPO or kind is CarouselSourceType.GITHUB_RELEASE:
        if _hits(haystack, TRAVEL_KEYWORDS) and not _hits(haystack, VIBECODING_KEYWORDS):
            return CarouselVertical.TRAVEL
        return CarouselVertical.VIBECODING

    scores = {
        CarouselVertical.QA: _hits(haystack, QA_KEYWORDS),
        CarouselVertical.VIBECODING: _hits(haystack, VIBECODING_KEYWORDS),
        CarouselVertical.TRAVEL: _hits(haystack, TRAVEL_KEYWORDS),
    }
    best = max(scores.values())
    if best == 0:
        return CarouselVertical.HYBRID
    # Technical first: precision beats beauty for QA / vibecoding sources.
    for candidate in (CarouselVertical.QA, CarouselVertical.VIBECODING, CarouselVertical.TRAVEL):
        if scores[candidate] == best:
            return candidate
    return CarouselVertical.HYBRID  # pragma: no cover - unreachable


__all__ = [
    "GithubRef",
    "canonicalize_url",
    "detect_content_type",
    "detect_source_type",
    "detect_vertical",
    "looks_like_url",
    "parse_github_ref",
]
