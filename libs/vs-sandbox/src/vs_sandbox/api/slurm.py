"""Public Slurm adapter contracts.

Operator credentials remain in an external TOML file. Product composition
loads that file by path and supplies trusted stage payloads to the generic
evaluation executor.
"""

from pathlib import Path

from vs_sandbox import host_command_client
from vs_sandbox.agent_gpu import (
    AgentGpuConfig,
    AgentGpuLauncher,
    AgentGpuRequestError,
    GpuCommand,
    GpuJobRequest,
    GpuLauncher,
    choose_partition,
)
from vs_sandbox.host_command_broker import (
    BrokerTransport,
    GpuCommands,
    HostCommandBroker,
    HostCommandBrokerError,
    RunRoots,
)
from vs_sandbox.host_command_client import SOCKET_ENV as COMMAND_BROKER_SOCKET_ENV
from vs_sandbox.host_command_client import TOKEN_ENV as COMMAND_BROKER_TOKEN_ENV
from vs_sandbox.job_confinement import HostJobConfinement, JobConfinement
from vs_sandbox.read_only_links import ReadOnlyPathLinkError, check_read_only_links
from vs_sandbox.slurm_broker import SlurmProcessBroker, SlurmProcessBrokerError
from vs_sandbox.slurm_broker_client import BrokeredProcesses, BrokerReplyError
from vs_sandbox.slurm_capture_plan import (
    SlurmCapturePlan,
    SlurmCapturePlanError,
    SlurmEvaluationPlan,
    read_slurm_capture_plan,
    write_slurm_capture_plan,
)
from vs_sandbox.slurm_executor import (
    SharedSlurmAdmission,
    SlurmCommandResult,
    SlurmEvaluationExecutor,
    SlurmExecutionMetadata,
    SlurmOutcomeUnknownError,
    SlurmStagePayload,
    SlurmTargetLifecycle,
)
from vs_sandbox.slurm_gpu_alias import (
    SlurmGpuAliasError,
    load_slurm_gpu_alias_settings,
    translate_slurm_gpu,
)
from vs_sandbox.slurm_policy import (
    SlurmExecutionPolicy,
    SlurmOperatorSettings,
    SlurmPolicyError,
    agent_gpu_capability,
    load_slurm_operator_settings,
    load_slurm_policy,
)
from vs_sandbox.slurm_profile import (
    GPU_ACTIVE_FRACTION_METRIC,
    HIP_API_TIME_METRIC_PREFIX,
    PROFILE_END,
    PROFILE_OUTPUT_ROOT,
    PROFILE_STATS_PREFIX,
    HipApiTime,
    ProfileCaptureDescriptor,
    ProfileStats,
    configured_capture_lifecycle,
    profile_capture_descriptor,
    require_profile_fields,
    split_profile_stats,
    trusted_profile_command,
)
from vs_sandbox.slurm_settings_toml import render_slurm_operator_toml, write_slurm_operator_toml

#: The single-file, standard-library-only client an agent's container runs.
HOST_COMMAND_CLIENT = Path(host_command_client.__file__).resolve()

__all__ = [
    "COMMAND_BROKER_SOCKET_ENV",
    "COMMAND_BROKER_TOKEN_ENV",
    "GPU_ACTIVE_FRACTION_METRIC",
    "HIP_API_TIME_METRIC_PREFIX",
    "HOST_COMMAND_CLIENT",
    "PROFILE_END",
    "PROFILE_OUTPUT_ROOT",
    "PROFILE_STATS_PREFIX",
    "AgentGpuConfig",
    "AgentGpuLauncher",
    "AgentGpuRequestError",
    "BrokerReplyError",
    "BrokerTransport",
    "BrokeredProcesses",
    "GpuCommand",
    "GpuCommands",
    "GpuJobRequest",
    "GpuLauncher",
    "HipApiTime",
    "HostCommandBroker",
    "HostCommandBrokerError",
    "HostJobConfinement",
    "JobConfinement",
    "ProfileCaptureDescriptor",
    "ProfileStats",
    "ReadOnlyPathLinkError",
    "RunRoots",
    "SharedSlurmAdmission",
    "SlurmCapturePlan",
    "SlurmCapturePlanError",
    "SlurmCommandResult",
    "SlurmEvaluationExecutor",
    "SlurmEvaluationPlan",
    "SlurmExecutionMetadata",
    "SlurmExecutionPolicy",
    "SlurmGpuAliasError",
    "SlurmOperatorSettings",
    "SlurmOutcomeUnknownError",
    "SlurmPolicyError",
    "SlurmProcessBroker",
    "SlurmProcessBrokerError",
    "SlurmStagePayload",
    "SlurmTargetLifecycle",
    "agent_gpu_capability",
    "check_read_only_links",
    "choose_partition",
    "configured_capture_lifecycle",
    "load_slurm_gpu_alias_settings",
    "load_slurm_operator_settings",
    "load_slurm_policy",
    "profile_capture_descriptor",
    "read_slurm_capture_plan",
    "render_slurm_operator_toml",
    "require_profile_fields",
    "split_profile_stats",
    "translate_slurm_gpu",
    "trusted_profile_command",
    "write_slurm_capture_plan",
    "write_slurm_operator_toml",
]
