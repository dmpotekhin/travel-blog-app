"""Hook engine (Phase 3): ranked hook candidates, each backed by a source line.

No model runs here. Patterns are keyword families from ``enums.hook_categories_for``;
a candidate is only produced when a fact from the resolver matches, and it always
carries that fact verbatim as ``source_support`` — so a human reviewing the hook
lab sees exactly which sentence the hook came from.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from loguru import logger

from core.models import (
    CarouselHookCandidate,
    CarouselSourceContext,
    CarouselVertical,
    HookCategory,
)

from ..fact_guard import normalize
from ..vertical_profiles import profile_for

_DIGITS = re.compile(r"\d")


@dataclass(frozen=True)
class HookPattern:
    """One deterministic hook shape and the source signal it needs."""

    key: str
    category: HookCategory
    template: str
    keywords: Tuple[str, ...]
    expected_emotion: str
    base_score: float
    rationale: str

    def matches(self, fact: str) -> bool:
        lowered = fact.lower()
        return any(keyword in lowered for keyword in self.keywords)


TRAVEL_PATTERNS: Tuple[HookPattern, ...] = (
    HookPattern(
        "travel_route",
        HookCategory.ROUTE_GUIDE,
        "Маршрут: {claim}",
        ("→", "—", "->", "маршрут", "route"),
        "ясность",
        0.58,
        "источник описывает конкретный маршрут",
    ),
    HookPattern(
        "travel_season",
        HookCategory.SEASONALITY,
        "{claim}",
        ("сезон", "закрыт", "дожд", "не работал", "не работает"),
        "предупреждение",
        0.62,
        "сезонность меняет план поездки",
    ),
    HookPattern(
        "travel_culture",
        HookCategory.CULTURAL_CONTRAST,
        "{claim}",
        ("чаевые", "обыча", "местные", "не принято", "этикет"),
        "удивление",
        0.6,
        "культурная деталь, которой нет в путеводителях",
    ),
    HookPattern(
        "travel_reality",
        HookCategory.EXPECTATION_VS_REALITY,
        "{claim}",
        ("оказалось", " но ", "зато", "не так"),
        "разочарование → опыт",
        0.64,
        "источник показывает расхождение ожидания и реальности",
    ),
    HookPattern(
        "travel_budget",
        HookCategory.BUDGET,
        "{claim}",
        ("₽", "$", "€", "руб", "стоит", "бюджет", "цена"),
        "прагматизм",
        0.61,
        "цена или бюджет названы самим источником",
    ),
    HookPattern(
        "travel_self",
        HookCategory.PERSONAL_EXPERIENCE,
        "{claim}",
        ("я ", "мы ", "мне ", "наш"),
        "доверие",
        0.5,
        "личный опыт автора источника",
    ),
)

QA_PATTERNS: Tuple[HookPattern, ...] = (
    HookPattern(
        "qa_flaky",
        HookCategory.FLAKY_TEST,
        "{claim}",
        ("flaky", "нестабильн", "падает только в ci", "локально зелёный", "intermittent"),
        "узнавание",
        0.68,
        "источник описывает нестабильный тест",
    ),
    HookPattern(
        "qa_failure",
        HookCategory.FAILURE,
        "{claim}",
        ("падает", "fail", "ошибк", "красн", "сломал", "упал"),
        "тревога",
        0.6,
        "источник описывает падение",
    ),
    HookPattern(
        "qa_numbers",
        HookCategory.NUMBER_LIST,
        "{claim}",
        ("из 10", "прогонов", "%", "раз из"),
        "конкретика",
        0.66,
        "в источнике есть число, а не оценка",
    ),
    HookPattern(
        "qa_mystery",
        HookCategory.MYSTERY,
        "{claim}",
        ("похоже", "непонятн", "не воспроизвод", "странн", "shared state"),
        "интрига",
        0.57,
        "источник описывает неочевидное поведение",
    ),
    HookPattern(
        "qa_cost",
        HookCategory.COST,
        "{claim}",
        ("два дня", "стоил", "часов", "дней", "потрат", "замороз", "блокир"),
        "цена проблемы",
        0.63,
        "источник называет цену проблемы (время/релиз)",
    ),
    HookPattern(
        "qa_root_cause",
        HookCategory.ROOT_CAUSE,
        "{claim}",
        ("причина", "root cause", "оказалась в", "из-за", "порядок запуска"),
        "ясность",
        0.64,
        "источник называет причину",
    ),
    HookPattern(
        "qa_ci",
        HookCategory.CI_SLOWDOWN,
        "{claim}",
        (" ci", "ci,", "pipeline", "сборка", "runner"),
        "контекст",
        0.55,
        "источник про CI-конвейер, а не про локальный запуск",
    ),
    HookPattern(
        "qa_myth",
        HookCategory.MYTH_BUSTING,
        "{claim}",
        ("миф", "на самом деле", "не в том", "не поможет", "бесполезн"),
        "разоблачение",
        0.59,
        "источник опровергает ожидаемое объяснение",
    ),
)

VIBECODING_PATTERNS: Tuple[HookPattern, ...] = (
    HookPattern(
        "vibe_speed",
        HookCategory.SPEED,
        "{claim}",
        ("за вечер", "за час", "за ночь", "weekend", "быстро"),
        "скорость",
        0.66,
        "источник называет срок сборки",
    ),
    HookPattern(
        "vibe_evening",
        HookCategory.EVENING_BUILD,
        "{claim}",
        ("за вечер", "вечер", "после работы"),
        "«я тоже так смогу»",
        0.68,
        "источник описывает сборку за один вечер",
    ),
    HookPattern(
        "vibe_agent",
        HookCategory.AGENT_WORKFLOW,
        "{claim}",
        ("агент", "agent", "пайплайн", "pipeline", "автоматич", "модул"),
        "интерес к архитектуре",
        0.62,
        "источник описывает автоматизацию/агента",
    ),
    HookPattern(
        "vibe_local",
        HookCategory.LOCAL_AI,
        "{claim}",
        ("локальн", "local", "ollama", "gguf", "на своей машине"),
        "автономность",
        0.6,
        "источник про локальную модель",
    ),
    HookPattern(
        "vibe_prompt",
        HookCategory.PROMPT_TO_PRODUCT,
        "{claim}",
        ("промпт", "prompt"),
        "простота входа",
        0.58,
        "источник показывает путь от промпта к продукту",
    ),
    HookPattern(
        "vibe_experiment",
        HookCategory.EXPERIMENT,
        "{claim}",
        ("эксперимент", "попробовал", "проверил", "собрал"),
        "любопытство",
        0.56,
        "источник описывает эксперимент",
    ),
    HookPattern(
        "vibe_antipattern",
        HookCategory.ANTI_PATTERN,
        "{claim}",
        ("не работает", "грабл", "ошибк", "проблем"),
        "облегчение",
        0.57,
        "источник описывает грабли",
    ),
    HookPattern(
        "vibe_stack",
        HookCategory.STACK_TOUR,
        "{claim}",
        ("стек", "fastapi", "streamlit", "sqlite", "python", "deepseek", "pydantic"),
        "технический интерес",
        0.55,
        "источник перечисляет стек",
    ),
    HookPattern(
        "vibe_tools",
        HookCategory.TOOL_COMPARISON,
        "{claim}",
        ("deepseek", "gemini", "openai", "replicate", "модель"),
        "сравнение",
        0.54,
        "источник называет конкретные инструменты",
    ),
    HookPattern(
        "vibe_ai_vs_human",
        HookCategory.AI_VS_HUMAN,
        "{claim}",
        ("вместо", " ai", "ии ", "модель отвечает"),
        "любопытство",
        0.53,
        "источник показывает, что делает модель, а что человек",
    ),
)

PATTERNS_BY_VERTICAL: Dict[CarouselVertical, Tuple[HookPattern, ...]] = {
    CarouselVertical.TRAVEL: TRAVEL_PATTERNS,
    CarouselVertical.QA: QA_PATTERNS,
    CarouselVertical.VIBECODING: VIBECODING_PATTERNS,
}


class HookEngine:
    """Proposes ranked hooks for a researched context (supervised by default)."""

    def __init__(self, *, max_text_chars: int = 100) -> None:
        self.max_text_chars = max_text_chars

    def patterns_for(self, vertical: object) -> Tuple[HookPattern, ...]:
        """Pattern families of a vertical; hybrid may borrow from every face."""
        profile = profile_for(vertical)
        if profile.vertical is CarouselVertical.HYBRID:
            return TRAVEL_PATTERNS + QA_PATTERNS + VIBECODING_PATTERNS
        return PATTERNS_BY_VERTICAL.get(profile.vertical, ())

    def source_lines(self, context: CarouselSourceContext) -> List[str]:
        """Facts to mine, in source order, deduplicated."""
        lines: List[str] = []
        for fact in context.sourced_facts:
            if fact.text and fact.text not in lines:
                lines.append(fact.text)
        for fact in context.facts:
            if fact and fact not in lines:
                lines.append(fact)
        return lines

    def generate(
        self,
        context: CarouselSourceContext,
        *,
        vertical: object = CarouselVertical.HYBRID,
        limit: int = 5,
    ) -> List[CarouselHookCandidate]:
        """Ranked candidates; an empty source yields an empty list, never filler."""
        profile = profile_for(vertical)
        lines = self.source_lines(context)
        if not lines:
            return []

        candidates: List[CarouselHookCandidate] = []
        used_lines: set = set()
        for pattern in self.patterns_for(profile.vertical):
            line = next((line for line in lines if line not in used_lines and pattern.matches(line)), "")
            if not line:
                continue
            used_lines.add(line)
            scores = self._score(pattern, line)
            candidates.append(
                CarouselHookCandidate(
                    category=pattern.category.value,
                    pattern=pattern.key,
                    text=self._render(pattern, line),
                    score=round(min(sum(scores.values()), 1.0), 2),
                    expected_emotion=pattern.expected_emotion,
                    rationale=pattern.rationale,
                    source_support=line,
                    scores_json=json.dumps(scores, ensure_ascii=False),
                )
            )

        candidates.sort(key=lambda candidate: (-candidate.score, candidate.pattern))
        logger.debug(
            "hook engine: {} candidates from {} source lines (vertical={})",
            len(candidates),
            len(lines),
            profile.vertical.value,
        )
        return candidates[: max(limit, 0)]

    def _score(self, pattern: HookPattern, line: str) -> Dict[str, float]:
        """Explainable score: pattern strength + concrete numbers + source length."""
        scores: Dict[str, float] = {"pattern": round(pattern.base_score, 2)}
        if _DIGITS.search(line):
            scores["numbers"] = 0.12
        if normalize(line) and len(line.split()) >= 6:
            scores["detail"] = 0.08
        return scores

    def _render(self, pattern: HookPattern, line: str) -> str:
        """Fill the template and keep the hook inside the headline budget."""
        claim = " ".join(line.split())
        if len(claim) > self.max_text_chars:
            claim = claim[: self.max_text_chars].rsplit(" ", 1)[0]
        return pattern.template.format(claim=claim)


__all__ = [
    "HookEngine",
    "HookPattern",
    "PATTERNS_BY_VERTICAL",
    "QA_PATTERNS",
    "TRAVEL_PATTERNS",
    "VIBECODING_PATTERNS",
]
