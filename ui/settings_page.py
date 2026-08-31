"""VibeCoding settings — provider, keys, model, schedule, auto-publish.

Writes settings to ``config.yaml`` (block style) and API keys to ``.env`` —
secrets are NEVER written into ``config.yaml`` (project rule). Reloads the
settings cache after saving so the running app picks the values up.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import yaml
import streamlit as st

from core.config import get_settings, reload_settings

_CONFIG = os.path.join(_ROOT, "config.yaml")
_ENV = os.path.join(_ROOT, ".env")

_DAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _read_config_dict() -> dict:
    with open(_CONFIG, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _write_config_dict(data: dict) -> None:
    with open(_CONFIG, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _update_env(pairs: dict) -> None:
    """Upsert key=value rows into .env without rewriting unrelated secrets."""
    existing = {}
    if os.path.exists(_ENV):
        for line in open(_ENV, encoding="utf-8"):
            line = line.rstrip("\n")
            if not line or line.startswith("#") or "=" not in line:
                continue
            k = line.split("=", 1)[0].strip()
            existing[k] = line
    for k, v in pairs.items():
        if v:  # only write non-empty keys; empty means "leave as-is"
            existing[k] = f"{k}={v}"
    with open(_ENV, "w", encoding="utf-8") as fh:
        fh.write("\n".join(existing.values()) + ("\n" if existing else ""))


def render() -> None:
    st.subheader("⚙️ Настройки VibeCoding")
    cfg = get_settings()
    v = cfg.vibecoding

    with st.expander("🎨 Генерация изображений", expanded=True):
        provider = st.selectbox("Провайдер", ["replicate", "openai", "huggingface", "mock"],
                                index=["replicate", "openai", "huggingface", "mock"].index(v.image_generation.provider))
        model = st.text_input("Модель", value=v.image_generation.model,
                              help="Replicate: stability-ai/sdxl; OpenAI: dall-e-3; HF: модель на inference")
        ig = v.image_generation
        c1, c2, c3 = st.columns(3)
        width = c1.number_input("Ширина", min_value=64, max_value=2048, value=int(ig.width or 1024), step=64)
        height = c2.number_input("Высота", min_value=64, max_value=2048, value=int(ig.height or 1024), step=64)
        num = c3.number_input("Кол-во изображений", min_value=1, max_value=4, value=int(ig.num_outputs or 1))
        api_key_env = st.selectbox("Переменная ключа", ["REPLICATE_API_TOKEN", "OPENAI_API_KEY", "HUGGINGFACE_API_KEY"],
                                   index=["REPLICATE_API_TOKEN", "OPENAI_API_KEY", "HUGGINGFACE_API_KEY"].index(ig.api_key_env))

    with st.expander("🔑 API-ключи (пишутся в .env, не в config.yaml)"):
        rkey = st.text_input("Replicate API Token", value="", type="password")
        okey = st.text_input("OpenAI API Key", value="", type="password")
        hkey = st.text_input("HuggingFace Token", value="", type="password")

    with st.expander("🧠 Текст (DeepSeek)"):
        # DeepSeek model is taken from the shared `deepseek` config block; here we
        # only expose the VibeCoding text-generation overrides.
        max_tokens = st.number_input("Max tokens", min_value=256, max_value=4096, value=int(v.text_generation.max_tokens or 1500), step=64)
        temperature = st.slider("Temperature", 0.0, 1.5, float(v.text_generation.temperature))

    with st.expander("🗓 Расписание"):
        auto = st.checkbox("Автопубликация черновиков", value=bool(v.auto_publish))
        enabled = st.checkbox("VibeCoding включён", value=bool(v.enabled))
        stime = st.text_input("Время публикации (HH:MM)", value=v.schedule_time)
        days = st.multiselect("Дни недели (0=Пн ... 6=Вс)", list(range(7)),
                              default=v.schedule_days if v.schedule_days else [0, 1, 2, 3, 4, 5, 6],
                              format_func=lambda d: f"{d} — {_DAY_LABELS[d]}")

    if st.button("💾 Сохранить настройки", type="primary"):
        try:
            data = _read_config_dict()
            section = data.setdefault("vibecoding", {})
            section["enabled"] = bool(enabled)
            section["schedule_time"] = stime.strip() or "12:00"
            section["schedule_days"] = sorted(set(int(d) for d in days))
            section["auto_publish"] = bool(auto)
            ig = section.setdefault("image_generation", {})
            ig["provider"] = provider
            ig["model"] = model.strip()
            ig["api_key_env"] = api_key_env
            ig["width"] = int(width)
            ig["height"] = int(height)
            ig["num_outputs"] = int(num)
            tg = section.setdefault("text_generation", {})
            tg["max_tokens"] = int(max_tokens)
            tg["temperature"] = float(temperature)
            _write_config_dict(data)
            _update_env({
                "REPLICATE_API_TOKEN": rkey,
                "OPENAI_API_KEY": okey,
                "HUGGINGFACE_API_KEY": hkey,
            })
            reload_settings()
            st.success("Настройки сохранены и перезагружены.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Не удалось сохранить: {exc}")

    st.caption("Секреты записываются только в .env. config.yaml хранит только настройки (блочным стилем).")
