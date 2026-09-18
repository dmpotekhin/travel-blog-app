"""Upload-Post publisher (Phase 5): the wire contract, offline via MockTransport.

Nothing here talks to the network: every request is answered by
``httpx.MockTransport``, and the assertions are about the bytes we would send.
"""

import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from core.models import PublicationStatus
from modules.carousels.publishing import (
    MockCarouselPublisher,
    PublishRequest,
    UploadPostPublisher,
)

TOKEN = "test-token"
USER = "test-user"


def slide_files(tmp_path: Path, count: int = 6) -> list:
    paths = []
    for order in range(1, count + 1):
        path = tmp_path / f"slide_{order:02d}.jpg"
        Image.new("RGB", (768, 1376), (20, 20, 30)).save(path, format="JPEG")
        paths.append(path)
    return paths


def request_for(tmp_path: Path, **overrides) -> PublishRequest:
    payload = dict(
        job_id=11,
        title="Флаки-тест в CI",
        caption="Разбор флаки-теста",
        hashtags=["qa", "тесты"],
        platforms=["tiktok", "instagram"],
        slide_paths=[str(p) for p in slide_files(tmp_path)],
    )
    payload.update(overrides)
    return PublishRequest(**payload)


def publisher_with(handler, **kwargs) -> UploadPostPublisher:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.upload-post.com"
    )
    return UploadPostPublisher(
        token=TOKEN, user=USER, client=client, backoff_seconds=0.0, **kwargs
    )


async def test_publish_sends_one_multipart_call_per_carousel(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        return httpx.Response(200, json={"success": True, "request_id": "req-1"})

    publisher = publisher_with(handler)
    results = await publisher.publish(request_for(tmp_path))
    await publisher.aclose()

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/upload_photos"
    assert seen["auth"] == f"Apikey {TOKEN}"
    assert seen["content_type"].startswith("multipart/form-data")
    body = seen["body"]
    assert body.count(b'name="photos[]"') == 6
    assert b"\xff\xd8\xff" in body  # real JPG bytes, not a placeholder
    assert b'name="user"' in body and USER.encode() in body
    assert b'name="title"' in body
    assert body.count(b'name="platform[]"') == 2
    assert b"tiktok" in body and b"instagram" in body
    assert [r.platform for r in results] == ["tiktok", "instagram"]


async def test_async_upload_returns_processing_with_request_id(tmp_path):
    publisher = publisher_with(
        lambda request: httpx.Response(
            200, json={"success": True, "request_id": "req-async-9"}
        )
    )
    results = await publisher.publish(request_for(tmp_path))
    await publisher.aclose()

    assert {r.status for r in results} == {PublicationStatus.PROCESSING}
    assert {r.request_id for r in results} == {"req-async-9"}
    assert all(json.loads(r.raw_response_json)["success"] is True for r in results)


async def test_synchronous_result_is_read_per_platform(tmp_path):
    payload = {
        "success": True,
        "results": [
            {"platform": "tiktok", "success": True, "post_url": "https://tiktok.com/@x/1"},
            {"platform": "instagram", "success": False, "error": "rate limited"},
        ],
    }
    publisher = publisher_with(lambda request: httpx.Response(200, json=payload))
    results = await publisher.publish(request_for(tmp_path))
    await publisher.aclose()

    by_platform = {r.platform: r for r in results}
    assert by_platform["tiktok"].status is PublicationStatus.PUBLISHED
    assert by_platform["tiktok"].post_url == "https://tiktok.com/@x/1"
    assert by_platform["instagram"].status is PublicationStatus.FAILED
    assert "rate limited" in by_platform["instagram"].error_message


async def test_rejected_upload_is_failed_and_keeps_the_error(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, json={"message": "Invalid API key"})

    publisher = publisher_with(handler)
    results = await publisher.publish(request_for(tmp_path))
    await publisher.aclose()

    assert len(calls) == 1  # a 401 must not be retried
    assert {r.status for r in results} == {PublicationStatus.FAILED}
    assert all("Invalid API key" in r.error_message for r in results)


async def test_server_error_is_retried_then_failed(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503, text="upstream down")

    publisher = publisher_with(handler, max_retries=2)
    results = await publisher.publish(request_for(tmp_path))
    await publisher.aclose()

    assert len(calls) == 3  # initial + 2 retries
    assert {r.status for r in results} == {PublicationStatus.FAILED}
    assert "503" in results[0].error_message


async def test_network_error_is_reported_not_raised(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    publisher = publisher_with(handler, max_retries=0)
    results = await publisher.publish(request_for(tmp_path))
    await publisher.aclose()

    assert {r.status for r in results} == {PublicationStatus.FAILED}
    assert "no route to host" in results[0].error_message


async def test_missing_credentials_fail_loudly(tmp_path):
    publisher = UploadPostPublisher(token="", user="")
    with pytest.raises(Exception) as excinfo:
        await publisher.publish(request_for(tmp_path))
    assert "credential" in str(excinfo.value).lower() or "token" in str(excinfo.value).lower()


async def test_fetch_status_reports_per_platform_results(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/uploadposts/status"
        assert request.url.params.get("request_id") == "req-77"
        return httpx.Response(
            200,
            json={
                "success": True,
                "request_id": "req-77",
                "results": [
                    {"platform": "tiktok", "success": True, "post_url": "https://tiktok.com/@x/77", "views": 1200},
                    {"platform": "instagram", "success": True},
                ],
            },
        )

    publisher = publisher_with(handler)
    report = await publisher.fetch_status("req-77")
    await publisher.aclose()

    assert report.platforms["tiktok"]["post_url"] == "https://tiktok.com/@x/77"
    assert report.platforms["instagram"]["success"] is True
    assert report.success is True
    assert report.raw["success"] is True


async def test_dry_run_publisher_uploads_nothing_and_says_so(tmp_path):
    publisher = MockCarouselPublisher()
    results = await publisher.publish(request_for(tmp_path, dry_run=True))
    await publisher.aclose()

    assert {r.status for r in results} == {PublicationStatus.MANUAL}
    assert {r.platform for r in results} == {"tiktok", "instagram"}
    for result in results:
        raw = json.loads(result.raw_response_json)
        assert raw["dry_run"] is True
        assert result.post_url == ""  # no invented URL
        assert result.error_message == ""
