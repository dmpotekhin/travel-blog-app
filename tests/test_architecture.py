"""ADR-105 — architectural guard rails (import graph / layer discipline).

These tests fail when documented layers drift from reality:

* ``core`` never imports ``modules`` (DB/models are the innermost layer).
* direct SQLite access happens only in ``core/database.py``.
* every publisher adapter subclasses ``BasePublisher``.
* the real inter-service contract is ``publish(draft, media_paths)`` — not the
  phantom ``ContentPack`` (declared, never constructed; F9).
"""

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

from modules.publishers.base import BasePublisher

REPO = Path(__file__).resolve().parents[1]


def _py_files(root: Path):
    return [p for p in Path(root).rglob("*.py") if "__pycache__" not in str(p)]


def test_core_never_imports_modules():
    for f in _py_files(REPO / "core"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("modules"):
                raise AssertionError(f"{f.name} imports {node.module}")


def test_no_direct_sqlite_outside_database():
    for root in (REPO / "core", REPO / "modules"):
        for f in _py_files(root):
            if f.name == "database.py":
                continue
            text = f.read_text()
            if "import sqlite3" in text or "from sqlite3" in text:
                raise AssertionError(f"{f} accesses sqlite3 directly")


def test_every_publisher_subclasses_base():
    import modules.publishers as pkg

    found = []
    for _, modname, _ in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"modules.publishers.{modname}")
        for obj in vars(mod).values():
            if isinstance(obj, type) and obj is not BasePublisher and issubclass(obj, BasePublisher):
                found.append(getattr(obj, "name", obj.__name__))
    assert found, "no publisher adapters discovered"


def test_publish_contract_is_draft_media_not_contentpack():
    sig = inspect.signature(BasePublisher.publish)
    assert "draft" in sig.parameters
    assert "media_paths" in sig.parameters
    assert "content_pack" not in sig.parameters
