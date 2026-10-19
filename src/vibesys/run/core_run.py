"""Drive one core run with the runtime's production loop over the host's services.

``drive_core_run`` is the one place a core run's services reach the loop. It builds the
shell over the run's durable store (a fresh run when the store is empty, a recovery of
the committed envelope otherwise), joins it with the publication delivery, the run clock
and the host's control channel, then runs ``vs_runtime``'s loop to the run's terminal
status and maps that outcome to the status a plugin returns. It chooses no policy: the
strategy, limits and deadline were fixed by composition (``CoreServices``).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.events import CoreEventType, RunEndingData, RunFailure, RunFailureKind
from vs_core.api import AttemptPhase, EndReason, RetainedCandidate, RunResultProposal, StopMode
from vs_core.api import RunStatus as CoreRunStatus
from vs_project.api import Project
from vs_runtime.api import RunStatus
from vs_runtime.api.core import (
    CoreResumeError,
    CoreRunHost,
    CoreRuntime,
    JournalPublicationDelivery,
    OperatorInbox,
    RunControlBridge,
    RunLoopConfig,
    drive_core,
    reject_ended_resume,
    reject_legacy_resume,
    start_core_awaiting_lease,
)
from vs_runtime.api.infrastructure import RunStopped

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.run.host import CoreHost
    from vibesys.run.integration import LocalRunIntegration
    from vs_core.api import ArtifactRef
    from vs_runtime.api import ArtifactStore
    from vs_runtime.api.core import AgentEvaluationBridge, ExecutorRefusal, StallNotes

_UNFINISHED_REASON = "the run ended without recording a result"


@dataclass(frozen=True)
class RunEnd:
    """How a run ended: its status and, for a failure, the data that says why."""

    status: RunStatus
    failure: RunFailure | None = None


class CoreRunRefusedError(RuntimeError):
    """An executor the run needs is not composed, so the run cannot make progress."""

    def __init__(self, run_id: str, refusal: ExecutorRefusal) -> None:
        """Name the run, the request and the executor role that refused."""
        self.refusal = refusal
        super().__init__(
            f"run {run_id!r}: no executor for {refusal.role.value} request "
            f"{refusal.request_id.root!r}: {refusal.detail}"
        )


class _SteerTexts:
    """Where steer text lives: the run's content-addressed artifact store.

    A steer control carries only the digest of its text, so the text must be readable by
    that digest before the control is submitted.
    """

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    def put(self, ref: ArtifactRef, text: str) -> None:
        """Store the text; its content address must be the digest the control carries."""
        receipt = self._artifacts.write(text.encode())
        if receipt.sha256 != ref.digest:
            message = f"steer artifact {ref.artifact_id.root!r} stored under {receipt.sha256}"
            raise RuntimeError(message)


def _loop_config(run_id: str, lease_seconds: float) -> RunLoopConfig:
    # One lease holder per process: a restart of the same run is a new host.
    return RunLoopConfig(
        host_id=f"vibesys:{run_id}:{os.getpid()}:{uuid.uuid4().hex[:8]}",
        lease_duration=lease_seconds,
    )


def ensure_resumable(project_root: Path, run_id: str) -> None:
    """Reject resuming a run that cannot go on, before any resource opens.

    Two runs cannot: one a legacy loop created (the core cannot continue its state, and
    starting a fresh run under its identity would discard that run's history), and one whose
    core record is terminal (core never applies another request to it, so booting it only to
    find it ended would cost the container and environment and say nothing). Every entrypoint
    resolves its resume reference through here, so the CLI, headless and the server all
    refuse with the same diagnostic: the run, and the legacy file or the stored outcome and
    reason.
    """
    project = Project.open(project_root)
    try:
        reject_legacy_resume(project, run_id)
    except CoreResumeError as error:
        diagnostic = error.diagnostic
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code=diagnostic.code,
                stage=diagnostic.stage,
                message=(
                    f"cannot resume run {run_id!r}: it was created by a legacy loop "
                    f"({diagnostic.path}, schema {diagnostic.source_schema}); "
                    "start a new run instead"
                ),
            )
        ) from error
    try:
        reject_ended_resume(project, run_id)
    except CoreResumeError as error:
        diagnostic = error.diagnostic
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code=diagnostic.code, stage=diagnostic.stage, message=diagnostic.message
            )
        ) from error


async def drive_core_run(host: CoreHost, integration: LocalRunIntegration) -> RunEnd:
    """Run one core run to its terminal status over ``host.services``.

    The loop reads operator controls from ``integration.control`` and, when the services
    carry one, from the run's control inbox, where ``vibesys control`` writes.

    Raises ``RunStopped`` when the operator's stop ended the run, so the session reports
    a stopped run, and ``CoreRunRefusedError`` when composition left an executor role
    unserved. Every exit after core starts closes a configured evaluation bridge and
    releases the run lease, including interruption while the bridge itself starts.
    """
    services = host.services
    shell = CoreRuntime(
        services.store,
        services.search.driver(services.config, services.bindings.registry),
        services.state,
        config=services.config,
        bindings=services.bindings,
    )
    delivery = JournalPublicationDelivery(services.publications, services.bindings.registry)
    controls = RunControlBridge(
        integration.control,
        _SteerTexts(services.artifacts),
        inbox=None
        if services.control_inbox is None
        else OperatorInbox(services.control_inbox, integration.run_control),
        control=integration.run_control,
        # A process that opens an existing run is the operator's explicit resume of it.
        lifts_standing_stop=True,
    )
    loop = CoreRunHost(shell, delivery, services.clock, controls, workspaces=host.workspaces)
    config = _loop_config(services.run_id, host.timing.lease_seconds)
    await start_core_awaiting_lease(loop, config)
    if services.stop_requested:
        # A force stop killed the server before the run committed its stop; the run
        # commits it now, before asking its search for anything, and only drains.
        integration.control.request_stop(StopMode.DRAIN)
    bridge = services.agent_evaluation
    run_error: BaseException | None = None
    try:
        if bridge is not None:
            bridge.attach(shell, services.clock)
            await bridge.serve()
        outcome = await drive_core(loop, config, stall_notes=_unacknowledged_notes(bridge))
    except BaseException as error:
        run_error = error
        raise
    finally:
        try:
            try:
                if bridge is not None:
                    await bridge.close()
            except BaseException as cleanup_error:
                if run_error is None:
                    raise
                run_error.add_note(
                    "Additional error while closing the agent evaluation bridge: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        finally:
            try:
                # An ended run answers every control left, also when the loop raised after
                # core recorded the end; a run that may resume keeps its requests.
                controls.end(shell.record.envelope.core)
            except BaseException as end_error:
                if run_error is None:
                    raise
                run_error.add_note(
                    "Additional error while answering the run's last controls: "
                    f"{type(end_error).__name__}: {end_error}"
                )
            finally:
                # A crashed process cannot reach this line, so its restart waits out the
                # lease (see ``start_core_awaiting_lease``); any return or raise here frees it.
                shell.release_lease(now_at=services.clock.now())
                integration.events.emit(CoreEventType.RUN_ENDING, data=run_ending(shell))
    if outcome.refusal is not None:
        raise CoreRunRefusedError(services.run_id, outcome.refusal)
    status = _status_of(outcome.result, shell)
    return RunEnd(
        status, _failure_of(outcome.result, shell) if status is RunStatus.FAILED else None
    )


def run_ending(shell: CoreRuntime) -> RunEndingData:
    """How the run's durable record stands now: its end reason, resumability and finished rounds."""
    core = shell.record.envelope.core
    result = core.run.result
    reason = None
    if result is not None:
        reason = next((ending for ending in EndReason if _ended_by(result, ending)), None)
    return RunEndingData(
        reason=reason,
        summary="" if result is None else result.reason,
        resumable=core.run.status != CoreRunStatus.TERMINAL,
        rounds_finished=sum(
            1 for attempt in core.attempts.attempts if attempt.phase == AttemptPhase.TERMINAL
        ),
    )


def _unacknowledged_notes(bridge: AgentEvaluationBridge | None) -> StallNotes:
    """A stalled run's report names submissions whose handle never reached their agent."""

    def notes() -> tuple[str, ...]:
        owed = () if bridge is None else bridge.unacknowledged()
        if not owed:
            return ()
        return ("charged submissions the agent never learned of: " + ", ".join(owed),)

    return notes


def _ended_by(result: RunResultProposal, ending: EndReason) -> bool:
    """Whether the framework ended the run for ``ending`` (its reason starts with the ending)."""
    return result.reason.split(":", 1)[0] == ending.value


def _status_of(result: RunResultProposal | None, shell: CoreRuntime) -> RunStatus:
    """Map the run's recorded result to the status a plugin returns.

    The framework ends a stopped run, an operator's or the deadline's, ``cancelled`` after
    adopting the search's last choice. A time budget is the normal way an optimization run
    ends, so a deadline that adopted a verified retained candidate is a success; with none it
    is a failure. An operator stop is reported as stopped.
    """
    if result is None:
        return RunStatus.FAILED
    if _ended_by(result, EndReason.STOPPED):
        raise RunStopped(result.reason)  # the session reports why it stopped
    kept = _ended_by(result, EndReason.DEADLINE) and _adopted_candidate(shell) is not None
    return RunStatus.SUCCEEDED if result.outcome == "success" or kept else RunStatus.FAILED


def _failure_of(result: RunResultProposal | None, shell: CoreRuntime) -> RunFailure:
    """Why a failed run stopped, read from the result and the counts core kept.

    A failed strategy may still ask core to adopt its best partial candidate. Otherwise it
    ends with nothing to keep when its time ran out, when every allowed workstream ran, or
    when its strategy gave up earlier. A run that recorded no result at all (an unfinished
    record) is reported as ``NO_RESULT`` with a fixed reason. ``BUDGET_EXHAUSTED`` is inferred
    from the counts (``started >= budget``), so a strategy that gives up on its last permitted
    workstream is labelled the same.
    """
    core = shell.record.envelope.core
    started = len(core.attempts.attempts)
    budget = core.run.limits.max_attempts
    if result is None:
        kind = RunFailureKind.NO_RESULT
        reason = _UNFINISHED_REASON
    elif _ended_by(result, EndReason.DEADLINE):
        kind = RunFailureKind.DEADLINE
        reason = result.reason
    elif started >= budget:
        kind = RunFailureKind.BUDGET_EXHAUSTED
        reason = result.reason
    else:
        kind = RunFailureKind.NO_RESULT
        reason = result.reason
    return RunFailure(
        kind=kind,
        reason=reason,
        workstreams_started=started,
        workstream_budget=budget,
        candidates_kept=sum(1 for item in core.settlement.settlements if item.eligible),
        adopted_candidate=_adopted_candidate(shell),
    )


def _adopted_candidate(shell: CoreRuntime) -> str | None:
    """The verified retained candidate's stable revision identity, if core adopted one."""
    adoption = shell.record.envelope.core.settlement.adoption
    if (
        adoption is None
        or not adoption.verified
        or not isinstance(adoption.selection, RetainedCandidate)
    ):
        return None
    return adoption.selection.revision.revision_id.root


__all__ = [
    "CoreRunRefusedError",
    "RunEnd",
    "drive_core_run",
    "ensure_resumable",
]
