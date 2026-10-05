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
from .fake_cluster import FakeCluster, ManualClock, SecondsRange, SlurmTimingProfile
from .fake_connector import REQUESTS_FILE, FakeConnector
from .identity import runtime_content_identity
from .phase_register import MergedPhase, PhaseAnomaly, PhaseRegister
from .recorded_traces import CANCEL_REACTIONS, LIFETIMES
from .runner import (
    SERVICE_NOT_READY_EXIT_CODE,
    SchedulerReading,
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
    SlurmPhase,
    SlurmProcess,
    SlurmSubmissionRejectedError,
    SlurmTreeArtifact,
    phase_of,
)
from .staging import tree_content_identity
from .trace_replay import (
    DEFAULT_COMMAND_SECONDS,
    IssuedCommand,
    SchedulerTrace,
    TraceConnector,
    TraceStep,
)

__all__ = [
    "CANCEL_REACTIONS",
    "DEFAULT_COMMAND_SECONDS",
    "LIFETIMES",
    "PORT_PLACEHOLDER",
    "REQUESTS_FILE",
    "SERVICE_NOT_READY_EXIT_CODE",
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
    "FakeCluster",
    "FakeConnector",
    "IssuedCommand",
    "ManualClock",
    "MergedPhase",
    "PhaseAnomaly",
    "PhaseRegister",
    "SchedulerReading",
    "SchedulerTrace",
    "SecondsRange",
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
    "SlurmPhase",
    "SlurmProcess",
    "SlurmService",
    "SlurmSshTransport",
    "SlurmSubmissionRejectedError",
    "SlurmTimingProfile",
    "SlurmTransport",
    "SlurmTreeArtifact",
    "TraceConnector",
    "TraceStep",
    "load_slurm_config",
    "phase_of",
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

    def collect(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCollectOutcome:
        """Collect terminal evidence, preserving partial results as Unknown."""
        ...
