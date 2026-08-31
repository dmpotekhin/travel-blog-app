from fastapi.testclient import TestClient

from app import app


def test_health():
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_stats():
    with TestClient(app) as client:
        r = client.get("/api/stats")
        assert r.status_code == 200
        body = r.json()
        assert "summary" in body
        assert "by_status" in body
        assert "by_platform" in body


def test_calendar():
    with TestClient(app) as client:
        r = client.get("/api/calendar")
        assert r.status_code == 200
        assert "due" in r.json()


def test_scheduler_tick_runs():
    with TestClient(app) as client:
        r = client.post("/api/scheduler/tick")
        assert r.status_code == 200
        assert isinstance(r.json(), dict)
