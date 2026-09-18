"""Domain models: enums, the state machine, and entity models.

The state machine is centralized here so illegal transitions (e.g.
``published -> processing``) are rejected everywhere with a single check.
"""
from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Dict, Iterable, List, Optional

from pydantic import BaseModel, Field, field_validator

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
# Carousel Factory (ADR-107): three verticals (travel / qa / vibecoding) that
# all go SOURCE -> RESEARCH -> HOOKS -> SLIDES -> RENDER -> VERIFY -> APPROVE ->
# PUBLISH -> ANALYTICS -> LEARNINGS. URLs and GitHub refs are the primary
# entry points. The enums live here (not in modules/carousels) because
# core/database.py must know the entities it persists and must never import
# from modules/* (import cycle) — same ruling as the storyboard models.
# --------------------------------------------------------------------------


class CarouselVertical(str, Enum):
    """The three faces of the factory; ``hybrid`` is the honest fallback."""

    TRAVEL = "travel"
    QA = "qa"
    VIBECODING = "vibecoding"
    HYBRID = "hybrid"


class CarouselSourceType(str, Enum):
    """Where a carousel comes from (URL + GitHub are the priority inputs)."""

    URL = "url"
    ARTICLE_URL = "article_url"
    TELEGRAM_POST = "telegram_post"
    ZEN_POST = "zen_post"
    GITHUB_REPO = "github_repo"
    GITHUB_ISSUE = "github_issue"
    GITHUB_PR = "github_pr"
    GITHUB_DISCUSSION = "github_discussion"
    GITHUB_RELEASE = "github_release"
    CITY = "city"
    PHOTO_ARCHIVE = "photo_archive"
    TEST_REPORT = "test_report"
    CODE_SNIPPET = "code_snippet"
    QA_ARTIFACT = "qa_artifact"
    VIBECODING_POST = "vibecoding_post"
    VIBECODING_SESSION = "vibecoding_session"
    MANUAL_TOPIC = "manual_topic"


#: Source types that resolve through the GitHub researcher.
GITHUB_SOURCE_TYPES: frozenset = frozenset(
    {
        CarouselSourceType.GITHUB_REPO,
        CarouselSourceType.GITHUB_ISSUE,
        CarouselSourceType.GITHUB_PR,
        CarouselSourceType.GITHUB_DISCUSSION,
        CarouselSourceType.GITHUB_RELEASE,
    }
)

#: Source types that resolve through the URL researcher.
URL_SOURCE_TYPES: frozenset = frozenset(
    {
        CarouselSourceType.URL,
        CarouselSourceType.ARTICLE_URL,
        CarouselSourceType.TELEGRAM_POST,
        CarouselSourceType.ZEN_POST,
    }
)


class CarouselStatus(str, Enum):
    """Lifecycle of one carousel job (the factory's own state machine)."""

    PENDING = "pending"
    RESEARCHING = "researching"
    RESEARCHED = "researched"
    NARRATIVE_DRAFTED = "narrative_drafted"
    SLIDES_PLANNED = "slides_planned"
    RENDERING = "rendering"
    RENDERED = "rendered"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    NEEDS_REVISION = "needs_revision"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    FAILED = "failed"


class CarouselAutonomyMode(str, Enum):
    """``supervised`` (default) never publishes without a human approval."""

    SUPERVISED = "supervised"
    FULL_AUTONOMOUS = "full_autonomous"


class CarouselVerificationStatus(str, Enum):
    """Outcome of the per-slide verification pass."""

    PENDING = "pending"
    PASSED = "passed"
    NEEDS_REVISION = "needs_revision"
    FAILED = "failed"


