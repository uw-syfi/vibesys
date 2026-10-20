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
    GpuJobOutcome,
    GpuJobRequest,
    GpuLauncher,
    choose_partition,
    parse_partition_limits,
)
from vs_sandbox.agent_gpu_cap import CANCELLED_BEFORE_SUBMISSION, CappedGpuLauncher
from vs_sandbox.gpu_command_runner import (
    GpuCaptureRecovery,
    GpuCommandRunner,
    gpu_capture_execution_identity,
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
from vs_sandbox.host_sandbox import remote_job_prefix
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
from vs_sandbox.slurm_comparison import (
    SERVE_PORT_PLACEHOLDER,
    BaselineServer,
    ComparisonMachine,
    ComparisonNotConfiguredError,
    comparison_policy,
    serve_command,
    serve_script,
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
from vs_sandbox.slurm_ledger import (
    Attached,
    AttachOutcome,
    AttachPending,
    LedgerAttachError,
    LedgerWithdrawnError,
    SlurmLedgerAttachment,
    open_ledger_attachment,
)
from vs_sandbox.slurm_ledger_evaluations import CLOSE_RELEASE_BOUND_S
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
from vs_sandbox.slurm_queue_status import (
    SlurmQueueStatusReader,
    queue_status_from_snapshot,
    slurm_queue_status_reader,
)
from vs_sandbox.slurm_settings_toml import render_slurm_operator_toml, write_slurm_operator_toml
from vs_sandbox.slurm_stage_output import SlurmStageOutputOpener

#: The single-file, standard-library-only client an agent's container runs.
HOST_COMMAND_CLIENT = Path(host_command_client.__file__).resolve()

__all__ = [
    "CANCELLED_BEFORE_SUBMISSION",
    "CLOSE_RELEASE_BOUND_S",
    "COMMAND_BROKER_SOCKET_ENV",
    "COMMAND_BROKER_TOKEN_ENV",
    "GPU_ACTIVE_FRACTION_METRIC",
    "HIP_API_TIME_METRIC_PREFIX",
    "HOST_COMMAND_CLIENT",
    "PROFILE_END",
    "PROFILE_OUTPUT_ROOT",
    "PROFILE_STATS_PREFIX",
    "SERVE_PORT_PLACEHOLDER",
    "AgentGpuConfig",
    "AgentGpuLauncher",
    "AgentGpuRequestError",
    "AttachOutcome",
    "AttachPending",
    "Attached",
    "BaselineServer",
    "BrokerReplyError",
    "BrokerTransport",
    "BrokeredProcesses",
    "CappedGpuLauncher",
    "ComparisonMachine",
    "ComparisonNotConfiguredError",
    "GpuCaptureRecovery",
    "GpuCommand",
    "GpuCommandRunner",
    "GpuCommands",
    "GpuJobOutcome",
    "GpuJobRequest",
    "GpuLauncher",
    "HipApiTime",
    "HostCommandBroker",
    "HostCommandBrokerError",
    "HostJobConfinement",
    "JobConfinement",
    "LedgerAttachError",
    "LedgerWithdrawnError",
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
    "SlurmLedgerAttachment",
    "SlurmOperatorSettings",
    "SlurmOutcomeUnknownError",
    "SlurmPolicyError",
    "SlurmProcessBroker",
    "SlurmProcessBrokerError",
    "SlurmQueueStatusReader",
    "SlurmStageOutputOpener",
    "SlurmStagePayload",
    "SlurmTargetLifecycle",
    "agent_gpu_capability",
    "check_read_only_links",
    "choose_partition",
    "comparison_policy",
    "configured_capture_lifecycle",
    "gpu_capture_execution_identity",
    "load_slurm_gpu_alias_settings",
    "load_slurm_operator_settings",
    "load_slurm_policy",
    "open_ledger_attachment",
    "parse_partition_limits",
    "profile_capture_descriptor",
    "queue_status_from_snapshot",
    "read_slurm_capture_plan",
    "remote_job_prefix",
    "render_slurm_operator_toml",
    "require_profile_fields",
    "serve_command",
    "serve_script",
    "slurm_queue_status_reader",
    "split_profile_stats",
    "translate_slurm_gpu",
    "trusted_profile_command",
    "write_slurm_capture_plan",
    "write_slurm_operator_toml",
]
