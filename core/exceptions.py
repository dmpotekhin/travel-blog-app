"""Domain exceptions for the travel blog platform.

All application errors inherit from TravelBlogError so a single except clause
can catch everything. Error categories drive retry/backoff decisions.
"""
from __future__ import annotations

from typing import List, Optional


class TravelBlogError(Exception):
    """Base exception for all application-level errors."""

    def __init__(
        self,
        message: str = "",
        *,
        code: str = "app_error",
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.cause = cause


class ConfigurationError(TravelBlogError):
    """Invalid or missing configuration."""

    def __init__(self, message: str = "", *, cause: Exception | None = None) -> None:
        super().__init__(message, code="config_error", cause=cause)


class DatabaseError(TravelBlogError):
    """A database operation failed."""

    def __init__(self, message: str = "", *, cause: Exception | None = None) -> None:
        super().__init__(message, code="database_error", cause=cause)


class StateTransitionError(TravelBlogError):
    """An illegal state machine transition was attempted."""

    def __init__(self, message: str = "", *, cause: Exception | None = None) -> None:
        super().__init__(message, code="state_transition_error", cause=cause)


class NotFoundError(TravelBlogError):
    """Requested entity does not exist."""

    def __init__(self, message: str = "", *, cause: Exception | None = None) -> None:
        super().__init__(message, code="not_found", cause=cause)


class DuplicateError(TravelBlogError):
    """Attempt to create a record that already exists."""

    def __init__(self, message: str = "", *, cause: Exception | None = None) -> None:
        super().__init__(message, code="duplicate", cause=cause)


class AIProviderError(TravelBlogError):
    """An AI provider call failed."""

    def __init__(
        self,
        message: str = "",
        *,
        cause: Exception | None = None,
        code: str = "ai_error",
    ) -> None:
        super().__init__(message, code=code, cause=cause)


class RateLimitError(AIProviderError):
    """AI provider returned a rate-limit (429) response."""

    def __init__(self, message: str = "", *, cause: Exception | None = None) -> None:
        super().__init__(message, code="rate_limit", cause=cause)


class PublishError(TravelBlogError):
    """A publisher failed. ``permanent`` marks non-retryable failures."""

    def __init__(
        self,
        message: str = "",
        *,
        cause: Exception | None = None,
        code: str = "publish_error",
        permanent: bool = False,
    ) -> None:
        super().__init__(message, code=code, cause=cause)
        self.permanent = permanent


class MediaProcessingError(TravelBlogError):
    """Image/video processing failed or a dependency is unavailable."""

    def __init__(self, message: str = "", *, cause: Exception | None = None) -> None:
        super().__init__(message, code="media_error", cause=cause)


class VibeCodingError(TravelBlogError):
    """VibeCoding generation or publishing failed."""

    def __init__(self, message: str = "", *, cause: Exception | None = None) -> None:
        super().__init__(message, code="vibecoding_error", cause=cause)


class StoryboardValidationError(TravelBlogError):
    """A storyboard cannot be approved/published as it stands (e.g. missing alt-text)."""

    def __init__(
        self,
        message: str = "",
        *,
        issues: Optional[List[str]] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(message, code="storyboard_invalid", cause=cause)
        #: machine-readable reasons (see modules/narrative_heuristics.alt_text_issues)
        self.issues: List[str] = list(issues or [])


class CarouselError(TravelBlogError):
    """Carousel Factory (ADR-107) failed: source, planning, render or verification."""

    def __init__(self, message: str = "", *, cause: Optional[Exception] = None) -> None:
        super().__init__(message, code="carousel_error", cause=cause)


class InsufficientSourceDataError(CarouselError):
    """The source did not carry enough verified facts to build a carousel.

    Raised instead of inventing content: the caller must mark the job
    ``low_confidence`` and ask a human to supply the missing facts.
    """

    def __init__(
        self,
        message: str = "",
        *,
        issues: Optional[List[str]] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.code = "insufficient_source_data"
        #: machine-readable reasons (what is missing)
        self.issues: List[str] = list(issues or [])


class PublishNotApprovedError(CarouselError):
    """Publishing was attempted without a human approval (supervised mode)."""

    def __init__(self, message: str = "", *, cause: Optional[Exception] = None) -> None:
        super().__init__(message, cause=cause)
        self.code = "publish_not_approved"


class ErrorCategory:
    """Error classification for the retry strategy."""

    TEMPORARY = "temporary"
    PERMANENT = "permanent"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    VALIDATION = "validation"
