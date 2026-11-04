"""A semantic Slurm evaluation refused by a failed root proof says the run environment is unavailable.

The semantic executor rebuilds its observation from the Slurm executor's; were it
to drop the flag, the refusal would read as an ordinary, retryable failure.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    ExecutorObservation,
    SemanticEvaluationStage,
)
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, build_stack
from vs_sim.api.testing import SimThreads, VirtualClock, run_virtual
from vs_slurm.api import (
    FakeConnector,
    SlurmConfig,
    SlurmJobRunner,
    SlurmLocalTransport,
    SlurmWorkspaceRootProbeError,
    WorkspaceRootProof,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_POLL_SECONDS = 10.0
# Guards against a hang only; the probe job fails within a few polls.
_VIRTUAL_LIMIT_SECONDS = 100_000.0
_TERMINAL = {EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED}


def _failing_proof(root: Path, threads: SimThreads) -> WorkspaceRootProof:
    """A proof whose probe job fails on its node: its staged workspace is gone."""
    remote = root / "remote"
    remote.mkdir(parents=True, exist_ok=True)
    connector = FakeConnector(root / "probe-cluster")

    def hide_staging(
        request: dict[str, object], answer: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        if request["operation"] == "exec" and " sbatch " in f" {request.get('command', '')} ":
            for workspace in remote.glob("*/*/workspace"):
                if workspace.is_symlink():
                    workspace.unlink()
                else:
                    shutil.rmtree(workspace)
        return answer(request)

    connector.intercept(hide_staging)
    runner = SlurmJobRunner(
        SlurmConfig(
            name="probe",
            remote_workspace_root=str(remote),
            transport=SlurmLocalTransport(
                kind="local",
                shell_command=("fake-connector", "shell"),
                rsync_command=("fake-connector", "rsync"),
            ),
            poll_interval_seconds=_POLL_SECONDS,
        ),
        processes=connector,
        scratch_root=root / "probe-state",
        clock=threads.clock,
    )
    return WorkspaceRootProof(
        runner, state_root=root / "probe-state", run_id="semantic-refusal", threads=threads
    )


def _request(snapshot: str) -> EvaluationRequest:
    digest = ContentDigest.sha256(b"same")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    return EvaluationRequest(
        key="refused-by-the-root-proof",
        stages=(
            EvaluationStep(
                name=EvidenceKind.ACCURACY.value,
                payload=SemanticEvaluationStage(
                    snapshot=snapshot, kind=EvidenceKind.ACCURACY, fingerprints=fingerprints
                ).model_dump(mode="json"),
            ),
        ),
    )


def test_a_semantic_evaluation_refused_by_a_failed_proof_keeps_the_unavailable_flag(
    tmp_path: Path,
) -> None:
    threads = SimThreads(VirtualClock(limit=_VIRTUAL_LIMIT_SECONDS))
    proof = _failing_proof(tmp_path / "probe", threads)
    cluster = ScenarioCluster()
    handle_id = "semantic-refused-0001"

    async def main() -> ExecutorObservation | None:
        proof.start()
        failure: SlurmWorkspaceRootProbeError | None = None
        while failure is None:
            try:
                proven = proof.check()
            except SlurmWorkspaceRootProbeError as error:
                failure = error
            else:
                assert not proven, "the probe job was meant to fail"
                await threads.clock.sleep(_POLL_SECONDS)
        assert not failure.retryable
        stack = await build_stack(tmp_path / "stack", cluster, root_proof=proof)
        try:
            await stack.executor.submit(_request(stack.snapshot), handle_id=handle_id)
            observation = await stack.executor.inspect(handle_id)
            while observation is None or observation.state not in _TERMINAL:
                await threads.clock.sleep(_POLL_SECONDS)
                observation = await stack.executor.inspect(handle_id)
            return observation
        finally:
            await stack.executor.close()
            proof.close()

    observation = run_virtual(threads.clock, main(), driver=threads)

    assert observation is not None
    assert observation.state is EvaluationState.FAILED
    assert observation.run_environment_unavailable is True
    assert cluster.submissions == []
