"""Prompt templates for the Visual Narrative Studio (ADR-106).

Kept as data (like ``modules/content/prompts.py``) so they are testable and
tweakable without touching the service's control flow. The unified prompt asks
for *one* JSON document that carries both the narrative header (title, logline,
arc, emotional journey, hook/climax/ending) and the storyboard (beats + shots),
so a single model call produces the whole plan.
"""

from __future__ import annotations

from typing import Dict, List

from core.models import NarrativeBeatType

#: System prompt: the studio's contract with any text provider.
NARRATIVE_PLAN_SYSTEM = (
    "You are a visual storytelling editor for a travel blog. You turn a folder "
    "of photographs and the factual notes taken from them into ONE intentional "
    "visual narrative: a story arc, an emotional journey and a shot list "
    "(storyboard) that a human editor can still adjust.\n"
    "Hard rules:\n"
    "1. Use ONLY the provided facts. Never invent places, names, dates or events "
    "that are not in the material.\n"
    "2. Each photograph is referenced by its exact path, copied verbatim.\n"
    "3. Order the shots so that the emotion moves: setup -> conflict -> "
    "development -> climax -> resolution -> reflection. A narrative must contain "
    "at least a setup, a climax and a resolution.\n"
    "4. Alt-text must describe what is visible for somebody who cannot see the "
    "photo (never a filename, never 'photo of a city').\n"
    "5. Reply with JSON only, no commentary, no markdown fences.\n"
    "Write all human-readable text in Russian."
)

#: The exact JSON contract asked from the provider.
NARRATIVE_JSON_SHAPE = (
    "{\n"
    '  "title": "заголовок визуальной истории",\n'
    '  "logline": "одно предложение: о чём эта история",\n'
    '  "narrative_arc": "как разворачивается рассказ (2-3 предложения)",\n'
    '  "emotional_journey": "настроение кадр за кадром: A -> B -> C",\n'
    '  "primary_theme": "главная тема",\n'
    '  "secondary_themes": ["вторая тема", "третья тема"],\n'
    '  "hook": "чем цепляем читателя в начале",\n'
    '  "climax": "в чём кульминация истории",\n'
    '  "ending": "чем история закрывается",\n'
    '  "beats": [\n'
    '    {"beat_type": "setup", "title": "...", "description": "...",\n'
    '     "emotional_tone": "...", "visual_goal": "...",\n'
    '     "photo_paths": ["/абсолютный/путь/к/фото.jpg"]}\n'
    "  ],\n"
    '  "shots": [\n'
    '    {"photo_path": "/абсолютный/путь/к/фото.jpg",\n'
    '     "caption": "подпись кадра",\n'
    '     "alt_text": "описание для незрячего читателя",\n'
    '     "crop_recommendation": "как кадрировать (например rule_of_thirds_4_5)",\n'
    '     "focus_point": "0.5,0.45",\n'
    '     "visual_metaphor": "что кадр символизирует",\n'
    '     "pacing_weight": 1.2,\n'
    '     "is_hero_image": false}\n'
    "  ],\n"
    '  "accessibility_notes": ["что важно для доступности"],\n'
    '  "cultural_sensitivity_notes": ["что проверить перед публикацией"]\n'
    "}"
)

#: Whisper of the arc contract, injected into the user prompt.
_BEAT_LINE = "- {name}: {goal}"

#: User prompt for one city -> one visual narrative plan.
NARRATIVE_PLAN_USER = (
    "ГОРОД: {city}\n"
    "СТРАНА: {country}\n"
    "ГОД(Ы) ПОСЕЩЕНИЯ: {year}\n"
    "ДОСТУПНО КАДРОВ: {photo_count} (используй не больше {max_photos})\n"
    "ЦЕЛЕВЫЕ ПЛАТФОРМЫ: {platforms}\n\n"
    "ФАКТЫ ИЗ ФОТОГРАФИЙ:\n{facts}\n\n"
    "{base_story_block}"
    "СЮЖЕТНАЯ АРКА (используй эти типы битов, порядок обязателен):\n{beat_guide}\n\n"
    "Построй визуальный нарратив по этой раскадровке. Верни ТОЛЬКО JSON:\n"
    "{shape}"
)

#: How each beat type should behave, shared by the prompt and the local builder.
BEAT_GUIDE: Dict[str, Dict[str, str]] = {
    NarrativeBeatType.SETUP.value: {
        "goal": "задать место, время и настроение; 1-2 самых узнаваемых кадра",
        "tone": "любопытство, предвкушение",
        "metaphor": "открывающийся вид — вход в историю",
    },
    NarrativeBeatType.CONFLICT.value: {
        "goal": "показать препятствие, контраст или трудность дня",
        "tone": "напряжение, неуверенность",
        "metaphor": "преграда на пути",
    },
    NarrativeBeatType.DEVELOPMENT.value: {
        "goal": "развернуть детали, людей, быт, второстепенные сюжеты",
        "tone": "вовлечённость, интерес",
        "metaphor": "путь внутрь места",
    },
    NarrativeBeatType.CLIMAX.value: {
        "goal": "самый сильный кадр — эмоциональная вершина истории",
        "tone": "восторг, потрясение",
        "metaphor": "момент, ради которого стоило ехать",
    },
    NarrativeBeatType.RESOLUTION.value: {
        "goal": "закрыть историю выводом, спокойным кадром или деталью",
        "tone": "спокойствие, удовлетворение",
        "metaphor": "выдох после подъёма",
    },
    NarrativeBeatType.REFLECTION.value: {
        "goal": "посмотреть на место со стороны, дать читателю паузу",
        "tone": "ностальгия, лёгкая грусть",
        "metaphor": "взгляд назад",
    },
}

#: Appended to the factual base-story prompt when a storyboard exists (ADR-106).
NARRATIVE_STORY_BLOCK = (
    "\n\nВИЗУАЛЬНЫЙ НАРРАТИВ (раскадровка уже собрана редактором):\n"
    "Заголовок: {title}\n"
    "Логлайн: {logline}\n"
    "Арка: {arc}\n"
    "Эмоциональная дуга: {journey}\n"
    "Хук: {hook}\n"
    "Кульминация: {climax}\n"
    "Финал: {ending}\n"
    "Биты по порядку:\n{beats}\n"
    "Следуй этому порядку кадров: текст должен вести читателя по этим битам, "
    "а фотографии — идти в указанной последовательности."
)

#: One line per beat in the prompt above.
NARRATIVE_BEAT_LINE = "- [{order}] {beat_type} — {title}: {description} (кадры: {photos})"


def beat_guide_block() -> str:
    """Render :data:`BEAT_GUIDE` for the user prompt."""
    return "\n".join(
        _BEAT_LINE.format(name=name, goal=guide["goal"]) for name, guide in BEAT_GUIDE.items()
    )


def platform_list(platforms: List[str]) -> str:
    return ", ".join(platforms) if platforms else "(не заданы)"
