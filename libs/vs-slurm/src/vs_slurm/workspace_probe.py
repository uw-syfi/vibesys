"""One-shot lifecycle proof for a submit-node workspace root."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vs_decode.api import Unreadable, ValidationOptions, decode_json
from vs_sim.api import ProcessTimeoutError

from .cluster_store import WorkspaceProbeStore
from .config import SlurmConfig, SlurmJobResources, minimal_job
from .errors import (
    SlurmError,
    SlurmSbatchFailedError,
    SlurmSubmissionRejectedError,
    SlurmWorkspaceRootProbeError,
)
from .remote_operations import RemoteOperationError
from .runner import (
    SchedulerReading,
    SlurmFileArtifact,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmJobWaitResult,
)
from .scheduler_states import EndOutcome
from .staging import tree_content_identity

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_sim.api import Event

    from .remote_operations import RemoteOperationEvidence

_RECORD = "workspace-root-probe"
_SUBMIT_MARKER_FILE = ".vibesys-submit-token"
_NODE_MARKER_FILE = ".vibesys-node-token"
_MAX_STATE_BYTES = 64 * 1024
# The record version written before retry generations; still read.
_LEGACY_RECORD_VERSION = 2
# How long the probe waits in the queue when ``queue_timeout_seconds`` is unset.
_DEFAULT_QUEUE_TIMEOUT_SECONDS = 1800
# Once started, the probe gets its time limit plus this long to be seen ending.
_RUN_MARGIN_SECONDS = 120
# How many of a run's attempts may end without a verdict on the root (preempted,
# cancelled, a lost node, overran) before the probe gives up for good. Counted across
# resumes in the durable record, so a probe that can never run cannot retry forever.
_INCONCLUSIVE_ATTEMPTS = 5
# The endings of a job whose script ran to an exit: its exit code and node token are a
# verdict on the root. Every other ending says nothing about it.
_VERDICT_ENDS = frozenset({EndOutcome.OK, EndOutcome.NONZERO_EXIT, EndOutcome.FAILED})
# While the probe is queued, the operator hears of it at once, then this often...
_QUEUED_NOTICE_INTERVAL_SECONDS = 300.0
# ...or sooner when the scheduler's reason changes, but never more often than this.
_QUEUED_NOTICE_SPACING_SECONDS = 60.0
_BOUNDARY_FAILURES = (
    OSError,
    SlurmError,
    RemoteOperationError,
    ValidationError,
    UnicodeError,
    ProcessTimeoutError,
)


# How an attempt ended without a verdict: the scheduler's own ending, a state this version
# cannot read, a job still running past the probe's limit, or a wait that lost the scheduler.
type _JobEnd = EndOutcome | Literal["unknown", "still_running", "unobserved"]


class _ProbeState(BaseModel):
    """Durable intent and outcome for one run-scoped startup probe."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # Version 3 added ``generation`` and the "unscheduled" outcome; version 4 the
    # inconclusive attempts. Versions 2 and 3 are read.
    version: Literal[2, 3, 4] = 4
    # Which attempt of this run's probe the record is about. A resume after a probe
    # the scheduler never started retries under the next generation's operation id.
    generation: int = Field(default=0, ge=0)
    operation_id: str
    workspace_identity: str
    claim_identity: str
    phase: Literal[
        "prepared",
        "claiming",
        "claimed",
        "submitting",
        "rejecting",
        "accepted",
    ] = "prepared"
    handle: SlurmJobHandle | None = None
    outcome: Literal[
        "pending",
        "verified",
        "infrastructure",
        "job_failed",
        "node_missing",
        "node_wrong",
        "unscheduled",
        "stopped",
        "inconclusive",
        "exhausted",
    ] = "pending"
    exit_code: int | None = None
    # Present exactly for "unscheduled": how long the job waited in the queue, and the
    # scheduler's pending reason ("" when it gave none).
    waited_seconds: float | None = None
    pending_reason: str | None = None
    # How many of the run's attempts so far, this one included, ended without a verdict;
    # carried to the next generation. ``job_end`` is set exactly for "inconclusive" and
    # "exhausted": how this attempt's job ended.
    inconclusive_attempts: int = Field(default=0, ge=0)
    job_end: _JobEnd | None = None

    @model_validator(mode="after")
    def _consistent(self) -> _ProbeState:
        unscheduled = self.outcome == "unscheduled"
        if unscheduled != (self.waited_seconds is not None) or unscheduled != (
            self.pending_reason is not None
        ):
            message = "waited_seconds and pending_reason are set exactly when unscheduled"
            raise ValueError(message)
        if (self.outcome in _INCONCLUSIVE_OUTCOMES) != (self.job_end is not None):
            message = "job_end is set exactly when the attempt ended without a verdict"
            raise ValueError(message)
        retryable = self.outcome in _RETRYABLE_OUTCOMES
        if self.version == _LEGACY_RECORD_VERSION and (
            self.generation or retryable or self.inconclusive_attempts
        ):
            message = "a version 2 record has no generation and no retryable outcome"
            raise ValueError(message)
        return self


