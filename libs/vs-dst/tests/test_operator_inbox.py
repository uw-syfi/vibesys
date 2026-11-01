"""The operator's inbox requests, through the production control bridge, at any point of a run (#3475).

``vibesys control`` posts a request to the run's durable inbox; the run's `RunControlBridge`
applies it at a poll, once, as core's control ``op-<id>``, and answers it: applied, or rejected
with core's reason. The host of the simulation builds that bridge, so a pause, a withdrawal (a
resume of a held pause) and a request the run refuses land at every commit and effect of a run,
with a host killed after the post, and each ends as the uninterrupted run did and is answered.
"""

from __future__ import annotations

import random
from functools import cache
from typing import TYPE_CHECKING

from vs_dst.api import (
    Action,
    Interrupted,
    Interruption,
    Moment,
    OperatorRequest,
    SimulatedRun,
    StepLoopHosting,
    simulate,
)
from vs_project.api import RequestState
from vs_runtime.api.infrastructure import ControlRequest, PauseRequest, ResumeRequest
from vs_sim.api.testing import swept

from ._turns import TurnsWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState

STEP_LOOP = StepLoopHosting()

EPOCH = 1
"""The epoch of the first host: a pause that names it is for that process, as `vibesys control` sends it."""


@cache
def _normal() -> SimulatedRun:
    normal = simulate(TurnsWorld(2), STEP_LOOP)
    assert normal.finished
    return normal


@cache
def _points() -> tuple[Interruption, ...]:
    """Every moment of the run an operator's request can land on, sampled as the suite's depth says."""
    return tuple(p for moment in Moment for p in _normal().interruptions(Action.PAUSE, moment))


@cache
def _kills() -> tuple[Interruption, ...]:
    return _normal().interruptions(Action.KILL)


def _at(point: Interruption, request: ControlRequest) -> OperatorRequest:
    return OperatorRequest(point.moment, point.index, request)


def _recorded(core: CoreState | None, control_id: str) -> int:
    assert core is not None
    return sum(1 for control in core.run.controls if control.control_id.root == f"op-{control_id}")


def _answered_once(interrupted: Interrupted, control_id: str, why: object) -> RequestState | None:
    """The request is decided, and core holds it exactly when it was applied.

    A request the run refuses (it is finishing, or it targets an epoch that died) is rejected
    with a reason, and reaches nothing.
    """
    assert interrupted.inbox is not None
    status = interrupted.inbox.status(control_id, ended=None)
    if status is None:  # the host ended before the run reached the point the request lands on
        assert _recorded(interrupted.core, control_id) == 0, why
        return None
    recorded = _recorded(interrupted.core, control_id)
    assert status.state is not RequestState.PENDING, f"{why}: {control_id} was never answered"
    assert recorded == (status.state is RequestState.APPLIED), f"{why}: {control_id} {status}"
    assert (status.state is RequestState.REJECTED) == bool(status.reason), why
    if status.state is RequestState.APPLIED:
        _applied_in_its_epoch(interrupted, control_id, why)
    return status.state


def _applied_in_its_epoch(interrupted: Interrupted, control_id: str, why: object) -> None:
    """A pause names the process it is for: no later process applies it."""
    held = f'"op-{control_id}"'.encode()
    epochs = [
        record.epoch
        for record in interrupted.log_records
        if (body := record.body) is not None
        and body.field == "core_input"
        and held in body.value.event
    ]
    assert epochs == [EPOCH] or not control_id.startswith("p"), f"{why}: {control_id} in {epochs}"


def _ended_as_the_uninterrupted_run(interrupted: Interrupted, why: object) -> None:
    detail = f"{why}: {interrupted.outcome.value}; {interrupted.detail}"
    assert interrupted.matches_uninterrupted(), detail
    assert interrupted.never_stuck(), detail
    assert interrupted.every_step_finished_once(), detail


