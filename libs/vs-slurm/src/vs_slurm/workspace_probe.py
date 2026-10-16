"""One-shot lifecycle proof for a submit-node workspace root."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, ValidationError

from vs_decode.api import Unreadable, ValidationOptions, decode_json
from vs_sim.api import ProcessTimeoutError

from .cluster_store import WorkspaceProbeStore
from .errors import (
    SlurmError,
    SlurmSubmissionRejectedError,
    SlurmWorkspaceRootProbeError,
)
from .remote_operations import RemoteOperationError
from .runner import (
    SlurmFileArtifact,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
)
from .staging import tree_content_identity

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from .remote_operations import RemoteOperationEvidence

_RECORD = "workspace-root-probe"
_SUBMIT_MARKER_FILE = ".vibesys-submit-token"
_NODE_MARKER_FILE = ".vibesys-node-token"
_MAX_STATE_BYTES = 64 * 1024
_BOUNDARY_FAILURES = (
    OSError,
    SlurmError,
    RemoteOperationError,
    ValidationError,
    UnicodeError,
    ProcessTimeoutError,
)


class _ProbeState(BaseModel):
    """Durable intent and outcome for one run-scoped startup probe."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[2] = 2
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
    ] = "pending"
    exit_code: int | None = None


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


def _operation_id(run_id: str) -> str:
    return "workspace-root-probe-" + hashlib.sha256(run_id.encode()).hexdigest()[:32]


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
    )
    return request, tree_content_identity(workspace, excludes=(".git/",)), node_token, returned


def _load(
    store: WorkspaceProbeStore,
    operation_id: str,
    identity: str,
    claim_identity: str,
) -> _ProbeState | None:
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
    if (
        state.operation_id != operation_id
        or state.workspace_identity != identity
        or state.claim_identity != claim_identity
    ):
        raise SlurmWorkspaceRootProbeError.state_invalid()
    return state


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
    if status == SlurmJobStatus.UNKNOWN:
        raise SlurmWorkspaceRootProbeError.settlement_unconfirmed()
    if not status.active:
        return
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


def _stored_failure(state: _ProbeState) -> SlurmWorkspaceRootProbeError:
    job_id = state.handle.job_id if state.handle is not None else "unknown"
    if state.outcome == "job_failed":
        return SlurmWorkspaceRootProbeError.job_failed(job_id, state.exit_code)
    if state.outcome == "node_missing":
        return SlurmWorkspaceRootProbeError.node_token_missing(job_id)
    if state.outcome == "node_wrong":
        return SlurmWorkspaceRootProbeError.node_token_wrong(job_id)
    if state.outcome == "infrastructure":
        return SlurmWorkspaceRootProbeError.infrastructure()
    return SlurmWorkspaceRootProbeError.state_invalid()


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


def _raise_unresolved(cause: BaseException | None = None) -> NoReturn:
    error = SlurmWorkspaceRootProbeError.submission_unresolved()
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
    ) -> None:
        self._runner = runner
        self._operation_id = _operation_id(run_id)
        self._store = WorkspaceProbeStore(state_root)
        self._claim_identity = _claim_identity(state_root, self._operation_id)
        self._pause = pause

    def run(self) -> None:
        """Serialize this run's preparation and retain its terminal receipt."""
        with self._store.lock():
            self._run_locked()

    def _run_locked(self) -> None:
        request, identity, node_token, _returned = _request(self._store, self._operation_id)
        state = _load(
            self._store,
            self._operation_id,
            identity,
            self._claim_identity,
        ) or _ProbeState(
            operation_id=self._operation_id,
            workspace_identity=identity,
            claim_identity=self._claim_identity,
        )
        if state.outcome != "pending":
            self._finish_stored(state)
            return
        state, handle = self._handle(request, state)
        self._verify(state, handle, node_token)

    def _finish_stored(self, state: _ProbeState) -> None:
        _cleanup(self._runner, state)
        if state.outcome != "verified":
            raise _stored_failure(state)

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

    def _reject_before_submission(
        self, state: _ProbeState, error: SlurmSubmissionRejectedError
    ) -> NoReturn:
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
        failure = SlurmWorkspaceRootProbeError.infrastructure()
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
        try:
            observed = self._runner.wait(handle, pause=self._pause)
        except _BOUNDARY_FAILURES as error:
            self._abandon(state, handle, error)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-209102 [BLE001]; a caller cancellation can interrupt a scheduler wait with any BaseException, but the accepted probe still must be settled and cleaned before propagation.
            self._abandon(state, handle, error)
        if observed.timed_out or observed.status not in {
            SlurmJobStatus.COMPLETED,
            SlurmJobStatus.FAILED,
        }:
            self._abandon(state, handle, SlurmWorkspaceRootProbeError.infrastructure())
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
        error = _stored_failure(failed)
        if cause is None:
            raise error
        raise error from cause

    def _abandon(
        self,
        state: _ProbeState,
        handle: SlurmJobHandle,
        cause: BaseException,
    ) -> NoReturn:
        _settle(self._runner, handle, pause=self._pause)
        failed = state.model_copy(update={"outcome": "infrastructure", "exit_code": None})
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
        if not isinstance(cause, Exception):
            raise cause
        raise SlurmWorkspaceRootProbeError.infrastructure() from cause


def probe_workspace_root(
    runner: SlurmJobRunner,
    *,
    state_root: Path,
    run_id: str,
    pause: Callable[[float], None],
) -> None:
    """Prove both directions of one local Slurm root, exactly once per run.

    Intent is durable before the scheduler boundary.  A lost reply is reconciled
    through the stable scheduler name and is never answered by resubmission.
    Completion is durable before cleanup, so a restart repeats cleanup instead
    of the job.  Every known outcome removes the operation tree and its exact
    content-cache material; uncertain settlement or cleanup fails closed.
    """
    try:
        _ProbeLifecycle(runner, state_root, run_id, pause).run()
    except SlurmWorkspaceRootProbeError:
        raise
    except _BOUNDARY_FAILURES as error:
        raise SlurmWorkspaceRootProbeError.infrastructure() from error
