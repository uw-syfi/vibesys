"""``SlurmEvaluationExecutor.close`` returns when a cluster call ends as it starts to drain.

A cluster call whose caller was cancelled keeps running in its worker thread, and
``close`` waits for it. A call that has just ended stays tracked until a callback
runs on a later event-loop turn. Waiting on such a call returns without yielding
to the loop, so a ``close`` that began in that window spun forever and starved the
callback that would have released it.

The window opens on one loop turn after the thread returns, and which turn the
caller's ``close`` lands on depends on how the loop interleaves the two. The test
sweeps the turns so the window is crossed whatever the interleaving.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import EvaluationRequest, EvaluationStep
from vs_evaluation.api.testing import wait_until_executor_started
from vs_sandbox.api.slurm import (
    SharedSlurmAdmission,
    SlurmEvaluationExecutor,
    SlurmStagePayload,
)
from vs_sim.api.testing import wait_until_started
from vs_slurm.api import (
    ClusterInspectOutcome,
    ClusterSubmitOutcome,
    ClusterTarget,
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

_HANDLE = "evaluation-under-test"
_TURNS = range(8)


class _GatedCluster(FakeCluster):
    """Every batch stays PENDING; once armed, ``inspect`` parks until released."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()
        self.accepted = threading.Event()

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        if isinstance(request, SlurmBatchRequest):
            self.script(operation_id, states=(SlurmJobStatus.PENDING,))
        outcome = super().submit(request, operation_id=operation_id)
        self.accepted.set()
        return outcome

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        if self.armed:
            self.entered.set()
            self.release.wait()
        outcome = super().inspect(target, by_job_id=by_job_id)
        if self.armed:
            self.returned.set()
        return outcome


def _executor(root: Path, cluster: _GatedCluster) -> SlurmEvaluationExecutor:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    return SlurmEvaluationExecutor(
        SlurmConfig(
            name="fake-cluster",
            remote_workspace_root="/runs",
            transport=SlurmSshTransport(host="fake-cluster"),
            poll_interval_seconds=1.0,
        ),
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=root / "handles",
        cluster=cluster,
        admission=SharedSlurmAdmission(1),
        pause=lambda _seconds: None,
    )


def _request() -> EvaluationRequest:
    return EvaluationRequest(
        key="close-drain",
        stages=(
            EvaluationStep(
                name="accuracy",
                payload=SlurmStagePayload(command="run-accuracy", timeout_seconds=5).model_dump(
                    mode="json"
                ),
            ),
        ),
    )


@pytest.mark.parametrize("turns", _TURNS)
async def test_close_returns_when_an_abandoned_cluster_call_ends_as_it_begins(
    tmp_path: Path, turns: int
) -> None:
    cluster = _GatedCluster()
    first = _executor(tmp_path, cluster)
    await first.submit(_request(), handle_id=_HANDLE)
    await wait_until_executor_started(cluster.accepted, first, _HANDLE)
    await first.close()

    # A restarted host polls the durable record; the poll's cluster call is the
    # one that outlives its cancelled caller.
    restarted = _executor(tmp_path, cluster)
    cluster.armed = True
    poller = asyncio.ensure_future(restarted.poll(_HANDLE))
    try:
        await wait_until_started(cluster.entered, poller)
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
    finally:
        cluster.release.set()
    # Hold the loop until the worker thread has returned, so its completion is
    # already queued when the loop next runs; then land close on the chosen turn.
    cluster.returned.wait()
    for _ in range(turns):
        await asyncio.sleep(0)

    await restarted.close()
