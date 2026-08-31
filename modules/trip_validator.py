"""Trip.com (Trip Moments) content validator (G2).

Validates a travel ``Draft`` for the Trip.com platform against the guidelines in
``config.trip_guidelines``. Works on the real draft shape: ``content`` + ``title``
+ ``photos_json`` (``{"photos":[{path,filename,sha256,analysis}]}``) + the owning
``City`` (for the geotag check). Photo-scene heuristics (restaurant / interior)
read the per-photo ``analysis`` produced by the AI provider.

Never raises: returns a :class:`~modules.validation_base.ValidationResult`. All
runs are logged through the project Loguru logger.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from loguru import logger

from core.config import Config
from modules.validation_base import (
    ValidationResult,
    check,
    contains_emoji,
    find_forbidden,
    word_count,
)

# Photo-scene keywords (lowercased, substring match against the analysis blob).
_RESTAURANT_WORDS = (
    "restaurant", "еда", "ресторан", "кафе", "cafe", "coffee", "кофе", "food",
    "кухня", "обед", "ужин", "закуска", "пицц", "sushi", "бар", "bar",
)
_INTERIOR_WORDS = (
    "interior", "интерьер", "внутри", "inside", "зал", "hall", "уют", "комната",
    "room", "салон", "помещен", "свет", "стол", "столик", "table", "кирпич",
)


def _list_or_dict(data: Any) -> List[Dict[str, Any]]:
    """Normalise photos_json into a list of photo dicts."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        photos = data.get("photos", [])
        if isinstance(photos, list):
            return [x for x in photos if isinstance(x, dict)]
    return []


def _analysis_blob(photo: Dict[str, Any]) -> str:
    """Join all analysis text fields of a photo into one lowercase blob."""
    analysis = photo.get("analysis") or {}
    if not isinstance(analysis, dict):
        return ""
    parts = [
        str(analysis.get("scene", "")),
        " ".join(analysis.get("objects", []) or []),
        " ".join(analysis.get("subjects", []) or []),
        str(analysis.get("raw", "")),
    ]
    return " ".join(parts).lower()


class TripValidator:
    name = "trip_com"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.g = config.trip_guidelines

    def _is_restaurant(self, photos: List[Dict[str, Any]]) -> bool:
        return any(
            any(w in _analysis_blob(p) for w in _RESTAURANT_WORDS) for p in photos
        )

    def _has_interior(self, photos: List[Dict[str, Any]]) -> bool:
        return any(
            any(w in _analysis_blob(p) for w in _INTERIOR_WORDS) for p in photos
        )

    def _photos(self, draft) -> List[Dict[str, Any]]:
        try:
            data = json.loads(draft.photos_json or "[]")
        except (ValueError, TypeError):
            data = []
        return _list_or_dict(data)

    def validate(self, draft, city=None) -> ValidationResult:
        g = self.g
        content = draft.content or ""
        title = draft.title or ""
        photos = self._photos(draft)
        n_photos = len(photos)
        words = word_count(content)

        # photos / restaurant-aware minimum
        is_restaurant = self._is_restaurant(photos)
        min_photos = g.min_photos_restaurant if is_restaurant else g.min_photos_attraction
        photos_ok = n_photos >= min_photos
        photos_label = (
            f"Фото ({n_photos}/{min_photos}, ресторан)"
            if is_restaurant else f"Фото ({n_photos}/{min_photos})"
        )

        # interior photo (only meaningful when the rule is on)
        interior_ok = (not g.require_interior_photo) or self._has_interior(photos)

        # forbidden patterns
        found = find_forbidden(content, g.forbidden_patterns)
        forbidden_ok = not found

        # geotag
        has_geo = bool(
            city
            and (getattr(city, "latitude", None) is not None)
            and (getattr(city, "longitude", None) is not None)
        )
        geotag_ok = (not g.geotag_required) or has_geo

        # AI disclaimer (recommended, not gating)
        disclaimer_ok = True
        if g.ai_disclaimer:
            disclaimer_ok = g.ai_disclaimer.lower() in content.lower()

        # title emoji (recommendation)
        emoji_ok = (not g.title_emoji_recommended) or contains_emoji(title)

        checks = [
            check("min_words", f"Минимум {g.min_words} слов",
                  words >= g.min_words,
                  message=f"В тексте {words} слов.",
                  recommendation=f"Дополни текст минимум до {g.min_words} слов."),
            check("min_photos", photos_label, photos_ok,
                  message=f"Требуется {min_photos} фото (найдено {n_photos}).",
                  recommendation=f"Добавь ещё фото до {min_photos}."),
            check("interior_photo", "Интерьерное/атмосферное фото",
                  interior_ok,
                  severity="error" if g.require_interior_photo else "info",
                  message="Рекомендуется фото интерьера/обстановки.",
                  recommendation="Добавь фото интерьера (зал, стол, уют)."),
            check("forbidden", "Нет запрещённых паттернов", forbidden_ok,
                  message=f"Найдены: {', '.join(found)}." if found else "",
                  recommendation="Убери запрещённые значения (watermark/logo/qr/http/tel:/@)."),
            check("geotag", "Геотег (координаты)", geotag_ok,
                  message="У города нет координат." if not geotag_ok else "",
                  recommendation="Проставь lat/lon для города, чтобы пост был геотегирован."),
            check("ai_disclaimer", "AI-дисклеймер", disclaimer_ok,
                  severity="warning",
                  message="" if disclaimer_ok else "Нет упоминания AI-помощи.",
                  recommendation=f"Добавь: «{g.ai_disclaimer}»"),
            check("title_emoji", "Эмодзи в заголовке", emoji_ok,
                  severity="warning" if g.title_emoji_recommended else "info",
                  message="" if emoji_ok else "В заголовке нет эмодзи.",
                  recommendation="Добавь эмодзи в заголовок для вовлечения."),
            check("video_duration", "Длительность видео", True,
                  severity="info",
                  message="Видео в посте не обнаружено — проверка не применима."),
        ]

        result = ValidationResult(validator=self.name, checks=checks)
        logger.bind(validator=self.name, post_id=getattr(draft, "id", None)) \
            .info("Trip validation: {}", result.summary())
        return result
