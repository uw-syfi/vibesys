"""A stop that lands while sbatch's reply is still in flight cancels the job it submitted.

The cluster has accepted the job (it exists and is queued) but the executor has
not yet seen the reply, so it holds no job id. Whichever of the stop and the
reply the executor handles first, the evaluation must end CANCELED with exactly
one scancel of the submitted job. A Fake blocking runner holds the completed reply
until the stop is issued, so the test never depends on time or a real worker thread.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

import vs_evaluation.api.testing as evaluation_testing
from vs_evaluation.api import (
    EvaluationCoordinator,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    loop_evaluation_deadline,
)
from vs_evaluation.api.testing import wait_until_executor_started
from vs_sandbox.api.slurm import SlurmEvaluationExecutor, SlurmStagePayload
from vs_sim.api import ProcessOutcome, ProcessRun, ProcessSpec
from vs_sim.api.testing import NO_PROCESSES, ManualClock
from vs_slurm.api import SlurmCluster, SlurmConfig, SlurmConnectorTransport, SlurmJobRunner
from vs_slurm.fake_connector import HOLD_FILE, executing_cluster, handle, recorded_commands

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class _HeldReplyConnector:
    """The executing fake connector, in process."""

    def __init__(self, state: Path) -> None:
        self._state = state

    def run(self, spec: ProcessSpec, *, timeout: float | None) -> ProcessRun:
        stdin = spec.input.decode() or None
        del timeout
        request = json.loads(stdin or "{}")
        response = handle(self._state, request)
        return ProcessOutcome(0, json.dumps(response).encode(), b"")


class _HeldReplyRunner:
    """Run the first cluster call, but withhold its result from the executor."""

    def __init__(self) -> None:
        self.reply_ready = asyncio.Event()
        self.release_reply = asyncio.Event()
        self._held = True

    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Finish the first call, then park its reply until the test releases it."""
        result = function(*args, **kwargs)
        if self._held:
            self._held = False
            self.reply_ready.set()
            await self.release_reply.wait()
        return result


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
    blocking = _HeldReplyRunner()
    executor = SlurmEvaluationExecutor(
        config,
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=SlurmCluster(
            SlurmJobRunner(config, processes=connector, clock=ManualClock()),
            state_root=tmp_path / "identity",
        ),
        clock=ManualClock(),
        sleeper=ManualClock(),
        blocking=blocking,
        processes=NO_PROCESSES,
    )
    coordinator = EvaluationCoordinator(
        executor,
        evaluation_testing.InMemoryEvaluationStore(),
        ManualClock(),
        deadline_factory=loop_evaluation_deadline,
    )
    try:
        evaluation = await coordinator.submit(_request())
        await wait_until_executor_started(blocking.reply_ready, executor, evaluation.id)
        stopping = asyncio.create_task(evaluation.cancel())
        blocking.release_reply.set()
        record = await stopping
    finally:
        blocking.release_reply.set()
        await executor.close()

    assert record.state is EvaluationState.CANCELED
    scancels = [command for command in recorded_commands(state) if command.startswith("scancel ")]
    assert len(scancels) == 1
