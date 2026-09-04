"""ADR-101 — API and CLI entrypoints use config.yaml as the single config source.

Regression guard: entrypoints must NOT construct a bare ``Config()`` (which
would silently ignore ``app.dry_run``, ``publishing.*`` and any user-edited
settings). They must delegate to ``core.config.load_config_file``.
"""

from core.config import Config


def test_app_loads_config_from_yaml(monkeypatch):
    sentinel = Config()
    import core.config as cc

    monkeypatch.setattr(cc, "load_config_file", lambda: sentinel)

    from app import _load_config

    assert _load_config() is sentinel


def test_cli_loads_config_from_yaml(monkeypatch):
    sentinel = Config()
    import core.config as cc

    monkeypatch.setattr(cc, "load_config_file", lambda: sentinel)

    from cli import _load_config

    assert _load_config() is sentinel


def test_load_config_file_returns_validated_setting_from_file(tmp_path, monkeypatch):
    """End-to-end: a real config.yaml value (dry_run) reaches the caller."""
    import core.config as cc

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("app:\n  dry_run: false\n", encoding="utf-8")
    monkeypatch.setattr(cc, "CONFIG_PATH", cfg_path)

    cfg = cc.load_config_file()
    assert cfg.app.dry_run is False
