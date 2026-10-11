"""A candidate whose measurement job is still running when its attempt settles still settles.

Settling an attempt over a running job makes the attempt wait for the job's release. A cluster
job can keep running for minutes and then end failed, with no evidence, or cancelled by the
settlement itself. However it ends, the attempt must reach terminal so its settlement completes
and the run ends; before this was fixed the run stalled with every workstream finished (core
state: attempt `closing`, release dependency on the job, settlement `pending`, nothing open).
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import run_shell

from vs_core.api import (
    AttemptPhase,
    ObserveOwnedJob,
    OwnedJob,
    ResourceId,
    RunStatus,
    SubmitMeasurement,
)
from vs_core.testing.drive import Failed, Running

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.testing.drive import Answer


@dataclass
class SlowCandidateJobs(Executors):
    """Candidate jobs keep running for ``running_polls`` polls, then end as the script says.

    The input's own measurement is never slow, so the run starts as any run does.
    """

    running_polls: int = 0
    ends_failed: bool = False
    polls: Counter[str] = field(default_factory=Counter)

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, ObserveOwnedJob):
            job = next(
                item
                for item in core.evaluation.jobs
                if isinstance(item, OwnedJob) and item.resource_id == request.resource_id
            )
            if job.plan.purpose != "baseline":
                self.seen.append(request)
                self.polls[request.resource_id.root] += 1
                if self.polls[request.resource_id.root] <= self.running_polls:
                    return Running(resource_id=request.resource_id)
                if self.ends_failed:
                    return Failed(accepted=True)
        return super().__call__(request, core)


def _submit(request: SubmitMeasurement) -> Answer:
    assert request.request_id is not None
    return Running(resource_id=ResourceId(root=f"job:{request.request_id.root}"))


@settings(max_examples=16, deadline=None, derandomize=True, database=None)
@given(
    running_polls=st.integers(0, 6),
    ends_failed=st.booleans(),
    agent_evaluation=st.booleans(),
    passed=st.booleans(),
)
def test_a_candidate_settled_over_a_running_measurement_job_still_settles(
    *, running_polls: int, ends_failed: bool, agent_evaluation: bool, passed: bool
) -> None:
    executors = SlowCandidateJobs(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque(implemented() for _ in range(6)),
        judge=deque(reviewed(passed=passed) for _ in range(6)),
        agent_evaluation=agent_evaluation,
        submit=_submit,
        running_polls=running_polls,
        ends_failed=ends_failed,
    )

    finished = run_shell(executors)

    assert finished.halted is None
    assert finished.core.run.status == RunStatus.TERMINAL
    for attempt in finished.core.attempts.attempts:
        assert attempt.phase == AttemptPhase.TERMINAL
        assert attempt.release_dependencies == ()
    assert finished.core.settlement.pending == ()
