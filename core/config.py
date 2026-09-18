"""Configuration loading: config.yaml (typed settings) + .env (secrets).

Secrets live only in .env (loaded via pydantic-settings). Settings live only in
config.yaml. Nothing secret is ever read from config.yaml or hardcoded.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"


class Secrets(BaseSettings):
    """Secret credentials — loaded only from .env, never from config.yaml."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: str = ""
    deepseek_api_key: str = ""
    facebook_access_token: str = ""
    facebook_page_id: str = ""
    vk_access_token: str = ""
    vk_group_id: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    trip_username: str = ""
    trip_password: str = ""
    replicate_api_token: str = ""
    openai_api_key: str = ""
    huggingface_api_key: str = ""
    # Carousel Factory (ADR-107). Secrets only ever come from .env.
    uploadpost_token: str = ""
    uploadpost_user: str = ""
    github_token: str = ""


# --- Typed section models (mirror config.yaml) ---


class AppConfig(BaseModel):
    environment: str = "development"
    timezone: str = "UTC"
    dry_run: bool = True
    auto_publish: bool = False


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    api_port: int = 8000
    ui_port: int = 8501


class ArchiveConfig(BaseModel):
    path: str = ""
    recursive: bool = True
    incremental: bool = True


class ScheduleConfig(BaseModel):
    enabled: bool = True
    posts_per_day: int = 2
    publish_time: str = "08:00"
    timezone: str = "UTC"


class AIConfig(BaseModel):
    provider: str = "gemini"


class GeminiConfig(BaseModel):
    requests_per_minute: int = 15
    requests_per_day: int = 1500
    min_interval_seconds: float = 4.3
    retry_429_seconds: int = 60
    max_retries: int = 3
    model: str = "gemini-2.0-flash"
    temperature: float = 0.4


class DeepSeekConfig(BaseModel):
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    temperature: float = 0.6


class PublishingConfig(BaseModel):
    facebook: bool = True
    vk: bool = True
    telegram: bool = True
    zen: bool = True
    instagram: bool = True
    youtube: bool = True
    trip_com: bool = True


class TelegramConfig(BaseModel):
    chat_id: str = ""


class VKConfig(BaseModel):
    api_version: str = ""
    group_id: str = ""


class FacebookConfig(BaseModel):
    page_id: str = ""


class TripConfig(BaseModel):
    guide_title_template: str = "{city} в {year}: взгляд спустя годы"


class ImageGenerationConfig(BaseModel):
    """Image-gen provider config (replicate | openai | huggingface | mock)."""

    enabled: bool = True
    provider: str = "replicate"
    model: str = "stability-ai/sdxl"
    api_key_env: str = "REPLICATE_API_TOKEN"
    width: int = 1024
    height: int = 1024
    num_outputs: int = 1


class TextGenerationConfig(BaseModel):
    """VibeCoding text-generation config (reuses the DeepSeek client)."""

    model: str = "deepseek-chat"
    max_tokens: int = 1500
    temperature: float = 0.7


class VibeDefaultPrompts(BaseModel):
    """Default text/image prompts; ``{topic}`` is substituted at generation time."""

    text: str = (
        "Напиши пост для блога о вайбкодинге на тему {topic}. Используй личный опыт, "
        "примеры кода, расскажи о трудностях и решениях. Стиль: живой, вдохновляющий. "
        "Объём 500-700 слов."
    )
    image: str = (
        "Футуристический кодер за рабочим столом, вокруг летают строки кода, "
        "светящиеся синим, абстрактный фон, высокотехнологичный стиль, 4k, детализированно"
    )


class VibeCodingConfig(BaseModel):
    """VibeCoding content type: generation, scheduling, auto-publish."""

    enabled: bool = True
    schedule_time: str = "12:00"
    schedule_days: List[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    auto_publish: bool = False
    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)
    text_generation: TextGenerationConfig = Field(default_factory=TextGenerationConfig)
    default_prompts: VibeDefaultPrompts = Field(default_factory=VibeDefaultPrompts)


