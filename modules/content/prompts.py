"""Prompt templates for content generation (section 28-29).

Kept as data (dicts + functions) so they are testable and tweakable without
touching the engine's control flow.
"""

from __future__ import annotations

from typing import List

from core.models import Platform

BASE_STORY_SYSTEM = (
    "You are a seasoned travel writer. Write vivid, honest, first-person travel "
    "stories. Use concrete details from the photographs: locations, people, "
    "objects, mood, and any readable signs (names, dates, prices). Never invent "
    "facts that are not in the provided material. Write in Russian."
)

#: Prompt to analyse ONE photo and return the {{ImageAnalysis}} JSON shape.
ANALYSIS_PROMPT = (
    "Проанализируй эту фотографию для travel-блога. Верни ТОЛЬКО JSON без "
    "пояснений в формате:\n"
    '{"subjects": ["..."], "scene": "одно предложение о сцене", '
    '"objects": ["..."], "mood": "настроение", '
    '"text": "видимый текст/подписи/вывески", '
    '"quality_ok": true, "quality_reason": ""}\n'
    "Если фото размытое/нечитаемое — quality_ok=false и причина в quality_reason."
)

#: User prompt for the *factual base* story (one city → one narrative).
BASE_STORY_USER = (
    "Город: {city}\n"
    "Страна: {country}\n"
    "Год(ы) посещения: {year}\n"
    "Количество фотографий: {photo_count}\n\n"
    "ФАКТЫ ИЗ ФОТОГРАФИЙ (по одной на фото):\n"
    "{facts}\n\n"
    "Напиши связный travel-story (800-1200 слов) об этом городе, используя "
    "только перечисленные факты. Дай название. Текст — сплошной, без "
    "маркдауна, без разделов 'Введение'. В конце добавь платформо-независимые "
    "хэштеги (5-7) через пробел."
)

#: One factual line per analysed photo, injected into the base prompt.
FACT_LINE = (
    "- {filename}: {scene} (объекты: {objects}; настроение: {mood}; текст: {text})"
)

# Per-platform adaptation rules: tone, length, structure, hashtag count.
PLATFORM_RULES: dict[str, dict] = {
    Platform.FACEBOOK.value: {
        "system": (
            "You are adapting a travel story for a Facebook post. Keep the "
            "first-person warmth, aim for 700-1000 words, 2-4 short paragraphs, "
            "a friendly hook in the first line, and 5-8 relevant hashtags. "
            "Reply in Russian, plain text."
        ),
        "format": "{base}",
    },
    Platform.VK.value: {
        "system": (
            "You are adapting a travel story for a VK post. Aim for 500-800 "
            "words, 2-3 paragraphs, casual but informative tone, link-friendly. "
            "Add 5-8 hashtags. Reply in Russian, plain text."
        ),
        "format": "{base}",
    },
    Platform.TELEGRAM.value: {
        "system": (
            "You are adapting a travel story for a Telegram channel post. "
            "Aim for 400-700 words, 2-4 short paragraphs, punchy hook, "
            "easy to scan on mobile. Add 4-6 hashtags. Reply in Russian, plain text."
        ),
        "format": "{base}",
    },
    Platform.ZEN.value: {
        "system": (
            "You are adapting a travel story for a Yandex Zen article. Rewrite "
            "as a longer structured article (1000-1500 words) with a clear "
            "title, 3-5 subheadings using '##', and a conclusion. Reply in Russian."
        ),
        "format": "{base}",
    },
    Platform.INSTAGRAM.value: {
        "system": (
            "You are adapting a travel story for an Instagram caption. Write "
            "200-400 words, a strong first line, no markdown, 10-15 hashtags at "
            "the end. Reply in Russian."
        ),
        "format": "{base}",
    },
    Platform.YOUTUBE.value: {
        "system": (
            "You are adapting a travel story for a YouTube video description. "
            "Write 150-300 words summary + 5-8 hashtags + a 'Таймкоды:' section "
            "placeholder if timestamps are known. Reply in Russian."
        ),
        "format": "{base}",
    },
    Platform.TRIP_COM.value: {
        "system": (
            "You are adapting a travel story for a Trip.com review/note. Write "
            "100-250 words, practical and factual, mention best time to visit "
            "and tips, no hashtags. Reply in Russian."
        ),
        "format": "{base}",
    },
}

#: User prompt for adapting the base story to one platform.
ADAPT_USER = (
    "Вот базовая travel-история:\n\n{base}\n\n"
    "Перепиши её под формат платформы: {platform}. Соблюдай правила платформы. "
    "Верни ТОЛЬКО готовый текст (и хэштеги, если требуются), без пояснений."
)


def facts_block(facts: List[str]) -> str:
    return "\n".join(FACT_LINE.format(**{"filename": f["filename"], **f}) for f in facts)
