"""A job whose measurement core ended is cancelled, even when one cancel is not enough.

When core ends a strategy's measurement at its limit, nothing waits on the measurement's job
any longer, so evaluation cancels it rather than leave it holding nodes in the scheduler. A
cancel can itself end at its bound unanswered (a slow ``scancel``), and the job id can arrive
only after the measurement has ended (a slow ``sbatch``). Neither may leave the job running.

The run is the dynamic search on the real core (``vs_core.testing.drive``) measuring its
input; the executors leave chosen requests about that job unanswered until their bound passes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
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
    ClockAdvanced,
    ObserveOwnedJob,
    RequestPhase,
    SubmitMeasurement,
)
from vs_core.testing.drive import Running, drive
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vs_core.api import CoreEvent, CoreState, Request, RequestRecord
    from vs_core.testing.drive import Answer, Script, Trace


@dataclass
class _SlowJobSteps:
    """The scripted far side, slow on the input's measurement job.

    Its polls go unanswered while the measurement is open, so the measurement ends at its own
    limit; afterwards the job is seen still queued. The first ``silent_cancels`` cancels of
    the job go unanswered past their bound.
    """

    inner: Executors
    silent_cancels: int
    baseline: set[str] = field(default_factory=set)
    cancels: int = 0

    def __call__(self, request: Request, core: CoreState) -> Answer | tuple[Answer, ...]:
        """What the executors report for ``request``."""
        if isinstance(request, ObserveOwnedJob) and request.resource_id.root in self.baseline:
            if _submission(core) is None or _open(_submission(core)):
                return ()
            return Running(resource_id=request.resource_id)
        if isinstance(request, CancelOwnedJob) and request.resource_id.root in self.baseline:
            self.cancels += 1
            if self.cancels <= self.silent_cancels:
                return ()
        answer = self.inner(request, core)
        if (
            isinstance(request, SubmitMeasurement)
            and request.plan.purpose == "baseline"
            and isinstance(answer, Running)
        ):
            self.baseline.add(answer.resource_id.root)
        return answer


def _submission(core: CoreState) -> RequestRecord | None:
    return next(
        (
            record
            for record in core.records.records
            if isinstance(record.request, SubmitMeasurement)
            and record.request.plan.purpose == "baseline"
        ),
        None,
    )


def _open(record: RequestRecord | None) -> bool:
    return record is not None and record.phase not in (
        RequestPhase.COMPLETED,
        RequestPhase.BLOCKED,
    )


def _executors() -> Executors:
    return Executors(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
        exhausted="not json",
    )


def _drive(script: Script, **overrides: object) -> Trace:
    selected = config()
    hosting = replace(
        harness(selected), shell_events=True, deadline_at=1_000_000.0, max_steps=2000, **overrides
    )
    return drive(dynamic.driver(selected, hosting.registry), selected, script, hosting)


def _cancels(trace: Trace, jobs: set[str]) -> list[RequestRecord]:
    return [
        record
        for record in trace.core.records.records
        if isinstance(record.request, CancelOwnedJob) and record.request.resource_id.root in jobs
    ]


@settings(max_examples=examples(pr=4, nightly=12), deadline=None)
@given(silent_cancels=st.integers(min_value=0, max_value=3))
def test_an_orphaned_jobs_cancel_that_ends_at_its_bound_is_sent_again(silent_cancels: int) -> None:
    """A cancel that ended without an answer proves nothing, so core sends it again.

    Re-sends stay within the backend's ``request_retries.limit``: past it core waits for the
    job's own end. Regression: any cancel on the ledger, a blocked one included, counted as
    the job's cancel, so the job stayed queued with nothing waiting on it.
    """
    script = _SlowJobSteps(inner=_executors(), silent_cancels=silent_cancels)
    trace = _drive(script)

    submission = _submission(trace.core)
    assert submission is not None
    assert submission.failure is not None
    cancels = _cancels(trace, script.baseline)
    limit = trace.core.run.limits.request_retries.limit
    unanswered = min(silent_cancels, limit + 1)
    assert len(cancels) == min(silent_cancels + 1, limit + 1)
    assert all(record.failure is not None for record in cancels[:unanswered])
    if silent_cancels <= limit:
        (answered,) = cancels[unanswered:]
        assert answered.failure is None
        assert answered.observation is not None
        assert answered.observation.accepted


@dataclass
class _LateJobId:
    """The scripted far side, whose answer to the input's submission (the job id) comes only
    after core has ended the submission at its limit, as a slow ``sbatch`` would."""

    inner: Executors
    baseline: set[str] = field(default_factory=set)
    past_deadline: bool = False
    """Whether the submission also outlasts the run, so the run closes before the answer."""

    def __call__(self, request: Request, core: CoreState) -> Answer | tuple[Answer, ...]:
        """What the executors report for ``request``."""
        answer = self.inner(request, core)
        if (
            isinstance(request, SubmitMeasurement)
            and request.plan.purpose == "baseline"
            and isinstance(answer, Running)
        ):
            self.baseline.add(answer.resource_id.root)
        return answer

    def during(self, request: Request, core: CoreState) -> tuple[CoreEvent, ...]:
        """While the input's submission runs, time passes its limit (and the run's deadline)."""
        if not isinstance(request, SubmitMeasurement) or request.plan.purpose != "baseline":
            return ()
        record = _submission(core)
        assert record is not None
        assert record.dispatched_at is not None
        assert record.limit_from_dispatch is not None
        passed = record.dispatched_at + record.limit_from_dispatch + 1.0
        if self.past_deadline:
            passed = max(passed, core.run.deadline_at + 1.0)
        return (ClockAdvanced(now_at=passed),)


def test_a_job_whose_id_arrives_after_its_submission_ended_is_cancelled() -> None:
    """The job a late ``sbatch`` answer names is cancelled once its id is known.

    The submission first ends at its limit with no job id known; the late answer ends it
    again, and that second end, with the id known, asks for the cancel. A guard: the re-sent
    cancel of a blocked one must leave this path working.
    """
    script = _LateJobId(inner=_executors())
    trace = _drive(script, during_turn=script.during)

    submission = _submission(trace.core)
    assert submission is not None
    assert submission.failure is not None
    assert script.baseline
    assert _cancels(trace, script.baseline)


@pytest.mark.xfail(
    strict=True,
    reason="PR #3235: core's end of the blocked submission at close took the executor's "
    "sequence 0, so the late sbatch answer halts the host instead of naming a job to cancel",
)
def test_a_job_whose_id_arrives_after_the_run_closed_is_cancelled() -> None:
    """The late ``sbatch`` answer may come after the run closed around its blocked submission.

    The run's deadline passes while the input's submission runs, so the run closes and core
    ends the submission it waits on. The answer that then names the job is still taken: the
    job is cancelled and the run ends.
    """
    script = _LateJobId(inner=_executors(), past_deadline=True)
    trace = _drive(script, during_turn=script.during)

    assert trace.finished
    assert script.baseline
    cancels = _cancels(trace, script.baseline)
    assert cancels
    assert any(record.observation is not None and record.observation.accepted for record in cancels)