class TripGuidelinesConfig(BaseModel):
    """Trip.com (Trip Moments) content-compliance rules."""

    enabled: bool = True
    min_photos_attraction: int = 3
    min_photos_restaurant: int = 5
    require_interior_photo: bool = True
    min_words: int = 100
    forbidden_patterns: List[str] = Field(
        default_factory=lambda: ["watermark", "logo", "qr", "http", "tel:", "@"]
    )
    video_min_duration_sec: int = 30
    ai_disclaimer: str = "This post was created with the assistance of AI."
    geotag_required: bool = True
    title_emoji_recommended: bool = True
    saturation_boost: float = 1.2
    block_non_compliant: bool = False


class VibeCodingGuidelinesConfig(BaseModel):
    """Educational/expert content-compliance rules for VibeCoding posts."""

    enabled: bool = True
    min_words: int = 200
    max_words: int = 700
    min_photos: int = 2
    max_photos: int = 5
    require_code_screenshot: bool = True
    recommend_ai_image: bool = True
    title_max_length: int = 80
    hashtag_count_min: int = 3
    hashtag_count_max: int = 5
    video_min_duration_sec: int = 15
    video_max_duration_sec: int = 60
    forbidden_patterns: List[str] = Field(
        default_factory=lambda: ["заработай", "миллион", "100%", "бесплатно"]
    )
    engagement_question_required: bool = True
    default_engagement_question: str = "А какой инструмент используешь ты? Делитесь в комментариях!"
    ai_image_prompt_template: str = "Футуристический кодер, строки кода, абстрактный фон, стиль технологичный"
    block_non_compliant: bool = False


class RetryConfig(BaseModel):
    max_attempts: int = 3
    initial_delay_seconds: int = 60
    exponential_backoff: bool = True


class QueueConfig(BaseModel):
    sync_after_scan: bool = True      # enqueue cities when new ones are scanned
    direct_schedule: bool = True      # allow scheduling directly from the queue


class VideoConfig(BaseModel):
    photos_per_video: int = 4
    photo_duration_seconds: int = 4
    resolution: str = "1080x1920"
    font: str = "Arial"


class MediaConfig(BaseModel):
    jpeg_quality: int = 90
    video: VideoConfig = Field(default_factory=VideoConfig)


class ContentConfig(BaseModel):
    photos_per_city: int = 8
    max_hashtags: int = 10


class MediaPreset(BaseModel):
    format: str = "JPEG"
    width: int = 0
    height: int = 0
    max_mb: float = 5.0


class VisualNarrativeConfig(BaseModel):
    """Visual Narrative Studio settings (config.yaml ``visual_narrative``).

    ``provider`` empty means "follow ai.provider"; ``mock`` produces a
    deterministic plan without any network call, which is also what
    ``app.dry_run`` forces.
    """

    enabled: bool = True
    provider: str = "mock"
    max_photos: int = 12
    require_alt_text: bool = True
    enforce_narrative_arc: bool = True
    allow_manual_override: bool = True
    #: opt-in hard gate: refuse to generate platform content until approved
    require_approval: bool = False


class CarouselResolutionConfig(BaseModel):
    """Slide canvas: 9:16 portrait, 768x1376 (TikTok / Reels safe)."""

    width: int = 768
    height: int = 1376


class CarouselUrlSourceConfig(BaseModel):
    """Config for the URL researcher (config.yaml ``carousels.sources.url``)."""

    enabled: bool = True
    timeout_seconds: int = 30
    cache_ttl_seconds: int = 3600
    extract_code_blocks: bool = True
    extract_images: bool = True
    extract_brand_colors: bool = True
    #: polite crawling: min seconds between two requests to the same host
    min_request_interval_seconds: float = 1.0
    user_agent: str = "travel-blog-app-carousel/1.0 (+https://github.com/dmpotekhin/travel-blog-app)"
    respect_robots_txt: bool = True
    max_redirects: int = 5


