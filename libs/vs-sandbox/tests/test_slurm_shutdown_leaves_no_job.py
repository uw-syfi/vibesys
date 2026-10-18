"""Once a run's Slurm executor has closed, none of the run's jobs is left on the cluster.

Regression: a headless dynamic run ended by SIGTERM left an evaluation job queued on a
shared cluster with no process left to own it. The run used the evaluation ledger
(#2757), whose ``close`` detached every job for a later resume instead of cancelling it,
and an evaluation whose submission was in flight when the shutdown began was submitted
during that shutdown and detached too. A submission that arrives after ``close`` has
begun was also accepted, and its job was never cancelled.

Each example drives one executor through a generated sequence of submissions,
cancellations and pauses with ``close`` at a generated point, on a virtual loop whose
ties are broken by a generated seed, so a shutdown lands before, during and after each
submission's dispatch. Jobs never leave the queue by themselves, so only the executor
can end them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_evaluation.api import (
    EvaluationRequest,
    EvaluationStep,
    ExecutorRejectedError,
    ExecutorSubmissionError,
)
from vs_sandbox.api.slurm import (
    SharedSlurmAdmission,
    SlurmEvaluationExecutor,
    SlurmLedgerAttachment,
    SlurmStagePayload,
)
from vs_sim.api import yield_now
from vs_sim.api.testing import VirtualClock, examples, run_virtual
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitOutcome,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerPolicy,
    SecondsRange,
    SlurmBatchRequest,
    SlurmConfig,
    SlurmJobRequest,
    SlurmSshTransport,
    SlurmTimingProfile,
)

if TYPE_CHECKING:
    from pathlib import Path

PROJECT = "demo-0123456789ab"
HANDLES = tuple(f"eval-{index}" for index in range(3))
CONFIG = SlurmConfig(
    name="fake-cluster",
    remote_workspace_root="/runs",
    transport=SlurmSshTransport(host="fake-cluster"),
    poll_interval_seconds=1.0,
)
# Jobs wait in the queue for a year, far past any run here: only a cancel ends one.
_YEAR_S = 365 * 24 * 3600.0
NEVER = SlurmTimingProfile(
    queue_wait_s=SecondsRange.exactly(_YEAR_S),
    run_s=SecondsRange.exactly(0.0),
    completing_s=SecondsRange.exactly(0.0),
)


class _Scheduler(FakeCluster):
    """The Fake scheduler, noting the operation identity of every submission it receives."""

    def __init__(self, clock: VirtualClock) -> None:
        super().__init__(clock=clock, timing=NEVER)
        self.operations: list[str] = []

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        self.operations.append(operation_id)
        return super().submit(request, operation_id=operation_id)

    def queued(self) -> list[str]:
        """The operations whose job the scheduler still holds, queued or running."""
        live = []
        for operation in dict.fromkeys(self.operations):
            observed = self.inspect(operation)
            if isinstance(observed, ClusterObservation) and not observed.status.terminal:
                live.append(operation)
        return live


@dataclass(frozen=True)
class Submit:
    handle: int


@dataclass(frozen=True)
class Cancel:
    handle: int


@dataclass(frozen=True)
class Pause:
    """Let the executor's own tasks run: ``turns`` loop turns, then ``seconds`` of virtual time."""

    turns: int
    seconds: float


@dataclass(frozen=True)
class Close:
    pass


# Loop turns after the run's steps for a task the executor left behind to reach sbatch.
_SETTLE_TURNS = 20

Step = Submit | Cancel | Pause | Close

_handle = st.integers(min_value=0, max_value=len(HANDLES) - 1)
_steps = st.lists(
    st.one_of(
        st.builds(Submit, _handle),
        st.builds(Cancel, _handle),
        st.builds(
            Pause,
            st.integers(min_value=0, max_value=6),
            st.sampled_from([0.0, 0.5, 3.0]),
        ),
        st.just(Close()),
    ),
    max_size=12,
)

Mode = Literal["direct", "ledger"]


