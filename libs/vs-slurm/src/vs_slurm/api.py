"""Public API for credential-neutral Slurm job execution.

The library stages a local workspace, submits one Slurm job, waits for its
terminal state, and collects declared artifacts. The built-in transport uses
OpenSSH and rsync with credentials managed outside VibeSys. Sites with custom
gateways can instead provide a versioned JSON connector executable.
"""

from typing import Protocol

from .cluster import SlurmCluster
from .cluster_types import (
    ClusterCancelOutcome,
    ClusterCancelRequested,
    ClusterCollected,
    ClusterCollectOutcome,
    ClusterConflict,
    ClusterHandle,
    ClusterInspectOutcome,
    ClusterObservation,
    ClusterRejected,
    ClusterResult,
    ClusterSubmitOutcome,
    ClusterSubmitted,
    ClusterTarget,
    ClusterUnknown,
)
from .cluster_types import validate_operation_id as validate_cluster_operation_id
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
from .fake_connector import FakeConnector
from .identity import runtime_content_identity
from .runner import (
    SlurmArtifactTarget,
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
    "Cluster",
    "ClusterCancelOutcome",
    "ClusterCancelRequested",
    "ClusterCollectOutcome",
    "ClusterCollected",
    "ClusterConflict",
    "ClusterHandle",
    "ClusterInspectOutcome",
    "ClusterObservation",
    "ClusterRejected",
    "ClusterResult",
    "ClusterSubmitOutcome",
    "ClusterSubmitted",
    "ClusterTarget",
    "ClusterUnknown",
    "FakeConnector",
    "SlurmArtifactTarget",
    "SlurmBatchHandle",
    "SlurmBatchRequest",
    "SlurmBatchResult",
    "SlurmBatchStage",
    "SlurmBatchStageResult",
    "SlurmBatchWaitResult",
    "SlurmCluster",
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
    "validate_cluster_operation_id",
]


class Cluster(Protocol):
    """Stable cluster operations. Unknown requires inspection before replay.

    Reusing an operation ID with another payload returns Conflict. Cancellation
    acknowledges a request only; inspect must confirm scheduler termination.
    Missing collection evidence never implies a successful exit status.
    """

    def submit(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        """Validate and submit once under a caller-supplied stable identity."""
        ...

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        """Observe scheduler evidence without submitting work."""
        ...

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        """Record cancellation intent, leaving confirmation to inspect."""
        ...

    def collect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCollectOutcome:
        """Collect terminal evidence, preserving partial results as Unknown."""
        ...
