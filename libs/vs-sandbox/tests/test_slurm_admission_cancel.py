"""A Slurm evaluation cancelled while it waits for local admission stays cancelled.

Evaluations beyond the shared admission capacity wait in-process, before any
scheduler contact, with a durable record that says dispatch has not started.
Cancelling one there must end it: every read path (the pure poll the runtime
uses to prove release, and recovery after a restart) reports it ended and
nothing ever submits it.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import (
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    PollPhase,
    ResourceRequirements,
)
from vs_evaluation.api.testing import wait_until_executor_started
from vs_sandbox.api.slurm import (
    SharedSlurmAdmission,
    SlurmEvaluationExecutor,
    SlurmStagePayload,
)
from vs_slurm.api import (
    ClusterSubmitOutcome,
    FakeCluster,
    SlurmBatchRequest,
    SlurmConfig,
    SlurmJobRequest,
    SlurmJobStatus,
    SlurmSshTransport,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

_HOLDER = "holder-evaluation"
_WAITER = "waiting-evaluation"


class _QueuedCluster(FakeCluster):
    """Every accepted batch stays PENDING; records which identities were submitted."""

    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []
        self.holder_accepted = threading.Event()

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        if isinstance(request, SlurmBatchRequest) and operation_id not in self.submitted:
            self.submitted.append(operation_id)
            self.script(operation_id, states=(SlurmJobStatus.PENDING,))
        outcome = super().submit(request, operation_id=operation_id)
        if operation_id == _HOLDER:
            self.holder_accepted.set()
        return outcome


def _config() -> SlurmConfig:
    return SlurmConfig(
        name="fake-cluster",
        remote_workspace_root="/runs",
        transport=SlurmSshTransport(host="fake-cluster"),
        poll_interval_seconds=1.0,
    )


def _request(key: str) -> EvaluationRequest:
    return EvaluationRequest(
        key=key,
        stages=(
            EvaluationStep(
                name="accuracy",
                payload=SlurmStagePayload(command="run-accuracy", timeout_seconds=5).model_dump(
                    mode="json"
                ),
            ),
        ),
    )


def _executor(root: Path, cluster: _QueuedCluster) -> SlurmEvaluationExecutor:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    return SlurmEvaluationExecutor(
        _config(),
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=root / "handles",
        cluster=cluster,
        admission=SharedSlurmAdmission(1),
        # Pacing is free: the holder's wait loop never sleeps on the wall clock.
        pause=lambda _seconds: None,
    )


async def _cancel_while_waiting_for_admission(
    executor: SlurmEvaluationExecutor, cluster: _QueuedCluster
) -> None:
    """Fill the one admission slot, queue a second evaluation behind it, and cancel that one."""
    await executor.submit(_request("holder"), handle_id=_HOLDER)
    await wait_until_executor_started(cluster.holder_accepted, executor, _HOLDER)
    await executor.submit(_request("waiter"), handle_id=_WAITER)
    # The waiter's task reaches the admission queue within a few loop turns.
    for _ in range(100):
        if (await executor.availability(ResourceRequirements())).queue_depth == 1:
            break
        await asyncio.sleep(0)
    assert (await executor.availability(ResourceRequirements())).queue_depth == 1

    await executor.cancel(_WAITER)

    observed = await executor.inspect(_WAITER)
    assert observed is not None
    assert observed.state is EvaluationState.CANCELED


async def test_poll_reports_an_evaluation_cancelled_before_dispatch_as_ended(
    tmp_path: Path,
) -> None:
    cluster = _QueuedCluster()
    executor = _executor(tmp_path, cluster)
    try:
        await _cancel_while_waiting_for_admission(executor, cluster)

        polled = await executor.poll(_WAITER)

        # The runtime proves a cancelled job released by polling it ENDED. A
        # cancelled evaluation that polls "awaiting local admission" can never
        # be released, and its scope close never reports every job ended.
        assert polled.phase is PollPhase.ENDED, polled
        assert polled.terminal is not None
        assert polled.terminal.state is EvaluationState.CANCELED
        assert _WAITER not in cluster.submitted
    finally:
        await executor.close()


async def test_recovery_after_restart_never_submits_an_evaluation_cancelled_before_dispatch(
    tmp_path: Path,
) -> None:
    cluster = _QueuedCluster()
    executor = _executor(tmp_path, cluster)
    await _cancel_while_waiting_for_admission(executor, cluster)
    await executor.close()

    # A restarted host recovers every durable record it finds.
    restarted = _executor(tmp_path, cluster)
    try:
        recovered = await restarted.inspect(_WAITER)

        assert recovered is not None
        assert recovered.state is EvaluationState.CANCELED, recovered
    finally:
        await restarted.close()
    assert _WAITER not in cluster.submitted


async def test_closing_the_executor_ends_an_evaluation_still_awaiting_admission(
    tmp_path: Path,
) -> None:
    cluster = _QueuedCluster()
    executor = _executor(tmp_path, cluster)
    await executor.submit(_request("holder"), handle_id=_HOLDER)
    await wait_until_executor_started(cluster.holder_accepted, executor, _HOLDER)
    await executor.submit(_request("waiter"), handle_id=_WAITER)
    for _ in range(100):
        if (await executor.availability(ResourceRequirements())).queue_depth == 1:
            break
        await asyncio.sleep(0)

    await executor.close()

    restarted = _executor(tmp_path, cluster)
    try:
        polled = await restarted.poll(_WAITER)
        assert polled.phase is PollPhase.ENDED, polled
        assert polled.terminal is not None
        assert polled.terminal.state is EvaluationState.CANCELED
        # A resubmission of the same handle does not revive it.
        await restarted.submit(_request("waiter"), handle_id=_WAITER)
        assert (await restarted.poll(_WAITER)).phase is PollPhase.ENDED
    finally:
        await restarted.close()
    assert _WAITER not in cluster.submitted
