"""Hide the operator's private cluster paths in what an evaluation reports.

The operator's Slurm profile names absolute paths on the cluster (its interpreter, setup
script, workspace root). A stage's output echoes them (a failed import names the interpreter,
a traceback names its packages), and that output reaches the agent that submitted the
evaluation and the operator's screens. Nothing downstream needs them: the agent cannot change
them, and the screens show the stage's own verdict. ``mask_observation`` is applied once, where
the executor turns the cluster's report into evidence, so every projection of it is masked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_redaction.api import mask_host_paths
from vs_sandbox.api.slurm import SlurmCommandResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_evaluation.api import EvaluationStepResult, ExecutorObservation
    from vs_slurm.api import SlurmConfig


def private_paths(config: SlurmConfig, policy_paths: Sequence[str]) -> tuple[str, ...]:
    """The operator's absolute paths: the profile's own, and the first word of its sbatch command."""
    return (config.remote_workspace_root, *config.sbatch_command[:1], *policy_paths)


def mask_observation(observed: ExecutorObservation, paths: Sequence[str]) -> ExecutorObservation:
    """``observed`` with every operator path in its text replaced; unchanged when none is configured."""
    if not paths:
        return observed

    def mask(text: str | None) -> str | None:
        return None if text is None else mask_host_paths(text, paths)

    return observed.model_copy(
        update={
            "failure": mask(observed.failure),
            "operator_failure": mask(observed.operator_failure),
            "stage_results": tuple(_mask_step(step, paths) for step in observed.stage_results),
        }
    )


def _mask_step(step: EvaluationStepResult, paths: Sequence[str]) -> EvaluationStepResult:
    def mask(text: str | None) -> str | None:
        return None if text is None else mask_host_paths(text, paths)

    update: dict[str, object] = {
        "failure": mask(step.failure),
        "operator_failure": mask(step.operator_failure),
        "operator_summary": mask(step.operator_summary),
        "stdout_tail": mask(step.stdout_tail),
        "stderr_tail": mask(step.stderr_tail),
        "service_log_tail": mask(step.service_log_tail),
    }
    if step.result is not None:
        raw = SlurmCommandResult.model_validate(step.result)
        masked = raw.model_copy(
            update={
                "output": mask_host_paths(raw.output, paths),
                "stdout": mask_host_paths(raw.stdout, paths),
                "stderr": mask_host_paths(raw.stderr, paths),
                "collection_failure": mask(raw.collection_failure),
            }
        )
        update["result"] = masked.model_dump(mode="json")
    return step.model_copy(update=update)
