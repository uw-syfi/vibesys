"""A run's attachment to its project's evaluation ledger (#2757, Phase 1).

With an attachment, the Slurm executor submits through the ledger: a request
whose key already has a job attaches to it, a finished one is collected from
the cluster's disk, and only a request the ledger does not hold is submitted.
The run renews a lease on each request it attaches to. Stopping or crashing
cancels nothing; a request no run leases for the grace period is withdrawn by
the sweep, which any attached run performs.

The calls here are blocking (they reach the cluster); the executor runs them on
its worker threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, assert_never

from vs_sandbox.slurm_wiring import make_evaluation_ledger
from vs_slurm.api import (
    BATCH_RESULT_LAYOUT,
    AttachJob,
    ClaimAttempt,
    ClusterConflict,
    ClusterObservation,
    ClusterRejected,
    ClusterSubmitted,
    ClusterUnknown,
    LedgerError,
    LedgerLabels,
    LedgerLease,
    LedgerRecord,
    LedgerState,
    ResolveSubmission,
    SlurmBatchHandle,
    SlurmSubmissionRejectedError,
    evaluation_key,
    handle_of_job,
    job_of_handle,
    ledger_operation_id,
    submit_step,
    sweep_unleased,
    validate_holder,
    validate_project,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import Clock, Processes
    from vs_slurm.api import (
        Cluster,
        EvaluationLedger,
        LedgerPolicy,
        SlurmBatchRequest,
        SlurmConfig,
        WithdrawOutcome,
    )

# A request is re-read at most this many times per attach call: each lost claim
# race costs one read, and a race is lost to a strictly newer attempt.
_ATTACH_ROUNDS = 8


@dataclass(frozen=True, slots=True)
class Attached:
    """The request's job, with its artifacts landing in this run's workspace."""

    key: str
    operation_id: str
    handle: SlurmBatchHandle


@dataclass(frozen=True, slots=True)
class AttachPending:
    """Another process is submitting the request's job right now; ask again after a pause."""

    key: str
    reason: str


AttachOutcome = Attached | AttachPending


class LedgerWithdrawnError(RuntimeError):
    """The request was withdrawn (by an operator, or for want of a lease) while attached."""

    @classmethod
    def of(cls, key: str, reason: str | None) -> LedgerWithdrawnError:
        """Name the request and why it was withdrawn."""
        return cls(f"evaluation ledger request {key[:12]} was withdrawn: {reason or 'no reason'}")


class LedgerAttachError(RuntimeError):
    """The ledger names a job this build cannot collect."""

    @classmethod
    def unknown_layout(cls, key: str, layout: int) -> LedgerAttachError:
        """Name a job whose result files a newer build wrote."""
        return cls(
            f"evaluation ledger request {key[:12]} has result layout {layout}; "
            f"this build reads layout {BATCH_RESULT_LAYOUT}"
        )


