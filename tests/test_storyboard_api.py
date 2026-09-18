"""Admin-API tests for the Visual Narrative Studio endpoints (ADR-106).

The API module keeps a single Database connection in its lifespan handler, so the
tests point ``core.database.DEFAULT_DB_PATH`` at a temporary file *before* the
TestClient starts and seed that file in their own event loop. The real
``travel_blog.db`` is never touched.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import core.database as database_module
from core import models as m
from core.config import Config
from core.database import Database


def _config() -> Config:
    """Config for the API process: no dry-run, deterministic mock provider."""
    cfg = Config()
    cfg.app.dry_run = False
    cfg.visual_narrative.provider = "mock"
    return cfg


def _seed(db_path: Path, photos: int = 4) -> int:
    """Create a city with scanned photos in a throw-away database."""

    async def run() -> int:
        db = Database(str(db_path))
        await db.connect()
        try:
            city = await db.add_city(m.City(name="Moscow", country="Russia", year=2019))
            for index in range(1, photos + 1):
                await db.add_photo(
                    m.Photo(
                        city_id=city.id,
                        path=f"/archive/Moscow_2019/photo_{index}.jpg",
                        filename=f"photo_{index}.jpg",
                        sha256=f"sha{index}",
                        scan_status=m.ScanStatus.SCANNED,
                    )
                )
            return int(city.id or 0)
        finally:
            await db.close()

    return asyncio.run(run())


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "api.db"
    city_id = _seed(db_path)
    monkeypatch.setattr(database_module, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(app_module, "_load_config", _config)
    with TestClient(app_module.app) as test_client:
        test_client.city_id = city_id  # type: ignore[attr-defined]
        yield test_client


def test_generate_read_and_update_storyboard(client) -> None:
    city_id = client.city_id

    generated = client.post(f"/api/cities/{city_id}/storyboard/generate")
    assert generated.status_code == 200
    body = generated.json()
    assert body["dry_run"] is False
    assert body["storyboard"]["status"] == "draft"
    assert body["storyboard"]["version"] == 1
    assert body["plan"]["shots"]
    storyboard_id = body["storyboard"]["id"]

    read_back = client.get(f"/api/cities/{city_id}/storyboard")
    assert read_back.status_code == 200
    bundle = read_back.json()
    assert bundle["storyboard"]["id"] == storyboard_id
    assert bundle["beats"] and bundle["shots"]
    assert bundle["issues"] == []

    by_id = client.get(f"/api/storyboards/{storyboard_id}")
    assert by_id.status_code == 200
    assert by_id.json()["storyboard"]["title"] == bundle["storyboard"]["title"]

    shot = bundle["shots"][0]
    updated = client.put(
        f"/api/cities/{city_id}/storyboard",
        json={
            "storyboard": {"title": "Москва: ритм города", "logline": "Один день в столице"},
            "shots": [
                {"shot_id": shot["id"], "caption": "Рассвет", "alt_text": "Пустая Красная площадь на рассвете"},
            ],
            "shot_order": [item["id"] for item in reversed(bundle["shots"])],
        },
    )
    assert updated.status_code == 200
    edited = updated.json()
    assert edited["storyboard"]["title"] == "Москва: ритм города"
    assert edited["shots"][0]["id"] == bundle["shots"][-1]["id"]
    assert edited["issues"] == []

    approved = client.post(f"/api/cities/{city_id}/storyboard/approve", json={"force": False})
    assert approved.status_code == 200
    assert approved.json()["storyboard"]["status"] == "approved"

    # an approved storyboard cannot be approved twice (state machine)
    again = client.post(f"/api/cities/{city_id}/storyboard/approve", json={"force": False})
    assert again.status_code == 409
    assert again.json()["code"] == "state_transition_error"


def test_dry_run_preview_is_not_persisted(client) -> None:
    city_id = client.city_id

    preview = client.post(f"/api/cities/{city_id}/storyboard/generate", params={"dry_run": "true"})

    assert preview.status_code == 200
    body = preview.json()
    assert body["dry_run"] is True
    assert body["storyboard"]["id"] is None
    assert body["plan"]["shots"]
    assert client.get(f"/api/cities/{city_id}/storyboard").status_code == 404


def test_approval_is_refused_while_alt_text_is_missing(client) -> None:
    city_id = client.city_id
    client.post(f"/api/cities/{city_id}/storyboard/generate")
    bundle = client.get(f"/api/cities/{city_id}/storyboard").json()

    broken = client.put(
        f"/api/cities/{city_id}/storyboard",
        json={"shots": [{"shot_id": bundle["shots"][0]["id"], "alt_text": ""}]},
    )
    assert broken.status_code == 200
    assert any(issue.startswith("alt_text_missing") for issue in broken.json()["issues"])

    refused = client.post(f"/api/cities/{city_id}/storyboard/approve", json={"force": False})
    assert refused.status_code == 422
    assert refused.json()["code"] == "storyboard_invalid"
    assert any(issue.startswith("alt_text_missing") for issue in refused.json()["issues"])


def test_unknown_city_and_storyboard_return_404(client) -> None:
    assert client.get("/api/cities/99999/storyboard").status_code == 404
    assert client.get("/api/storyboards/99999").status_code == 404
    assert client.post("/api/cities/99999/storyboard/approve", json={}).status_code == 404


def test_shot_order_must_match_the_storyboard(client) -> None:
    city_id = client.city_id
    client.post(f"/api/cities/{city_id}/storyboard/generate")
    bundle = client.get(f"/api/cities/{city_id}/storyboard").json()

    response = client.put(
        f"/api/cities/{city_id}/storyboard",
        json={"shot_order": [item["id"] for item in bundle["shots"][:-1]]},
    )

    assert response.status_code == 422
    assert "shot_order_mismatch" in response.json()["issues"]
