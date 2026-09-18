"""Domain models: enums, the state machine, and entity models.

The state machine is centralized here so illegal transitions (e.g.
``published -> processing``) are rejected everywhere with a single check.
"""
from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Dict, Iterable, List, Optional

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


class NarrativeBeatType(str, Enum):
    """Beat types of the visual narrative arc (Visual Narrative Studio)."""

    SETUP = "setup"
    CONFLICT = "conflict"
    DEVELOPMENT = "development"
    CLIMAX = "climax"
    RESOLUTION = "resolution"
    REFLECTION = "reflection"


class StoryboardStatus(str, Enum):
    """Lifecycle of a storyboard (own machine, independent of CityStatus)."""

    DRAFT = "draft"
    APPROVED = "approved"
    ARCHIVED = "archived"


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


# A storyboard is the visual narrative that sits between photo analysis and
# platform content. ``draft`` is the studio output; only a human approves it;
# ``archived`` is terminal — a new version is a new row, never an edit.
_STORYBOARD_TRANSITIONS: Dict[str, set] = {
    StoryboardStatus.DRAFT: {StoryboardStatus.APPROVED, StoryboardStatus.ARCHIVED},
    # approved storyboards stay editable: an edit demotes them back to draft.
    StoryboardStatus.APPROVED: {StoryboardStatus.DRAFT, StoryboardStatus.ARCHIVED},
    StoryboardStatus.ARCHIVED: set(),
}


def storyboard_transition(current: str, target: str) -> None:
    """Validate a storyboard state transition via the centralized state machine."""
    check_transition(current, target, _STORYBOARD_TRANSITIONS, "storyboard")


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


# --------------------------------------------------------------------------
# Visual Narrative Studio (ADR-106): the visual layer that sits between photo
# analysis and platform content. Beats/shots/storyboard are persisted rows;
# ``VisualNarrativePlan`` is the (unpersisted) plan, the last two are read models.
# --------------------------------------------------------------------------


def dump_json_list(items: Iterable[str]) -> str:
    """JSON-encode a list of strings the way the DB columns expect it."""
    return json.dumps([str(item) for item in items], ensure_ascii=False)


def load_json_list(raw: Optional[str]) -> List[str]:
    """Tolerant JSON list decoding — a bad payload must never crash the UI."""
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if isinstance(data, list):
        return [str(item) for item in data]
    return []


class NarrativePhoto(BaseModel):
    """One analysed photograph reduced to the facts the narrative may use."""

    path: str
    filename: str = ""
    scene: str = ""
    objects: List[str] = Field(default_factory=list)
    mood: str = ""
    text: str = ""
    quality_ok: bool = True
    taken_at: Optional[str] = None

    @property
    def stem(self) -> str:
        name = self.filename or self.path.rsplit("/", 1)[-1]
        return name.rsplit(".", 1)[0]


class NarrativeContext(BaseModel):
    """Input of the visual-narrative step: facts only, no free invention."""

    city_id: int
    city: str
    country: str = ""
    year: Optional[int] = None
    language: str = "ru"
    photos: List[NarrativePhoto] = Field(default_factory=list)
    base_story: str = ""
    target_platforms: List[str] = Field(default_factory=list)


class NarrativeBeat(BaseModel):
    """One beat of the narrative arc (setup -> ... -> reflection)."""

    id: Optional[int] = None
    storyboard_id: Optional[int] = None
    beat_type: NarrativeBeatType = NarrativeBeatType.SETUP
    order: int = 0
    title: str = ""
    description: str = ""
    emotional_tone: str = ""
    visual_goal: str = ""
    photo_paths_json: str = "[]"

    @property
    def photo_paths(self) -> List[str]:
        return load_json_list(self.photo_paths_json)

    def set_photo_paths(self, paths: Iterable[str]) -> NarrativeBeat:
        self.photo_paths_json = dump_json_list(paths)
        return self


class StoryboardShot(BaseModel):
    """One frame: a photograph plus how it is presented to the reader."""

    id: Optional[int] = None
    storyboard_id: Optional[int] = None
    photo_path: str
    order: int = 0
    caption: str = ""
    alt_text: str = ""
    crop_recommendation: str = ""
    focus_point: str = ""
    visual_metaphor: str = ""
    pacing_weight: float = 1.0
    is_hero_image: bool = False


class Storyboard(BaseModel):
    """The persisted visual narrative of one city (one row per version)."""

    id: Optional[int] = None
    city_id: int
    title: str = ""
    logline: str = ""
    narrative_arc: str = ""
    emotional_journey: str = ""
    primary_theme: str = ""
    secondary_themes_json: str = "[]"
    target_platforms_json: str = "[]"
    accessibility_notes_json: str = "[]"
    cultural_sensitivity_notes_json: str = "[]"
    status: StoryboardStatus = StoryboardStatus.DRAFT
    version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def secondary_themes(self) -> List[str]:
        return load_json_list(self.secondary_themes_json)

    @property
    def target_platforms(self) -> List[str]:
        return load_json_list(self.target_platforms_json)

    @property
    def accessibility_notes(self) -> List[str]:
        return load_json_list(self.accessibility_notes_json)

    @property
    def cultural_sensitivity_notes(self) -> List[str]:
        return load_json_list(self.cultural_sensitivity_notes_json)


class VisualNarrativePlan(BaseModel):
    """Unpersisted plan produced by a provider (or by the local heuristics)."""

    city_id: int
    selected_photos: List[str] = Field(default_factory=list)
    beats: List[NarrativeBeat] = Field(default_factory=list)
    shots: List[StoryboardShot] = Field(default_factory=list)
    hook: str = ""
    climax: str = ""
    ending: str = ""
    accessibility_notes: List[str] = Field(default_factory=list)
    cultural_sensitivity_notes: List[str] = Field(default_factory=list)
    # Narrative header — persisted onto the storyboard row.
    title: str = ""
    logline: str = ""
    narrative_arc: str = ""
    emotional_journey: str = ""
    primary_theme: str = ""
    secondary_themes: List[str] = Field(default_factory=list)
    # Provenance / honesty: a plan always says who produced it and how.
    provider: str = ""
    model: str = ""
    degraded: bool = False
    degradation_reason: str = ""
    dry_run: bool = False
    raw: str = ""


class StoryboardBundle(BaseModel):
    """Read model: a storyboard with its beats, shots and validation issues."""

    storyboard: Storyboard
    beats: List[NarrativeBeat] = Field(default_factory=list)
    shots: List[StoryboardShot] = Field(default_factory=list)
    issues: List[str] = Field(default_factory=list)


class VisualNarrativeResult(BaseModel):
    """Typed outcome of one Visual Narrative Studio run."""

    city_id: int
    storyboard: Storyboard
    plan: VisualNarrativePlan
    created: bool = True
    warnings: List[str] = Field(default_factory=list)
    dry_run: bool = False


class BaseStory(BaseModel):
    """The factual base travel story before platform adaptation."""

    city: str
    country: str
    year: Optional[int] = None
    facts: List[str] = Field(default_factory=list)
    observations: List[str] = Field(default_factory=list)
    selected_photos: List[str] = Field(default_factory=list)
