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
    MeasurementFinished,
    Measure,
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
        if not isinstance(request, ObserveOwnedJob) or request.resource_id.root not in self.baseline:
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


def _run(silent: frozenset[int], queued: int) -> tuple[Trace, SlowPolls]:
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
    hosting = replace(harness(selected), shell_events=True, deadline_at=1_000_000.0)
    return drive(dynamic.driver(selected, hosting.registry), selected, script, hosting), script


def _input_decisions(trace: Trace) -> set[str]:
    return {
        submission.request.decision_id.root
        for submission in trace.decisions
        if isinstance(submission, Wait)
        and isinstance(submission.request, Measure)
        and submission.request.plan.purpose == "baseline"
    }


@settings(max_examples=examples(pr=6, nightly=60), deadline=None)
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
    finished = [
        event
        for event in trace.events
        if isinstance(event, MeasurementFinished)
        and event.decision is not None
        and event.decision.root in decisions
    ]
    assert len(finished) == 1
    assert finished[0].result.status == "succeeded"
    # Every unanswered poll was followed by another, until the job finished.
    assert script.polls > max(silent | {queued - 1}, default=-1)
    # A finished job is never cancelled.
    assert not any(
        isinstance(request, CancelOwnedJob) and request.resource_id.root in script.baseline
        for request in trace.requests
    )