class SlideType(str, Enum):
    """Deterministic slide formats (see modules/carousels/templates)."""

    HERO_HOOK = "hero_hook"
    PROBLEM = "problem"
    AGITATION = "agitation"
    SOLUTION = "solution"
    PROOF = "proof"
    FEATURE = "feature"
    CTA = "cta"
    SYMPTOM = "symptom"
    INVESTIGATION = "investigation"
    FIX_CODE = "fix_code"
    PREVENTION_CHECKLIST = "prevention_checklist"
    BOLD_CLAIM = "bold_claim"
    TASK_CONTEXT = "task_context"
    OLD_WAY = "old_way"
    NEW_WAY = "new_way"
    METRICS_COMPARISON = "metrics_comparison"
    ARCHITECTURE_DIAGRAM = "architecture_diagram"
    TERMINAL_BLOCK = "terminal_block"
    CODE_BLOCK = "code_block"
    CHECKLIST = "checklist"
    QUOTE_CARD = "quote_card"
    PHOTO_CARD = "photo_card"
    ROUTE_MAP = "route_map"
    CULTURAL_NOTE = "cultural_note"


#: Friendly names used in narrative plans / early specs -> canonical SlideType.
SLIDE_TYPE_ALIASES: Dict[str, str] = {
    "hero": SlideType.HERO_HOOK.value,
    "hook": SlideType.HERO_HOOK.value,
    "symptom_list": SlideType.SYMPTOM.value,
    "symptoms": SlideType.SYMPTOM.value,
    "roots_cause": SlideType.INVESTIGATION.value,
    "root_cause": SlideType.INVESTIGATION.value,
    "code_fix": SlideType.FIX_CODE.value,
    "fix": SlideType.FIX_CODE.value,
    "prevention": SlideType.PREVENTION_CHECKLIST.value,
    "metrics": SlideType.METRICS_COMPARISON.value,
    "architecture": SlideType.ARCHITECTURE_DIAGRAM.value,
    "terminal": SlideType.TERMINAL_BLOCK.value,
    "code": SlideType.CODE_BLOCK.value,
    "photo": SlideType.PHOTO_CARD.value,
    "quote": SlideType.QUOTE_CARD.value,
    "route": SlideType.ROUTE_MAP.value,
}


def normalize_slide_type(value: str) -> str:
    """Map a slide-type name (canonical, alias or spaced) onto a canonical value."""
    raw = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in SLIDE_TYPE_ALIASES:
        return SLIDE_TYPE_ALIASES[raw]
    try:
        return SlideType(raw).value
    except ValueError:
        return raw


class HookCategory(str, Enum):
    """Hook families per vertical (travel / qa / vibecoding)."""

    # travel
    HIDDEN_PLACE = "hidden_place"
    EXPECTATION_VS_REALITY = "expectation_vs_reality"
    BUDGET = "budget"
    SOLO_TRAVEL = "solo_travel"
    CULTURAL_CONTRAST = "cultural_contrast"
    ROUTE_GUIDE = "route_guide"
    PERSONAL_EXPERIENCE = "personal_experience"
    SECRET_SPOT = "secret_spot"
    SEASONALITY = "seasonality"
    # qa
    FAILURE = "failure"
    CONTRARIAN = "contrarian"
    NUMBER_LIST = "number_list"
    MYSTERY = "mystery"
    COST = "cost"
    BEFORE_AFTER = "before_after"
    MYTH_BUSTING = "myth_busting"
    FLAKY_TEST = "flaky_test"
    CI_SLOWDOWN = "ci_slowdown"
    ROOT_CAUSE = "root_cause"
    # vibecoding
    SPEED = "speed"
    LOCAL_AI = "local_ai"
    AGENT_WORKFLOW = "agent_workflow"
    PROMPT_TO_PRODUCT = "prompt_to_product"
    EXPERIMENT = "experiment"
    ANTI_PATTERN = "anti_pattern"
    STACK_TOUR = "stack_tour"
    AI_VS_HUMAN = "ai_vs_human"
    EVENING_BUILD = "evening_build"
    TOOL_COMPARISON = "tool_comparison"


TRAVEL_HOOK_CATEGORIES: tuple = (
    HookCategory.HIDDEN_PLACE,
    HookCategory.EXPECTATION_VS_REALITY,
    HookCategory.BUDGET,
    HookCategory.SOLO_TRAVEL,
    HookCategory.CULTURAL_CONTRAST,
    HookCategory.ROUTE_GUIDE,
    HookCategory.PERSONAL_EXPERIENCE,
    HookCategory.SECRET_SPOT,
    HookCategory.SEASONALITY,
)