class CarouselGithubSourceConfig(BaseModel):
    """Config for the GitHub researcher (``carousels.sources.github``)."""

    enabled: bool = True
    api_base: str = "https://api.github.com"
    use_gh_cli_if_available: bool = True
    include_readme: bool = True
    include_issues: bool = True
    include_prs: bool = True
    include_discussions: bool = True
    include_releases: bool = True
    max_comments: int = 50
    timeout_seconds: int = 30


class CarouselSourcesConfig(BaseModel):
    """All source adapters (URL + GitHub are the MVP entry points)."""

    url: CarouselUrlSourceConfig = Field(default_factory=CarouselUrlSourceConfig)
    github: CarouselGithubSourceConfig = Field(default_factory=CarouselGithubSourceConfig)


class CarouselVerticalProfileConfig(BaseModel):
    """One vertical profile (travel / qa / vibecoding / hybrid).

    Empty defaults on purpose: the built-in profiles live in
    ``modules.carousels.vertical_profiles`` and a config block only overrides
    the fields it names, so a partial ``travel: {}`` in config.yaml keeps the
    shipped defaults.
    """

    preferred_sources: List[str] = Field(default_factory=list)
    default_narrative_template: str = ""
    hook_categories: List[str] = Field(default_factory=list)
    slide_types: List[str] = Field(default_factory=list)
    visual_style: str = ""
    caption_style: str = ""
    hashtag_strategy: str = ""
    cta_strategy: str = ""
    forbidden: List[str] = Field(default_factory=list)


class CarouselGeminiConfig(BaseModel):
    """Gemini usage rules: backgrounds/illustrations only for technical verticals."""

    enabled: bool = True
    model: str = "gemini-3.1-flash-image-preview"
    use_for_background_only_in_technical_verticals: bool = True
    #: image-to-image: slides 2..6 reuse slide 1 as the style reference
    reuse_first_slide_as_reference: bool = True


class CarouselUploadPostConfig(BaseModel):
    """Upload-Post publishing (TikTok + Instagram) — off unless enabled."""

    enabled: bool = True
    base_url: str = "https://api.upload-post.com"
    auto_add_music: bool = True
    privacy_level: str = "PUBLIC_TO_EVERYONE"
    async_upload: bool = True
    max_retries: int = 2
    timeout_seconds: int = 120


class CarouselAnalyticsConfig(BaseModel):
    """When to pull metrics back from Upload-Post."""

    enabled: bool = True
    collect_after_hours: int = 24
    recollect_after_hours: int = 72
    max_posts_per_run: int = 50


class CarouselLearningConfig(BaseModel):
    """Learnings store + recommendation engine."""

    enabled: bool = True
    min_sample_size: int = 3
    rolling_history: int = 100
    #: never auto-apply recommendations in supervised mode
    auto_apply_recommendations: bool = False


class CarouselPerformanceLabConfig(BaseModel):
    """Weights of the composite carousel score (config.yaml ``performance_lab``)."""

    weights: Dict[str, float] = Field(
        default_factory=lambda: {
            "views": 0.20,
            "likes": 0.25,
            "comments": 0.20,
            "shares": 0.15,
            "saves": 0.10,
            "approval": 0.05,
            "rejection_penalty": 0.05,
        }
    )
    min_sample_size: int = 3
    rolling_history: int = 100


class CarouselHealthConfig(BaseModel):
    """Honesty gates: refuse to render a carousel the source cannot support."""

    min_source_confidence: float = 0.5
    require_source_refs_for_facts: bool = True
    require_alt_text: bool = True
    require_approval_for_publish: bool = True
    max_headline_chars: int = 90
    max_bullets_per_slide: int = 5
    max_body_lines: int = 3


#: Generic system/automation framing: a travel fact worded this way already
#: speaks the brand, even if the operator edits ``builder_angle_keywords``.
SYSTEM_FRAMING = (
    "систем",
    "автоматиз",
    "пайплайн",
    "pipeline",
    "агент",
    "agent",
    "workflow",
    "воронк",
    "стек",
    "stack",
    "датасет",
    "dataset",
    "exif",
    "gps",
    "api",
)


