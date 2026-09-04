"""Domain models: enums, the state machine, and entity models.

The state machine is centralized here so illegal transitions (e.g.
``published -> processing``) are rejected everywhere with a single check.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .exceptions import StateTransitionError


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class Platform(str, Enum):
    FACEBOOK = "facebook"
    VK = "vk"
    TELEGRAM = "telegram"
    ZEN = "zen"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    TRIP_COM = "trip_com"


class CityStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DRAFTED = "drafted"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    ERROR = "error"


class DraftStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"


class PublicationStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"
    DISABLED = "disabled"
    MANUAL = "manual"   # content prepared; a human must complete this platform


class ScanStatus(str, Enum):
    PENDING = "pending"
    SCANNED = "scanned"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"
    FAILED = "failed"
    MISSING = "missing"


class TaskType(str, Enum):
    AI_ANALYSIS = "ai_analysis"
    CONTENT_GENERATION = "content_generation"
    MEDIA_PROCESSING = "media_processing"
    PUBLICATION = "publication"
    RETRY = "retry"


class OperationStatus(str, Enum):
    STARTED = "started"
    SUCCESS = "success"
    FAILED = "failed"


class VibeCodingStatus(str, Enum):
    DRAFT = "draft"
    PENDING = "pending"
    PUBLISHED = "published"
    ERROR = "error"


# --------------------------------------------------------------------------
# State machine: allowed transitions
# --------------------------------------------------------------------------

_CITY_TRANSITIONS: Dict[str, set] = {
    CityStatus.QUEUED: {CityStatus.PROCESSING, CityStatus.ERROR},
    CityStatus.PROCESSING: {CityStatus.DRAFTED, CityStatus.ERROR},
    CityStatus.DRAFTED: {CityStatus.APPROVED, CityStatus.QUEUED, CityStatus.ERROR},
    CityStatus.APPROVED: {CityStatus.PUBLISHING, CityStatus.QUEUED},
    CityStatus.PUBLISHING: {CityStatus.PUBLISHED, CityStatus.ERROR},
    CityStatus.PUBLISHED: set(),          # terminal — no transitions without admin action
    CityStatus.ERROR: {CityStatus.QUEUED},  # recovery
}

_DRAFT_TRANSITIONS: Dict[str, set] = {
    DraftStatus.PENDING: {DraftStatus.APPROVED, DraftStatus.REJECTED, DraftStatus.PUBLISHED},
    DraftStatus.APPROVED: {DraftStatus.PUBLISHED, DraftStatus.PENDING},
    DraftStatus.REJECTED: {DraftStatus.PENDING},
    DraftStatus.PUBLISHED: set(),          # terminal
}

_PUBLICATION_TRANSITIONS: Dict[str, set] = {
    PublicationStatus.PENDING: {PublicationStatus.PROCESSING, PublicationStatus.FAILED, PublicationStatus.DISABLED},
    PublicationStatus.PROCESSING: {PublicationStatus.SCHEDULED, PublicationStatus.PUBLISHED, PublicationStatus.FAILED, PublicationStatus.MANUAL},
    PublicationStatus.SCHEDULED: {PublicationStatus.PROCESSING, PublicationStatus.PUBLISHED, PublicationStatus.FAILED},
    PublicationStatus.PUBLISHED: set(),    # terminal — idempotency guard
    PublicationStatus.FAILED: {PublicationStatus.PENDING, PublicationStatus.DISABLED},
    PublicationStatus.DISABLED: set(),
    # A human completes a manual platform (zen/instagram/youtube/trip_com):
    # the prepared content is finished and the post is marked as published.
    PublicationStatus.MANUAL: {PublicationStatus.PUBLISHED, PublicationStatus.DISABLED},
}

_VIBECODING_TRANSITIONS: Dict[str, set] = {
    VibeCodingStatus.DRAFT: {VibeCodingStatus.PENDING, VibeCodingStatus.PUBLISHED, VibeCodingStatus.ERROR},
    VibeCodingStatus.PENDING: {VibeCodingStatus.PUBLISHED, VibeCodingStatus.ERROR, VibeCodingStatus.DRAFT},
    VibeCodingStatus.PUBLISHED: set(),    # terminal
    VibeCodingStatus.ERROR: {VibeCodingStatus.DRAFT, VibeCodingStatus.PENDING},
}


def validate_transition(current: str, target: str, table: Dict[str, set]) -> bool:
    """Return True if ``current -> target`` is a legal transition."""
    allowed = table.get(current, set())
    return target in allowed


def check_transition(current: str, target: str, table: Dict[str, set], entity: str = "entity") -> None:
    """Raise ``StateTransitionError`` on an illegal transition."""
    if not validate_transition(current, target, table):
        raise StateTransitionError(
            f"Illegal {entity} transition: {current!r} -> {target!r}"
        )


def can_transition(current: str, table: Dict[str, set]) -> set:
    """Return the set of legal next states for ``current``."""
    return set(table.get(current, set()))


def city_transition(current: str, target: str) -> None:
    """Validate a city state transition via the centralized state machine."""
    check_transition(current, target, _CITY_TRANSITIONS, "city")


def draft_transition(current: str, target: str) -> None:
    """Validate a draft state transition via the centralized state machine."""
    check_transition(current, target, _DRAFT_TRANSITIONS, "draft")


def vibecoding_transition(current: str, target: str) -> None:
    """Validate a VibeCoding post state transition via the state machine."""
    check_transition(current, target, _VIBECODING_TRANSITIONS, "vibecoding_post")


# --------------------------------------------------------------------------
# Entity models
# --------------------------------------------------------------------------


class City(BaseModel):
    id: Optional[int] = None
    name: str = Field(..., max_length=200)
    country: str = Field(default="", max_length=100)
    year: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    folder_path: str = ""
    status: CityStatus = CityStatus.QUEUED
    priority: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Photo(BaseModel):
    id: Optional[int] = None
    city_id: Optional[int] = None
    path: str
    filename: str = ""
    size: int = 0
    modified_at: Optional[datetime] = None
    sha256: str = ""
    taken_at: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    country: str = ""
    city: str = ""
    year: Optional[int] = None
    scan_status: ScanStatus = ScanStatus.PENDING
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Draft(BaseModel):
    id: Optional[int] = None
    city_id: int
    platform: str
    title: str = ""
    content: str = ""
    photos_json: str = "[]"
    status: DraftStatus = DraftStatus.PENDING
    content_version: int = 1
    ai_provider: str = ""
    ai_model: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Publication(BaseModel):
    id: Optional[int] = None
    city_id: int
    platform: str
    external_id: str = ""
    url: str = ""
    status: PublicationStatus = PublicationStatus.PENDING
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    content_version: int = 1
    error_message: str = ""
    retry_count: int = 0
    created_at: Optional[datetime] = None


class VibeCodingPost(BaseModel):
    id: Optional[int] = None
    title: str = ""
    topic: str = ""
    prompt_text: str = ""
    image_prompt: str = ""
    generated_text: str = ""
    image_url: str = ""
    status: VibeCodingStatus = VibeCodingStatus.DRAFT
    platform_status: str = "{}"
    created_at: Optional[datetime] = None
    published_at: Optional[datetime] = None


class PendingTask(BaseModel):
    id: Optional[int] = None
    city_id: Optional[int] = None
    task_type: str
    payload_json: str = "{}"
    status: str = "pending"
    retry_count: int = 0
    next_attempt_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class GeminiStats(BaseModel):
    id: Optional[int] = None
    date: str
    requests_count: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    rate_limit_errors: int = 0
    server_errors: int = 0


class AICacheEntry(BaseModel):
    id: Optional[int] = None
    image_hash: str
    provider: str
    model: str
    prompt_hash: str
    response: str = ""
    created_at: Optional[datetime] = None


class OperationLog(BaseModel):
    id: Optional[int] = None
    correlation_id: str = ""
    city_id: Optional[int] = None
    task_id: Optional[int] = None
    operation: str
    start_time: datetime
    end_time: Optional[datetime] = None
    duration: Optional[float] = None
    status: str = "started"
    error_message: str = ""


# --------------------------------------------------------------------------
# Content pipeline value objects
# --------------------------------------------------------------------------


class SelectedPhoto(BaseModel):
    path: str
    caption: Optional[str] = None


class ContentPack(BaseModel):
    """A fully prepared, platform-specific publication payload for one city."""

    city_id: int
    city: str
    country: str
    year: Optional[int] = None
    platform: str
    title: str
    text: str
    photos: List[str] = Field(default_factory=list)
    video_paths: List[str] = Field(default_factory=list)
    hashtags: List[str] = Field(default_factory=list)
    content_version: int = 1


class BaseStory(BaseModel):
    """The factual base travel story before platform adaptation."""

    city: str
    country: str
    year: Optional[int] = None
    facts: List[str] = Field(default_factory=list)
    observations: List[str] = Field(default_factory=list)
    selected_photos: List[str] = Field(default_factory=list)
