"""CLI + UI wiring for the Carousel Factory (Phase 7).

The CLI walk here is the offline one: a fixture context stands in for a source
that was never fetched, so nothing in the assertions is a claim about the web.
"""

import asyncio
import json

import pytest

import cli
from core.database import Database  # noqa: F401 - keeps the import path honest
from core.exceptions import CarouselError
from modules.carousels.service import CarouselFactory

FIXTURE = {
    "source_type": "url",
    "vertical": "travel",
    "source_url": "http://example.invalid/porto",
    "external_id": "porto",
    "title": "Porto on foot",
    "summary": "Fixture context: three days, transit costs, the two decks of the bridge.",
    "facts": [
        "The metro from the airport takes 27 minutes.",
        "Coffee past the bridge costs 1.20 EUR.",
    ],
    "sourced_facts": [
        {"text": "The metro from the airport takes 27 minutes.", "source": "fixture"},
    ],
    "confidence": 0.9,
    "raw_payload_json": "{}",
}

ACTIONS = [
    "create", "list", "status", "research", "draft", "plan", "render",
    "verify", "submit", "queue", "approve", "reject", "publish",
    "collect", "metrics", "score", "refresh", "learnings", "advice",
]


def _run(args):
    return asyncio.run(cli._dispatch(cli._build_parser().parse_args(args)))


@pytest.fixture()
def db_args(tmp_path):
    return ["--db", str(tmp_path / "cli.db")]


@pytest.fixture()
def fixture_path(tmp_path):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return str(path)


def test_parser_accepts_every_carousel_action():
    parser = cli._build_parser()

    for action in ACTIONS:
        args = parser.parse_args(["carousel", action, "--job-id", "1"])

        assert args.action == action
        assert args.job_id == 1


def test_parser_rejects_an_unknown_source_type():
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["carousel", "create", "--source-type", "nope"])


def test_dry_run_walk_from_source_to_publication(db_args, fixture_path):
    created = _run(["carousel", "create", "--source", FIXTURE["source_url"], *db_args])
    job_id = str(created["job_id"])
    assert created["status"] == "pending"

    researched = _run(
        ["carousel", "research", "--job-id", job_id, "--fixture", fixture_path, *db_args]
    )
    assert researched["status"] == "researched"
    assert researched["warnings"], "a dry run says out loud that it fetched nothing"

    assert _run(["carousel", "draft", "--job-id", job_id, *db_args])["status"] == "narrative_drafted"
    planned = _run(["carousel", "plan", "--job-id", job_id, *db_args])
    assert planned["slides"] == 6

    rendered = _run(["carousel", "render", "--job-id", job_id, *db_args])
    assert rendered["status"] == "rendered"
    assert rendered["slides"] == 6

    verified = _run(["carousel", "verify", "--job-id", job_id, *db_args])
    assert verified["checked"] == 6
    assert verified["failed"] == []

    submitted = _run(["carousel", "submit", "--job-id", job_id, *db_args])
    assert submitted["status"] == "awaiting_approval"

    queue = _run(["carousel", "queue", *db_args])
    assert [row["job_id"] for row in queue["queue"]] == [int(job_id)]

    approved = _run(
        ["carousel", "approve", "--job-id", job_id, "--by", "tester", "--note", "ok", *db_args]
    )
    assert approved["status"] == "approved"

    published = _run(["carousel", "publish", "--job-id", job_id, *db_args])
    rows = sorted(published["publications"], key=lambda row: row["platform"])
    assert [row["platform"] for row in rows] == ["instagram", "tiktok"]
    # dry_run records the intent as MANUAL and uploads nothing
    assert {row["status"] for row in rows} == {"manual"}
    assert {row["request_id"] for row in rows} == {""}


def test_dry_run_collects_nothing_and_says_so(db_args, fixture_path):
    created = _run(["carousel", "create", "--source", FIXTURE["source_url"], *db_args])
    job_id = str(created["job_id"])
    _run(["carousel", "research", "--job-id", job_id, "--fixture", fixture_path, *db_args])
    _run(["carousel", "draft", "--job-id", job_id, *db_args])
    _run(["carousel", "plan", "--job-id", job_id, *db_args])
    _run(["carousel", "render", "--job-id", job_id, *db_args])
    _run(["carousel", "verify", "--job-id", job_id, *db_args])
    _run(["carousel", "submit", "--job-id", job_id, *db_args])
    _run(["carousel", "approve", "--job-id", job_id, *db_args])
    _run(["carousel", "publish", "--job-id", job_id, *db_args])

    collected = _run(["carousel", "collect", "--job-id", job_id, *db_args])
    assert collected["collected"] == 0, "a dry run has nothing to read back"
    assert collected["metrics"] == []

    # no metrics -> no score, and no invented number
    with pytest.raises(CarouselError, match="метрик"):
        _run(["carousel", "score", "--job-id", job_id, *db_args])


def test_rejection_needs_a_reason(db_args):
    created = _run(["carousel", "create", "--source", FIXTURE["source_url"], *db_args])

    refused = _run(["carousel", "reject", "--job-id", str(created["job_id"]), *db_args])

    assert "note" in refused["error"]


def test_actions_that_need_a_job_say_so(db_args):
    assert "job-id" in _run(["carousel", "status", *db_args])["error"]
    assert "job-id" in _run(["carousel", "publish", *db_args])["error"]


def test_ui_pipeline_buttons_match_the_service():
    from ui import carousel_page

    for step, _label in carousel_page.STEPS:
        assert callable(getattr(CarouselFactory, step, None)), step
    assert callable(carousel_page.render)
