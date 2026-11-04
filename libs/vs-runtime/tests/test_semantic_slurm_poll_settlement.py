"""A live semantic Slurm owner settles its streams before polling exposes terminality."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    ExecutorObservation,
    PollPhase,
    SemanticEvaluationStage,
)
from vs_runtime.api.core import StageRun
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, build_stack
from vs_sim.api import Capture, OutputStream
from vs_sim.api.testing import ManualClock, arrival, yield_until
from vs_slurm.api import ClusterObservation, ClusterUnknown, SlurmJobStatus

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vs_runtime.api.infrastructure import SemanticSlurmEvaluationExecutor
    from vs_slurm.api import (
        ClusterCollectOutcome,
        ClusterInspectOutcome,
        ClusterTarget,
    )

pytestmark = pytest.mark.asyncio

_HANDLE = "held-terminal-settlement"
_TERMINAL = frozenset({EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED})


class _ObservedCluster(ScenarioCluster):
    """Count collection and fail any inspection attempted after live settlement."""

    def __init__(self) -> None:
        super().__init__()
        self.states = (SlurmJobStatus.PENDING, SlurmJobStatus.COMPLETED)
        self.collect_calls = 0
        self.fail_inspections = False

    def inspect(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
    ) -> ClusterInspectOutcome:
        if self.fail_inspections:
            return ClusterUnknown(
                operation_id=_HANDLE, reason="cluster read failed after settlement"
            )
        observed = super().inspect(target, by_job_id=by_job_id)
        if isinstance(observed, ClusterObservation) and observed.status is SlurmJobStatus.PENDING:
            return observed.model_copy(
                update={"pending_reason": "Priority", "estimated_start": "2030-01-02T03:04:05"}
            )
        return observed

    def collect(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCollectOutcome:
        self.collect_calls += 1
        return super().collect(target, by_job_id=by_job_id, observed=observed)


class _ParkFirstSleep:
    """Park the owner's first poll pause; later pauses pass on a manual clock."""

    def __init__(self) -> None:
        self.parked = asyncio.Event()
        self.release = asyncio.Event()
        self._then = ManualClock()

    async def sleep(self, seconds: float) -> None:
        if not self.parked.is_set():
            self.parked.set()
            await self.release.wait()
            return
        await self._then.sleep(seconds)


@dataclass
class _Streams:
    """One in-memory stream per stage, including its terminal close evidence."""

    opened: list[StageRun] = field(default_factory=list)
    closed: list[StageRun] = field(default_factory=list)
    output: dict[StageRun, Capture] = field(default_factory=dict)

    @contextmanager
    def open(self, run: StageRun) -> Iterator[Capture]:
        self.opened.append(run)
        sink = self.output[run] = Capture(1 << 16)
        try:
            yield sink
        finally:
            self.closed.append(run)


def _request(snapshot: str) -> EvaluationRequest:
    digest = ContentDigest.sha256(b"same")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    return EvaluationRequest(
        key="terminal-stream-settlement",
        stages=tuple(
            EvaluationStep(
                name=kind.value,
                payload=SemanticEvaluationStage(
                    snapshot=snapshot,
                    kind=kind,
                    fingerprints=fingerprints,
                ).model_dump(mode="json"),
            )
            for kind in (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)
        ),
    )


async def _terminal(executor: SemanticSlurmEvaluationExecutor) -> ExecutorObservation:
    """Yield to the owner until its public observation has settled."""
    observed = await yield_until(
        lambda: executor.inspect(_HANDLE),
        lambda seen: seen is not None and seen.state in _TERMINAL,
        "the live Slurm owner's terminal settlement",
    )
    assert observed is not None
    return observed


async def test_live_poll_waits_for_its_owner_to_settle_stage_streams(tmp_path: Path) -> None:
    """A second collector cannot publish ENDED while the live owner still owes settlement."""
    cluster = _ObservedCluster()
    sleeper = _ParkFirstSleep()
    streams = _Streams()
    stack = await build_stack(
        tmp_path,
        cluster,
        stage_output=streams.open,
        sleeper=sleeper,
    )
    await stack.executor.submit(_request(stack.snapshot), handle_id=_HANDLE)
    terminal_task = asyncio.create_task(_terminal(stack.executor))
    try:
        await arrival(sleeper.parked.wait(), terminal_task)

        live_settled = await stack.executor.poll(_HANDLE)

        terminal = await terminal_task
        assert terminal.state is EvaluationState.SUCCEEDED
        assert live_settled.phase is PollPhase.ENDED
        assert live_settled.terminal == terminal

        runs = [StageRun(_HANDLE, "accuracy"), StageRun(_HANDLE, "benchmark")]
        assert streams.opened == runs
        assert streams.closed == runs
        assert {run.stage_id: streams.output[run].kept(OutputStream.STDOUT) for run in runs} == {
            "accuracy": b"ok",
            "benchmark": stack.cluster.benchmark_output.encode(),
        }
        assert cluster.collect_calls == 1

        cluster.fail_inspections = True
        settled = await yield_until(
            lambda: stack.executor.poll(_HANDLE),
            lambda polled: polled.phase is PollPhase.ENDED,
            "the settled owner's terminal poll",
        )
        assert settled.terminal == terminal
        assert settled.attempt == live_settled.attempt
        assert cluster.collect_calls == 1
    finally:
        sleeper.release.set()
        await asyncio.gather(terminal_task, return_exceptions=True)
        await stack.executor.close()
