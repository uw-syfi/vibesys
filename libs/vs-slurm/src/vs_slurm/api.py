"""Public API for credential-neutral Slurm job execution.

The library stages a local workspace, submits one Slurm job, waits for its
terminal state, and collects declared artifacts. The built-in transport uses
OpenSSH and rsync with credentials managed outside VibeSys. Sites with custom
gateways can instead provide a versioned JSON connector executable.
"""

from .config import (
    PORT_PLACEHOLDER,
    SlurmConfig,
    SlurmConfigError,
    SlurmConnectorTransport,
    SlurmService,
    SlurmSshTransport,
    SlurmTransport,
    load_slurm_config,
    shell_join_with_port,
)
from .identity import runtime_content_identity
from .runner import (
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStage,
    SlurmBatchStageResult,
    SlurmBatchWaitResult,
    SlurmError,
    SlurmFileArtifact,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobResult,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmJobWaitResult,
    SlurmProcess,
    SlurmSubmissionRejectedError,
    SlurmTreeArtifact,
)
from .staging import tree_content_identity

__all__ = [
    "PORT_PLACEHOLDER",
    "SlurmBatchHandle",
    "SlurmBatchRequest",
    "SlurmBatchResult",
    "SlurmBatchStage",
    "SlurmBatchStageResult",
    "SlurmBatchWaitResult",
    "SlurmConfig",
    "SlurmConfigError",
    "SlurmConnectorTransport",
    "SlurmError",
    "SlurmFileArtifact",
    "SlurmJobHandle",
    "SlurmJobRequest",
    "SlurmJobResult",
    "SlurmJobRunner",
    "SlurmJobStatus",
    "SlurmJobWaitResult",
    "SlurmProcess",
    "SlurmService",
    "SlurmSshTransport",
    "SlurmSubmissionRejectedError",
    "SlurmTransport",
    "SlurmTreeArtifact",
    "load_slurm_config",
    "runtime_content_identity",
    "shell_join_with_port",
    "tree_content_identity",
]