class CarouselBrandContentMixConfig(BaseModel):
    """How much of the brand surface each face is allowed to take."""

    vibecoding: float = 0.60
    travel_as_case_study: float = 0.25
    qa_trust: float = 0.10
    personal_lifestyle: float = 0.05


class CarouselBrandTravelRulesConfig(BaseModel):
    """Travel is a case study of the AI stack, not a lifestyle feed."""

    require_builder_angle: bool = True
    allow_pure_lifestyle: bool = True
    max_pure_travel_percentage: float = 30.0
    #: supervised default: the guard warns in the slide plan, never blocks.
    warning_only: bool = True
    builder_angle_keywords: List[str] = Field(
        default_factory=lambda: [
            "система",
            "автоматизация",
            "пайплайн",
            "агент",
            "контент-фабрика",
            "EXIF",
            "GPS",
            "датасет",
            "архив как источник",
            "personal AI stack",
            "монетизация",
            "воронка",
        ]
    )

    def has_builder_angle(self, text: str) -> bool:
        """True when the wording frames the fact as a system, not a postcard."""
        if not self.require_builder_angle:
            return True
        lowered = (text or "").lower()
        if any(keyword.lower() in lowered for keyword in self.builder_angle_keywords if keyword):
            return True
        return any(frame in lowered for frame in SYSTEM_FRAMING)


class CarouselBrandRulesConfig(BaseModel):
    """One face: what it is for and what it must never claim."""

    role: str = ""
    #: hook categories that already position testing as reliability proof
    reliability_categories: List[str] = Field(default_factory=list)
    forbidden: List[str] = Field(default_factory=list)


class CarouselBrandCtaFunnelConfig(BaseModel):
    """Every series leads into the funnel; the wording lives in config, not code."""

    default_cta_by_vertical: Dict[str, str] = Field(default_factory=dict)
    destination: str = "telegram_lead_magnet"
    products: List[str] = Field(default_factory=list)

    def cta_for(self, vertical: object) -> str:
        """CTA configured for a vertical (empty string when it has none)."""
        wanted = str(getattr(vertical, "value", vertical) or "").strip().lower()
        for name, cta in self.default_cta_by_vertical.items():
            if str(name).strip().lower() == wanted and isinstance(cta, str):
                return cta.strip()
        return ""


class CarouselBrandConfig(BaseModel):
    """The brand strategy the factory speaks with (``carousels.brand``)."""

    primary_identity: str = ""
    secondary_identity: str = ""
    trust_layer: str = ""
    content_mix: CarouselBrandContentMixConfig = Field(
        default_factory=CarouselBrandContentMixConfig
    )
    travel_rules: CarouselBrandTravelRulesConfig = Field(
        default_factory=CarouselBrandTravelRulesConfig
    )
    qa_rules: CarouselBrandRulesConfig = Field(
        default_factory=lambda: CarouselBrandRulesConfig(role="trust_layer")
    )
    vibecoding_rules: CarouselBrandRulesConfig = Field(
        default_factory=lambda: CarouselBrandRulesConfig(role="primary_brand")
    )
    cta_funnel: CarouselBrandCtaFunnelConfig = Field(
        default_factory=CarouselBrandCtaFunnelConfig
    )


