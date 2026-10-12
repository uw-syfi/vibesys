"""Whatever the executors answer, the run ends and the liveness invariants hold.

The search starts by measuring the input, then plans, implements, reviews and measures
candidates. Each measurement can succeed, be refused for its workload, end without
evidence because the evaluation infrastructure failed, or never be answered (lost);
each agent turn can reply, reply with something unparsable, fail in a retryable way, or
be lost. These tests generate sequences of those outcomes, run the whole search on the
production shell, and check `vs_core.testing.liveness`: a bounded number of requests, no
request repeated without new information, and a terminal run with nothing left open.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.strategy.world import DynamicWorld

from vs_core.api import (
    DispatchTurn,
    MeasurementFailure,
    ObserveOwnedJob,
    OwnedJob,
    ResourceId,
    ResumeSessionTurn,
    SubmitMeasurement,
)
from vs_core.testing.drive import Failed, Retryable, Succeeded, Unknown
from vs_core.testing.liveness import Journal, spin_violations
from vs_dst.api import simulate

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


class Scenario(DynamicWorld):
    """A world that answers each measurement and agent turn with its generated outcome."""

    def __init__(self, measurements: list[Measurement], turns: list[Turn]) -> None:
        super().__init__()
        self._measurements = measurements
        self._turns = turns
        self._submitted_plans = 0
        self._asked = 0
        self._outcomes: dict[str, Measurement] = {}

    def __call__(self, request: Request, core: CoreState) -> Answer:
        answer: Answer | None = None
        if isinstance(request, SubmitMeasurement):
            answer = self._measurement_outcome(request)
        elif isinstance(request, ObserveOwnedJob) and self._ends_without_evidence(request, core):
            answer = Failed(accepted=True)
        elif isinstance(request, DispatchTurn | ResumeSessionTurn):
            answer = self._turn_outcome(request)
        return super().__call__(request, core) if answer is None else answer

    def _measurement_outcome(self, request: SubmitMeasurement) -> Answer | None:
        outcome = _next(self._measurements, self._submitted_plans, Measurement.SUCCEEDED)
        self._submitted_plans += 1
        self._outcomes[request.plan.model_dump_json()] = outcome
        if outcome is Measurement.WORKLOAD_FAILURE:
            return Failed(MeasurementFailure.WORKLOAD)
        return Unknown() if outcome is Measurement.LOST else None

    def _turn_outcome(self, request: DispatchTurn | ResumeSessionTurn) -> Answer | None:
        outcome = _next(self._turns, self._asked, Turn.REPLIES)
        self._asked += 1
        if outcome is Turn.UNPARSABLE:
            lease = ResourceId(root=f"lease:{request.turn.session.session_id.root}")
            return Succeeded(output_json="not json", resource_id=lease)
        if outcome is Turn.RETRYABLE_FAILURE:
            return Retryable()
        return Unknown() if outcome is Turn.LOST else None

    def _ends_without_evidence(self, request: ObserveOwnedJob, core: CoreState) -> bool:
        """Whether the job's measurement ended without any evidence (its infrastructure failed)."""
        job = next(
            item
            for item in core.evaluation.jobs
            if isinstance(item, OwnedJob) and item.resource_id == request.resource_id
        )
        outcome = self._outcomes.get(job.plan.model_dump_json())
        return outcome is Measurement.INFRASTRUCTURE_FAILURE


# Outcomes an executor reports and then follows with a later observation or none needed.
_ANSWERED_MEASUREMENTS = [
    Measurement.SUCCEEDED,
    Measurement.WORKLOAD_FAILURE,
    Measurement.INFRASTRUCTURE_FAILURE,
]
_ANSWERED_TURNS = [Turn.REPLIES, Turn.UNPARSABLE]


# A run asks for at most two measurements (the input, then one candidate) and four turns, so
# longer lists would only repeat runs.
@settings(max_examples=15, deadline=None, derandomize=True, database=None)
@given(
    measurements=st.lists(st.sampled_from(_ANSWERED_MEASUREMENTS), max_size=2),
    turns=st.lists(st.sampled_from(_ANSWERED_TURNS), max_size=4),
)
def test_every_sequence_of_answered_outcomes_ends_the_run_live(
    measurements: list[Measurement], turns: list[Turn]
) -> None:
    """Success, workload failure, infrastructure failure and bad replies end the run live."""
    run = simulate(Scenario(measurements, turns))

    assert run.finished
    assert not run.liveness_violations


# An executor that answers Unknown or a retryable failure and then goes silent leaves its
# request open. Core blocks it once its reconciliation bound passes on the run clock, and
# the strategy ends the work that awaited it.
@pytest.mark.parametrize("position", range(2))
def test_a_lost_measurement_ends_the_run_live(position: int) -> None:
    """The ``position``-th measurement of the run (input, then candidates) is never answered."""
    outcomes = [Measurement.SUCCEEDED] * position + [Measurement.LOST]
    run = simulate(Scenario(outcomes, []))

    assert run.finished
    assert not run.liveness_violations


@pytest.mark.parametrize("outcome", [Turn.LOST, Turn.RETRYABLE_FAILURE])
def test_a_turn_the_executor_never_finishes_ends_the_run_live(outcome: Turn) -> None:
    run = simulate(Scenario([], [outcome]))

    assert run.finished
    assert not run.liveness_violations


@pytest.mark.parametrize("measurement", _ANSWERED_MEASUREMENTS)
def test_the_committed_ledger_alone_shows_no_repeated_request(measurement: Measurement) -> None:
    """A harness that sees only the committed record (the composition one) checks the same rule."""
    finished = simulate(Scenario([measurement], []))

    assert finished.halted is None
    assert spin_violations(Journal.from_ledger(finished.core)) == []
