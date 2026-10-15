"""Public API for credential-neutral Slurm job execution.

The library stages a local workspace, submits one Slurm job, waits for its
terminal state, and collects declared artifacts. The built-in transport uses
OpenSSH and rsync with credentials managed outside VibeSys. A host that is
itself a submit node uses the local transport instead. Sites with custom
gateways can provide a versioned JSON connector executable.
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
    SlurmLocalTransport,
    SlurmService,
    SlurmSshTransport,
    SlurmTransport,
    load_slurm_config,
    shell_join_with_port,
)
from .connector_protocol import (
    MAX_CONNECTOR_REQUEST_BYTES,
    MAX_CONNECTOR_RESPONSE_BYTES,
    MAX_TRANSPORT_STREAM_BYTES,
    SLURM_CONNECTOR_REQUEST,
    SlurmConnectorExecRequest,
    SlurmConnectorGetRequest,
    SlurmConnectorPutRequest,
    SlurmConnectorRequest,
    SlurmConnectorResponse,
    SlurmConnectorSyncToRequest,
)
from .fake_cluster import FakeCluster, SecondsRange, SlurmTimingProfile
from .fake_connector import REQUESTS_FILE, FakeConnector
from .identity import runtime_content_identity
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
    SlurmJobWaitResult,
    SlurmSubmissionRejectedError,
    SlurmTreeArtifact,
)
from .scheduler_read import READ_END as SCHEDULER_READ_END
from .scheduler_read import split_framed as split_framed_scheduler_query
from .scheduler_read import unframed as unframed_output
from .scheduler_states import (
    INITIAL_LIFE,
    AccountingRow,
    Completing,
    Ended,
    EndOutcome,
    JobLife,
    NoAccountingRow,
    Notice,
    NotInQueue,
    Queued,
    QueueRow,
    Running,
    SlurmJobStatus,
    SlurmPhase,
    SlurmRawState,
    SlurmStatus,
    Step,
    Unknown,
    advance,
    advance_with_notice,
    life_for_status,
    needs_accounting,
    observe,
    observe_with_notice,
    parse_accounting_row,
    parse_queue_row,
    phase_of,
    status_of,
)
from .staging import tree_content_identity
from .trace_replay import (
    DEFAULT_COMMAND_SECONDS,
    IssuedCommand,
    SchedulerTrace,
    TraceConnector,
    TraceStep,
)
from .transport_broker import (
    SlurmTransportBrokerPolicy,
    SlurmTransportRequestError,
    slurm_transport_broker_available,
    slurm_transport_broker_policy,
)

__all__ = [
    "CANCEL_REACTIONS",
    "DEFAULT_COMMAND_SECONDS",
    "INITIAL_LIFE",
    "LIFETIMES",
    "MAX_CONNECTOR_REQUEST_BYTES",
    "MAX_CONNECTOR_RESPONSE_BYTES",
    "MAX_TRANSPORT_STREAM_BYTES",
    "PORT_PLACEHOLDER",
    "REQUESTS_FILE",
    "SCHEDULER_READ_END",
    "SERVICE_NOT_READY_EXIT_CODE",
    "SLURM_CONNECTOR_REQUEST",
    "AccountingRow",
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
    "Completing",
    "EndOutcome",
    "Ended",
    "FakeCluster",
    "FakeConnector",
    "IssuedCommand",
    "JobLife",
    "NoAccountingRow",
    "NotInQueue",
    "Notice",
    "QueueRow",
    "Queued",
    "Running",
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
    "SlurmConnectorExecRequest",
    "SlurmConnectorGetRequest",
    "SlurmConnectorPutRequest",
    "SlurmConnectorRequest",
    "SlurmConnectorResponse",
    "SlurmConnectorSyncToRequest",
    "SlurmConnectorTransport",
    "SlurmError",
    "SlurmFileArtifact",
    "SlurmJobHandle",
    "SlurmJobRequest",
    "SlurmJobResult",
    "SlurmJobRunner",
    "SlurmJobStatus",
    "SlurmJobWaitResult",
    "SlurmLocalTransport",
    "SlurmPhase",
    "SlurmRawState",
    "SlurmService",
    "SlurmSshTransport",
    "SlurmStatus",
    "SlurmSubmissionRejectedError",
    "SlurmTimingProfile",
    "SlurmTransport",
    "SlurmTransportBrokerPolicy",
    "SlurmTransportRequestError",
    "SlurmTreeArtifact",
    "Step",
    "TraceConnector",
    "TraceStep",
    "Unknown",
    "advance",
    "advance_with_notice",
    "life_for_status",
    "load_slurm_config",
    "needs_accounting",
    "observe",
    "observe_with_notice",
    "parse_accounting_row",
    "parse_queue_row",
    "phase_of",
    "runtime_content_identity",
    "shell_join_with_port",
    "slurm_transport_broker_available",
    "slurm_transport_broker_policy",
    "split_framed_scheduler_query",
    "status_of",
    "tree_content_identity",
    "unframed_output",
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
