"""A candidate whose measurement job is still running when its attempt settles still settles.

Settling an attempt over a running job makes the attempt wait for the job's release. A cluster
job can keep running for minutes and then end failed, with no evidence, or cancelled by the
settlement itself. However it ends, the attempt must reach terminal so its settlement completes
and the run ends; before this was fixed the run stalled with every workstream finished (core
state: attempt `closing`, release dependency on the job, settlement `pending`, nothing open).
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.strategy._replies import implemented, reviewed
from tests.vibesys.orchestration.dynamic.strategy.world import DynamicWorld

from vs_core.api import AttemptPhase, ObserveOwnedJob, OwnedJob
from vs_core.testing.drive import Failed, Running
from vs_dst.api import simulate

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import CoreState, Request
    from vs_core.testing.drive import Answer


class SlowCandidateJobs(DynamicWorld):
    """Candidate jobs keep running for ``running_polls`` polls, then end as the world says.

    The input's own measurement is never slow, so the run starts as any run does.
    """

    def __init__(
        self,
        *,
        running_polls: int,
        ends_failed: bool,
        agent_evaluation: bool,
        implementer: Sequence[str],
        judge: Sequence[str],
    ) -> None:
        super().__init__(
            polls=(0,), agent_evaluation=agent_evaluation, implementer=implementer, judge=judge
        )
        self._running_polls = running_polls
        self._ends_failed = ends_failed
        self._polled: Counter[str] = Counter()

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, ObserveOwnedJob):
            job = next(
                item
                for item in core.evaluation.jobs
                if isinstance(item, OwnedJob) and item.resource_id == request.resource_id
            )
            if job.plan.purpose != "baseline":
                self._polled[request.resource_id.root] += 1
                if self._polled[request.resource_id.root] <= self._running_polls:
                    return Running(resource_id=request.resource_id)
                if self._ends_failed:
                    return Failed(accepted=True)
        return super().__call__(request, core)


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
    world = SlowCandidateJobs(
        implementer=[implemented() for _ in range(6)],
        judge=[reviewed(passed=passed) for _ in range(6)],
        agent_evaluation=agent_evaluation,
        running_polls=running_polls,
        ends_failed=ends_failed,
    )

    finished = simulate(world)

    assert finished.halted is None
    assert finished.finished
    assert not finished.liveness_violations
    for attempt in finished.core.attempts.attempts:
        assert attempt.phase == AttemptPhase.TERMINAL
        assert attempt.release_dependencies == ()
    assert finished.core.settlement.pending == ()
