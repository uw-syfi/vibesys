"""The production semantic Slurm evaluation executor over a workspace environment and a Fake cluster.

One place builds it for every test that needs the real executor with the Fake Slurm cluster
beneath it and a workspace environment's real workspaces above it: the skeleton world's runs
and the executor-level tests that need no run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.workspace_world import RUN_ID

from vs_runtime.api import render_stage_failure
from vs_runtime.api.infrastructure import (
    ScalarBenchmarkContract,
    SemanticSlurmEvaluationExecutor,
    TrustedEvaluationPlan,
)
from vs_sandbox.api.slurm import SlurmEvaluationPlan, SlurmExecutionPolicy
from vs_sim.api.testing import NO_PROCESSES, current_virtual_clock
from vs_slurm.api import SlurmConfig, SlurmSshTransport

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.workspace_world import WorkspaceEnv

    from vs_runtime.api.infrastructure import RunControlChannel, RuntimeWorkspaces
    from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster
    from vs_sim.api import Clock

JOB_TIMEOUT_S = 300
# A Slurm job that runs this long, or waits this long in the queue, is cancelled and fails: well
# inside the run deadline (1000 s), so a job that never leaves the queue fails on its own queue
# deadline instead of running into the run's.
QUEUE_TIMEOUT_S = 300


class LoopPacing:
    """Paces job polls on the virtual loop the world runs on, so polling lets run time pass."""

    async def sleep(self, seconds: float) -> None:
        """Sleep on the running virtual loop's clock."""
        await current_virtual_clock().sleep(seconds)


def slurm_evaluation(  # noqa: PLR0913  # lint-waiver: LW-731843 [PLR0913]; the environment, the root under which its handles live, the cluster, the workspaces and the two optional seams are independent inputs of the one executor.
    env: WorkspaceEnv,
    root: Path,
    cluster: ScenarioCluster,
    workspaces: RuntimeWorkspaces,
    *,
    clock: Clock,
    control: RunControlChannel | None = None,
) -> SemanticSlurmEvaluationExecutor:
    """The executor over ``workspaces`` and ``cluster``, its durable records under ``root``.

    ``clock`` is what the job deadlines count on.
    """
    namespace = env.project.state.local_namespace(RUN_ID, "evaluation")
    return SemanticSlurmEvaluationExecutor(
        SlurmConfig(
            name="test",
            remote_workspace_root="/runs",
            transport=SlurmSshTransport(host="test"),
            job_timeout_seconds=JOB_TIMEOUT_S,
            queue_timeout_seconds=QUEUE_TIMEOUT_S,
        ),
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=root / "slurm.toml",
            accuracy_command=("python", "accuracy.py"),
            benchmark_command=("python", "benchmark.py"),
        ),
        TrustedEvaluationPlan(
            accuracy_command="unused",
            benchmark_command="unused",
            benchmark_contract=ScalarBenchmarkContract(
                output_argument="--output", metric="throughput"
            ),
        ),
        workspaces,
        namespace,  # type: ignore[arg-type]  # the evaluation namespace is a StateNamespace
        root / "handles",
        stage_failure_text=render_stage_failure,
        clock=clock,
        sleeper=LoopPacing(),
        cluster=cluster,
        control=control,
        blocking=env.execution.blocking,
        processes=NO_PROCESSES,
    )
