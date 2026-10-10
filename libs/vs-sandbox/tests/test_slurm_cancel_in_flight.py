"""A stop that lands while sbatch's reply is still in flight cancels the job it submitted.

The cluster has accepted the job (it exists and is queued) but the executor has
not yet seen the reply, so it holds no job id. Whichever of the stop and the
reply the executor handles first, the evaluation must end CANCELED with exactly
one scancel of the submitted job. The reply is held until the stop is issued, so
the test never depends on how long the submission takes.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from typing import TYPE_CHECKING

import pytest
from tests.support.started_operation import wait_until_executor_started

import vs_evaluation.api.testing as evaluation_testing
from vs_evaluation.api import (
    EvaluationCoordinator,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
)
from vs_sandbox.api.slurm import SlurmEvaluationExecutor, SlurmStagePayload
from vs_sim.api.testing import ManualClock
from vs_slurm.api import SlurmCluster, SlurmConfig, SlurmConnectorTransport, SlurmJobRunner
from vs_slurm.fake_connector import HOLD_FILE, executing_cluster, handle, recorded_commands

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _HeldReplyConnector:
    """The executing fake connector, in process, that withholds sbatch's reply on request."""

    def __init__(self, state: Path) -> None:
        self._state = state
        self.submitted = threading.Event()
        self.release_reply = threading.Event()

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        request = json.loads(stdin or "{}")
        response = handle(self._state, request)
        command = str(request.get("command", ""))
        if "sbatch" in command:
            # The job now exists on the cluster; the executor has not heard of it.
            self.submitted.set()
            self.release_reply.wait()
        return subprocess.CompletedProcess(list(argv), 0, json.dumps(response), "")


def _request() -> EvaluationRequest:
    return EvaluationRequest(
        key="in-flight",
        stages=tuple(
            EvaluationStep(
                name=name, payload=SlurmStagePayload(command="true").model_dump(mode="json")
            )
            for name in ("accuracy", "benchmark")
        ),
    )


@pytest.mark.asyncio
async def test_a_stop_during_sbatch_reply_cancels_the_submitted_job_once(tmp_path: Path) -> None:
    state = executing_cluster(tmp_path / "cluster")
    (state / HOLD_FILE).touch()
    connector = _HeldReplyConnector(state)
    config = SlurmConfig(
        name="fake",
        remote_workspace_root=str(tmp_path / "remote"),
        transport=SlurmConnectorTransport(kind="connector", command=("connector",)),
        poll_interval_seconds=0.001,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = SlurmEvaluationExecutor(
        config,
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=SlurmCluster(
            SlurmJobRunner(config, process=connector), state_root=tmp_path / "identity"
        ),
    )
    coordinator = EvaluationCoordinator(
        executor, evaluation_testing.InMemoryEvaluationStore(), ManualClock()
    )
    try:
        evaluation = await coordinator.submit(_request())
        await wait_until_executor_started(connector.submitted, executor, evaluation.id)
        stopping = asyncio.create_task(evaluation.cancel())
        connector.release_reply.set()
        record = await stopping
    finally:
        connector.release_reply.set()
        await executor.close()

    assert record.state is EvaluationState.CANCELED
    scancels = [command for command in recorded_commands(state) if command.startswith("scancel ")]
    assert len(scancels) == 1