QA_HOOK_CATEGORIES: tuple = (
    HookCategory.FAILURE,
    HookCategory.CONTRARIAN,
    HookCategory.NUMBER_LIST,
    HookCategory.MYSTERY,
    HookCategory.COST,
    HookCategory.BEFORE_AFTER,
    HookCategory.MYTH_BUSTING,
    HookCategory.FLAKY_TEST,
    HookCategory.CI_SLOWDOWN,
    HookCategory.ROOT_CAUSE,
)

VIBECODING_HOOK_CATEGORIES: tuple = (
    HookCategory.SPEED,
    HookCategory.LOCAL_AI,
    HookCategory.AGENT_WORKFLOW,
    HookCategory.PROMPT_TO_PRODUCT,
    HookCategory.EXPERIMENT,
    HookCategory.ANTI_PATTERN,
    HookCategory.STACK_TOUR,
    HookCategory.AI_VS_HUMAN,
    HookCategory.EVENING_BUILD,
    HookCategory.TOOL_COMPARISON,
)

#: Which hook families belong to which vertical (planner + hook engine use it).
HOOK_CATEGORIES_BY_VERTICAL: Dict[CarouselVertical, tuple] = {
    CarouselVertical.TRAVEL: TRAVEL_HOOK_CATEGORIES,
    CarouselVertical.QA: QA_HOOK_CATEGORIES,
    CarouselVertical.VIBECODING: VIBECODING_HOOK_CATEGORIES,
    CarouselVertical.HYBRID: (
        HookCategory.NUMBER_LIST,
        HookCategory.BEFORE_AFTER,
        HookCategory.PERSONAL_EXPERIENCE,
        HookCategory.MYSTERY,
    ),
}


class CarouselLearningScope(str, Enum):
    """What a learning row is aggregated by."""

    VERTICAL = "vertical"
    SOURCE_TYPE = "source_type"
    HOOK_CATEGORY = "hook_category"
    SLIDE_FORMAT = "slide_format"
    VISUAL_STYLE = "visual_style"
    PLATFORM = "platform"
    POSTING_TIME = "posting_time"
    DAY_OF_WEEK = "day_of_week"
    TOPIC_TAG = "topic_tag"


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


# Carousel jobs traverse research -> hooks/narrative -> slides -> render ->
# verify -> (human) approval -> publish -> analytics. No status may ever be
# written directly: every move goes through ``carousel_transition``.
_CAROUSEL_TRANSITIONS: Dict[str, set] = {
    CarouselStatus.PENDING: {CarouselStatus.RESEARCHING, CarouselStatus.FAILED},
    CarouselStatus.RESEARCHING: {CarouselStatus.RESEARCHED, CarouselStatus.FAILED},
    CarouselStatus.RESEARCHED: {CarouselStatus.NARRATIVE_DRAFTED, CarouselStatus.FAILED},
    CarouselStatus.NARRATIVE_DRAFTED: {
        CarouselStatus.SLIDES_PLANNED,
        CarouselStatus.AWAITING_APPROVAL,
        CarouselStatus.FAILED,
    },
    CarouselStatus.SLIDES_PLANNED: {CarouselStatus.RENDERING, CarouselStatus.FAILED},
    CarouselStatus.RENDERING: {CarouselStatus.RENDERED, CarouselStatus.FAILED},
    CarouselStatus.RENDERED: {CarouselStatus.VERIFYING, CarouselStatus.FAILED},
    CarouselStatus.VERIFYING: {
        CarouselStatus.VERIFIED,
        CarouselStatus.NEEDS_REVISION,
        CarouselStatus.FAILED,
    },
    CarouselStatus.NEEDS_REVISION: {
        CarouselStatus.RENDERING,
        CarouselStatus.SLIDES_PLANNED,
        CarouselStatus.AWAITING_APPROVAL,
        CarouselStatus.FAILED,
    },
    CarouselStatus.VERIFIED: {CarouselStatus.AWAITING_APPROVAL, CarouselStatus.APPROVED},
    CarouselStatus.AWAITING_APPROVAL: {
        CarouselStatus.APPROVED,
        CarouselStatus.NEEDS_REVISION,
        CarouselStatus.FAILED,
    },
    CarouselStatus.APPROVED: {CarouselStatus.PUBLISHING, CarouselStatus.FAILED},
    CarouselStatus.PUBLISHING: {CarouselStatus.PUBLISHED, CarouselStatus.FAILED},
    CarouselStatus.PUBLISHED: {CarouselStatus.ANALYZING},
    CarouselStatus.ANALYZING: {CarouselStatus.ANALYZED, CarouselStatus.FAILED},
    CarouselStatus.FAILED: {CarouselStatus.PENDING, CarouselStatus.RESEARCHING},
}


