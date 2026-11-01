"""Drive one core run with the runtime's step loop over the host's services.

``drive_core_run`` is the one place a core run's services reach the loop. It opens the run's log
(a fresh run when the log holds no start of the run, a fold of its records otherwise), joins it
with the search, the operator's controls and the run clock, and runs ``vs_runtime``'s step loop to
the run's rest: ended, or parked by a pause. It then maps that rest to the status a plugin
returns. It chooses no policy: the strategy, limits and deadline were fixed by composition
(``CoreServices``). The run's lock is the only fence, so there is no lease to take or release.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.events import CoreEventType, RunEndingData, RunFailure, RunFailureKind
from vibesys.run.commit_view import ObservedInputs, RecordView
from vibesys.run.run_clock import start_clock_from
from vibesys.run.run_controls import OperatorControls, StoredSteerArtifacts
from vibesys.run.stall_report import StallReporter
from vibesys.run.store_migration import migrate_saved_run
from vs_core.api import (
    AttemptPhase,
    EndReason,
    RetainedCandidate,
    RunResultProposal,
)
from vs_core.api import RunStatus as CoreRunStatus
from vs_core.api.recovery import RunRest
from vs_project.api import Project
from vs_runlog.api import FileFoldCache
from vs_runtime.api import RunStatus
from vs_runtime.api.core import (
    CoreResumeError,
    OperatorInbox,
    RunControlBridge,
    reject_legacy_resume,
)
from vs_runtime.api.infrastructure import RunStopped, core_workspace_holds
from vs_runtime.api.step_runner import (
    CoreInputs,
    RunLogStepLog,
    RunOpening,
    StallBoard,
    StepLoopTiming,
    resume_paused,
    run_step_loop,
    start_run,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vibesys.run.host import CoreHost
    from vibesys.run.integration import LocalRunIntegration
    from vs_core.api import CoreState
    from vs_runtime.api.core import AgentEvaluationBridge

_UNFINISHED_REASON = "the run ended without recording a result"

CLEAR_TIMEOUT_SECONDS = 120.0
"""How long one ``clear`` may take before the loop counts its backend as unreachable."""
CLEAR_BACKOFF_SECONDS = 1.0
CLEAR_BACKOFF_MAX_SECONDS = 30.0


@dataclass(frozen=True)
class RunEnd:
    """How a run ended: its status and, for a failure, the data that says why.

    ``uncompared`` is whether it adopted a candidate never compared with the input.
    """

    status: RunStatus
    failure: RunFailure | None = None
    uncompared: bool = False


class RunLogUnavailableError(RuntimeError):
    """The run has no run.log to append to: the loop's records have nowhere to go."""

    def __init__(self, run_id: str) -> None:
        """Name the run."""
        super().__init__(f"run {run_id!r} has no run.log, so core has no log to run on")


