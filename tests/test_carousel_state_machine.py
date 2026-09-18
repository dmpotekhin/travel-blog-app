"""Carousel job state machine: legal moves, illegal moves, approval gate."""

import pytest

from core.exceptions import CarouselError, PublishNotApprovedError, StateTransitionError
from modules.carousels import state_machine as sm
from modules.carousels.enums import CarouselStatus


def test_happy_path_has_only_legal_moves():
    assert len(sm.HAPPY_PATH) >= 10
    pairs = list(zip(sm.HAPPY_PATH, sm.HAPPY_PATH[1:]))
    for current, target in pairs:
        sm.assert_transition(current, target)


def test_happy_path_starts_pending_and_ends_analyzed():
    assert sm.HAPPY_PATH[0] == CarouselStatus.PENDING.value
    assert sm.HAPPY_PATH[-1] == CarouselStatus.ANALYZED.value


def test_illegal_transition_is_refused():
    with pytest.raises(StateTransitionError):
        sm.assert_transition(CarouselStatus.PENDING, CarouselStatus.PUBLISHED)
    with pytest.raises(StateTransitionError):
        sm.assert_transition(CarouselStatus.APPROVED, CarouselStatus.PENDING)


def test_pending_can_only_research_or_fail():
    assert sm.next_states(CarouselStatus.PENDING) == {"researching", "failed"}
    assert sm.next_states("pending") == {"researching", "failed"}


def test_failed_job_may_be_retried():
    assert sm.next_states(CarouselStatus.FAILED) == {"pending", "researching"}
    assert CarouselStatus.RESEARCHING.value in sm.next_states("failed")


def test_needs_revision_can_re_render_or_re_plan_or_ask_for_approval():
    assert sm.next_states(CarouselStatus.NEEDS_REVISION) == {
        "rendering",
        "slides_planned",
        "awaiting_approval",
        "failed",
    }
    assert sm.revision_allowed(CarouselStatus.NEEDS_REVISION) is False  # it *is* the revision state
    assert sm.revision_allowed(CarouselStatus.VERIFYING) is True
    assert sm.revision_allowed(CarouselStatus.PENDING) is False


def test_analyzed_is_terminal():
    assert sm.is_terminal(CarouselStatus.ANALYZED) is True
    assert sm.next_states(CarouselStatus.ANALYZED) == set()
    assert sm.is_terminal(CarouselStatus.PENDING) is False
    assert sm.TERMINAL_STATUSES == {CarouselStatus.ANALYZED.value}


def test_awaiting_approval_and_needs_revision_need_a_human():
    assert sm.HUMAN_ACTION_STATUSES == {
        CarouselStatus.AWAITING_APPROVAL.value,
        CarouselStatus.NEEDS_REVISION.value,
    }
    assert set(sm.iter_human_action_statuses()) == sm.HUMAN_ACTION_STATUSES


def test_verified_may_only_be_approved_or_queued_for_approval():
    assert sm.next_states(CarouselStatus.VERIFIED) == {
        "awaiting_approval",
        "approved",
    }


def test_publish_requires_an_approved_job():
    from modules.carousels.models import CarouselJob

    with pytest.raises(PublishNotApprovedError):
        sm.require_publish_allowed(CarouselJob(id=1, status=CarouselStatus.VERIFIED))
    with pytest.raises(PublishNotApprovedError):
        sm.require_publish_allowed(CarouselJob(id=1, status=CarouselStatus.AWAITING_APPROVAL))
    # approved: the human gate is satisfied, no exception
    sm.require_publish_allowed(CarouselJob(id=1, status=CarouselStatus.APPROVED))


def test_can_publish_follows_core_publishable_statuses():
    from modules.carousels.models import CarouselJob

    assert sm.can_publish(CarouselJob(status=CarouselStatus.APPROVED)) is True
    for status in (
        CarouselStatus.PENDING,
        CarouselStatus.VERIFIED,
        CarouselStatus.AWAITING_APPROVAL,
        CarouselStatus.PUBLISHED,
    ):
        assert sm.can_publish(CarouselJob(status=status)) is False


def test_auto_approval_requires_explicit_config_opt_in():
    assert sm.should_auto_approve(sm.MODE_FULL_AUTONOMOUS, require_human_approval=False) is True
    assert sm.should_auto_approve(sm.MODE_FULL_AUTONOMOUS, require_human_approval=True) is False
    assert sm.should_auto_approve(sm.MODE_SUPERVISED, require_human_approval=False) is False


def test_autonomy_mode_normalisation():
    assert sm.to_autonomy_mode("full_autonomous").value == "full_autonomous"
    assert sm.to_autonomy_mode(" FULL_AUTONOMOUS ").value == "full_autonomous"
    assert sm.to_autonomy_mode(None).value == "supervised"
    with pytest.raises(CarouselError):
        sm.to_autonomy_mode("yolo")


def test_status_value_accepts_enum_and_string():
    assert sm.status_value(CarouselStatus.APPROVED) == "approved"
    assert sm.status_value("approved") == "approved"
    assert sm.status_value(CarouselStatus.SLIDES_PLANNED) == "slides_planned"


def test_transition_table_is_a_copy():
    table = sm.transition_table()
    table["pending"] = {"nothing"}
    assert sm.next_states("pending") == {"researching", "failed"}
    assert sm.allowed_transitions(CarouselStatus.PENDING) == {"researching", "failed"}


def test_describe_and_plan_next_explain_the_job():
    from modules.carousels.models import CarouselJob

    job = CarouselJob(id=7, status=CarouselStatus.SLIDES_PLANNED)
    described = sm.describe(job)
    assert described["job_id"] == 7
    assert described["status"] == "slides_planned"
    assert "rendering" in set(described["next_states"])  # type: ignore[arg-type]
    assert sm.plan_next(job) == "rendering"
    assert sm.plan_next(CarouselJob(status=CarouselStatus.ANALYZED)) is None