# Outcomes that leave nothing behind and say nothing about the root: the scheduler never
# started the job, the run stopped first, or the job ended without its script reaching an
# exit. A resume retries under the next generation.
_RETRYABLE_OUTCOMES = frozenset({"unscheduled", "stopped", "inconclusive"})
# Outcomes of an attempt whose job ended without a verdict; "exhausted" is the last allowed.
_INCONCLUSIVE_OUTCOMES = frozenset({"inconclusive", "exhausted"})


@dataclass(frozen=True, slots=True)
class WorkspaceRootProbeQueued:
    """The probe job still waits in the scheduler's queue.

    ``pending_reason`` is the scheduler's own reason code (``Priority``, ``Resources``),
    or None when it gave none. ``queue_timeout_seconds`` is how long the probe waits in
    the queue in all before it is cancelled and fails.
    """

    job_id: str
    pending_reason: str | None
    waited_seconds: float
    queue_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class WorkspaceRootProbeWatch:
    """How the run that started a probe watches it and stops it.

    ``stop`` ends the probe's wait at its next poll. ``queued`` hears that the job still
    waits in the queue: at once, then on a bounded cadence or when the reason changes.
    """

    stop: Event
    queued: Callable[[WorkspaceRootProbeQueued], None] | None = None


class _QueuedNotices:
    """Decide which pending readings the operator hears of: a bounded, deduplicated stream.

    The first reading is reported, then one every ``_QUEUED_NOTICE_INTERVAL_SECONDS``, and
    a changed reason as soon as ``_QUEUED_NOTICE_SPACING_SECONDS`` passed since the last
    notice. So at most one notice per spacing, and a reason that holds for a spacing is
    always reported.
    """

    def __init__(
        self, sink: Callable[[WorkspaceRootProbeQueued], None], queue_timeout_seconds: float
    ) -> None:
        self._sink = sink
        self._queue_timeout_seconds = queue_timeout_seconds
        self._last: tuple[str | None, float] | None = None

    def observe(self, job_id: str, reading: SchedulerReading, waited_seconds: float) -> None:
        reason = reading.reason or None
        if self._last is not None:
            last_reason, last_waited = self._last
            since = waited_seconds - last_waited
            due = (
                _QUEUED_NOTICE_SPACING_SECONDS
                if reason != last_reason
                else _QUEUED_NOTICE_INTERVAL_SECONDS
            )
            if since < due:
                return
        self._last = (reason, waited_seconds)
        self._sink(
            WorkspaceRootProbeQueued(
                job_id=job_id,
                pending_reason=reason,
                waited_seconds=waited_seconds,
                queue_timeout_seconds=self._queue_timeout_seconds,
            )
        )


class _ProbeIntent(BaseModel):
    """Scheduler-wide claim that makes one run's probe globally unique."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[2] = 2
    operation_id: str
    workspace_identity: str
    claim_identity: str


class _ProbeAcceptance(_ProbeIntent):
    """Scheduler identity published after acceptance or reconciliation."""

    job_id: str


def _operation_id(run_id: str, generation: int) -> str:
    """The run's probe operation id; generation 0 keeps the version 2 derivation."""
    key = run_id if generation == 0 else f"{run_id}\0retry-{generation}"
    return "workspace-root-probe-" + hashlib.sha256(key.encode()).hexdigest()[:32]


def _token(kind: str, operation_id: str) -> str:
    return hashlib.sha256(f"{kind}:{operation_id}".encode()).hexdigest()


def _claim_identity(state_root: Path, operation_id: str) -> str:
    """Distinguish this durable ledger from another host racing the same run."""
    identity = state_root.stat()
    return hashlib.sha256(
        f"{state_root.resolve()}\0{identity.st_dev}\0{identity.st_ino}\0{operation_id}".encode()
    ).hexdigest()


def _request(
    store: WorkspaceProbeStore, operation_id: str
) -> tuple[SlurmJobRequest, str, str, Path]:
    """Build the probe's reproducible payload and collection target."""
    submit_token = _token("submit", operation_id)
    try:
        workspace = store.prepare_workspace(_SUBMIT_MARKER_FILE, submit_token)
    except (OSError, ValueError) as error:
        raise SlurmWorkspaceRootProbeError.state_invalid() from error
    node_token = _token("node", operation_id)
    returned = store.artifact_path
    request = SlurmJobRequest(
        workspace=workspace,
        command=(
            "sh",
            "-c",
            f'test "$(cat {_SUBMIT_MARKER_FILE})" = "$1" '
            f"&& printf '%s\\n' \"$2\" > {_NODE_MARKER_FILE}",
            "vibesys-workspace-root-probe",
            submit_token,
            node_token,
        ),
        file_artifacts=(
            SlurmFileArtifact(
                remote_path=_NODE_MARKER_FILE,
                local_path=returned,
                collect_on_failure=True,
            ),
        ),
        # The probe only reads and writes two small files: holding it behind the
        # evaluation's GPU request would gate startup on GPU queue capacity.
        resources=SlurmJobResources.MINIMAL,
    )
    return request, tree_content_identity(workspace, excludes=(".git/",)), node_token, returned


