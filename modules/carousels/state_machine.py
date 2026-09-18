"""Carousel job state machine.

A thin, explicit façade over ``core.models``: carousel code (service, API, UI,
CLI) never reaches into the transition tables directly, and the
human-in-the-loop publication gate lives in exactly one place.

Guarantees:

* every status change goes through :func:`assert_transition` (the repository
  refuses to write ``status`` any other way);
* publishing is only legal for an ``approved`` job — a job is approved either
  by a human or, in ``full_autonomous`` mode, by the service calling
  ``approve()`` explicitly (never silently at publish time).
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional, Set, Union

from core.exceptions import CarouselError, PublishNotApprovedError
from core.models import (
    CAROUSEL_PUBLISHABLE_STATUSES,
    CarouselAutonomyMode,
    CarouselJob,
    CarouselStatus,
    carousel_next_states,
    carousel_publication_transition,
    carousel_transition,
    carousel_transition_table,
)

__all__ = [
    "AUTONOMY_MODES",
    "HAPPY_PATH",
    "HUMAN_ACTION_STATUSES",
    "MODE_FULL_AUTONOMOUS",
    "MODE_SUPERVISED",
    "TERMINAL_STATUSES",
    "allowed_transitions",
    "assert_publication_transition",
    "assert_transition",
    "can_publish",
    "is_terminal",
    "next_states",
    "require_publish_allowed",
    "should_auto_approve",
    "status_value",
    "to_autonomy_mode",
    "transition_table",
]

MODE_SUPERVISED = CarouselAutonomyMode.SUPERVISED.value
MODE_FULL_AUTONOMOUS = CarouselAutonomyMode.FULL_AUTONOMOUS.value

#: Modes config.yaml may declare (``carousels.mode``).
AUTONOMY_MODES: frozenset = frozenset({MODE_SUPERVISED, MODE_FULL_AUTONOMOUS})

#: Nothing follows these states.
TERMINAL_STATUSES: frozenset = frozenset({CarouselStatus.ANALYZED.value})

#: States where a human is expected to act (Approval Queue in the UI).
HUMAN_ACTION_STATUSES: frozenset = frozenset(
    {CarouselStatus.AWAITING_APPROVAL.value, CarouselStatus.NEEDS_REVISION.value}
)

#: Canonical unattended path, used for progress display and the scheduler.
HAPPY_PATH: tuple = (
    CarouselStatus.PENDING.value,
    CarouselStatus.RESEARCHING.value,
    CarouselStatus.RESEARCHED.value,
    CarouselStatus.NARRATIVE_DRAFTED.value,
    CarouselStatus.SLIDES_PLANNED.value,
    CarouselStatus.RENDERING.value,
    CarouselStatus.RENDERED.value,
    CarouselStatus.VERIFYING.value,
    CarouselStatus.VERIFIED.value,
    CarouselStatus.AWAITING_APPROVAL.value,
    CarouselStatus.APPROVED.value,
    CarouselStatus.PUBLISHING.value,
    CarouselStatus.PUBLISHED.value,
    CarouselStatus.ANALYZING.value,
    CarouselStatus.ANALYZED.value,
)


def status_value(status: Union[str, CarouselStatus]) -> str:
    """Normalise a status to its stored string value."""
    if isinstance(status, CarouselStatus):
        return status.value
    return str(status)


def transition_table() -> Dict[str, Set[str]]:
    """Copy of the whole state machine (docs, UI, tests)."""
    return {
        status_value(current): {status_value(target) for target in targets}
        for current, targets in carousel_transition_table().items()
    }


def allowed_transitions(status: Union[str, CarouselStatus]) -> Set[str]:
    """Legal next statuses of ``status`` (empty for terminal states)."""
    return {status_value(target) for target in carousel_next_states(status_value(status))}


def next_states(status: Union[str, CarouselStatus]) -> Set[str]:
    """Alias of :func:`allowed_transitions` (reads better in UI code)."""
    return allowed_transitions(status)


def is_terminal(status: Union[str, CarouselStatus]) -> bool:
    """True when no transition leaves this status."""
    return not allowed_transitions(status)


def assert_transition(current: Union[str, CarouselStatus], target: Union[str, CarouselStatus]) -> None:
    """Raise ``StateTransitionError`` unless ``current -> target`` is legal."""
    carousel_transition(status_value(current), status_value(target))


def assert_publication_transition(
    current: Union[str, CarouselStatus], target: Union[str, CarouselStatus]
) -> None:
    """Validate a per-platform publication status change (shared table)."""
    carousel_publication_transition(status_value(current), status_value(target))


def to_autonomy_mode(mode: Union[str, CarouselAutonomyMode, None]) -> CarouselAutonomyMode:
    """Coerce a config/CLI string to the enum, rejecting unknown modes."""
    if isinstance(mode, CarouselAutonomyMode):
        return mode
    value = str(mode or MODE_SUPERVISED).strip().lower()
    if value not in AUTONOMY_MODES:
        raise CarouselError(
            f"Unknown autonomy mode {mode!r}; expected one of {sorted(AUTONOMY_MODES)}"
        )
    return CarouselAutonomyMode(value)


def can_publish(job: CarouselJob) -> bool:
    """True only for an approved job (supervised mode default)."""
    return status_value(job.status) in {
        status_value(status) for status in CAROUSEL_PUBLISHABLE_STATUSES
    }


def require_publish_allowed(job: CarouselJob) -> None:
    """Raise :class:`PublishNotApprovedError` unless the job may be published.

    There is no autonomy bypass here on purpose: ``full_autonomous`` means the
    *service* approves the job itself (recording ``approved``), not that the
    publisher accepts unapproved work.
    """
    if can_publish(job):
        return
    raise PublishNotApprovedError(
        f"Carousel job {job.id} is {status_value(job.status)!r}; "
        "publishing requires a human approval (status 'approved')"
    )


def should_auto_approve(
    mode: Union[str, CarouselAutonomyMode, None], require_human_approval: bool
) -> bool:
    """True when the service may approve a job on the operator's behalf.

    Requires BOTH ``mode: full_autonomous`` in config AND an explicit
    ``require_human_approval: false`` — two switches, so autonomy can never be
    enabled by accident.
    """
    return to_autonomy_mode(mode) is CarouselAutonomyMode.FULL_AUTONOMOUS and not require_human_approval


def reached_happy_path(status: Union[str, CarouselStatus], step: str) -> bool:
    """True when ``status`` is at or past ``step`` on the canonical path."""
    current = status_value(status)
    try:
        return HAPPY_PATH.index(current) >= HAPPY_PATH.index(step)
    except ValueError:
        return False


def revision_allowed(status: Union[str, CarouselStatus]) -> bool:
    """True when a failed verification may send the job back to re-render."""
    return CarouselStatus.NEEDS_REVISION.value in allowed_transitions(status)


def iter_human_action_statuses() -> Iterable[str]:
    """Statuses that belong in the Approval Queue."""
    return tuple(sorted(HUMAN_ACTION_STATUSES))


def describe(job: CarouselJob) -> Dict[str, object]:
    """Small read model for API/UI: where the job stands and what it allows."""
    return {
        "job_id": job.id,
        "status": status_value(job.status),
        "vertical": job.vertical.value if hasattr(job.vertical, "value") else str(job.vertical),
        "autonomy_mode": (
            job.autonomy_mode.value
            if hasattr(job.autonomy_mode, "value")
            else str(job.autonomy_mode)
        ),
        "next_states": sorted(allowed_transitions(job.status)),
        "is_terminal": is_terminal(job.status),
        "can_publish": can_publish(job),
        "requires_human_action": status_value(job.status) in HUMAN_ACTION_STATUSES,
    }


def plan_next(job: CarouselJob) -> Optional[str]:
    """The next happy-path step for this job, or None if it left the path."""
    current = status_value(job.status)
    if current not in HAPPY_PATH:
        return None
    index = HAPPY_PATH.index(current)
    if index + 1 >= len(HAPPY_PATH):
        return None
    return HAPPY_PATH[index + 1]
