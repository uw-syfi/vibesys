"""Public Slurm adapter contracts.

Operator credentials remain in an external TOML file. Product composition
loads that file by path and supplies trusted stage payloads to the generic
evaluation executor.
"""

from vs_sandbox.slurm_broker import SlurmProcessBroker, SlurmProcessBrokerError
from vs_sandbox.slurm_broker_client import run_brokered_process
from vs_sandbox.slurm_capture_plan import (
    SlurmCapturePlan,
    SlurmCapturePlanError,
    SlurmEvaluationPlan,
    read_slurm_capture_plan,
    read_slurm_evaluation_plan,
    write_slurm_capture_plan,
    write_slurm_evaluation_plan,
)
from vs_sandbox.slurm_executor import (
    SharedSlurmAdmission,
    SlurmCommandResult,
    SlurmEvaluationExecutor,
    SlurmExecutionMetadata,
    SlurmStagePayload,
    SlurmTargetLifecycle,
)
from vs_sandbox.slurm_policy import (
    SlurmExecutionPolicy,
    SlurmPolicyError,
    load_slurm_policy,
)

__all__ = [
    "SharedSlurmAdmission",
    "SlurmCapturePlan",
    "SlurmCapturePlanError",
    "SlurmCommandResult",
    "SlurmEvaluationExecutor",
    "SlurmEvaluationPlan",
    "SlurmExecutionMetadata",
    "SlurmExecutionPolicy",
    "SlurmPolicyError",
    "SlurmProcessBroker",
    "SlurmProcessBrokerError",
    "SlurmStagePayload",
    "SlurmTargetLifecycle",
    "load_slurm_policy",
    "read_slurm_capture_plan",
    "read_slurm_evaluation_plan",
    "run_brokered_process",
    "write_slurm_capture_plan",
    "write_slurm_evaluation_plan",
]
