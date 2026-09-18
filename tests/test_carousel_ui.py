"""Streamlit smoke test for the Carousel page (real page, stub service layer).

``AppTest`` runs the page code in-process, so these checks are about the UI
contract, not about the pipeline (covered by the pytest suites and the E2E
scripts):

* the Hub offers exactly the source types / verticals the enums define;
* the Pipeline tab shows the slides the service returned and skips the JPG
  notice when a rendered file exists;
* the Approval Queue renders a job that is waiting for a human and its
  approve/reject buttons call the service;
* Analytics and Learnings render the numbers the service returned, verbatim;
* publishing is unavailable until the service says the job may be published
  (the gate is the state machine's answer, not a status string in the UI);
* nothing in the page talks to the network.

The service layer is replaced by a recording stub, so the page cannot mutate a
real database and cannot reach a real platform.
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytest.importorskip("streamlit")
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

from core.models import (  # noqa: E402 - after importorskip
    CarouselJob,
    CarouselLearning,
    CarouselLearningScope,
    CarouselMetric,
    CarouselSlide,
    CarouselSourceType,
    CarouselStatus,
    CarouselVertical,
    SlideType,
)
from modules.carousels.analytics.learnings import Recommendation  # noqa: E402
from modules.carousels.analytics.scoring import CarouselScore  # noqa: E402
from modules.carousels.service import CarouselFactory  # noqa: E402
from ui import carousel_page as page  # noqa: E402

JOB_ID = 7


class StubCarousel:
    """Records every call and returns canned values the UI must render as-is."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Tuple[Any, ...], Dict[str, Any]]] = []
        self.jobs: List[CarouselJob] = [
            CarouselJob(
                id=JOB_ID,
                status=CarouselStatus.AWAITING_APPROVAL,
                vertical=CarouselVertical.QA,
                title="mock qa carousel",
            )
        ]
        self.report: Dict[str, Any] = {
            "status": CarouselStatus.AWAITING_APPROVAL.value,
            "vertical": CarouselVertical.QA.value,
            "next": "approve",
            "can_publish": False,
            "autonomy_mode": "supervised",
            "requires_human_action": True,
            "warnings": [],
        }
        self.slides: List[CarouselSlide] = [
            CarouselSlide(
                id=1,
                job_id=JOB_ID,
                order=1,
                slide_type=SlideType.HERO_HOOK,
                headline="Готовый слайд",
                final_image_path="/tmp/carousel_smoke/1.jpg",
            ),
            CarouselSlide(
                id=2,
                job_id=JOB_ID,
                order=2,
                slide_type=SlideType.PROBLEM,
                headline="Ещё не отрисован",
                final_image_path="",
            ),
        ]
        self.metrics: List[CarouselMetric] = [
            CarouselMetric(
                id=1,
                job_id=JOB_ID,
                platform="tiktok",
                metric_name="views",
                metric_value=120.0,
                raw_value="120",
                source="platform_api",
            )
        ]
        self.learnings: List[CarouselLearning] = [
            CarouselLearning(
                id=1,
                scope_type=CarouselLearningScope.VERTICAL,
                scope_value="qa",
                metric_name="engagement_rate",
                metric_value=0.0625,
                sample_size=4,
                confidence=0.5,
            )
        ]
        self.score = CarouselScore(
            score=0.42, components={"engagement_rate": 0.42}, basis="rates_only", sample_size=4
        )
        self.recommendation_rows: List[Recommendation] = [
            Recommendation(
                scope_type="vertical",
                scope_value="qa",
                metric_name="engagement_rate",
                metric_value=0.0625,
                sample_size=4,
                confidence=0.5,
                rationale="выборка ещё мала",
            )
        ]

    # -- recording helpers -------------------------------------------------
    def _log(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def called(self, name: str) -> List[Tuple[str, Tuple[Any, ...], Dict[str, Any]]]:
        return [call for call in self.calls if call[0] == name]

    # -- service surface used by the page ----------------------------------
    async def create_job(self, **kwargs: Any) -> CarouselJob:
        self._log("create_job", **kwargs)
        return self.jobs[0]

    async def list_jobs(self, status: Optional[str] = None, limit: int = 50) -> List[CarouselJob]:
        self._log("list_jobs", status, limit)
        return list(self.jobs)

    async def status_report(self, job_id: int) -> Dict[str, Any]:
        self._log("status_report", job_id)
        return dict(self.report)

    async def get_bundle(self, job_id: int) -> SimpleNamespace:
        self._log("get_bundle", job_id)
        return SimpleNamespace(slides=list(self.slides), publications=[])

    async def approval_queue(self, limit: int = 50) -> List[CarouselJob]:
        self._log("approval_queue", limit)
        return list(self.jobs)

    async def approve(self, job_id: int, approved_by: str = "", note: str = "") -> CarouselJob:
        self._log("approve", job_id, approved_by, note)
        return self.jobs[0]

    async def reject(self, job_id: int, reason: str = "", rejected_by: str = "") -> CarouselJob:
        self._log("reject", job_id, reason, rejected_by)
        return self.jobs[0]

    async def publish(self, job_id: int) -> List[Any]:
        self._log("publish", job_id)
        return []

    async def collect_metrics(self, job_id: int) -> List[CarouselMetric]:
        self._log("collect_metrics", job_id)
        return []

    async def score_job(self, job_id: int) -> CarouselScore:
        self._log("score_job", job_id)
        return self.score

    async def list_metrics(self, job_id: int) -> List[CarouselMetric]:
        self._log("list_metrics", job_id)
        return list(self.metrics)

    async def refresh_learnings(self, vertical: Optional[str] = None) -> List[CarouselLearning]:
        self._log("refresh_learnings", vertical)
        return list(self.learnings)

    async def list_learnings(self, limit: int = 200) -> List[CarouselLearning]:
        self._log("list_learnings", limit)
        return list(self.learnings)

    async def recommendations(
        self, vertical: Optional[str] = None, limit: int = 5
    ) -> List[Recommendation]:
        self._log("recommendations", vertical, limit)
        return list(self.recommendation_rows)

    # the six pipeline steps: one service method each
    async def research(self, job_id: int) -> Any:
        self._log("research", job_id)
        return SimpleNamespace()

    async def draft_narrative(self, job_id: int) -> Any:
        self._log("draft_narrative", job_id)
        return SimpleNamespace()

    async def plan_slides(self, job_id: int) -> Any:
        self._log("plan_slides", job_id)
        return SimpleNamespace()

    async def render_slides(self, job_id: int) -> Any:
        self._log("render_slides", job_id)
        return SimpleNamespace()

    async def verify_slides(self, job_id: int) -> Any:
        self._log("verify_slides", job_id)
        return SimpleNamespace()

    async def submit_for_approval(self, job_id: int) -> Any:
        self._log("submit_for_approval", job_id)
        return SimpleNamespace()


class StubDatabase:
    """One connection per rerun is the page's rule; here it is a no-op."""

    def __init__(self) -> None:
        self.opened = 0

    async def connect(self) -> None:
        self.opened += 1

    async def close(self) -> None:
        return None


@pytest.fixture()
def stub(monkeypatch: pytest.MonkeyPatch) -> StubCarousel:
    """Replace the service layer and forbid any network traffic."""
    fake = StubCarousel()
    monkeypatch.setattr(page, "_factory", lambda db, cfg: fake)
    monkeypatch.setattr(page, "Database", StubDatabase)

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the carousel page must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", _blocked, raising=False)
    httpx_module: Any
    try:
        import httpx as _httpx_module

        httpx_module = _httpx_module
    except ImportError:  # pragma: no cover - httpx ships with the project
        httpx_module = None
    if httpx_module is not None:
        monkeypatch.setattr(httpx_module.AsyncClient, "send", _blocked, raising=False)
        monkeypatch.setattr(httpx_module.AsyncClient, "request", _blocked, raising=False)
    return fake


ROOT = str(Path(__file__).resolve().parents[1])


def _page_script(root: str) -> None:  # runs in AppTest's fresh module namespace
    import sys

    if root not in sys.path:
        sys.path.insert(0, root)
    import streamlit as st  # noqa: F401 - the page reads this from its own namespace

    from ui import carousel_page

    carousel_page.render()


def _run() -> Any:
    app = AppTest.from_function(_page_script, kwargs={"root": ROOT})
    app.run(timeout=60)
    return app


def _texts(app: Any) -> str:
    chunks: List[str] = []
    for name in (
        "title",
        "header",
        "subheader",
        "markdown",
        "caption",
        "info",
        "success",
        "warning",
        "error",
    ):
        for element in getattr(app, name, []) or []:
            value = getattr(element, "value", None)
            if value is not None:
                chunks.append(str(value))
    for element in getattr(app, "metric", []) or []:
        chunks.append(f"{element.label} = {element.value}")
    return "\n".join(chunks)


def _button(app: Any, key: str) -> Any:
    for element in getattr(app, "button", []) or []:
        if getattr(element, "key", None) == key:
            return element
    return None


def _click(app: Any, key: str) -> Any:
    element = _button(app, key)
    assert element is not None, f"кнопка {key} не отрисована"
    element.click()
    app.run(timeout=60)
    return app


def _assert_clean(app: Any) -> None:
    assert not app.exception, [e.value for e in app.exception]
    errors = [e.value for e in getattr(app, "error", []) or []]
    assert not errors, errors


def test_import_and_pipeline_steps_are_real_service_methods() -> None:
    assert callable(page.render)
    assert len(page.STEPS) == 6
    for step, label in page.STEPS:
        assert callable(getattr(CarouselFactory, step)), f"{step} — не метод сервиса"
        assert label


def test_hub_lists_enum_source_types_and_verticals(stub: StubCarousel) -> None:
    app = _run()
    _assert_clean(app)

    forms = [box for box in app.selectbox if box.key == "cf_type"]
    assert forms, "селектор типа источника не отрисован"
    assert list(forms[0].options) == [kind.value for kind in CarouselSourceType]

    verticals = [box for box in app.selectbox if box.key == "cf_vertical"]
    assert verticals
    assert list(verticals[0].options) == ["auto", *(v.value for v in CarouselVertical)]


def test_pipeline_renders_slides_and_only_notes_missing_jpgs(stub: StubCarousel) -> None:
    from PIL import Image

    rendered = Path("/tmp/carousel_smoke/1.jpg")
    rendered.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (20, 20, 20)).save(rendered, "JPEG")
    try:
        app = _run()
        _assert_clean(app)
        text = _texts(app)
        assert "Готовый слайд" in text
        assert "Ещё не отрисован" in text
        # the first slide has a rendered JPG on disk, the second does not
        assert text.count("JPG ещё не отрисован") == 1
        assert [call for call in stub.called("status_report")], "статус не запрашивался"
    finally:
        rendered.unlink(missing_ok=True)