@dataclass
class SlurmLedgerAttachment:
    """One run's attachment to its project's ledger, shared by all its Slurm executors.

    ``holder`` names the run in every lease it writes. ``clock`` is the wall
    clock that leases are compared against across processes.
    """

    ledger: EvaluationLedger
    config: SlurmConfig
    project: str
    holder: str
    policy: LedgerPolicy
    clock: Clock
    _renewed: dict[str, float] = field(default_factory=dict)
    _swept_at: float | None = None

    def __post_init__(self) -> None:
        """Reject a project or holder that cannot name a ledger path."""
        validate_project(self.project)
        validate_holder(self.holder)

    def key(self, request: SlurmBatchRequest) -> str:
        """The request's idempotency key, over the setup script's content on the cluster too."""
        setup = request.setup_script
        return evaluation_key(
            self.project,
            request,
            self.config,
            setup_script_sha256=None if setup is None else self.ledger.operator_file_sha256(setup),
        )

    def attach(
        self,
        cluster: Cluster,
        request: SlurmBatchRequest,
        *,
        labels: LedgerLabels,
    ) -> AttachOutcome:
        """Attach to the request's job, submitting it only when the ledger holds none."""
        key = self.key(request)
        for _ in range(_ATTACH_ROUNDS):
            step = submit_step(self.ledger.read(key))
            match step:
                case AttachJob(record=record):
                    return self._attached(record, request)
                case ResolveSubmission(record=record):
                    return self._resolve(cluster, request, record)
                case ClaimAttempt(attempt=attempt):
                    claimed = LedgerRecord(
                        key=key,
                        project=self.project,
                        attempt=attempt,
                        operation_id=ledger_operation_id(key, attempt),
                        owner=self.holder,
                        created_at=self.clock.now(),
                        labels=labels,
                        state=LedgerState.SUBMITTING,
                    )
                    stored = self.ledger.claim(claimed)
                    if stored == claimed:
                        self.renew(key)
                        return self._submit(cluster, request, claimed)
                    # Another process claimed this attempt first: read what it recorded.
                case _:
                    assert_never(step)
        return AttachPending(key, "the request's attempts kept changing")

    def renew(self, key: str) -> None:
        """Write this run's lease on ``key``, valid for one lease period from now."""
        now = self.clock.now()
        self.ledger.renew(
            key,
            LedgerLease(
                holder=self.holder, renewed_at=now, expires_at=now + self.policy.lease_seconds
            ),
        )
        self._renewed[key] = now

    def release(self, key: str) -> None:
        """Drop this run's lease: the job lives on for the grace period, or for other holders."""
        self.ledger.release(key, self.holder)
        self._renewed.pop(key, None)

    def heartbeat(self, cluster: Cluster, key: str, operation_id: str) -> None:
        """Keep an attached request leased, and sweep the project when a sweep is due.

        Every third of a lease period it reads the request: if the attempt this run
        attached to (``operation_id``) is no longer the current one, or is
        withdrawn, it raises ``LedgerWithdrawnError``; otherwise it renews the lease.
        """
        now = self.clock.now()
        last = self._renewed.get(key)
        if last is None or now - last >= self.policy.lease_seconds / 3:
            entry = self.ledger.read(key)
            if entry is None:
                raise LedgerWithdrawnError.of(key, "the request left the ledger")
            record = entry.record
            if record.operation_id != operation_id:
                raise LedgerWithdrawnError.of(key, "a newer attempt replaced it")
            if record.state is LedgerState.WITHDRAWN:
                raise LedgerWithdrawnError.of(key, record.withdrawn_reason)
            self.renew(key)
        self.sweep_if_due(cluster)

    def sweep_if_due(self, cluster: Cluster) -> tuple[WithdrawOutcome, ...]:
        """Withdraw unleased requests, at most once per lease period per run."""
        now = self.clock.now()
        if self._swept_at is not None and now - self._swept_at < self.policy.lease_seconds:
            return ()
        self._swept_at = now
        return sweep_unleased(
            self.ledger, cluster, now=now, grace_seconds=self.policy.grace_seconds
        )

    def _attached(self, record: LedgerRecord, request: SlurmBatchRequest) -> Attached:
        job = record.job
        if job is None:  # a submitted record always names its job (LedgerRecord validates it)
            raise LedgerError.unreadable(record.key, "submitted record without a job")
        if job.result_layout != BATCH_RESULT_LAYOUT:
            raise LedgerAttachError.unknown_layout(record.key, job.result_layout)
        self.renew(record.key)
        handle = handle_of_job(job, operation_id=record.operation_id, request=request)
        return Attached(record.key, record.operation_id, handle)

    def _resolve(
        self, cluster: Cluster, request: SlurmBatchRequest, record: LedgerRecord
    ) -> AttachOutcome:
        """Find a claimed attempt's job by its name before submitting it."""
        observed = cluster.inspect(record.operation_id)
        if isinstance(observed, ClusterObservation) and isinstance(
            observed.handle, SlurmBatchHandle
        ):
            return self._record_job(record, request, observed.handle)
        return self._submit(cluster, request, record)

    def _submit(
        self, cluster: Cluster, request: SlurmBatchRequest, record: LedgerRecord
    ) -> AttachOutcome:
        submitted = cluster.submit(request, operation_id=record.operation_id)
        match submitted:
            case ClusterSubmitted(handle=SlurmBatchHandle() as handle):
                return self._record_job(record, request, handle)
            case ClusterSubmitted():
                return AttachPending(record.key, "the submission returned no batch handle")
            case ClusterRejected(reason=reason):
                # Definitely not submitted: withdraw the attempt so the next ask claims a new one.
                self.ledger.update(
                    record.model_copy(
                        update={
                            "state": LedgerState.WITHDRAWN,
                            "withdrawn_reason": f"rejected: {reason}",
                        }
                    )
                )
                raise SlurmSubmissionRejectedError(reason)
            case ClusterConflict(reason=reason) | ClusterUnknown(reason=reason):
                # Another process is submitting this attempt from another workspace, or
                # the answer was lost: the job is found by name on the next ask.
                return AttachPending(record.key, reason)
            case _:
                assert_never(submitted)

    def _record_job(
        self, record: LedgerRecord, request: SlurmBatchRequest, handle: SlurmBatchHandle
    ) -> Attached:
        job = job_of_handle(handle)
        self.ledger.update(
            record.model_copy(
                update={
                    "state": LedgerState.SUBMITTED,
                    "job": job,
                }
            )
        )
        self.renew(record.key)
        # The handle may be another process's (found by name after its crash): its
        # artifacts would land in that process's workspace. Rebase onto this request.
        rebased = handle_of_job(job, operation_id=record.operation_id, request=request)
        return Attached(record.key, record.operation_id, rebased)


def open_ledger_attachment(  # noqa: PLR0913  # lint-waiver: LW-275701 [PLR0913]; each argument is an independent operator setting, identity or injected port of the one ledger connection; a carrier type would only rename this signature.
    config: SlurmConfig,
    policy: LedgerPolicy,
    *,
    project: str,
    holder: str,
    clock: Clock,
    processes: Processes,
    state_root: Path,
) -> SlurmLedgerAttachment:
    """Attach run ``holder`` to the ledger of ``project`` on the configured cluster."""
    ledger = make_evaluation_ledger(
        config, project=project, state_root=state_root, processes=processes
    )
    return SlurmLedgerAttachment(
        ledger=ledger, config=config, project=project, holder=holder, policy=policy, clock=clock
    )


__all__ = [
    "AttachOutcome",
    "AttachPending",
    "Attached",
    "LedgerAttachError",
    "LedgerWithdrawnError",
    "SlurmLedgerAttachment",
    "open_ledger_attachment",
]
