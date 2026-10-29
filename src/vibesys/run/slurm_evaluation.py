"""Product binding of the generic semantic Slurm executor to its failure prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime.api import render_stage_failure
from vs_runtime.api.infrastructure import SemanticSlurmEvaluationExecutor

if TYPE_CHECKING:
    from pathlib import Path

    from vs_evaluation.api import EvaluationStateNamespace
    from vs_runtime.api import Workspaces
    from vs_runtime.api.core import StageOutputOpener
    from vs_runtime.api.infrastructure import RunControlChannel, TrustedEvaluationPlan
    from vs_sandbox.api.slurm import SharedSlurmAdmission, SlurmEvaluationPlan, SlurmExecutionPolicy
    from vs_sim.api import Clock, Processes, Sleeper
    from vs_slurm.api import Cluster, SlurmConfig, WorkspaceRootProof


class SlurmSemanticEvaluationExecutor(SemanticSlurmEvaluationExecutor):
    """The generic executor with the product's rendered stage-failure text."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-940002 [PLR0913]; these arguments are independent injected ports or policy facts, unchanged from the relocated constructor; grouping them would add a shallow carrier.
        self,
        config: SlurmConfig,
        policy: SlurmExecutionPolicy,
        plan: SlurmEvaluationPlan,
        trusted_plan: TrustedEvaluationPlan,
        workspaces: Workspaces,
        namespace: EvaluationStateNamespace,
        handle_root: Path,
        *,
        clock: Clock,
        sleeper: Sleeper,
        admission: SharedSlurmAdmission | None = None,
        cluster: Cluster | None = None,
        processes: Processes | None = None,
        ledger_identity: tuple[str, str] | None = None,
        stage_output: StageOutputOpener | None = None,
        root_proof: WorkspaceRootProof | None = None,
        control: RunControlChannel | None = None,
    ) -> None:
        """Bind external Slurm policy to semantic evaluation state."""
        super().__init__(
            config,
            policy,
            plan,
            trusted_plan,
            workspaces,
            namespace,
            handle_root,
            stage_failure_text=render_stage_failure,
            clock=clock,
            sleeper=sleeper,
            admission=admission,
            cluster=cluster,
            processes=processes,
            ledger_identity=ledger_identity,
            stage_output=stage_output,
            root_proof=root_proof,
            control=control,
        )


__all__ = ["SlurmSemanticEvaluationExecutor"]
