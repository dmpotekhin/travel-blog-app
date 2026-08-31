"""VibeCoding educational/expert content validator (G3).

Validates a :class:`~core.models.VibeCodingPost` against the guidelines in
``config.vibecoding_guidelines`` (word range, hashtags, forbidden clickbait,
engagement question, title length, media). Runs on the real post shape
(``generated_text`` + ``image_url`` + ``topic``/``title``).

Never raises: returns a :class:`~modules.validation_base.ValidationResult`. All
runs are logged through the project Loguru logger.
"""

from __future__ import annotations

from loguru import logger

from core.config import Config
from modules.validation_base import (
    ValidationResult,
    check,
    count_hashtags,
    has_question,
    find_forbidden,
    word_count,
)


class VibeCodingValidator:
    name = "vibecoding"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.g = config.vibecoding_guidelines

    def validate(self, post) -> ValidationResult:
        g = self.g
        text = post.generated_text or ""
        title = (post.title or post.topic or "")
        words = word_count(text)
        hashtags = count_hashtags(text)
        has_media = bool(getattr(post, "image_url", ""))
        n_photos = 1 if has_media else 0

        # word range: below min is an error (thin content), above max is a warning
        words_ok = words >= g.min_words
        words_over = words > g.max_words

        # hashtags: below min is a warning (engagement), above max is info
        hashtags_ok = hashtags >= g.hashtag_count_min
        hashtags_over = hashtags > g.hashtag_count_max

        # title length is a hard rule
        title_ok = len(title) <= g.title_max_length

        # forbidden clickbait
        found = find_forbidden(text, g.forbidden_patterns)
        forbidden_ok = not found

        # engagement question
        has_q = has_question(text) or (g.default_engagement_question and g.default_engagement_question.lower() in text.lower())

        checks = [
            check("min_words", f"Объём ≥ {g.min_words} слов", words_ok,
                  message=f"В тексте {words} слов.",
                  recommendation=f"Дополни текст минимум до {g.min_words} слов."),
            check("max_words", f"Объём ≤ {g.max_words} слов", not words_over,
                  severity="warning",
                  message="" if not words_over else f"Текст {words} слов (> {g.max_words}).",
                  recommendation="Сократи пост до рекомендованного объёма."),
            check("min_photos", f"Минимум {g.min_photos} медиа", n_photos >= g.min_photos,
                  severity="warning",
                  message=f"Медиа: {n_photos}.",
                  recommendation="Добавь скриншот кода или второе изображение."),
            check("max_photos", f"Максимум {g.max_photos} медиа", n_photos <= g.max_photos,
                  severity="info",
                  message=f"Медиа: {n_photos}."),
            check("title_length", f"Заголовок ≤ {g.title_max_length} символов", title_ok,
                  message=f"Заголовок {len(title)} символов." if not title_ok else "",
                  recommendation="Сократи заголовок."),
            check("hashtags", f"Хэштегов {g.hashtag_count_min}–{g.hashtag_count_max}",
                  hashtags_ok,
                  severity="warning",
                  message=f"Хэштегов: {hashtags}." if not hashtags_ok else "",
                  recommendation=f"Добавь {g.hashtag_count_min}–{g.hashtag_count_max} хэштегов."),
            check("hashtags_max", f"Хэштегов не больше {g.hashtag_count_max}",
                  not hashtags_over, severity="info",
                  message=f"Хэштегов: {hashtags}." if hashtags_over else ""),
            check("forbidden", "Нет запрещённых кликбейт-паттернов", forbidden_ok,
                  message=f"Найдены: {', '.join(found)}." if found else "",
                  recommendation="Убери запрещённые обещания (заработай/миллион/100%/бесплатно)."),
            check("engagement_question", "Вовлекающий вопрос", has_q,
                  severity="error" if g.engagement_question_required else "info",
                  message="" if has_q else "Нет вопроса к читателю.",
                  recommendation=f"Добавь вопрос, например: «{g.default_engagement_question}»"),
            check("media", "AI-изображение", has_media,
                  severity="info" if g.recommend_ai_image else "error",
                  message="Изображение отсутствует." if not has_media else "AI-изображение сгенерировано."),
            check("code_screenshot", "Скриншот кода", True,
                  severity="warning",
                  message="Скриншот кода не прикреплён.",
                  recommendation="Добавь скриншот кода для экспертной ценности."),
            check("video_duration", "Длительность видео", True,
                  severity="info",
                  message="Видео в посте не обнаружено — проверка не применима."),
        ]

        result = ValidationResult(validator=self.name, checks=checks)
        logger.bind(validator=self.name, post_id=getattr(post, "id", None)) \
            .info("VibeCoding validation: {}", result.summary())
        return result
