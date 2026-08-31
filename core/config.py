"""Configuration loading: config.yaml (typed settings) + .env (secrets).

Secrets live only in .env (loaded via pydantic-settings). Settings live only in
config.yaml. Nothing secret is ever read from config.yaml or hardcoded.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import yaml
from pydantic import BaseModel, Field
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
