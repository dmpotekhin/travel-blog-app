"""Shared content-validation scaffolding for the travel/vibecoding validators.

Defines the small value objects a validator returns (a list of checks + the
computed verdict), plus light text helpers (word count, hashtag count, emoji,
forbidden-pattern scan). Kept free of any domain imports so both validators can
reuse it without a cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# A broad, safe emoji range (single emoji codepoints + common dingbats/symbols).
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # pictographs, transport, animals, food
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport
    "\U00002B00-\U00002BFF"   # arrows + misc symbols
    "\U0001F1E6-\U0001F1FF"   # flags
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0000FE30-\U0000FE4F"
    "\U0001F900-\U0001F9FF"
    "]+"
)
_QUESTION_RE = re.compile(
    r"(?:\?|[\wа-яё]{2,}\s+\?\s*$|как\w*\s|что\w*\s|какой\w*\s|где\s|"
    r"когда\s|почему\s|зачем\s|правда|.\w*версия\w*\s*$)",
    re.IGNORECASE | re.UNICODE,
)


def word_count(text: str) -> int:
    """Number of whitespace-separated words (Cyrillic/Latin alike)."""
    return len((text or "").split())


def count_hashtags(text: str) -> int:
    """Count ``#tag`` occurrences in the text."""
    if not text:
        return 0
    return len(re.findall(r"#\w+", text))


def contains_emoji(text: str) -> bool:
    return bool(text and _EMOJI_RE.search(text))


def has_question(text: str) -> bool:
    """Heuristic: is there an engagement question in the text?"""
    if not text:
        return False
    return bool(_QUESTION_RE.search(text))


def find_forbidden(text: str, patterns: List[str]) -> List[str]:
    """Return the subset of ``patterns`` found in ``text`` (case-insensitive)."""
    if not text:
        return []
    low = text.lower()
    return [p for p in patterns if p and p.lower() in low]


@dataclass
class ValidationCheck:
    """One rule in a validation checklist."""

    id: str
    label: str
    passed: bool
    severity: str = "error"     # error (must pass) | warning | info (recommendation)
    message: str = ""
    recommendation: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "passed": self.passed,
            "severity": self.severity,
            "message": self.message,
            "recommendation": self.recommendation,
        }


@dataclass
class ValidationResult:
    """Result of a validator run: the checklist + computed verdict."""

    validator: str = ""
    checks: List[ValidationCheck] = field(default_factory=list)

    @property
    def compliant(self) -> bool:
        """True if every ``error``-severity check passed (warnings don't gate)."""
        return all(c.passed for c in self.checks if c.severity == "error")

    @property
    def score(self) -> float:
        if not self.checks:
            return 100.0
        return round(100.0 * sum(1 for c in self.checks if c.passed) / len(self.checks), 1)

    @property
    def failed(self) -> List[ValidationCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def recommendations(self) -> List[str]:
        out: List[str] = []
        for c in self.checks:
            if c.passed:
                continue
            out.append(c.recommendation or c.message)
        return out

    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c.passed)
        total = len(self.checks)
        verdict = "compliant" if self.compliant else "needs improvement"
        return f"{self.validator}: {passed}/{total} checks passed, score {self.score:.0f}, {verdict}"

    def to_dict(self) -> Dict[str, object]:
        return {
            "validator": self.validator,
            "compliant": self.compliant,
            "score": self.score,
            "summary": self.summary(),
            "checks": [c.to_dict() for c in self.checks],
            "recommendations": self.recommendations,
        }


def check(
    cid: str,
    label: str,
    passed: bool,
    *,
    severity: str = "error",
    message: str = "",
    recommendation: str = "",
) -> ValidationCheck:
    """Tiny constructor helper to keep validator code terse."""
    return ValidationCheck(
        id=cid, label=label, passed=bool(passed), severity=severity,
        message=message, recommendation=recommendation,
    )