def test_every_pipeline_button_calls_its_service_method(stub: StubCarousel) -> None:
    app = _run()
    _assert_clean(app)
    for step, _label in page.STEPS:
        app = _click(app, f"cf_step_{step}")
        _assert_clean(app)
        calls = stub.called(step)
        assert calls, f"кнопка {step} не вызвала сервис"
        assert calls[0][1][0] == JOB_ID


def test_approval_queue_renders_waiting_job_and_decisions_reach_the_service(
    stub: StubCarousel,
) -> None:
    app = _run()
    _assert_clean(app)
    text = _texts(app)
    assert f"#{JOB_ID} · qa · mock qa carousel" in text
    assert "Публикация без согласования запрещена" in text

    app = _click(app, f"cf_q_yes_{JOB_ID}")
    _assert_clean(app)
    assert stub.called("approve"), "кнопка «Согласовать» не вызвала approve"

    app = _click(app, f"cf_q_no_{JOB_ID}")
    _assert_clean(app)
    assert stub.called("reject"), "кнопка «Отклонить» не вызвала reject"


def test_analytics_and_learnings_render_returned_numbers(stub: StubCarousel) -> None:
    app = _run()
    _assert_clean(app)
    text = _texts(app)

    assert "tiktok · views = 120" in text
    assert "carousel_score = 0.4200" in text
    assert "raw '120'" in text

    assert "vertical/qa · engagement_rate = 0.0625 · n=4 · confidence 0.50" in text
    assert "выборка ещё мала" in text


def test_publish_is_unavailable_while_the_service_says_no(stub: StubCarousel) -> None:
    app = _run()
    _assert_clean(app)
    publish_button = _button(app, "cf_publish")
    assert publish_button is not None
    assert getattr(publish_button, "disabled", None) is True

    publish_button.click()  # a disabled button must not run anything
    app.run(timeout=60)
    _assert_clean(app)
    assert not stub.called("publish"), "опубликовать удалось без согласования"


def test_publish_runs_once_the_state_machine_allows_it(stub: StubCarousel) -> None:
    stub.report = {
        "status": CarouselStatus.APPROVED.value,
        "vertical": CarouselVertical.QA.value,
        "next": "publish",
        "can_publish": True,
        "autonomy_mode": "supervised",
        "requires_human_action": False,
        "warnings": [],
    }
    app = _run()
    _assert_clean(app)
    publish_button = _button(app, "cf_publish")
    assert publish_button is not None
    assert getattr(publish_button, "disabled", None) is False

    app = _click(app, "cf_publish")
    _assert_clean(app)
    calls = stub.called("publish")
    assert calls and calls[0][1][0] == JOB_ID
