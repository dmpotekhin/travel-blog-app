"""Carousel API surface (Phase 5): the human-in-the-loop gate over HTTP."""

import uuid

import pytest
from fastapi.testclient import TestClient

import core.database as database_module
from app import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """The API on a throwaway database.

    ``Database()`` (built by the app lifespan) reads ``DEFAULT_DB_PATH``, and a
    dashboard process may hold the project file open — so point that constant at
    a temp file instead of competing for the real one.
    """
    monkeypatch.setattr(
        database_module, "DEFAULT_DB_PATH", str(tmp_path / "api.db"), raising=True
    )
    with TestClient(app) as test_client:
        yield test_client


def unique_ref() -> str:
    return f"https://github.com/acme/tool/issues/{uuid.uuid4().int % 100000}"


def create_job(client: TestClient) -> int:
    response = client.post(
        "/api/carousels/jobs",
        json={"source_type": "github_issue", "source_ref": unique_ref(), "vertical": "qa"},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def test_create_and_read_a_job(client: TestClient):
    job_id = create_job(client)
    body = client.get(f"/api/carousels/jobs/{job_id}").json()

    assert body["job"]["id"] == job_id
    assert body["job"]["status"] == "pending"
    assert body["job"]["vertical"] == "qa"
    assert body["slides"] == []
    assert body["publications"] == []


def test_unknown_source_type_is_rejected(client: TestClient):
    response = client.post(
        "/api/carousels/jobs",
        json={"source_type": "telepathy", "source_ref": "x"},
    )
    assert response.status_code == 422


def test_unknown_job_is_404(client: TestClient):
    assert client.get("/api/carousels/jobs/987654321").status_code == 404


def test_approve_before_verification_is_conflict(client: TestClient):
    job_id = create_job(client)
    response = client.post(
        f"/api/carousels/jobs/{job_id}/approve", json={"approved_by": "Братан"}
    )

    assert response.status_code == 409  # illegal transition, not a bad request
    assert "verif" in response.json()["detail"].lower()


def test_publish_without_approval_is_conflict(client: TestClient):
    job_id = create_job(client)
    response = client.post(f"/api/carousels/jobs/{job_id}/publish", json={})

    assert response.status_code == 409
    assert "approval" in response.json()["detail"].lower()


def test_research_with_the_mock_resolver_is_honest_about_empty_input(client: TestClient):
    job_id = create_job(client)
    response = client.post(
        f"/api/carousels/jobs/{job_id}/research", json={"resolver": "mock"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job"]["status"] == "researched"
    assert body["job"]["warnings"]  # empty context must not look confident


def test_job_list_and_approval_queue_are_lists(client: TestClient):
    job_id = create_job(client)

    listing = client.get("/api/carousels/jobs", params={"limit": 5})
    queue = client.get("/api/carousels/queue")
    publications = client.get(f"/api/carousels/jobs/{job_id}/publications")

    assert listing.status_code == 200 and isinstance(listing.json()["items"], list)
    assert queue.status_code == 200 and isinstance(queue.json()["items"], list)
    assert publications.status_code == 200 and publications.json()["items"] == []


def test_reject_needs_a_reason(client: TestClient):
    job_id = create_job(client)
    response = client.post(f"/api/carousels/jobs/{job_id}/reject", json={})

    assert response.status_code == 422  # reason is mandatory


# -- analytics + learnings (Phase 6) ----------------------------------------


def test_metrics_are_empty_until_something_is_collected(client: TestClient):
    job_id = create_job(client)

    listing = client.get(f"/api/carousels/jobs/{job_id}/metrics")
    collect = client.post(f"/api/carousels/jobs/{job_id}/metrics/collect")
    score = client.get(f"/api/carousels/jobs/{job_id}/score")

    assert listing.status_code == 200 and listing.json() == {"items": [], "count": 0}
    # nothing was published, so there is nothing to ask the platform about
    assert collect.status_code == 400
    assert "публикаций" in collect.json()["detail"]
    assert score.status_code == 400
    assert "метрик" in score.json()["detail"]


def test_learnings_and_recommendations_start_empty(client: TestClient):
    refresh = client.post("/api/carousels/learnings/refresh")
    learnings = client.get("/api/carousels/learnings")
    picks = client.get("/api/carousels/recommendations", params={"vertical": "travel"})

    assert refresh.status_code == 200 and refresh.json()["count"] == 0
    assert learnings.status_code == 200 and isinstance(learnings.json()["items"], list)
    assert picks.status_code == 200
    assert picks.json()["items"] == []  # a thin history must not invent advice
    assert picks.json()["vertical"] == "travel"


def test_analytics_summary_describes_the_pipeline(client: TestClient):
    create_job(client)

    response = client.get("/api/carousels/analytics/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["jobs"]["total"] >= 1
    assert "pending" in body["jobs"]["by_status"]
    assert body["autonomy"]["dry_run"] is True