class CarouselConfig(BaseModel):
    """Tri-Face Carousel Factory (config.yaml ``carousels``).

    ``supervised`` is the default and means: a human approves before any
    publish. ``full_autonomous`` only relaxes that when an operator
    explicitly sets both ``mode: full_autonomous`` and
    ``require_human_approval: false``.
    """

    enabled: bool = True
    mode: str = "supervised"
    require_human_approval: bool = True
    default_vertical: str = "hybrid"
    slide_count: int = 6
    resolution: CarouselResolutionConfig = Field(default_factory=CarouselResolutionConfig)
    format: str = "jpg"
    aspect_ratio: str = "9:16"
    bottom_safe_zone_percent: float = 20.0
    max_regeneration_attempts: int = 2
    output_dir: str = "public/carousels"
    renderer: str = "pillow"          # pillow | html | mock
    #: dry_run never touches Gemini/Upload-Post: placeholders + mock publisher.
    dry_run: bool = True
    hook_candidates_min: int = 3
    hook_candidates_max: int = 5
    target_platforms: List[str] = Field(default_factory=lambda: ["tiktok", "instagram"])
    sources: CarouselSourcesConfig = Field(default_factory=CarouselSourcesConfig)
    verticals: Dict[str, CarouselVerticalProfileConfig] = Field(default_factory=dict)
    gemini: CarouselGeminiConfig = Field(default_factory=CarouselGeminiConfig)
    upload_post: CarouselUploadPostConfig = Field(default_factory=CarouselUploadPostConfig)
    analytics: CarouselAnalyticsConfig = Field(default_factory=CarouselAnalyticsConfig)
    learning: CarouselLearningConfig = Field(default_factory=CarouselLearningConfig)
    performance_lab: CarouselPerformanceLabConfig = Field(default_factory=CarouselPerformanceLabConfig)
    health: CarouselHealthConfig = Field(default_factory=CarouselHealthConfig)
    brand: CarouselBrandConfig = Field(default_factory=CarouselBrandConfig)

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        mode = (value or "supervised").strip().lower()
        if mode not in ("supervised", "full_autonomous"):
            raise ValueError(f"carousels.mode must be supervised|full_autonomous, got {value!r}")
        return mode

    @field_validator("format")
    @classmethod
    def _jpg_only(cls, value: str) -> str:
        """TikTok accepts JPG only — anything else is silently a bug."""
        fmt = (value or "jpg").strip().lower().lstrip(".")
        if fmt not in ("jpg", "jpeg"):
            raise ValueError(f"carousels.format must be jpg (TikTok rejects PNG), got {value!r}")
        return "jpg"

    @property
    def is_full_autonomous(self) -> bool:
        """True only when autonomy was explicitly switched on."""
        return self.mode == "full_autonomous"

    @property
    def publishing_requires_approval(self) -> bool:
        """Supervised mode (or an explicit flag) always keeps the human in the loop."""
        if self.health.require_approval_for_publish:
            return not (self.is_full_autonomous and not self.require_human_approval)
        return not self.is_full_autonomous

    @property
    def bottom_safe_zone_pixels(self) -> int:
        """Height of the unusable bottom band (TikTok UI overlay)."""
        return int(round(self.resolution.height * self.bottom_safe_zone_percent / 100.0))


class Config(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)
    publishing: PublishingConfig = Field(default_factory=PublishingConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    vk: VKConfig = Field(default_factory=VKConfig)
    facebook: FacebookConfig = Field(default_factory=FacebookConfig)
    trip: TripConfig = Field(default_factory=TripConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    visual_narrative: VisualNarrativeConfig = Field(default_factory=VisualNarrativeConfig)
    carousels: CarouselConfig = Field(default_factory=CarouselConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    content: ContentConfig = Field(default_factory=ContentConfig)
    vibecoding: VibeCodingConfig = Field(default_factory=VibeCodingConfig)
    trip_guidelines: TripGuidelinesConfig = Field(default_factory=TripGuidelinesConfig)
    vibecoding_guidelines: VibeCodingGuidelinesConfig = Field(default_factory=VibeCodingGuidelinesConfig)
    media_presets: Dict[str, MediaPreset] = Field(default_factory=dict)


def load_config_from_dict(data: Dict[str, Any]) -> Config:
    """Build a validated Config from a parsed YAML dict."""
    return Config.model_validate(data)


def load_config_file() -> Config:
    """Load config.yaml and return validated Config."""
    if not CONFIG_PATH.exists():
        return Config()
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return load_config_from_dict(data)


@lru_cache(maxsize=1)
def get_settings() -> Config:
    """Cached settings accessor used across the app."""
    return load_config_file()


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    """Cached secrets accessor. Reads .env only."""
    return Secrets()


def reload_settings() -> Config:
    """Invalidate caches and reload settings (used after config edits)."""
    get_settings.cache_clear()
    get_secrets.cache_clear()
    return get_settings()