def carousel_transition(current: str, target: str) -> None:
    """Validate a carousel-job state transition (raises StateTransitionError)."""
    check_transition(current, target, _CAROUSEL_TRANSITIONS, "carousel_job")


def carousel_publication_transition(current: str, target: str) -> None:
    """Validate a carousel publication transition.

    A carousel publication is the same kind of row as a city publication
    (``pending -> processing -> published/failed/manual``), so it reuses the
    shared publication table instead of introducing a duplicate state machine.
    """
    check_transition(current, target, _PUBLICATION_TRANSITIONS, "carousel_publication")


def carousel_next_states(current: str) -> set:
    """Legal next statuses of a carousel job (UI/CLI hints, guards)."""
    return can_transition(current, _CAROUSEL_TRANSITIONS)


def carousel_transition_table() -> Dict[str, set]:
    """Copy of the carousel state machine (docs, UI, tests; never mutated)."""
    return {current: set(targets) for current, targets in _CAROUSEL_TRANSITIONS.items()}


#: Statuses in which a carousel may be published. Everything else must first
#: pass verification and a human approval (supervised mode is the default).
CAROUSEL_PUBLISHABLE_STATUSES: frozenset = frozenset({CarouselStatus.APPROVED})


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


# --------------------------------------------------------------------------
# Carousel Factory (ADR-107): entities. Every factual bullet/metric/code block
# keeps a source excerpt so the fact-guard can prove it came from the source
# instead of from the model's imagination.
# --------------------------------------------------------------------------


