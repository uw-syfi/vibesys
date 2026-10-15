"""A slow poll of the input's measurement job never ends the input's measurement.

Core follows a measurement's job by polling it, and each poll is a request with its own short
bound (the backend's reconciliation bound). On a busy cluster one status query can take longer
than that bound. The poll then ends at its bound and the job is polled again; the measurement
itself is ended only by its own limit (counted from the job's start), the run deadline or a stop.
A poll's bound passing once ended the whole baseline measurement with ``timed-out`` while the
job still waited in Slurm, and left the job queued with nothing waiting on it.

The run here is the dynamic search on the real core (``vs_core.testing.drive``): its own
``measure_input`` submits the input's measurement, and the executors leave some of core's polls
of that job unanswered until their bound passes.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.support.dynamic_script import Executors, config, harness
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)

from vibesys.orchestration.dynamic.search.api import dynamic
from vs_core.api import (
    CancelOwnedJob,
    Failed,
    FailureReason,
    Measure,
    MeasurementResult,
    ObserveOwnedJob,
    SubmitMeasurement,
    Wait,
)
from vs_core.testing.drive import Answer, Running, Trace, drive
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request

MOST_POLLS = 12
"""How many polls of the input's job the stories cover."""


@dataclass
class SlowPolls:
    """The scripted far side, except that the polls ``silent`` names of the input's job go
    unanswered (the status query outlasts its bound) and the job stays queued for ``queued``
    polls before it finishes.
    """

    inner: Executors
    silent: frozenset[int]
    queued: int
    polls: int = 0
    baseline: set[str] = field(default_factory=set)

    def __call__(self, request: Request, core: CoreState) -> Answer | tuple[Answer, ...]:
        """What the executors report for ``request``."""
        if (
            not isinstance(request, ObserveOwnedJob)
            or request.resource_id.root not in self.baseline
        ):
            answer = self.inner(request, core)
            if (
                isinstance(request, SubmitMeasurement)
                and request.plan.purpose == "baseline"
                and isinstance(answer, Running)
            ):
                self.baseline.add(answer.resource_id.root)
            return answer
        index = self.polls
        self.polls += 1
        if index in self.silent:
            return ()
        if index < self.queued:
            return Running(resource_id=request.resource_id)
        return self.inner(request, core)


def _run(silent: frozenset[int], queued: int, steps: int = 2000) -> tuple[Trace, SlowPolls]:
    selected = config()
    script = SlowPolls(
        inner=Executors(
            planner=deque([plan_reply(implement("h1"))]),
            implementer=deque([implemented()]),
            judge=deque([reviewed()]),
            exhausted="not json",
        ),
        silent=silent,
        queued=queued,
    )
    hosting = replace(
        harness(selected), shell_events=True, deadline_at=1_000_000.0, max_steps=steps
    )
    return drive(dynamic.driver(selected, hosting.registry), selected, script, hosting), script


def _input_decisions(trace: Trace) -> set[str]:
    return {
        submission.request.decision_id.root
        for submission in trace.decisions
        if isinstance(submission, Wait)
        and isinstance(submission.request, Measure)
        and submission.request.plan.purpose == "baseline"
    }


@settings(max_examples=examples(pr=4, nightly=60), deadline=None)
@given(
    silent=st.frozensets(st.integers(min_value=0, max_value=MOST_POLLS - 2), max_size=4),
    queued=st.integers(min_value=0, max_value=MOST_POLLS - 1),
)
@example(silent=frozenset({0}), queued=1)
@example(silent=frozenset({0, 1, 2}), queued=6)
def test_an_unanswered_poll_never_ends_the_input_measurement(
    silent: frozenset[int], queued: int
) -> None:
    trace, script = _run(silent, queued)

    decisions = _input_decisions(trace)
    assert len(decisions) == 1, decisions
    failed = [
        event
        for event in trace.events
        if isinstance(event, Failed)
        and event.decision is not None
        and event.decision.root in decisions
    ]
    assert failed == [], failed
    (submission,) = (
        record.request_id
        for record in trace.core.records.records
        if isinstance(record.request, SubmitMeasurement)
        and record.request.plan.purpose == "baseline"
    )
    results = [
        event
        for event in trace.events
        if isinstance(event, MeasurementResult) and event.source_request == submission
    ]
    # The input was measured once, with its evidence: the job ran to its end.
    assert len(results) == 1, results
    assert results[0].failure is None
    assert results[0].evidence
    # Every unanswered poll was followed by another, until the first answered poll after the
    # queue found the job finished.
    first_done = next(index for index in itertools.count(queued) if index not in silent)
    assert script.polls == first_done + 1
    # A finished job is never cancelled.
    assert not any(
        isinstance(request, CancelOwnedJob) and request.resource_id.root in script.baseline
        for request in trace.requests
    )


def test_a_measurement_whose_polls_never_answer_ends_at_its_own_limit_and_cancels_its_job() -> None:
    """With no poll ever answered, the measurement still ends: at its own limit, not a poll's.

    Its job is then cancelled, never left in the scheduler's queue with nothing waiting on it.
    """
    # The job is never seen to end (no poll answers), so the drive stops after its steps;
    # the measurement's limit falls well inside them.
    trace, script = _run(frozenset(range(10_000)), 0, steps=300)

    decisions = _input_decisions(trace)
    (submission,) = (
        record
        for record in trace.core.records.records
        if isinstance(record.request, SubmitMeasurement)
        and record.request.plan.purpose == "baseline"
    )
    failed = [
        event
        for event in trace.events
        if isinstance(event, Failed)
        and event.decision is not None
        and event.decision.root in decisions
    ]
    assert [event.reason for event in failed] == [FailureReason.TIMED_OUT]
    assert submission.dispatched_at is not None
    assert submission.limit_from_dispatch is not None
    assert failed[0].limit_at == submission.dispatched_at + submission.limit_from_dispatch
    cancels = [
        request
        for request in trace.requests
        if isinstance(request, CancelOwnedJob) and request.resource_id.root in script.baseline
    ]
    assert len(cancels) == 1