def _read(store: WorkspaceProbeStore) -> _ProbeState | None:
    """The stored record, upgraded to the current version; identity unchecked."""
    raw = store.read(_RECORD)
    if raw is None:
        return None
    state = decode_json(
        raw,
        _ProbeState,
        source="workspace root probe state",
        max_bytes=_MAX_STATE_BYTES,
        options=ValidationOptions(strict=True),
    )
    if isinstance(state, Unreadable):
        raise SlurmWorkspaceRootProbeError.state_invalid()
    return state.model_copy(update={"version": 4})


def _save(store: WorkspaceProbeStore, state: _ProbeState) -> None:
    store.write(_RECORD, state.model_dump_json())


def _remote_handle(
    runner: SlurmJobRunner,
    request: SlurmJobRequest,
    intent: _ProbeIntent,
    evidence: RemoteOperationEvidence,
) -> SlurmJobHandle | None:
    """Validate an existing remote claim and recover its accepted job."""
    try:
        remote_intent = _ProbeIntent.model_validate(evidence.intent, strict=True)
        accepted_document = evidence.accepted
        accepted = (
            None
            if accepted_document is None
            else _ProbeAcceptance.model_validate(accepted_document, strict=True)
        )
    except (AttributeError, ValidationError) as error:
        raise SlurmWorkspaceRootProbeError.state_invalid() from error
    if remote_intent != intent:
        raise SlurmWorkspaceRootProbeError.state_invalid()
    job_id = None if accepted is None else accepted.job_id
    if accepted is not None and (
        accepted.operation_id != intent.operation_id
        or accepted.workspace_identity != intent.workspace_identity
    ):
        raise SlurmWorkspaceRootProbeError.state_invalid()
    if job_id is None:
        job_id = runner.find_operation(intent.operation_id)
    if job_id is None:
        return None
    handle = runner.recover_handle(request, operation_id=intent.operation_id, job_id=job_id)
    if not isinstance(handle, SlurmJobHandle):
        raise SlurmWorkspaceRootProbeError.state_invalid()
    return handle


def _publish_acceptance(
    runner: SlurmJobRunner,
    intent: _ProbeIntent,
    handle: SlurmJobHandle,
) -> None:
    runner.record_operation_acceptance(
        intent.operation_id,
        _ProbeAcceptance(
            operation_id=intent.operation_id,
            workspace_identity=intent.workspace_identity,
            claim_identity=intent.claim_identity,
            job_id=handle.job_id,
        ).model_dump_json(),
    )


def _cleanup_checked(runner: SlurmJobRunner, state: _ProbeState) -> None:
    """Remove and confirm the probe's exact remote material once."""
    runner.cleanup_operation(
        state.operation_id,
        content_identity=state.workspace_identity,
    )


def _cleanup(runner: SlurmJobRunner, state: _ProbeState) -> None:
    """Clean the probe, retrying once if caller cancellation interrupted cleanup."""
    try:
        _cleanup_checked(runner, state)
    except _BOUNDARY_FAILURES as error:
        raise SlurmWorkspaceRootProbeError.cleanup_unconfirmed() from error
    except BaseException:
        # Caller cancellation can interrupt the cleanup boundary after its
        # effect is unknown. Retry the idempotent exact-path cleanup before the
        # original cancellation propagates.
        try:
            _cleanup_checked(runner, state)
        except BaseException as error:
            # Failure or repeated cancellation during recovery means remote
            # cleanup is unconfirmed and must fail closed.
            raise SlurmWorkspaceRootProbeError.cleanup_unconfirmed() from error
        raise


def _settle_checked(
    runner: SlurmJobRunner,
    handle: SlurmJobHandle,
    pause: Callable[[float], None],
) -> None:
    status = runner.poll(handle)
    if status.terminal:
        return
    # Queued, running, or a state this version cannot read: cancel it (idempotent) and
    # confirm that it ended.
    runner.cancel(handle)
    observed = runner.wait(handle, pause=pause)
    if observed.timed_out or not observed.terminal:
        raise SlurmWorkspaceRootProbeError.settlement_unconfirmed()


def _settle(
    runner: SlurmJobRunner,
    handle: SlurmJobHandle,
    *,
    pause: Callable[[float], None],
) -> None:
    """Cancel an active probe and prove that it reached a terminal state."""
    try:
        _settle_checked(runner, handle, pause)
    except SlurmWorkspaceRootProbeError:
        raise
    except _BOUNDARY_FAILURES as error:
        raise SlurmWorkspaceRootProbeError.settlement_unconfirmed() from error
    except BaseException:  # noqa: BLE001  # lint-waiver: LW-209104 [BLE001]; settlement can be interrupted by any caller BaseException, including KeyboardInterrupt; catching Exception or enumerating cancellation classes would leave accepted jobs active on the omitted cancellation path.
        # Settlement runs while another failure or caller cancellation is
        # already propagating. A cancellation at scancel has unknown effect;
        # retry the idempotent observation/cancel sequence and, once terminal,
        # return so the original cause remains authoritative.
        try:
            _settle_checked(runner, handle, pause)
        except BaseException as error:
            raise SlurmWorkspaceRootProbeError.settlement_unconfirmed() from error