@dataclass
class _Shutdown:
    """One executor over the Fake scheduler, and what the steps saw."""

    root: Path
    mode: Mode
    clock: VirtualClock = field(default_factory=lambda: VirtualClock(1000.0))
    refused: list[str] = field(default_factory=list)
    submitted: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.scheduler = _Scheduler(self.clock)
        workspace = self.root / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "bench.py").write_text("print('bench')\n", encoding="utf-8")
        ledger = None
        if self.mode == "ledger":
            ledger = SlurmLedgerAttachment(
                ledger=FakeEvaluationLedger(project=PROJECT),
                config=CONFIG,
                project=PROJECT,
                holder="run-1",
                policy=LedgerPolicy(),
                clock=self.clock,
            )
        self.executor = SlurmEvaluationExecutor(
            CONFIG,
            workspace=workspace,
            setup_script=None,
            service=None,
            support_trees={},
            handle_root=self.root / "handles",
            admission=SharedSlurmAdmission(2),
            cluster=self.scheduler,
            clock=self.clock,
            sleeper=self.clock,
            ledger=ledger,
        )

    async def play(self, steps: list[Step]) -> None:
        """Start each step in order, pausing where the steps say; return once all have ended.

        Submissions, cancellations and the close run as tasks of their own, so a
        submission can still be dispatching, or only just arriving, while the close runs.
        """
        started: list[asyncio.Task[None]] = []
        for step in steps:
            match step:
                case Submit(handle=index):
                    started.append(asyncio.create_task(self._submit(index)))
                case Cancel(handle=index):
                    started.append(asyncio.create_task(self._cancel(HANDLES[index])))
                case Pause(turns=turns, seconds=seconds):
                    for _ in range(turns):
                        await yield_now()
                    await self.clock.sleep(seconds)
                case Close():
                    started.append(asyncio.create_task(self.executor.close()))
        await asyncio.gather(*started)
        if Close() not in steps:
            await self.executor.close()

    async def queued_after(self, steps: list[Step]) -> list[str]:
        """Play the steps, give any task the executor left behind time to act, read the queue."""
        await self.play(steps)
        for _ in range(_SETTLE_TURNS):
            await yield_now()
        await self.clock.sleep(10 * CONFIG.poll_interval_seconds)
        return self.scheduler.queued()

    async def _submit(self, index: int) -> None:
        handle = HANDLES[index]
        try:
            await self.executor.submit(_request(index), handle_id=handle)
        except (ExecutorRejectedError, ExecutorSubmissionError):
            self.refused.append(handle)
        else:
            self.submitted.add(handle)

    async def _cancel(self, handle: str) -> None:
        # The core cancels only what it has submitted.
        if handle in self.submitted:
            await self.executor.cancel(handle)


def _request(index: int) -> EvaluationRequest:
    return EvaluationRequest(
        key=f"fused-{index}",
        owner_scope="attempt",
        stages=tuple(
            EvaluationStep(
                name=name,
                payload=SlurmStagePayload(
                    command=f"run-{name} {index}", timeout_seconds=60
                ).model_dump(mode="json"),
            )
            for name in ("accuracy", "benchmark")
        ),
    )


@pytest.mark.regression
@settings(max_examples=examples(pr=40, nightly=400))
@given(
    mode=st.sampled_from(["direct", "ledger"]),
    steps=_steps,
    schedule_seed=st.integers(min_value=0, max_value=2**16),
)
# The reported run: the ledger, a job submitted before the shutdown began.
@example(mode="ledger", steps=[Submit(0), Pause(6, 3.0), Close()], schedule_seed=0)
# A submission that arrives once the executor has closed.
@example(mode="direct", steps=[Close(), Submit(1)], schedule_seed=0)
# Submissions racing the close: the close does not see either of them.
@example(mode="direct", steps=[Submit(0), Close(), Submit(1)], schedule_seed=1)
def test_no_job_of_the_run_is_left_on_the_cluster_once_its_executor_closed(
    tmp_path_factory: pytest.TempPathFactory,
    mode: Mode,
    steps: list[Step],
    schedule_seed: int,
) -> None:
    run = _Shutdown(tmp_path_factory.mktemp("run"), mode)

    # Read the queue on the run's own loop, once every step has returned: the loop's
    # teardown cancels any task the executor left behind, which a process that exits
    # without its executor's help does not do.
    left = run_virtual(run.clock, run.queued_after(steps), schedule_seed=schedule_seed)

    assert left == []
