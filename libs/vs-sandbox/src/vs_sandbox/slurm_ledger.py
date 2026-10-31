"""A run's attachment to its project's evaluation ledger (#2757, Phase 1).

With an attachment, the Slurm executor submits through the ledger: a request
whose key already has a job attaches to it, a finished one is collected from
the cluster's disk, and only a request the ledger does not hold is submitted.
The run renews a lease on each request it attaches to. A run that stops, or
cancels an evaluation, withdraws each request no other run wants, cancelling its
job (``relinquish``), unless the policy keeps jobs on a stop
(``keep_jobs_on_stop``). A crash cancels nothing; a request no run leases for
the grace period is withdrawn by the sweep, which any attached run performs.

A run leases a request before it reads the request's record to attach to
it, so a withdrawal that marked the attempt before that read sees the lease
when it reads the request again, and undoes its mark; one that marks it after
sees the attempt attached to only through that lease, too
(``vs_slurm.api.withdraw``). Within one run, ``guard`` orders an evaluation's
attach after its sibling's withdrawal of the same request, and the other way.

The calls here are blocking (they reach the cluster); the executor runs them on
its worker threads, and holds ``guard`` around them on its event loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, assert_never

from vs_sandbox.slurm_wiring import make_evaluation_ledger
from vs_slurm.api import (
    BATCH_RESULT_LAYOUT,
    AttachJob,
    AwaitWithdrawal,
    CheckJob,
    ClaimAttempt,
    ClusterConflict,
    ClusterObservation,
    ClusterRejected,
    ClusterSubmitted,
    ClusterUnknown,
    JobMayRun,
    LedgerEntry,
    LedgerError,
    LedgerLabels,
    LedgerLease,
    LedgerRecord,
    LedgerState,
    OrphanChecks,
    ResolveSubmission,
    SlurmBatchHandle,
    SlurmSubmissionRejectedError,
    WithdrawalTiming,
    Withdrawer,
    evaluation_key,
    handle_of_job,
    job_of_handle,
    ledger_operation_id,
    relinquish,
    settle,
    settlement,
    submit_step,
    sweep_unleased,
    sweep_withdrawer,
    take_over_jobless,
    transitioned,
    undo_expired_withdrawal,
    validate_holder,
    validate_project,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import Clock, Processes
    from vs_slurm.api import (
        Cluster,
        ClusterCollectOutcome,
        EvaluationLedger,
        LedgerPolicy,
        SlurmBatchRequest,
        SlurmConfig,
        SlurmJobStatus,
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


_SWEEP = "sweep"
"""The key of ``SlurmLedgerAttachment._sweeping``."""


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
    # Holds the token of the sweep under way, if any (``sweep_if_due``).
    _sweeping: dict[str, object] = field(default_factory=dict, repr=False)
    # Which of this run's evaluations use each request, and the request of each.
    _users: dict[str, set[str]] = field(default_factory=dict)
    _used: dict[str, str] = field(default_factory=dict)
    # The attempts this process claimed, and when it claimed and first submitted each.
    _claimed_here: dict[str, float] = field(default_factory=dict)
    _guards: dict[str, asyncio.Lock] = field(default_factory=dict)
    _orphans: OrphanChecks = field(init=False)
    _timing: WithdrawalTiming = field(init=False)
    # One withdrawer, and so one writer id, for all this process's relinquishes, and
    # one for all its sweeps: one tried again knows a mark an earlier try left as its own.
    _withdrawer: Withdrawer = field(init=False)
    _sweeper: Withdrawer = field(init=False)

    def __post_init__(self) -> None:
        """Reject a project or holder that cannot name a ledger path."""
        validate_project(self.project)
        validate_holder(self.holder)
        self._orphans = OrphanChecks(window_seconds=self.policy.claim_seconds)
        self._timing = WithdrawalTiming(
            clock=self.clock, call_seconds=self.config.transport_timeout_seconds
        )
        self._withdrawer = Withdrawer(
            by=self.holder,
            reason=f"its run {self.holder} stopped wanting it",
            timing=self._timing,
        )
        self._sweeper = sweep_withdrawer(self._timing, grace_seconds=self.policy.grace_seconds)

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
        key: str | None = None,
    ) -> AttachOutcome:
        """Attach to the request's job, submitting it only when the ledger holds no usable one.

        ``key`` is the request's key when the caller computed it already. The
        call blocks while it stages and submits a job; meanwhile the caller
        keeps this run's claim alive with ``renew`` every third of a lease
        period, so no other process takes the claim over (``claim_expired``).

        Raises ``LedgerWithdrawnError`` when an operator withdrew the attempt this
        call was submitting, and ``SlurmSubmissionRejectedError`` when the cluster
        refused it.
        """
        key = key if key is not None else self.key(request)
        # Leased before any read: a withdrawal that misses this lease marked the
        # attempt before the read below, which then waits for it.
        self.renew(key)
        for _ in range(_ATTACH_ROUNDS):
            step = submit_step(self.ledger.read(key), now=self.clock.now())
            outcome: AttachOutcome | None
            match step:
                case AttachJob(record=record):
                    outcome = self._attached(record, request)
                case CheckJob(entry=current):
                    outcome = self._check(cluster, current, request)
                case ResolveSubmission(entry=current, expired=expired):
                    lapsed = self._claim_lapsed(current.record, expired=expired)
                    outcome = self._resolve(cluster, current, request, expired=lapsed)
                case AwaitWithdrawal(entry=current, expired=expired):
                    outcome = self._await_withdrawal(current, expired=expired)
                case ClaimAttempt(attempt=attempt):
                    outcome = self._claim(cluster, request, key, attempt, labels)
                case _:
                    assert_never(step)
            if outcome is not None:
                return outcome
            # The attempt moved on (settled, taken over, or claimed by another): read again.
        return AttachPending(key, "the request's attempts kept changing")

    def bind(self, handle: str, key: str) -> None:
        """Record that this run's evaluation ``handle`` asks for request ``key``."""
        self.ledger.bind_handle(self.holder, handle, key)

    def entry_of(self, handle: str) -> LedgerEntry | None:
        """The request this run's evaluation ``handle`` asked for, None if it never asked.

        Found through the handle alone, so a process that lost the content the request's
        key hashes still reaches its job.
        """
        key = self.ledger.bound_key(self.holder, handle)
        return None if key is None else self.ledger.read(key)

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

    def guard(self, key: str) -> asyncio.Lock:
        """The lock this run holds around each attach to, and each withdrawal of, ``key``.

        Without it, one evaluation's withdrawal could read the leases before a
        sibling evaluation of the same content leases the request, and cancel
        the job the sibling then attaches to: the run's single lease cannot tell
        the two apart.
        """
        return self._guards.setdefault(key, asyncio.Lock())

    def use(self, key: str, user: str) -> None:
        """Note that this run's evaluation ``user`` uses the request ``key``.

        An evaluation attaches to it, or resumes one attached to it; every one
        that uses it stops using it however it ends (``stop_using``).
        """
        before = self._used.get(user)
        if before is not None and before != key:
            self.stop_using(user)
        self._used[user] = key
        self._users.setdefault(key, set()).add(user)

    def request_of(self, user: str) -> str | None:
        """The request this run's evaluation ``user`` uses, if it uses one."""
        return self._used.get(user)

    def last_user(self, user: str, key: str | None = None) -> str | None:
        """The request ``user`` uses, if no other evaluation of this run uses it; nothing changes.

        ``key`` names the request when ``use`` was not called for it in this
        process. Two evaluations of one run with the same content share a
        request and its job: only the last to stop using it may relinquish it,
        and it stops using it (``stop_using``) only once the relinquish ended
        either way, so one whose outcome is unknown is tried again.
        """
        key = self._used.get(user, key)
        if key is None or self._users.get(key, set()) - {user}:
            return None
        return key

    def stop_using(self, user: str) -> None:
        """Forget that ``user`` uses its request."""
        key = self._used.pop(user, None)
        if key is None:
            return
        users = self._users.get(key, set())
        users.discard(user)
        if not users:
            self._users.pop(key, None)

    def relinquish(self, cluster: Cluster, key: str) -> WithdrawOutcome:
        """Stop wanting ``key``: drop this run's lease, and withdraw it if no other run wants it.

        The request's job is cancelled unless another run has leased the request
        within the grace period, or the job has already ended (``relinquish``).
        """
        self._renewed.pop(key, None)
        now = self.clock.now()
        return relinquish(
            self.ledger,
            cluster,
            key,
            self._withdrawer,
            since=now - self.policy.grace_seconds,
        )

    def heartbeat(self, cluster: Cluster, key: str, operation_id: str) -> None:
        """Keep an attached request leased, and sweep the project when a sweep is due.

        Every third of a lease period it reads the request: if the attempt this run
        attached to (``operation_id``) is no longer the current one, or is
        withdrawn, it raises ``LedgerWithdrawnError``; otherwise it renews the
        lease, which also sends back a withdrawal under way on the attempt.
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
                raise LedgerWithdrawnError.of(key, record.reason)
            self.renew(key)
        self.sweep_if_due(cluster)

    def settle(
        self,
        key: str,
        operation_id: str,
        status: SlurmJobStatus,
        collected: ClusterCollectOutcome | None,
    ) -> None:
        """Record how the attached job ended, so later asks need not ask the cluster.

        ``collected`` is what this run collected from it (None when it collected
        nothing): only a collected result in which every stage left its own
        makes the attempt reusable (``settlement``). Best effort: if another
        process moved the attempt on first, its record stands.
        """
        for _ in range(_ATTACH_ROUNDS):
            entry = self.ledger.read(key)
            if entry is None or entry.record.operation_id != operation_id:
                return
            if entry.record.state is not LedgerState.SUBMITTED or settle(
                self.ledger, entry, status, collected
            ):
                return
            job = entry.record.job
            if job is None or settlement(status, collected, job=job) is None:
                return  # nothing to record yet: not a lost swap, so do not read again

    def sweep_if_due(self, cluster: Cluster) -> tuple[WithdrawOutcome, ...]:
        """Withdraw unleased requests, at most once per lease period per run, one sweep at a time.

        All this process's sweeps share one writer id (``sweep_withdrawer``),
        so a second sweep running beside the first would take the first's mark
        for its own and could undo it while the first is about to cancel. A
        sweep that finds another under way, on any thread or within it,
        returns at once: the one under way covers the project.

        The executor calls in from its worker threads. A sweep claims its turn
        with one ``dict.setdefault`` on a string key, which is atomic (with the
        GIL, and under free-threading's per-object locking, it runs no Python
        code), so no lock, and no ``Threads`` to make one, is needed.
        """
        token = object()
        if self._sweeping.setdefault(_SWEEP, token) is not token:
            return ()
        try:
            now = self.clock.now()
            if self._swept_at is not None and now - self._swept_at < self.policy.lease_seconds:
                return ()
            self._swept_at = now
            return sweep_unleased(
                self.ledger,
                cluster,
                sweeper=self._sweeper,
                grace_seconds=self.policy.grace_seconds,
                orphans=self._orphans,
            )
        finally:
            del self._sweeping[_SWEEP]

    def _attached(self, record: LedgerRecord, request: SlurmBatchRequest) -> Attached:
        job = record.job
        if job is None:  # a submitted record always names its job (LedgerRecord validates it)
            raise LedgerError.unreadable(record.key, "submitted record without a job")
        if job.result_layout != BATCH_RESULT_LAYOUT:
            raise LedgerAttachError.unknown_layout(record.key, job.result_layout)
        handle = handle_of_job(job, operation_id=record.operation_id, request=request)
        return Attached(record.key, record.operation_id, handle)

    def _check(
        self, cluster: Cluster, entry: LedgerEntry, request: SlurmBatchRequest
    ) -> Attached | None:
        """Attach to a submitted job unless it ended unsuccessfully, which settles it first.

        A job that COMPLETED is attached to like a live one: this run collects it,
        and only then does its attempt settle (``SlurmLedgerAttachment.settle``).
        """
        observed = cluster.inspect(entry.record.operation_id)
        job = entry.record.job
        if (
            job is not None
            and isinstance(observed, ClusterObservation)
            and settlement(observed.status, None, job=job) is not None
        ):
            settle(self.ledger, entry, observed.status)
            return None  # read again: the failed attempt is replaced
        return self._attached(entry.record, request)

    def _await_withdrawal(self, entry: LedgerEntry, *, expired: bool) -> AttachPending | None:
        """Wait on a withdrawal under way; undo one whose withdrawer died (``AwaitWithdrawal``)."""
        record = entry.record
        if not expired:
            by = entry.withdrawal.by if entry.withdrawal is not None else "another process"
            return AttachPending(record.key, f"{by} is withdrawing the request")
        undo_expired_withdrawal(self.ledger, entry, now=self.clock.now())
        return None  # read again: the attempt as it was, or what another process made of it

    def _claim_lapsed(self, record: LedgerRecord, *, expired: bool) -> bool:
        """Whether the claim of a submitting attempt may be taken over (``ResolveSubmission``).

        Another run's lapses with its renewals (``expired``). This run's lease
        renews its own claims, so its own lapses by when it was submitted: one
        an earlier process of this run made is a dead process's at once, and
        one this process made lapses a claim window after its first submission,
        which this process submits again meanwhile (``_resolve``).
        """
        if record.owner != self.holder:
            return expired
        submitted_at = self._claimed_here.get(record.operation_id)
        if submitted_at is None:
            return True
        return self.clock.now() > submitted_at + self.policy.claim_seconds

    def _resolve(
        self,
        cluster: Cluster,
        entry: LedgerEntry,
        request: SlurmBatchRequest,
        *,
        expired: bool,
    ) -> AttachOutcome | None:
        """Find a claimed attempt's job by its name; take over the claim once it expired."""
        record = entry.record
        observed = cluster.inspect(record.operation_id)
        if isinstance(observed, ClusterObservation) and isinstance(
            observed.handle, SlurmBatchHandle
        ):
            return self._record_job(cluster, entry, request, observed.handle)
        if not expired and record.operation_id in self._claimed_here:
            # This process's own submission, whose answer was lost: submit it again.
            # A submit is once per operation id (``Cluster.submit``), so this finds
            # the job the lost one started, or starts the only one.
            return self._submit(cluster, request, entry)
        if not expired:
            return AttachPending(record.key, "another process is submitting the request's job")
        if record.owner == self.holder:
            return self._take_over_own(cluster, entry)
        # The submitter stopped renewing its claim: it died, or hangs. The attempt
        # fails only if no job runs under its name, first, by compare-and-swap, so a
        # submitter that recorded its job meanwhile wins (``take_over_jobless``).
        failed = transitioned(
            record,
            LedgerState.FAILED,
            reason="its submitter's claim expired before it recorded a job",
        )
        taken = take_over_jobless(self.ledger, cluster, entry, observed, failed)
        if isinstance(taken, JobMayRun):
            return AttachPending(record.key, f"the lapsed claim's job may run: {taken.reason}")
        return None  # read again: a new attempt, or the job the submitter recorded

    def _take_over_own(self, cluster: Cluster, entry: LedgerEntry) -> AttachPending | None:
        """Fail this run's own lapsed claim, once its job is surely cancelled.

        No submitter of this run is still on its way (an earlier process died;
        this one submits each attempt once, under ``guard``), so the job, if
        the lost submission started one, is cancelled first, and only a
        definite cancel lets the attempt fail: failing it with the job's fate
        unknown would let the next attempt start a second live job.
        """
        record = entry.record
        cancelled = cluster.cancel(record.operation_id)
        if isinstance(cancelled, ClusterUnknown):
            return AttachPending(record.key, f"the lapsed claim's job may run: {cancelled.reason}")
        failed = transitioned(
            record, LedgerState.FAILED, reason="its submission's outcome stayed unknown"
        )
        if self.ledger.replace(entry, failed):
            self._claimed_here.pop(record.operation_id, None)
        return None  # read again: a new attempt, or what another process recorded

    def _claim(
        self,
        cluster: Cluster,
        request: SlurmBatchRequest,
        key: str,
        attempt: int,
        labels: LedgerLabels,
    ) -> AttachOutcome | None:
        now = self.clock.now()
        claimed = LedgerRecord(
            key=key,
            project=self.project,
            attempt=attempt,
            operation_id=ledger_operation_id(key, attempt),
            owner=self.holder,
            created_at=now,
            claim_expires_at=now + self.policy.claim_seconds,
            labels=labels,
            state=LedgerState.SUBMITTING,
        )
        if self.ledger.claim(claimed) != claimed:
            return None  # another process claimed this attempt first: read what it recorded
        self._claimed_here[claimed.operation_id] = now
        self.renew(key)
        return self._submit(cluster, request, LedgerEntry(claimed))

    def _submit(
        self, cluster: Cluster, request: SlurmBatchRequest, entry: LedgerEntry
    ) -> AttachOutcome | None:
        record = entry.record
        submitted = cluster.submit(request, operation_id=record.operation_id)
        match submitted:
            case ClusterSubmitted(handle=SlurmBatchHandle() as handle):
                return self._record_job(cluster, entry, request, handle)
            case ClusterSubmitted():
                return AttachPending(record.key, "the submission returned no batch handle")
            case ClusterRejected(reason=reason):
                # Definitely not submitted: fail the attempt so the next ask claims a new one.
                self.ledger.replace(
                    entry, transitioned(record, LedgerState.FAILED, reason=f"rejected: {reason}")
                )
                raise SlurmSubmissionRejectedError(reason)
            case ClusterConflict(reason=reason) | ClusterUnknown(reason=reason):
                # The answer was lost, or another process submits this attempt: the
                # job is found by name on the next ask.
                return AttachPending(record.key, reason)
            case _:
                assert_never(submitted)

    def _record_job(
        self,
        cluster: Cluster,
        entry: LedgerEntry,
        request: SlurmBatchRequest,
        handle: SlurmBatchHandle,
    ) -> AttachOutcome | None:
        """Name the job in the attempt, by compare-and-swap; a lost swap is read again.

        If the attempt was withdrawn or taken over meanwhile, this job must not run:
        it is cancelled, and a withdrawal is reported to the caller.
        """
        record = entry.record
        job = job_of_handle(handle)
        if self.ledger.replace(entry, transitioned(record, LedgerState.SUBMITTED, job=job)):
            self.renew(record.key)
            # The handle may be another process's (found by name after its crash): its
            # artifacts would land in that process's workspace. Build this request's.
            rebased = handle_of_job(job, operation_id=record.operation_id, request=request)
            return Attached(record.key, record.operation_id, rebased)
        current = self.ledger.read(record.key)
        if current is None or current.record.operation_id != record.operation_id:
            cluster.cancel(record.operation_id)
            return None
        if current.record.state in {LedgerState.FAILED, LedgerState.WITHDRAWN}:
            cluster.cancel(record.operation_id)
            if current.record.state is LedgerState.WITHDRAWN:
                raise LedgerWithdrawnError.of(record.key, current.record.reason)
        return None


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
        config, project=project, state_root=state_root, processes=processes, clock=clock
    )
    return SlurmLedgerAttachment(
        ledger=ledger,
        config=config,
        project=project,
        holder=holder,
        policy=policy,
        clock=clock,
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
