"""Hook engine package (Phase 3).

``HookEngine`` proposes source-backed hook candidates; nothing is published
without a human picking one in supervised mode.
"""

from .engine import (
    PATTERNS_BY_VERTICAL,
    QA_PATTERNS,
    TRAVEL_PATTERNS,
    VIBECODING_PATTERNS,
    HookEngine,
    HookPattern,
)

__all__ = [
    "HookEngine",
    "HookPattern",
    "PATTERNS_BY_VERTICAL",
    "QA_PATTERNS",
    "TRAVEL_PATTERNS",
    "VIBECODING_PATTERNS",
]
