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


def _executors(*, plans: int = 1) -> Executors:
    """The far side's agents: ``plans`` planner replies, each scheduling h1, then nothing usable."""
    return Executors(
        planner=deque([plan_reply(implement("h1")) for _ in range(plans)]),
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


def test_a_job_whose_id_arrives_after_the_run_closed_has_ended_when_the_run_ends() -> None:
    """The late ``sbatch`` answer may come after the run closed around its blocked submission.

    The run's deadline passes while the input's submission runs, so the run closes and core
    ends the submission it waits on, numbering that end where the executor's first answer
    goes. The answer that then names the job is still taken (regression, PR #3235: it was
    refused as a sequence conflict, halting the host), and the run ends only once the job has.
    """
    script = _LateJobId(inner=_executors(), past_deadline=True)
    trace = _drive(script, during_turn=script.during)

    assert trace.finished
    assert script.baseline
    named = [
        job
        for job in trace.core.evaluation.jobs
        if job.resource_id is not None and job.resource_id.root in script.baseline
    ]
    assert named
    assert all(job.terminal for job in named)