def _stored_failure(state: _ProbeState, config: SlurmConfig) -> SlurmWorkspaceRootProbeError:
    job_id = state.handle.job_id if state.handle is not None else "unknown"
    by_job = _FAILURES_BY_JOB.get(state.outcome)
    if by_job is not None:
        return by_job(job_id)
    match state.outcome:
        case "job_failed":
            return SlurmWorkspaceRootProbeError.job_failed(job_id, state.exit_code)
        case "infrastructure":
            return SlurmWorkspaceRootProbeError.infrastructure()
        case "inconclusive" | "exhausted" if state.job_end is not None:
            return SlurmWorkspaceRootProbeError.inconclusive(
                job_id,
                ended=state.job_end,
                attempt=state.inconclusive_attempts,
                attempts=_INCONCLUSIVE_ATTEMPTS
                if state.outcome == "inconclusive"
                else state.inconclusive_attempts,
            )
        case "unscheduled" if state.waited_seconds is not None:
            return SlurmWorkspaceRootProbeError.never_scheduled(
                job_id,
                waited_seconds=state.waited_seconds,
                reason=state.pending_reason,
                request=minimal_job(config).description,
                queue_timeout_seconds=config.queue_timeout_seconds,
            )
        case _:
            return SlurmWorkspaceRootProbeError.state_invalid()


# Recorded outcomes whose error names only the probe's job.
_FAILURES_BY_JOB: dict[str, Callable[[str], SlurmWorkspaceRootProbeError]] = {
    "node_missing": SlurmWorkspaceRootProbeError.node_token_missing,
    "node_wrong": SlurmWorkspaceRootProbeError.node_token_wrong,
    "stopped": SlurmWorkspaceRootProbeError.stopped,
}


def _record_failure(
    store: WorkspaceProbeStore,
    state: _ProbeState,
    outcome: Literal["infrastructure", "job_failed", "node_missing", "node_wrong"],
    *,
    exit_code: int | None = None,
) -> _ProbeState:
    failed = state.model_copy(update={"outcome": outcome, "exit_code": exit_code})
    _save(store, failed)
    return failed


def _job_end(observed: SlurmJobWaitResult) -> _JobEnd:
    """How a job that gave no verdict ended, as far as the wait saw it."""
    if observed.ended is not None:
        return observed.ended
    if observed.status == SlurmJobStatus.UNKNOWN:
        return "unknown"
    return "still_running"


def _with_cause(
    error: SlurmWorkspaceRootProbeError, cause: BaseException
) -> SlurmWorkspaceRootProbeError:
    error.__cause__ = cause
    return error


def _raise_unresolved(cause: BaseException | None = None) -> NoReturn:
    # sbatch's own diagnostic is safe to show and usually says why (bad partition, QoS).
    detail = str(cause) if isinstance(cause, SlurmSbatchFailedError) else None
    error = SlurmWorkspaceRootProbeError.submission_unresolved(detail)
    if cause is None:
        raise error
    raise error from cause


