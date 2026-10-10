"""Closing a Slurm executor waits for cluster calls still running in worker threads.

A cluster call runs on a worker thread, and a cancelled caller stops waiting for it
without stopping it. These cases need real threads: the call is parked on a real event.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import EvaluationRequest, EvaluationStep
from vs_evaluation.api.testing import wait_until_executor_started
from vs_sandbox.api.slurm import SlurmEvaluationExecutor, SlurmStagePayload
from vs_sim.api.testing import wait_until_started
from vs_slurm.api import (
    ClusterInspectOutcome,
    ClusterTarget,
    FakeCluster,
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
)

if TYPE_CHECKING:
    from pathlib import Path

_HANDLE = "eval-held-inspect"


class _HeldInspectCluster(FakeCluster):
    """A scheduler whose inspection, once held, parks its worker thread until the gate opens."""

    def __init__(self) -> None:
        super().__init__()
        self.held = False
        self.inspect_entered = threading.Event()
        self.inspect_gate = threading.Event()
        self.inspect_returned = threading.Event()

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        if not self.held:
            return super().inspect(target, by_job_id=by_job_id)
        self.inspect_entered.set()
        self.inspect_gate.wait()
        observed = super().inspect(target, by_job_id=by_job_id)
        self.inspect_returned.set()
        return observed


def _config() -> SlurmConfig:
    return SlurmConfig(
        name="fake-cluster",
        remote_workspace_root="/runs",
        transport=SlurmSshTransport(host="fake-cluster"),
        poll_interval_seconds=0.001,
    )


def _request() -> EvaluationRequest:
    return EvaluationRequest(
        key="held",
        stages=tuple(
            EvaluationStep(
                name=name,
                payload=SlurmStagePayload(command="true").model_dump(mode="json"),
            )
            for name in ("accuracy", "benchmark")
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_the_caller", [True, False])
async def test_close_waits_for_every_cluster_call_still_running_in_a_thread(
    tmp_path: Path, *, cancel_the_caller: bool
) -> None:
    """Closing tells the owner nothing of the executor still runs, even an abandoned call (#1766)."""
    cluster = _HeldInspectCluster()
    accepted = threading.Event()
    cluster.script(_HANDLE, states=(SlurmJobStatus.PENDING,))
    cluster.on_accept(_HANDLE, accepted.set)
    config = _config()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def executor() -> SlurmEvaluationExecutor:
        return SlurmEvaluationExecutor(
            config,
            workspace=workspace,
            setup_script=None,
            service=None,
            support_trees={},
            handle_root=tmp_path / "handles",
            cluster=cluster,
        )

    first = executor()
    await first.submit(_request(), handle_id=_HANDLE)
    await wait_until_executor_started(accepted, first, _HANDLE)
    await first.close()

    # A restarted executor reads the durable evaluation without owning a task for it.
    second = executor()
    cluster.held = True
    reader = asyncio.create_task(second.inspect_only(_HANDLE))
    try:
        await wait_until_started(cluster.inspect_entered, reader)
        if cancel_the_caller:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        closer = asyncio.create_task(second.close())
        for _ in range(50):
            await asyncio.sleep(0)
        assert not closer.done()
        cluster.inspect_gate.set()
        await closer
        assert cluster.inspect_returned.is_set()
    finally:
        cluster.inspect_gate.set()
        await asyncio.gather(reader, return_exceptions=True)