def test_a_pause_posted_at_every_point_is_applied_once_and_the_run_ends_as_before() -> None:
    for number, point in enumerate(_points()):
        interrupted = _normal().operate(
            [_at(point, PauseRequest(control_id=f"p{number}", expected_epoch=EPOCH))]
        )
        _ended_as_the_uninterrupted_run(interrupted, point)
        _answered_once(interrupted, f"p{number}", point)


def test_a_pause_posted_while_the_run_is_going_is_applied() -> None:
    """The run's first commit is far from its end: a pause there is taken."""
    first = _points()[0]
    interrupted = _normal().operate(
        [_at(first, PauseRequest(control_id="p", expected_epoch=EPOCH))]
    )
    assert _answered_once(interrupted, "p", first) is RequestState.APPLIED


def test_a_held_pause_withdrawn_by_a_resume_leaves_the_run_going_at_every_point() -> None:
    """The operator pauses and resumes before the host has wound down: both are answered."""
    points = _points()
    for number, point in enumerate(points):
        later = points[min(number + 1, len(points) - 1)]
        interrupted = _normal().operate(
            [
                _at(point, PauseRequest(control_id=f"p{number}", expected_epoch=EPOCH)),
                _at(later, ResumeRequest(control_id=f"r{number}")),
            ]
        )
        _ended_as_the_uninterrupted_run(interrupted, (point, later))
        _answered_once(interrupted, f"p{number}", point)
        _answered_once(interrupted, f"r{number}", point)


def test_a_request_the_run_refuses_is_rejected_with_a_reason_and_changes_nothing() -> None:
    """A wait for capacity of a run that is not paused on a capacity limit is refused."""
    for number, point in enumerate(_points()):
        asked = ResumeRequest(control_id=f"c{number}", when="capacity_returns")
        interrupted = _normal().operate([_at(point, asked)])
        _ended_as_the_uninterrupted_run(interrupted, point)
        assert interrupted.inbox is not None
        status = interrupted.inbox.status(f"c{number}", ended=None)
        assert status is not None
        assert status.state is RequestState.REJECTED, point
        assert status.reason, point
        assert _recorded(interrupted.core, f"c{number}") == 0, point
        assert interrupted.resumes == 0, f"{point}: a refused request parks no host"


def test_a_host_killed_after_the_post_applies_the_request_in_the_next_process_once() -> None:
    pairs = [(point, kill) for point in _points() for kill in _kills()]
    for number in swept(len(pairs), sample=60):
        point, kill = pairs[number]
        interrupted = _normal().operate(
            [_at(point, PauseRequest(control_id="p", expected_epoch=EPOCH))], kill
        )
        _ended_as_the_uninterrupted_run(interrupted, (point, kill))
        _answered_once(interrupted, "p", (point, kill))


def _mix(seed: int) -> tuple[list[OperatorRequest], Interruption | None]:
    """A few requests of different kinds at random points, and maybe a kill."""
    rng = random.Random(seed)  # noqa: S311  # a seeded draw of test cases, not a secret
    requests: list[OperatorRequest] = []
    for index in range(rng.randint(1, 4)):
        point = rng.choice(_points())
        control_id = f"m{index}"
        request: ControlRequest = rng.choice(
            [
                PauseRequest(control_id=control_id, expected_epoch=EPOCH),
                ResumeRequest(control_id=control_id),
                ResumeRequest(control_id=control_id, when="capacity_returns"),
            ]
        )
        requests.append(_at(point, request))
    return requests, (rng.choice(_kills()) if rng.random() < 0.5 else None)


def test_any_mix_of_requests_and_a_kill_ends_as_before_with_every_request_answered_once() -> None:
    for seed in swept(400, sample=40):
        requests, kill = _mix(seed)
        interrupted = _normal().operate(requests, kill)
        _ended_as_the_uninterrupted_run(interrupted, (seed, requests, kill))
        assert interrupted.inbox is not None
        for sent in requests:
            control_id = sent.request.control_id
            _answered_once(interrupted, control_id, seed)