def dump_json_obj(value: object) -> str:
    """JSON-encode any payload for a ``*_json`` TEXT column."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return "null"


def load_json_obj(raw: Optional[str], default: Optional[object] = None) -> Optional[object]:
    """Tolerant JSON decoding of a ``*_json`` column (never raises)."""
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class SourceFact(BaseModel):
    """One fact extracted from the source, with its provenance."""

    text: str
    source_excerpt: str = ""
    source_ref: str = ""
    #: True only when the fact was read straight out of the source text.
    verified: bool = False
    #: True when the fact is an inference the carousel must not present as fact.
    is_hypothesis: bool = False


class CodeSnippet(BaseModel):
    """A code block lifted verbatim from the source (never model-generated)."""

    language: str = ""
    code: str = ""
    caption: str = ""
    source_excerpt: str = ""
    source_ref: str = ""
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    #: True when the block was cut because it did not fit — must be shown as
    #: "фрагмент" on the slide instead of silently misrepresenting the source.
    truncated: bool = False


class Metric(BaseModel):
    """A measured value with the excerpt it was read from."""

    name: str
    value: float = 0.0
    unit: str = ""
    source_excerpt: str = ""
    source_ref: str = ""
    is_verified: bool = False
    #: Raw string as written in the source ("2 дня", "20 → 4 мин").
    raw_value: str = ""


class ImageAsset(BaseModel):
    """An image the carousel may use, with its origin and license."""

    url_or_path: str
    alt_text: str = ""
    width: int = 0
    height: int = 0
    source_type: str = ""
    license_or_origin: str = ""
    is_real_photo: bool = False
    confidence: float = 0.0


class CarouselSourceContext(BaseModel):
    """Everything research learned about one source, plus how sure it is.

    ``facts``/``quotes`` stay plain strings (they are what the UI shows);
    ``sourced_facts`` carries the same facts with excerpts and refs so the
    fact-guard can verify each claim instead of trusting the model.
    """

    id: Optional[int] = None
    source_type: CarouselSourceType = CarouselSourceType.MANUAL_TOPIC
    vertical: CarouselVertical = CarouselVertical.HYBRID
    source_url: str = ""
    external_id: str = ""
    title: str = ""
    summary: str = ""
    facts: List[str] = Field(default_factory=list)
    sourced_facts: List[SourceFact] = Field(default_factory=list)
    quotes: List[str] = Field(default_factory=list)
    code_snippets: List[CodeSnippet] = Field(default_factory=list)
    metrics: List[Metric] = Field(default_factory=list)
    images: List[ImageAsset] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    brand_colors: List[str] = Field(default_factory=list)
    competitors: List[str] = Field(default_factory=list)
    content_type: str = ""
    language: str = ""
    author: str = ""
    published_at: str = ""
    confidence: float = 0.0
    warnings: List[str] = Field(default_factory=list)
    #: Canonical (query/fragment-free) URL of the source — the dedupe key.
    canonical_url: str = ""
    raw_payload_json: str = "{}"
    created_at: Optional[datetime] = None

    @property
    def is_low_confidence(self) -> bool:
        """True when the source cannot support an honest carousel yet."""
        return self.confidence < 0.5 or not self.facts

    def fact_texts(self) -> List[str]:
        """Every claim the narrative may build on (facts first, then excerpts)."""
        out = list(self.facts)
        for fact in self.sourced_facts:
            if fact.text and fact.text not in out:
                out.append(fact.text)
        return out


class CarouselJob(BaseModel):
    """One carousel run: source -> narrative -> slides -> render -> publish."""

    id: Optional[int] = None
    vertical: CarouselVertical = CarouselVertical.HYBRID
    source_type: CarouselSourceType = CarouselSourceType.MANUAL_TOPIC
    source_id: Optional[int] = None
    source_url: str = ""
    title: str = ""
    logline: str = ""
    status: CarouselStatus = CarouselStatus.PENDING
    autonomy_mode: CarouselAutonomyMode = CarouselAutonomyMode.SUPERVISED
    selected_hook_id: Optional[int] = None
    narrative_template: str = ""
    target_platforms_json: str = "[]"
    caption: str = ""
    hashtags_json: str = "[]"
    source_context_json: str = "{}"
    confidence: float = 0.0
    error_message: str = ""
    warnings_json: str = "[]"
    dry_run: bool = False
    #: Audit trail of the human approval gate (spec §13): who approved and when.
    approved_by: str = ""
    approved_at: Optional[datetime] = None
    created_by: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def target_platforms(self) -> List[str]:
        return load_json_list(self.target_platforms_json)

    @property
    def hashtags(self) -> List[str]:
        return load_json_list(self.hashtags_json)

    @property
    def warnings(self) -> List[str]:
        return load_json_list(self.warnings_json)

    def source_context(self) -> Optional[CarouselSourceContext]:
        """Decode the stored source context (None when there is none yet)."""
        payload = load_json_obj(self.source_context_json, None)
        if not isinstance(payload, dict) or not payload:
            return None
        return CarouselSourceContext.model_validate(payload)

    @property
    def requires_approval(self) -> bool:
        """Supervised jobs always need a human before publishing."""
        return self.autonomy_mode != CarouselAutonomyMode.FULL_AUTONOMOUS


class CarouselSlide(BaseModel):
    """One of the six 768x1376 slides of a carousel."""

    id: Optional[int] = None
    job_id: Optional[int] = None
    order: int = 0
    slide_type: SlideType = SlideType.HERO_HOOK
    headline: str = ""
    subheadline: str = ""
    body_text: str = ""
    bullets_json: str = "[]"

    code_json: str = "{}"
    metrics_json: str = "[]"
    image_asset_json: str = "{}"
    source_refs_json: str = "[]"
    background_prompt: str = ""
    background_image_path: str = ""
    background_style: str = ""
    accent_color: str = ""
    overlay_html: str = ""
    final_image_path: str = ""
    alt_text: str = ""
    quality_score: float = 0.0
    verification_status: CarouselVerificationStatus = CarouselVerificationStatus.PENDING
    verification_issues_json: str = "[]"
    regeneration_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("slide_type", mode="before")
    @classmethod
    def _canonicalize_slide_type(cls, value: object) -> object:
        """Accept canonical names and spec aliases ('symptom_list', 'code_fix').

        Unknown names are rejected instead of silently stored: the renderer
        picks a template by slide type, so a typo must surface immediately.
        """
        if isinstance(value, SlideType) or value is None:
            return value
        raw = normalize_slide_type(str(value))
        try:
            return SlideType(raw)
        except ValueError as exc:
            raise ValueError(f"Unknown slide type: {value!r}") from exc

    @property
    def bullets(self) -> List[str]:
        return load_json_list(self.bullets_json)

    @property
    def source_refs(self) -> List[str]:
        return load_json_list(self.source_refs_json)

    @property
    def verification_issues(self) -> List[str]:
        return load_json_list(self.verification_issues_json)

    def code(self) -> Optional[CodeSnippet]:
        payload = load_json_obj(self.code_json, None)
        if not isinstance(payload, dict) or not payload:
            return None
        return CodeSnippet.model_validate(payload)

    def metrics(self) -> List[Metric]:
        payload = load_json_obj(self.metrics_json, [])
        if not isinstance(payload, list):
            return []
        return [Metric.model_validate(item) for item in payload if isinstance(item, dict)]

    def image_asset(self) -> Optional[ImageAsset]:
        payload = load_json_obj(self.image_asset_json, None)
        if not isinstance(payload, dict) or not payload:
            return None
        return ImageAsset.model_validate(payload)


class CarouselHookCandidate(BaseModel):
    """A hook the engine proposes; a human picks one in supervised mode."""

    id: Optional[int] = None
    job_id: Optional[int] = None
    category: str = HookCategory.PERSONAL_EXPERIENCE.value
    pattern: str = ""
    text: str = ""
    score: float = 0.0
    expected_emotion: str = ""
    rationale: str = ""
    source_support: str = ""
    scores_json: str = "{}"
    is_selected: bool = False
    created_at: Optional[datetime] = None

    @property
    def score_breakdown(self) -> Dict[str, float]:
        payload = load_json_obj(self.scores_json, {})
        if not isinstance(payload, dict):
            return {}
        return {str(k): float(v) for k, v in payload.items() if isinstance(v, (int, float))}


class CarouselPublication(BaseModel):
    """One platform row of a published carousel (idempotent by request_id)."""

    id: Optional[int] = None
    job_id: Optional[int] = None
    platform: str = ""
    request_id: str = ""
    external_id: str = ""
    post_url: str = ""
    status: PublicationStatus = PublicationStatus.PENDING
    published_at: Optional[datetime] = None
    error_message: str = ""
    raw_response_json: str = "{}"
    created_at: Optional[datetime] = None


class CarouselMetric(BaseModel):
    """A collected number about a published carousel."""

    id: Optional[int] = None
    publication_id: Optional[int] = None
    job_id: Optional[int] = None
    platform: str = ""
    metric_name: str = ""
    metric_value: float = 0.0
    raw_value: str = ""
    source: str = ""
    collected_at: Optional[datetime] = None
    raw_payload_json: str = "{}"


class CarouselLearning(BaseModel):
    """An aggregated insight (rolling history) the recommender reads."""

    id: Optional[int] = None
    scope_type: CarouselLearningScope = CarouselLearningScope.VERTICAL
    scope_value: str = ""
    metric_name: str = ""
    metric_value: float = 0.0
    sample_size: int = 0
    confidence: float = 0.0
    updated_at: Optional[datetime] = None


class CarouselTemplate(BaseModel):
    """A named slide sequence + hook families + visual style per vertical."""

    id: Optional[int] = None
    vertical: CarouselVertical = CarouselVertical.HYBRID
    name: str = ""
    description: str = ""
    slide_sequence_json: str = "[]"
    hook_categories_json: str = "[]"
    visual_style: str = ""
    is_active: bool = True
    version: int = 1
    created_at: Optional[datetime] = None

    @property
    def slide_sequence(self) -> List[str]:
        return [normalize_slide_type(item) for item in load_json_list(self.slide_sequence_json)]

    @property
    def hook_categories(self) -> List[str]:
        return load_json_list(self.hook_categories_json)


class QAArtifact(BaseModel):
    """A QA artifact (issue / flaky test / CI failure / postmortem)."""

    id: Optional[int] = None
    source_type: CarouselSourceType = CarouselSourceType.QA_ARTIFACT
    external_id: str = ""
    title: str = ""
    summary: str = ""
    severity: str = ""
    symptoms_json: str = "[]"
    investigation_json: str = "[]"
    root_cause: str = ""
    #: False = the root cause is a hypothesis and must be shown as such.
    root_cause_verified: bool = False
    fix_description: str = ""
    code_snippet: str = ""
    code_language: str = ""
    metrics_json: str = "[]"
    tags_json: str = "[]"
    source_url: str = ""
    created_at: Optional[datetime] = None

    @property
    def symptoms(self) -> List[str]:
        return load_json_list(self.symptoms_json)

    @property
    def investigation(self) -> List[str]:
        return load_json_list(self.investigation_json)

    @property
    def tags(self) -> List[str]:
        return load_json_list(self.tags_json)


class VibecodingSession(BaseModel):
    """A builder session (what was asked, what was used, what came out)."""

    id: Optional[int] = None
    title: str = ""
    goal: str = ""
    prompts_json: str = "[]"
    tools_used_json: str = "[]"
    files_changed_json: str = "[]"
    diff_summary: str = ""
    tests_before: str = ""
    tests_after: str = ""
    duration_minutes: Optional[int] = None
    tokens_used: Optional[int] = None
    cost_estimate: Optional[float] = None
    outcome: str = ""
    lessons_json: str = "[]"
    screenshots_json: str = "[]"
    #: True only when nothing left the machine — claimed "local AI" must be true.
    local_only: bool = False
    source_url: str = ""
    created_at: Optional[datetime] = None

    @property
    def prompts(self) -> List[str]:
        return load_json_list(self.prompts_json)

    @property
    def tools_used(self) -> List[str]:
        return load_json_list(self.tools_used_json)

    @property
    def files_changed(self) -> List[str]:
        return load_json_list(self.files_changed_json)

    @property
    def lessons(self) -> List[str]:
        return load_json_list(self.lessons_json)


class CarouselSlidePlan(BaseModel):
    """The deterministic plan the renderer consumes (JSON before pixels)."""

    job_id: Optional[int] = None
    vertical: CarouselVertical = CarouselVertical.HYBRID
    title: str = ""
    logline: str = ""
    caption: str = ""
    hashtags: List[str] = Field(default_factory=list)
    visual_style: str = ""
    slides: List[CarouselSlide] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class CarouselBundle(BaseModel):
    """Read model: a job with its slides, hooks and publications."""

    job: CarouselJob
    slides: List[CarouselSlide] = Field(default_factory=list)
    hooks: List[CarouselHookCandidate] = Field(default_factory=list)
    publications: List[CarouselPublication] = Field(default_factory=list)
    issues: List[str] = Field(default_factory=list)


class CarouselSourceRecord(BaseModel):
    """Audit row of one source resolution: what we fetched and how sure we are.

    Kept next to the job (not inside it) so a job can be re-researched and the
    history of what the source actually said stays auditable.
    """

    id: Optional[int] = None
    job_id: Optional[int] = None
    source_type: CarouselSourceType = CarouselSourceType.URL
    vertical: CarouselVertical = CarouselVertical.HYBRID
    source_ref: str = ""
    canonical_url: str = ""
    external_id: str = ""
    title: str = ""
    content_type: str = ""
    confidence: float = 0.0
    warnings_json: str = "[]"
    context_json: str = "{}"
    raw_payload_json: str = "{}"
    resolver: str = ""
    created_at: Optional[datetime] = None

    @property
    def warnings(self) -> List[str]:
        return load_json_list(self.warnings_json)