def ensure_resumable(project_root: Path, run_id: str) -> None:
    """Validate today's loader prerequisites without restricting previous epoch endings.

    Retained older formats need the later facts importer. Leave their files in place and
    identify the missing prerequisite before opening execution resources.
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
                    f"run {run_id!r} needs the retained-facts importer for its legacy state "
                    f"({diagnostic.path}, schema {diagnostic.source_schema}); "
                    "the current core loader cannot read this format; retained files are unchanged"
                ),
            )
        ) from error


async def drive_core_run(host: CoreHost, integration: LocalRunIntegration) -> RunEnd:
    """Run one core run to its rest over ``host.services``.

    The loop reads operator controls from ``integration.control`` and, when the services
    carry one, from the run's control inbox, where ``vibesys control`` writes.

    Raises ``OperatorPaused`` when the run parked, ``RunStopped`` when the operator's stop ended
    it, so the session reports a stopped run, and ``StepLoopStalledError`` when the run waits on
    nothing. Every exit after the run's log opens closes a configured evaluation bridge and
    answers the controls left, including interruption while the bridge itself starts.
    """
    services = host.services
    log_file = integration.events.run_log
    if log_file is None:
        raise RunLogUnavailableError(services.run_id)
    epoch = integration.events.epoch
    clock = services.clock
    driver = services.search.driver(services.config, services.registry)
    migrate_saved_run(
        services.project,
        services.run_id,
        log_file,
        search=driver,
        registry=services.registry,
        epoch=epoch,
        stamp=clock.stamp,
    )
    records = log_file.records()
    start_clock_from(clock, records, epoch)
    started = start_run(
        log_file,
        records,
        RunOpening(
            driver=driver,
            config=services.config,
            registry=services.registry,
            seed=services.state,
            clock=clock,
            stamp=clock.stamp,
            cache=FileFoldCache(
                Project.state_cache_path_for(services.project.root, services.run_id)
            ),
            announcer=integration.events,
        ),
        epoch=epoch,
    )
    log, search, resumed = started.log, started.search, started.resumed
    controls = OperatorControls(
        RunControlBridge(
            integration.control,
            StoredSteerArtifacts(services.artifacts),
            inbox=None
            if services.control_inbox is None
            else OperatorInbox(
                services.control_inbox,
                integration.run_control,
                integration.events,
            ),
            control=integration.run_control,
            # A process that opens an existing run is the operator's explicit resume of it.
            lifts_standing_stop=True,
        ),
        services.steering,
    )
    inputs = CoreInputs(
        log,
        search,
        clock,
        controls=controls,
        control_poll_seconds=host.timing.control_poll_seconds,
    )
    bridge = services.agent_evaluation
    observed = ObservedInputs(
        inputs,
        RecordView(search, services.search.declaration, epoch=epoch, run_id=services.run_id),
        services.commit_observer,
        _unacknowledged_notes(bridge),
        clock,
    )
    board = StallBoard()
    stalls = asyncio.ensure_future(StallReporter(board, integration.events).run(clock))
    run_error: BaseException | None = None
    try:
        observed.begin(resumed=resumed)
        await _follow_core(host, log)
        if bridge is not None:
            bridge.attach(observed, clock)
            await bridge.serve()
        resume_paused(log, now_at=clock.now(), epoch=epoch, resumed=resumed)
        controls.bring_in(log, clock.now())
        rest = await run_step_loop(
            observed,
            services.runner(clock, epoch),
            clock,
            StepLoopTiming(
                cancel_grace_s=services.cancellation_bound_seconds,
                clear_timeout_s=CLEAR_TIMEOUT_SECONDS,
                clear_backoff_s=CLEAR_BACKOFF_SECONDS,
                clear_backoff_max_s=CLEAR_BACKOFF_MAX_SECONDS,
            ),
            board,
        )
    except BaseException as error:
        run_error = error
        raise
    finally:
        stalls.cancel()
        await asyncio.gather(stalls, return_exceptions=True)
        await _leave(bridge, controls, inputs, log.state, run_error)
        _record_ending(log.state, integration)
    return _run_end(rest, log.state, integration.environment_failure.reason)


async def _follow_core(host: CoreHost, log: RunLogStepLog) -> None:
    """Bind the run's workspaces to core's state and discard what core no longer references."""
    host.workspaces.follow(lambda: core_workspace_holds(log.state))
    await host.workspaces.release_unheld()


async def _leave(
    bridge: AgentEvaluationBridge | None,
    controls: OperatorControls,
    inputs: CoreInputs,
    core: CoreState,
    run_error: BaseException | None,
) -> None:
    """Close the evaluation bridge and answer the controls left, keeping the run's own error."""
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
            inputs.leave()
            # An ended run answers every control left, also when the loop raised after core
            # recorded the end; a run that may resume keeps its requests.
            controls.end(core)
        except BaseException as end_error:
            if run_error is None:
                raise
            run_error.add_note(
                "Additional error while answering the run's last controls: "
                f"{type(end_error).__name__}: {end_error}"
            )


def _record_ending(core: CoreState, integration: LocalRunIntegration) -> None:
    if core.run.status != CoreRunStatus.PARKED:
        integration.events.emit(CoreEventType.RUN_ENDING, data=run_ending(core))


