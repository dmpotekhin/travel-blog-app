"""Shared Streamlit renderer for a ValidationResult dict (G4).

Used by both the travel Posts tab and the VibeCoding page so the checklist UI is
identical everywhere. Feed it the dict returned by ``ValidationResult.to_dict()``.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st

_ICON = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}


def _severity(sev: str) -> str:
    return sev if sev in _ICON else "info"


def render_validation(result: dict) -> None:
    """Render a compliance checklist (passes ``None``/absent gracefully)."""
    if not result:
        st.info("Проверка: ссылка или данные отсутствуют.")
        return

    compliant = bool(result.get("compliant"))
    score = result.get("score", 0)
    summary = result.get("summary", "")
    validator = result.get("validator", "")

    badge = "✅ Соответствует" if compliant else "⚠️ Требует доработки"
    c1, c2 = st.columns(2)
    c1.metric("Статус", badge)
    c2.metric("Score", f"{score:.0f}%")

    st.caption(summary)

    for ch in result.get("checks", []):
        passed = bool(ch.get("passed"))
        sev = _severity(ch.get("severity", "info"))
        icon = _ICON[sev]
        mark = "✅" if passed else icon
        label = ch.get("label", ch.get("id", ""))
        st.markdown(f"**{mark} {label}**")
        msg = ch.get("message", "")
        rec = ch.get("recommendation", "")
        if msg:
            st.caption(msg)
        if rec and not passed:
            st.markdown(f"💡 {rec}")

    recs = result.get("recommendations", [])
    if recs:
        with st.expander("💡 Рекомендации по улучшению", expanded=False):
            for r in recs:
                st.markdown(f"- {r}")