class _ProbeLifecycle:
    """The serialized durable shell around the probe's pure token checks."""

    def __init__(
        self,
        runner: SlurmJobRunner,
        state_root: Path,
        run_id: str,
        pause: Callable[[float], None],
        watch: WorkspaceRootProbeWatch | None,
    ) -> None:
        self._runner = runner
        self._run_id = run_id
        self._state_root = state_root
        self._store = WorkspaceProbeStore(state_root)
        self._pause = pause
        self._stop = None if watch is None else watch.stop
        self._queued = None if watch is None else watch.queued
        self._operation_id = _operation_id(run_id, 0)
        self._claim_identity = ""

    def run(self) -> None:
        """Serialize this run's preparation and retain its terminal receipt."""
        with self._store.lock():
            self._run_locked()

    def _run_locked(self) -> None:
        if self._stop is not None and self._stop.is_set():
            raise SlurmWorkspaceRootProbeError.stopped(None)
        stored = _read(self._store)
        generation = 0 if stored is None else stored.generation
        request, identity, node_token = self._bind(generation)
        if stored is not None and (
            stored.operation_id != self._operation_id
            or stored.workspace_identity != identity
            or stored.claim_identity != self._claim_identity
        ):
            raise SlurmWorkspaceRootProbeError.state_invalid()
        if stored is not None and stored.outcome in _RETRYABLE_OUTCOMES:
            # The scheduler never started that attempt, or the run stopped it, and it was
            # settled and cleaned when recorded; confirm the cleanup, then retry under a
            # fresh operation id so the old claim is never reused.
            _cleanup(self._runner, stored)
            generation += 1
            carried = stored.inconclusive_attempts
            stored = None
            request, identity, node_token = self._bind(generation)
        else:
            carried = 0
        state = stored or _ProbeState(
            generation=generation,
            operation_id=self._operation_id,
            workspace_identity=identity,
            claim_identity=self._claim_identity,
            inconclusive_attempts=carried,
        )
        if state.outcome != "pending":
            self._finish_stored(state)
            return
        state, handle = self._handle(request, state)
        self._verify(state, handle, node_token)

    def _bind(self, generation: int) -> tuple[SlurmJobRequest, str, str]:
        """Select ``generation``'s operation and prepare its payload."""
        self._operation_id = _operation_id(self._run_id, generation)
        self._claim_identity = _claim_identity(self._state_root, self._operation_id)
        request, identity, node_token, _returned = _request(self._store, self._operation_id)
        return request, identity, node_token

    def _finish_stored(self, state: _ProbeState) -> None:
        _cleanup(self._runner, state)
        if state.outcome != "verified":
            raise _stored_failure(state, self._runner.config)

    def _handle(
        self, request: SlurmJobRequest, state: _ProbeState
    ) -> tuple[_ProbeState, SlurmJobHandle]:
        if state.phase == "accepted" and state.handle is not None:
            return state, state.handle
        if state.handle is not None:
            raise SlurmWorkspaceRootProbeError.state_invalid()
        if state.phase == "prepared":
            return self._begin_claim(request, state)
        if state.phase == "claiming":
            return self._resume_claiming(request, state)
        if state.phase == "claimed":
            return self._begin_submit(request, state)
        if state.phase == "submitting":
            return self._resume_submitting(request, state)
        if state.phase == "rejecting":
            self._publish_rejection(state)
        raise SlurmWorkspaceRootProbeError.state_invalid()

    def _intent(self, state: _ProbeState) -> _ProbeIntent:
        return _ProbeIntent(
            operation_id=self._operation_id,
            workspace_identity=state.workspace_identity,
            claim_identity=state.claim_identity,
        )

    def _save_phase(
        self,
        state: _ProbeState,
        phase: Literal[
            "prepared",
            "claiming",
            "claimed",
            "submitting",
            "rejecting",
            "accepted",
        ],
        *,
        handle: SlurmJobHandle | None = None,
    ) -> _ProbeState:
        updated = state.model_copy(update={"phase": phase, "handle": handle})
        _save(self._store, updated)
        return updated

    def _reconcile_claim(
        self, request: SlurmJobRequest, state: _ProbeState
    ) -> tuple[_ProbeState, SlurmJobHandle | None]:
        evidence = self._runner.inspect_operation(self._operation_id)
        if evidence.intent is None:
            return self._save_phase(state, "prepared"), None
        handle = _remote_handle(self._runner, request, self._intent(state), evidence)
        if evidence.rejected is not None and handle is None:
            self._finish_rejected(state)
        if handle is not None:
            return self._accept_handle(state, handle)
        return self._save_phase(state, "claimed"), None

    def _resume_claiming(
        self, request: SlurmJobRequest, state: _ProbeState
    ) -> tuple[_ProbeState, SlurmJobHandle]:
        try:
            reconciled, handle = self._reconcile_claim(request, state)
        except _BOUNDARY_FAILURES as error:
            raise SlurmWorkspaceRootProbeError.infrastructure() from error
        if handle is not None:
            return reconciled, handle
        if reconciled.phase == "claimed":
            return self._begin_submit(request, reconciled)
        return self._begin_claim(request, reconciled)

    def _begin_claim(
        self, request: SlurmJobRequest, state: _ProbeState
    ) -> tuple[_ProbeState, SlurmJobHandle]:
        claiming = self._save_phase(state, "claiming")
        intent = self._intent(claiming)
        try:
            claim = self._runner.claim_operation(self._operation_id, intent.model_dump_json())
        except _BOUNDARY_FAILURES as error:
            return self._recover_failed_claim(request, claiming, error)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209101 [BLE001]; caller cancellation may cross the remote atomic claim, so its durable effect must be reconciled before cancellation propagates.
            self._recover_cancelled_claim(request, claiming, error)
        if claim.evidence.intent is None:
            self._save_phase(claiming, "prepared")
            raise SlurmWorkspaceRootProbeError.infrastructure()
        handle = _remote_handle(self._runner, request, intent, claim.evidence)
        if claim.evidence.rejected is not None and handle is None:
            self._finish_rejected(claiming)
        if handle is not None:
            return self._accept_handle(claiming, handle)
        claimed = self._save_phase(claiming, "claimed")
        return self._begin_submit(request, claimed)

    def _recover_failed_claim(
        self,
        request: SlurmJobRequest,
        state: _ProbeState,
        error: BaseException,
    ) -> tuple[_ProbeState, SlurmJobHandle]:
        try:
            reconciled, handle = self._reconcile_claim(request, state)
        except _BOUNDARY_FAILURES as recovery_error:
            raise SlurmWorkspaceRootProbeError.infrastructure() from recovery_error
        if handle is not None:
            return reconciled, handle
        if reconciled.phase == "claimed":
            return self._begin_submit(request, reconciled)
        raise SlurmWorkspaceRootProbeError.infrastructure() from error

    def _recover_cancelled_claim(
        self,
        request: SlurmJobRequest,
        state: _ProbeState,
        error: BaseException,
    ) -> NoReturn:
        try:
            reconciled, handle = self._reconcile_claim(request, state)
        except _BOUNDARY_FAILURES as recovery_error:
            raise SlurmWorkspaceRootProbeError.infrastructure() from recovery_error
        if handle is not None:
            self._abandon(reconciled, handle, error)
        raise error

    def _begin_submit(
        self, request: SlurmJobRequest, state: _ProbeState
    ) -> tuple[_ProbeState, SlurmJobHandle]:
        submitting = self._save_phase(state, "submitting")
        try:
            handle = self._runner.submit(request, operation_id=self._operation_id)
        except SlurmSubmissionRejectedError as error:
            self._reject_before_submission(submitting, error)
        except _BOUNDARY_FAILURES as error:
            return self._reconcile_submit_failure(request, submitting, error)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209103 [BLE001]; cancellation after sbatch begins has unknown acceptance and must be reconciled and settled before propagation.
            accepted, handle = self._reconcile_submit_failure(request, submitting, error)
            self._abandon(accepted, handle, error)
        return self._accept_handle(submitting, handle)

    def _resume_submitting(
        self, request: SlurmJobRequest, state: _ProbeState
    ) -> tuple[_ProbeState, SlurmJobHandle]:
        try:
            evidence = self._runner.inspect_operation(self._operation_id)
            handle = _remote_handle(self._runner, request, self._intent(state), evidence)
        except _BOUNDARY_FAILURES as error:
            _raise_unresolved(error)
        if evidence.rejected is not None and handle is None:
            self._finish_rejected(state)
        if handle is None:
            _raise_unresolved()
        return self._accept_handle(state, handle)

    def _accept_handle(
        self, state: _ProbeState, handle: SlurmJobHandle
    ) -> tuple[_ProbeState, SlurmJobHandle]:
        accepted = state.model_copy(update={"phase": "accepted", "handle": handle})
        try:
            _save(self._store, accepted)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209106 [BLE001]; after scheduler acceptance even a caller cancellation or local durable-write failure must settle the exact accepted job before it propagates.
            self._abandon(accepted, handle, error)
        self._publish_or_fail(self._intent(accepted), accepted, handle)
        return accepted, handle

    def _reject_before_submission(self, state: _ProbeState, error: SlurmError) -> NoReturn:
        rejecting = self._save_phase(state, "rejecting")
        self._publish_rejection(rejecting, cause=error)

    def _publish_rejection(
        self, state: _ProbeState, *, cause: BaseException | None = None
    ) -> NoReturn:
        """Publish a proven pre-submission rejection from a replayable phase."""
        try:
            self._runner.record_operation_rejection(self._operation_id, "probe rejected")
        except _BOUNDARY_FAILURES as rejection_error:
            _raise_unresolved(rejection_error)
        self._finish_rejected(state, cause=cause)

    def _finish_rejected(
        self, state: _ProbeState, *, cause: BaseException | None = None
    ) -> NoReturn:
        """Consume durable pre-acceptance rejection and leave a safe retry point."""
        retryable = self._save_phase(state, "prepared")
        _cleanup(self._runner, retryable)
        failure = SlurmWorkspaceRootProbeError.rejected(None if cause is None else str(cause))
        if cause is None:
            raise failure
        raise failure from cause

    def _reconcile_submit_failure(
        self,
        request: SlurmJobRequest,
        state: _ProbeState,
        error: BaseException,
    ) -> tuple[_ProbeState, SlurmJobHandle]:
        try:
            evidence = self._runner.inspect_operation(self._operation_id)
            handle = _remote_handle(self._runner, request, self._intent(state), evidence)
        except _BOUNDARY_FAILURES as recovery_error:
            _raise_unresolved(recovery_error)
        if handle is None and isinstance(error, SlurmSbatchFailedError):
            # sbatch ran, exited nonzero without printing a job id, and no job carries
            # the probe's name: the scheduler refused it. Publishing the rejection
            # leaves a retry point, so a resume submits again.
            self._reject_before_submission(state, error)
        if handle is None:
            _raise_unresolved(error)
        return self._accept_handle(state, handle)

    def _publish_or_fail(
        self, intent: _ProbeIntent, state: _ProbeState, handle: SlurmJobHandle
    ) -> None:
        try:
            _publish_acceptance(self._runner, intent, handle)
        except _BOUNDARY_FAILURES as error:
            self._abandon(state.model_copy(update={"handle": handle}), handle, error)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209105 [BLE001]; caller cancellation can cross accepted-evidence publication after scheduler acceptance; catching Exception or enumerating cancellation classes would leave the accepted job active on an omitted BaseException path.
            self._abandon(state.model_copy(update={"handle": handle}), handle, error)

    def _verify(
        self,
        state: _ProbeState,
        handle: SlurmJobHandle,
        node_token: str,
    ) -> None:
        config = self._runner.config
        queue_seconds = config.queue_timeout_seconds or _DEFAULT_QUEUE_TIMEOUT_SECONDS
        observed = self._wait(state, handle, queue_seconds=queue_seconds)
        if observed.timed_out and observed.status == SlurmJobStatus.PENDING:
            self._unless_started(state, handle, observed)
            observed = self._wait(state, handle, queue_seconds=None)
        if observed.timed_out or observed.ended not in _VERDICT_ENDS:
            # Preempted, cancelled, a lost node, overran, or a state this version cannot
            # read: the job's script never reached an exit, so nothing is known of the root.
            failed = self._inconclusive(state, _job_end(observed))
            self._abandon(state, handle, _stored_failure(failed, config), failed=failed)
        self._clear_artifact(state, handle)
        exit_code = self._collect(state, handle)
        if exit_code != 0:
            self._known_failure(state, handle, "job_failed", exit_code=exit_code)
        returned_token = self._returned_token(state, handle)
        if returned_token != node_token:
            self._known_failure(state, handle, "node_wrong")
        verified = state.model_copy(update={"outcome": "verified"})
        try:
            _save(self._store, verified)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209107 [BLE001]; verified remote work remains owned until its terminal receipt is durable or the operation is abandoned and cleaned.
            self._abandon(state, handle, error)
        _cleanup(self._runner, verified)

    def _wait(
        self, state: _ProbeState, handle: SlurmJobHandle, *, queue_seconds: float | None
    ) -> SlurmJobWaitResult:
        """Wait out the queue (when ``queue_seconds`` is given), then the short run.

        A result still ``PENDING`` and ``timed_out`` means the queue bound passed.
        """
        job = minimal_job(self._runner.config)
        run_limit = job.time_limit_seconds or self._runner.config.job_timeout_seconds
        try:
            if queue_seconds is not None:
                queued = self._runner.wait_until_started(
                    handle,
                    pause=self._pause,
                    timeout_seconds=queue_seconds,
                    cancel_event=self._stop,
                    queued=self._queued_observer(handle, queue_seconds),
                )
                if queued.timed_out:
                    return queued
            return self._runner.wait(
                handle,
                pause=self._pause,
                timeout_seconds=run_limit + _RUN_MARGIN_SECONDS,
                cancel_event=self._stop,
            )
        except _BOUNDARY_FAILURES as error:
            # A wait that lost the scheduler says nothing about the root either.
            failed = self._stopped(state) or self._inconclusive(state, "unobserved")
            self._abandon(state, handle, error, failed=failed)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209102 [BLE001]; a caller cancellation can interrupt a scheduler wait with any BaseException, but the accepted probe still must be settled and cleaned before propagation.
            self._abandon(state, handle, error, failed=self._stopped(state))

    def _queued_observer(
        self, handle: SlurmJobHandle, queue_seconds: float
    ) -> Callable[[SchedulerReading, float], None] | None:
        if self._queued is None:
            return None
        notices = _QueuedNotices(self._queued, queue_seconds)
        return lambda reading, waited: notices.observe(handle.job_id, reading, waited)

    def _stopped(self, state: _ProbeState) -> _ProbeState | None:
        """The record of a wait the run's stop ended, or None when no stop was asked."""
        if self._stop is None or not self._stop.is_set():
            return None
        return state.model_copy(update={"outcome": "stopped", "exit_code": None})

    @staticmethod
    def _inconclusive(state: _ProbeState, ended: _JobEnd) -> _ProbeState:
        """The record of an attempt that ended without a verdict; the bound's last is final."""
        attempts = state.inconclusive_attempts + 1
        return state.model_copy(
            update={
                "outcome": "exhausted" if attempts >= _INCONCLUSIVE_ATTEMPTS else "inconclusive",
                "exit_code": None,
                "inconclusive_attempts": attempts,
                "job_end": ended,
            }
        )

    def _unless_started(
        self, state: _ProbeState, handle: SlurmJobHandle, queued: SlurmJobWaitResult
    ) -> None:
        """Return if the job started after the last poll; else cancel it as never scheduled."""
        try:
            reading = self._runner.inspect_job(handle.job_id)
        except _BOUNDARY_FAILURES:
            reading = None  # the last poll's PENDING stands; only the reason is lost
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209115 [BLE001]; caller cancellation during the queue-reason read must still settle and clean the accepted probe; catching Exception or enumerating cancellation classes would leave it queued on an omitted BaseException path.
            # > Reading the reason after settlement loses it (a cancelled job has none);
            # > a finally block cannot tell a cancellation from success without the error.
            self._abandon(state, handle, error)
        if reading is not None and reading.status != SlurmJobStatus.PENDING:
            return
        failed = state.model_copy(
            update={
                "outcome": "unscheduled",
                "exit_code": None,
                "waited_seconds": queued.waited_seconds,
                "pending_reason": (reading.reason if reading is not None else None) or "",
            }
        )
        self._abandon(state, handle, _stored_failure(failed, self._runner.config), failed=failed)

    def _clear_artifact(self, state: _ProbeState, handle: SlurmJobHandle) -> None:
        try:
            self._store.clear_artifact()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209108 [BLE001]; the scheduler job is owned until its remote operation is cleaned, including local artifact-store failures and caller cancellation.
            self._abandon(state, handle, error)

    def _collect(self, state: _ProbeState, handle: SlurmJobHandle) -> int | None:
        try:
            result = self._runner.collect(handle)
        except _BOUNDARY_FAILURES as error:
            self._known_failure(state, handle, "node_missing", cause=error)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209109 [BLE001]; caller cancellation can interrupt collection after the fixed scheduler job exists, so ownership must be settled and cleaned first.
            self._abandon(state, handle, error)
        return result.exit_code

    def _returned_token(self, state: _ProbeState, handle: SlurmJobHandle) -> str:
        try:
            return self._store.read_artifact().strip()
        except (OSError, UnicodeError) as error:
            self._known_failure(state, handle, "node_missing", cause=error)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209110 [BLE001]; caller cancellation during local verification cannot bypass cleanup of the accepted remote operation.
            self._abandon(state, handle, error)

    def _known_failure(
        self,
        state: _ProbeState,
        handle: SlurmJobHandle,
        outcome: Literal["job_failed", "node_missing", "node_wrong"],
        *,
        exit_code: int | None = None,
        cause: Exception | None = None,
    ) -> NoReturn:
        try:
            failed = _record_failure(self._store, state, outcome, exit_code=exit_code)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209114 [BLE001]; a local receipt failure must not strand the already terminal scheduler operation, even under caller cancellation.
            self._abandon(state, handle, error)
        _cleanup(self._runner, failed)
        error = _stored_failure(failed, self._runner.config)
        if cause is None:
            raise error
        raise error from cause

    def _abandon(
        self,
        state: _ProbeState,
        handle: SlurmJobHandle,
        cause: BaseException,
        *,
        failed: _ProbeState | None = None,
    ) -> NoReturn:
        """Settle and clean an accepted probe, recording ``failed`` (default: infrastructure)."""
        _settle(self._runner, handle, pause=self._pause)
        if failed is None:
            failed = state.model_copy(update={"outcome": "infrastructure", "exit_code": None})
        elif failed.outcome == "stopped" and isinstance(cause, Exception):
            # The stop's own cancellation error says nothing the operator needs.
            cause = _stored_failure(failed, self._runner.config)
        elif failed.outcome in _INCONCLUSIVE_OUTCOMES and not isinstance(
            cause, SlurmWorkspaceRootProbeError
        ):
            # The recorded outcome is the answer (retryable or not); the boundary error is
            # kept as its cause.
            cause = _with_cause(_stored_failure(failed, self._runner.config), cause)
        try:
            _save(self._store, failed)
        except BaseException as record_error:
            _cleanup(self._runner, failed)
            if record_error is not cause:
                cause.add_note(f"also could not persist probe failure: {record_error!r}")
            if not isinstance(cause, Exception):
                raise cause from record_error
            raise SlurmWorkspaceRootProbeError.infrastructure() from cause
        _cleanup(self._runner, failed)
        if not isinstance(cause, Exception) or isinstance(cause, SlurmWorkspaceRootProbeError):
            raise cause
        raise SlurmWorkspaceRootProbeError.infrastructure() from cause


def probe_workspace_root(
    runner: SlurmJobRunner,
    *,
    state_root: Path,
    run_id: str,
    pause: Callable[[float], None],
    watch: WorkspaceRootProbeWatch | None = None,
) -> None:
    """Prove both directions of one local Slurm root, exactly once per run.

    Intent is durable before the scheduler boundary.  A lost reply is reconciled
    through the stable scheduler name and is never answered by resubmission.
    Completion is durable before cleanup, so a restart repeats cleanup instead
    of the job.  Every known outcome removes the operation tree and its exact
    content-cache material; uncertain settlement or cleanup fails closed.

    With a ``watch``, its ``queued`` hears that the job waits in the queue, and setting
    its ``stop`` ends a wait at the next poll: the job is cancelled, settled and cleaned,
    the record says the run stopped it, and the probe fails; a resume retries it under
    a new identity.
    """
    try:
        _ProbeLifecycle(runner, state_root, run_id, pause, watch).run()
    except SlurmWorkspaceRootProbeError:
        raise
    except _BOUNDARY_FAILURES as error:
        raise SlurmWorkspaceRootProbeError.infrastructure() from error