def _run_end(rest: RunRest, core: CoreState, environment_failure: str | None) -> RunEnd:
    """How the run ended: the environment's failure, when it stopped the run, else its result."""
    if rest is RunRest.PARKED:
        raise OperatorPaused
    result = core.run.result
    uncompared = result is not None and result.uncompared
    if environment_failure is not None:
        # The environment's failure stopped the run; it ended as a failure, not a stop.
        return RunEnd(
            RunStatus.FAILED,
            _environment_failure(environment_failure, core),
            uncompared=uncompared,
        )
    status = _status_of(result, core)
    return RunEnd(
        status,
        _failure_of(result, core) if status is RunStatus.FAILED else None,
        uncompared=uncompared,
    )


def run_ending(core: CoreState) -> RunEndingData:
    """How the run's durable record stands now: its end reason, resumability and finished rounds."""
    result = core.run.result
    reason = None
    if result is not None:
        reason = next((ending for ending in EndReason if _ended_by(result, ending)), None)
    return RunEndingData(
        reason=reason,
        summary="" if result is None else result.reason,
        resumable=True,
        rounds_finished=sum(
            1 for attempt in core.attempts.attempts if attempt.phase == AttemptPhase.TERMINAL
        ),
        uncompared=result is not None and result.uncompared,
    )


def _unacknowledged_notes(bridge: AgentEvaluationBridge | None) -> Callable[[], Sequence[str]]:
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


class OperatorPaused(BaseException):
    """The epoch drained successfully; the saved search resumes on relaunch."""


class OperatorStopped(RunStopped):
    """The operator's stop ended the run; it carries what the run's result recorded of it."""

    def __init__(self, reason: str, *, uncompared: bool) -> None:
        """Keep ``reason`` as the message and whether the adopted candidate was never compared."""
        super().__init__(reason)
        self.uncompared = uncompared


def _status_of(result: RunResultProposal | None, core: CoreState) -> RunStatus:
    """Map the run's recorded result to the status a plugin returns.

    The framework ends a stopped run, an operator's or the deadline's, ``cancelled`` after
    adopting the search's last choice. A time budget is the normal way an optimization run
    ends, so a deadline that adopted a verified retained candidate is a success; with none it
    is a failure. An operator stop is reported as stopped.
    """
    if result is None:
        return RunStatus.FAILED
    if _ended_by(result, EndReason.STOPPED):
        raise OperatorStopped(
            result.reason, uncompared=result.uncompared
        )  # the session reports why it stopped
    kept = _ended_by(result, EndReason.DEADLINE) and _adopted_candidate(core) is not None
    return RunStatus.SUCCEEDED if result.outcome == "success" or kept else RunStatus.FAILED


def _failure_of(result: RunResultProposal | None, core: CoreState) -> RunFailure:
    """Why a failed run stopped, read from the result and the counts core kept.

    A failed strategy may still ask core to adopt its best partial candidate. Otherwise it
    ends with nothing to keep when its time ran out, when every allowed workstream ran, or
    when its strategy gave up earlier. A run that recorded no result at all (an unfinished
    record) is reported as ``NO_RESULT`` with a fixed reason. ``BUDGET_EXHAUSTED`` is inferred
    from the counts (``started >= budget``), so a strategy that gives up on its last permitted
    workstream is labelled the same.
    """
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
        adopted_candidate=_adopted_candidate(core),
    )


def _environment_failure(reason: str, core: CoreState) -> RunFailure:
    """A run the environment ended: it could run none of its work, whatever it adopted."""
    return RunFailure(
        kind=RunFailureKind.RUN_ENVIRONMENT_UNAVAILABLE,
        reason=reason,
        workstreams_started=len(core.attempts.attempts),
        workstream_budget=core.run.limits.max_attempts,
        candidates_kept=sum(1 for item in core.settlement.settlements if item.eligible),
        adopted_candidate=_adopted_candidate(core),
    )


def _adopted_candidate(core: CoreState) -> str | None:
    """The verified retained candidate's stable revision identity, if core adopted one."""
    adoption = core.settlement.adoption
    if (
        adoption is None
        or not adoption.verified
        or not isinstance(adoption.selection, RetainedCandidate)
    ):
        return None
    return adoption.selection.revision.revision_id.root


__all__ = [
    "OperatorPaused",
    "OperatorStopped",
    "RunEnd",
    "RunLogUnavailableError",
    "drive_core_run",
    "ensure_resumable",
]
