"""Whatever the executors answer, the run ends and the liveness invariants hold.

The search starts by measuring the input, then plans, implements, reviews and measures
candidates. Each measurement can succeed, be refused for its workload, end without
evidence because the evaluation infrastructure failed, or never be answered (lost);
each agent turn can reply, reply with something unparsable, fail in a retryable way, or
be lost. These tests generate sequences of those outcomes, run the whole search on the
production shell, and check `tests.support.liveness`: a bounded number of requests, no
request repeated without new information, and a terminal run with nothing left open.
"""

from __future__ import annotations

from collections import deque
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.liveness import Journal, spin_violations
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import run_shell

from vs_core.api import (
    DispatchTurn,
    MeasurementFailure,
    ResourceId,
    ResumeSessionTurn,
    SubmitMeasurement,
)
from vs_core.testing.drive import Failed, Retryable, Succeeded, Unknown

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.testing.drive import Answer


class Measurement(StrEnum):
    SUCCEEDED = "succeeded"
    WORKLOAD_FAILURE = "workload_failure"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    LOST = "lost"


class Turn(StrEnum):
    REPLIES = "replies"
    UNPARSABLE = "unparsable"
    RETRYABLE_FAILURE = "retryable_failure"
    LOST = "lost"


def _next[T](outcomes: list[T], position: int, default: T) -> T:
    """The outcome for the ``position``-th occurrence; the last one repeats, none means default."""
    if not outcomes:
        return default
    return outcomes[min(position, len(outcomes) - 1)]


class Scenario:
    """Executors that answer each measurement and agent turn with its generated outcome."""

    def __init__(self, measurements: list[Measurement], turns: list[Turn]) -> None:
        self.executors = Executors(
            planner=deque([plan_reply(implement("h1"))]),
            implementer=deque([implemented()]),
            judge=deque([reviewed()]),
        )
        self._measurements = measurements
        self._turns = turns
        self._submitted = 0
        self._asked = 0
        self._outcomes: dict[str, Measurement] = {}
        self.executors.infrastructure_failure = lambda plan: (
            self._outcomes.get(plan.model_dump_json()) is Measurement.INFRASTRUCTURE_FAILURE
        )

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, SubmitMeasurement):
            outcome = _next(self._measurements, self._submitted, Measurement.SUCCEEDED)
            self._submitted += 1
            self._outcomes[request.plan.model_dump_json()] = outcome
            if outcome is Measurement.WORKLOAD_FAILURE:
                return Failed(MeasurementFailure.WORKLOAD)
            if outcome is Measurement.LOST:
                return Unknown()
        if isinstance(request, DispatchTurn | ResumeSessionTurn):
            outcome = _next(self._turns, self._asked, Turn.REPLIES)
            self._asked += 1
            if outcome is Turn.UNPARSABLE:
                lease = ResourceId(root=f"lease:{request.turn.session.session_id.root}")
                return Succeeded(output_json="not json", resource_id=lease)
            if outcome is Turn.RETRYABLE_FAILURE:
                return Retryable()
            if outcome is Turn.LOST:
                return Unknown()
        return self.executors(request, core)


# Outcomes an executor reports and then follows with a later observation or none needed.
_ANSWERED_MEASUREMENTS = [
    Measurement.SUCCEEDED,
    Measurement.WORKLOAD_FAILURE,
    Measurement.INFRASTRUCTURE_FAILURE,
]
_ANSWERED_TURNS = [Turn.REPLIES, Turn.UNPARSABLE]


@settings(max_examples=40, deadline=None, derandomize=True, database=None)
@given(
    measurements=st.lists(st.sampled_from(_ANSWERED_MEASUREMENTS), max_size=5),
    turns=st.lists(st.sampled_from(_ANSWERED_TURNS), max_size=5),
)
def test_every_sequence_of_answered_outcomes_ends_the_run_live(
    measurements: list[Measurement], turns: list[Turn]
) -> None:
    """Success, workload failure, infrastructure failure and bad replies end the run live."""
    run_shell(Scenario(measurements, turns))


# An executor that answers Unknown or a retryable failure and then goes silent leaves its
# request open. Core blocks it once its reconciliation bound passes on the run clock, and
# the strategy ends the work that awaited it.
@pytest.mark.parametrize("position", range(2))
def test_a_lost_measurement_ends_the_run_live(position: int) -> None:
    """The ``position``-th measurement of the run (input, then candidates) is never answered."""
    outcomes = [Measurement.SUCCEEDED] * position + [Measurement.LOST]
    run_shell(Scenario(outcomes, []))


@pytest.mark.parametrize("outcome", [Turn.LOST, Turn.RETRYABLE_FAILURE])
def test_a_turn_the_executor_never_finishes_ends_the_run_live(outcome: Turn) -> None:
    run_shell(Scenario([], [outcome]))


@pytest.mark.parametrize("measurement", _ANSWERED_MEASUREMENTS)
def test_the_committed_ledger_alone_shows_no_repeated_request(measurement: Measurement) -> None:
    """A harness that sees only the committed record (the composition one) checks the same rule."""
    finished = run_shell(Scenario([measurement], []), live=False)

    assert spin_violations(Journal.from_ledger(finished.core)) == []
