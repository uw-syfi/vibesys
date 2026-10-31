"""Public API for credential-neutral Slurm job execution.

The library stages a local workspace, submits one Slurm job, waits for its
terminal state, and collects declared artifacts. The built-in transport uses
OpenSSH and rsync with credentials managed outside VibeSys. A host that is
itself a submit node uses the local transport instead. Sites with custom
gateways can provide a versioned JSON connector executable.
"""

from typing import Protocol

from vs_sim.api import ByteSource, OutputStream

from .cluster import SlurmCluster, recorded_job_ids
from .cluster_types import (
    OBSERVED_CANCEL_CALLS,
    ClusterCall,
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
    MinimalJob,
    SlurmConfig,
    SlurmConfigError,
    SlurmConnectorTransport,
    SlurmJobResources,
    SlurmLocalTransport,
    SlurmService,
    SlurmSshTransport,
    SlurmTransport,
    load_slurm_config,
    minimal_job,
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
from .errors import (
    SlurmError,
    SlurmSbatchFailedError,
    SlurmSubmissionRejectedError,
    SlurmTransferInterruptedError,
    SlurmTransportFailedError,
    SlurmWorkspaceRootProbeError,
)
from .fake_cluster import FakeCluster, SecondsRange, SlurmTimingProfile
from .fake_connector import REQUESTS_FILE, FakeConnector
from .fake_ledger import FakeEvaluationLedger
from .identity import runtime_content_identity
from .job_teardown import PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE
from .ledger import (
    BATCH_RESULT_LAYOUT,
    LEDGER_FORMAT_VERSION,
    MAX_WITHDRAWAL_WINDOW_SECONDS,
    AttachJob,
    AwaitWithdrawal,
    CheckJob,
    ClaimAttempt,
    Leased,
    LeaseVerdict,
    LedgerArtifact,
    LedgerEntry,
    LedgerError,
    LedgerLabels,
    LedgerLease,
    LedgerPolicy,
    LedgerRecord,
    LedgerStage,
    LedgerState,
    ResolveSubmission,
    Settlement,
    SubmitBoundJob,
    SubmitStep,
    Unleased,
    UnreadableLease,
    WithdrawalMark,
    WithdrawalUndone,
    check_transition,
    claim_expired,
    decode_lease,
    decode_record,
    evaluation_key,
    handle_of_job,
    held_by_others,
    job_of_handle,
    lease_verdict,
    ledger_operation_id,
    settlement,
    submit_step,
    transitioned,
    validate_handle,
    validate_holder,
    validate_project,
)
from .ledger import encode as encode_ledger_document
from .ledger_actions import (
    CLOCK_SKEW_SECONDS,
    EndedJobless,
    JobMayRun,
    Kept,
    NotInLedger,
    OrphanChecks,
    WithdrawalTiming,
    Withdrawer,
    Withdrawn,
    WithdrawOutcome,
    WithdrawUnknown,
    relinquish,
    settle,
    sweep_unleased,
    sweep_withdrawer,
    take_over_jobless,
    undo_expired_withdrawal,
    withdraw,
    withdrawal_window_seconds,
)
from .ledger_listing import (
    ENTRY_ID_LENGTH,
    EntryPhase,
    LedgerListing,
    LedgerRow,
    Resolved,
    Unresolved,
    entry_id,
    list_entries,
    resolve_entries,
)
from .ledger_store import RemoteEvaluationLedger
from .queue_view import (
    NodeGpus,
    OwnJob,
    QueuedJob,
    QueueSnapshot,
    QueueViewError,
    gpu_count,
    parse_queue_snapshot,
    queue_snapshot_command,
    sbatch_partitions,
)
from .recorded_traces import CANCEL_REACTIONS, LIFETIMES
from .remote_file import MAX_READ_BYTES as MAX_REMOTE_READ_BYTES
from .remote_file import RemoteFileAnswerError, SlurmFileSource
from .runner import (
    SERVICE_NOT_READY_EXIT_CODE,
    STAGE_TIMEOUT_EXIT_CODES,
    SchedulerReading,
    SlurmArtifactTarget,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStage,
    SlurmBatchStageResult,
    SlurmBatchWaitResult,
    SlurmFileArtifact,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobResult,
    SlurmJobRunner,
    SlurmJobWaitResult,
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
from .workspace_probe import (
    WorkspaceRootProbeQueued,
    WorkspaceRootProbeWatch,
    probe_workspace_root,
)
from .workspace_proof import (
    WorkspaceRootProbeFailed,
    WorkspaceRootProbeProven,
    WorkspaceRootProbeReport,
    WorkspaceRootProbeRetrying,
    WorkspaceRootProof,
)

__all__ = [
    "BATCH_RESULT_LAYOUT",
    "CANCEL_REACTIONS",
    "CLOCK_SKEW_SECONDS",
    "DEFAULT_COMMAND_SECONDS",
    "ENTRY_ID_LENGTH",
    "INITIAL_LIFE",
    "LEDGER_FORMAT_VERSION",
    "LIFETIMES",
    "MAX_CONNECTOR_REQUEST_BYTES",
    "MAX_CONNECTOR_RESPONSE_BYTES",
    "MAX_REMOTE_READ_BYTES",
    "MAX_TRANSPORT_STREAM_BYTES",
    "MAX_WITHDRAWAL_WINDOW_SECONDS",
    "OBSERVED_CANCEL_CALLS",
    "PORT_PLACEHOLDER",
    "PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE",
    "REQUESTS_FILE",
    "SCHEDULER_READ_END",
    "SERVICE_NOT_READY_EXIT_CODE",
    "SLURM_CONNECTOR_REQUEST",
    "STAGE_TIMEOUT_EXIT_CODES",
    "AccountingRow",
    "AttachJob",
    "AwaitWithdrawal",
    "CheckJob",
    "ClaimAttempt",
    "Cluster",
    "ClusterCall",
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
    "EndedJobless",
    "EntryPhase",
    "EvaluationLedger",
    "FakeCluster",
    "FakeConnector",
    "FakeEvaluationLedger",
    "IssuedCommand",
    "JobLife",
    "JobMayRun",
    "Kept",
    "LeaseVerdict",
    "Leased",
    "LedgerArtifact",
    "LedgerEntry",
    "LedgerError",
    "LedgerLabels",
    "LedgerLease",
    "LedgerListing",
    "LedgerPolicy",
    "LedgerRecord",
    "LedgerRow",
    "LedgerStage",
    "LedgerState",
    "MinimalJob",
    "NoAccountingRow",
    "NodeGpus",
    "NotInLedger",
    "NotInQueue",
    "Notice",
    "OrphanChecks",
    "OwnJob",
    "QueueRow",
    "QueueSnapshot",
    "QueueViewError",
    "Queued",
    "QueuedJob",
    "RemoteEvaluationLedger",
    "RemoteFileAnswerError",
    "ResolveSubmission",
    "Resolved",
    "Running",
    "SchedulerQueue",
    "SchedulerReading",
    "SchedulerTrace",
    "SecondsRange",
    "Settlement",
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
    "SlurmFileSource",
    "SlurmJobHandle",
    "SlurmJobRequest",
    "SlurmJobResources",
    "SlurmJobResult",
    "SlurmJobRunner",
    "SlurmJobStatus",
    "SlurmJobWaitResult",
    "SlurmLocalTransport",
    "SlurmPhase",
    "SlurmRawState",
    "SlurmSbatchFailedError",
    "SlurmService",
    "SlurmSshTransport",
    "SlurmStatus",
    "SlurmSubmissionRejectedError",
    "SlurmTimingProfile",
    "SlurmTransferInterruptedError",
    "SlurmTransport",
    "SlurmTransportBrokerPolicy",
    "SlurmTransportFailedError",
    "SlurmTransportRequestError",
    "SlurmTreeArtifact",
    "SlurmWorkspaceRootProbeError",
    "Step",
    "SubmitBoundJob",
    "SubmitStep",
    "TraceConnector",
    "TraceStep",
    "Unknown",
    "Unleased",
    "UnreadableLease",
    "Unresolved",
    "WithdrawOutcome",
    "WithdrawUnknown",
    "WithdrawalMark",
    "WithdrawalTiming",
    "WithdrawalUndone",
    "Withdrawer",
    "Withdrawn",
    "WorkspaceRootProbeFailed",
    "WorkspaceRootProbeProven",
    "WorkspaceRootProbeQueued",
    "WorkspaceRootProbeReport",
    "WorkspaceRootProbeRetrying",
    "WorkspaceRootProbeWatch",
    "WorkspaceRootProof",
    "advance",
    "advance_with_notice",
    "check_transition",
    "claim_expired",
    "decode_lease",
    "decode_record",
    "encode_ledger_document",
    "entry_id",
    "evaluation_key",
    "gpu_count",
    "handle_of_job",
    "held_by_others",
    "job_of_handle",
    "lease_verdict",
    "ledger_operation_id",
    "life_for_status",
    "list_entries",
    "load_slurm_config",
    "minimal_job",
    "needs_accounting",
    "observe",
    "observe_with_notice",
    "parse_accounting_row",
    "parse_queue_row",
    "parse_queue_snapshot",
    "phase_of",
    "probe_workspace_root",
    "queue_snapshot_command",
    "recorded_job_ids",
    "relinquish",
    "resolve_entries",
    "runtime_content_identity",
    "sbatch_partitions",
    "settle",
    "settlement",
    "shell_join_with_port",
    "slurm_transport_broker_available",
    "slurm_transport_broker_policy",
    "split_framed_scheduler_query",
    "status_of",
    "submit_step",
    "sweep_unleased",
    "sweep_withdrawer",
    "take_over_jobless",
    "transitioned",
    "tree_content_identity",
    "undo_expired_withdrawal",
    "unframed_output",
    "validate_cluster_operation_id",
    "validate_handle",
    "validate_holder",
    "validate_project",
    "withdraw",
    "withdrawal_window_seconds",
]


class SchedulerQueue(Protocol):
    """Reads the queue this user's jobs wait in; ``SlurmJobRunner`` is the real one."""

    def read_queue(self) -> QueueSnapshot:
        """One read-only reading; raises ``SlurmError`` or ``QueueViewError`` when it fails."""
        ...


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

    def cancel(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCancelOutcome:
        """Record cancellation intent, leaving confirmation to inspect.

        ``observed`` is a reading the caller just took of this target. When it
        shows the job queued or running, the scancel is the cancel's first
        transport call (``OBSERVED_CANCEL_CALLS``), with no fresh scheduler read
        before it; the rest of the cancel follows as without it. A reading of
        another job is Unknown, and nothing is cancelled.
        """
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

    def stage_output(
        self, handle: SlurmBatchHandle, index: int, stream: OutputStream
    ) -> ByteSource:
        """What stage ``index`` of the batch has written to ``stream`` so far, as it grows.

        The source reads the stage's output file where the job writes it; it raises
        ``FileNotFoundError`` until the stage has started. Reading it never changes the job.
        """
        ...


class EvaluationLedger(Protocol):
    """A project's durable evaluation records, shared by every process of the project.

    Implementations: ``RemoteEvaluationLedger`` (the cluster's shared filesystem)
    and ``FakeEvaluationLedger``; both pass ``tests/test_ledger_contract.py``.
    Every method raises ``LedgerError`` when the storage cannot be read or
    written: the request's state is then unknown, never absent.
    """

    def read(self, key: str) -> LedgerEntry | None:
        """The request's current attempt, its revision and every lease, or None if never asked for.

        A lease removed while it is read is absent, never an error.
        """
        ...

    def claim(self, record: LedgerRecord) -> LedgerRecord:
        """Create ``record``'s attempt atomically unless it exists; return the stored attempt.

        Of two processes claiming one attempt, exactly one gets its own record back.
        """
        ...

    def replace(self, current: LedgerEntry, record: LedgerRecord) -> bool:
        """Compare-and-swap: write ``record`` as the revision after ``current``.

        True when it was written. False when another writer already moved the
        attempt past ``current``: the caller reads the request again and decides
        anew. Raises ``LedgerError`` for a change ``check_transition`` refuses.
        """
        ...

    def write_withdrawal(
        self, current: LedgerEntry, document: WithdrawalMark | WithdrawalUndone
    ) -> bool:
        """Compare-and-swap: write ``document`` as the withdrawal revision after ``current``'s.

        True when it was written; False when another withdrawal revision of the
        attempt was written since ``current`` was read. Raises ``LedgerError``
        for a revision ``check_withdrawal`` refuses. Never touches the record.
        """
        ...

    def renew(self, key: str, lease: LedgerLease) -> None:
        """Write the holder's lease on the request; only that holder writes it."""
        ...

    def release(self, key: str, holder: str) -> None:
        """Drop the holder's lease on the request."""
        ...

    def list_keys(self) -> tuple[str, ...]:
        """Every request key the project's ledger holds."""
        ...

    def bind_handle(self, holder: str, handle: str, key: str) -> str:
        """Record that the run ``holder``'s evaluation ``handle`` asks for request ``key``.

        Create-only, so the first binding stands: returns the key the handle is bound
        to, which is ``key`` unless an earlier call bound another. A later process that
        knows only the handle finds the request through it (``bound_key``), without the
        content its key hashes. The index sits beside the requests and is no part of one,
        so a build that does not know it neither reads nor trips over it.
        """
        ...

    def bound_key(self, holder: str, handle: str) -> str | None:
        """The key the run ``holder``'s evaluation ``handle`` was bound to, None if never."""
        ...

    def operator_file_sha256(self, path: str) -> str | None:
        """The SHA-256 of a file on the cluster's filesystem (a setup script), None if absent.

        A job reads such a file at run time, so its content is part of the key.
        """
        ...
