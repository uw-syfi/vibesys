"""Transport-neutral request API for frontend clients."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING

from server.api.design import DesignLog
from server.api.experiments import (
    ExperimentProjection,
    ExperimentQueryResult,
    build_experiment_log,
)
from server.api.iterations import IterationSlice, page_iterations
from server.api.performance import (
    build_performance_context,
    summarize_objective,
)
from server.api.protocol import (
    ChatQuery,
    ChatResult,
    ChatThreadCreateQuery,
    ChatThreadInfo,
    CommandAck,
    ControlCommand,
    DesignPatch,
    DesignPatchQuery,
    DesignQuery,
    DesignRound,
    EventsQuery,
    ExperimentQuery,
    HistoryQuery,
    HypothesisEntry,
    IterationQuery,
    PauseCommand,
    PerformanceContext,
    PerformanceQuery,
    PerformanceRound,
    ProtocolRequest,
    Response,
    ResumeCommand,
    RunSnapshot,
    SnapshotQuery,
    SteerCommand,
    StopCommand,
    decode_request,
    failure_response,
)
from server.events import EventType, RunEvent, RunOverview, RunStartedData
from vibesys.api import (
    REFUSAL_TEXT,
    ControlAction,
    ControlStatus,
    IterationUnavailable,
    PauseNowRequest,
    PauseRequest,
    RefusalReason,
    ResumeRequest,
    RunRecordReadError,
    SteerRequest,
    StopMode,
)
from vibesys.api.hypothesis import agent_projection
from vs_sim.api import utc_datetime

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from server.chat.manager import ChatManager
    from server.controller import RunController
    from server.execution import ActiveAgentExecution, ExecutionTracker
    from server.integration import RunIntegrationAdapter
    from server.journal import WireJournal
    from vibesys.api import ControlRequest, RunControl, RunRecord, RunView, WorkspaceChange
    from vs_sim.api import Clock, Condition, Threads


@dataclass(frozen=True)
class SubscriptionBootstrap:
    """One journal state captured atomically for a subscription bootstrap."""

    run_id: str
    store_id: str
    floor: int
    through_sequence: int
    events: list[RunEvent]
    active_executions: list[ActiveAgentExecution]


class RunReadOnlyError(RuntimeError):
    """A mutating request sent to a finished run opened for inspection."""

    diagnostic_code = "run_read_only"

    def __init__(self) -> None:
        """Use one stable message for read-only command diagnostics."""
        super().__init__("This finished run is read-only")


class SnapshotStoreReplacedError(RuntimeError):
    """A fenced snapshot request whose event store is no longer current."""

    diagnostic_code = "snapshot_store_replaced"

    def __init__(self) -> None:
        """Keep the public failure independent of opaque store identifiers."""
        super().__init__("The run event store changed before the snapshot was captured")


class IterationStoreReplacedError(RuntimeError):
    """A fenced iteration request whose event store is no longer current."""

    diagnostic_code = "iteration_store_replaced"

    def __init__(self) -> None:
        """Keep the public failure independent of opaque store identifiers."""
        super().__init__("The run event store changed before iterations were captured")


class EventsStoreReplacedError(RuntimeError):
    """A fenced event-range request whose event store is no longer current."""

    diagnostic_code = "events_store_replaced"

    def __init__(self) -> None:
        """Keep the public failure independent of opaque store identifiers."""
        super().__init__("The run event store changed before history was captured")


class _RunControlState(StrEnum):
    """Lifecycle of the one control session owned by a server runtime."""

    AWAITING_SESSION = "awaiting_session"
    ACTIVE = "active"
    ENDED = "ended"


class RunControlUnavailableError(RuntimeError):
    """A control command that cannot be retained until a session can consume it."""

    diagnostic_code = "run_control_unavailable"

    def __init__(self, state: _RunControlState) -> None:
        """Explain whether the session is not installed yet or has ended."""
        timing = (
            "before the run session starts"
            if state is _RunControlState.AWAITING_SESSION
            else "after the run session ends"
        )
        super().__init__(f"Run control is unavailable {timing}")


class CapacityWaitUnavailableError(RuntimeError):
    """``command.resume`` waiting for capacity or with ``fallback``, for a run not so paused."""

    diagnostic_code = "capacity_wait_unavailable"

    def __init__(self) -> None:
        """Say why the run cannot wait for capacity or switch to its fallback."""
        super().__init__("The run is not paused on a provider capacity limit")


class RunStoppingError(RuntimeError):
    """``command.resume`` waiting for capacity or with ``fallback``, for a run that is stopping."""

    diagnostic_code = "run_stopping"

    def __init__(self) -> None:
        """Say why the run cannot wait for capacity or switch to its fallback."""
        super().__init__("The run is stopping")


class SteerTargetUnknownError(RuntimeError):
    """A steer to a role the search does not declare."""

    diagnostic_code = "steer_target_unknown"

    def __init__(self) -> None:
        """Say why no turn could take the steer."""
        super().__init__("The run has no such steer target")


class SteerTargetEndedError(RuntimeError):
    """A steer to an attempt that has ended."""

    diagnostic_code = "steer_target_ended"

    def __init__(self) -> None:
        """Say why no turn could take the steer."""
        super().__init__("The steer's target can no longer take a message")


class WithdrawNotStandingError(RuntimeError):
    """A withdrawal of a steer that is not, or is no longer, standing guidance."""

    diagnostic_code = "withdraw_not_standing"

    def __init__(self) -> None:
        """Say why nothing could be withdrawn."""
        super().__init__("The withdrawal names no standing guidance in force")


class ActionAttemptEndedError(RuntimeError):
    """A cancel of an attempt that has ended."""

    diagnostic_code = "action_attempt_ended"

    def __init__(self) -> None:
        """Say why nothing could be cancelled."""
        super().__init__(REFUSAL_TEXT[RefusalReason.ATTEMPT_ENDED])


class ActionAlreadyCancelledError(RuntimeError):
    """A cancel of a work item the run already cancelled."""

    diagnostic_code = "action_already_cancelled"

    def __init__(self) -> None:
        """Say why nothing could be cancelled."""
        super().__init__(REFUSAL_TEXT[RefusalReason.ALREADY_CANCELLED])


class ActionAboveLaunchLimitError(RuntimeError):
    """A parallel limit above the one the run was launched with."""

    diagnostic_code = "action_above_launch_limit"

    def __init__(self) -> None:
        """Say why the limit cannot be set."""
        super().__init__(REFUSAL_TEXT[RefusalReason.ABOVE_LAUNCH_LIMIT])


class ControlIdConflictError(RuntimeError):
    """A control whose ``control_id`` already named a different control."""

    diagnostic_code = "control_id_conflict"

    def __init__(self, control_id: str) -> None:
        """Name the reused id."""
        super().__init__(f"Control id {control_id!r} already named a different control")


_REFUSALS: dict[RefusalReason, Callable[[], Exception]] = {
    RefusalReason.NOT_PAUSED_ON_CAPACITY: CapacityWaitUnavailableError,
    RefusalReason.STOPPING: RunStoppingError,
    RefusalReason.UNKNOWN_TARGET: SteerTargetUnknownError,
    RefusalReason.ENDED_TARGET: SteerTargetEndedError,
    RefusalReason.NOT_STANDING: WithdrawNotStandingError,
    # The run ended before its session did: the same answer as a control after the session.
    RefusalReason.RUN_ENDED: partial(RunControlUnavailableError, _RunControlState.ENDED),
    RefusalReason.ATTEMPT_ENDED: ActionAttemptEndedError,
    RefusalReason.ALREADY_CANCELLED: ActionAlreadyCancelledError,
    RefusalReason.ABOVE_LAUNCH_LIMIT: ActionAboveLaunchLimitError,
}
"""The protocol error each refusal answers with."""


type ControlProtocolCommand = PauseCommand | ResumeCommand | SteerCommand | ControlCommand
"""Every protocol request that controls the run."""


def control_request(command: ControlProtocolCommand) -> ControlRequest:
    """The operator control a protocol command carries, in the schema every ingress shares.

    A single-action command has no control id of its own; its envelope's ``request_id``
    names it, so resending the same request is one control.
    """
    if isinstance(command, ControlCommand):
        return command.control
    control_id = f"wire-{hashlib.sha256(command.request_id.encode()).hexdigest()[:32]}"
    if isinstance(command, PauseCommand):
        return PauseRequest(control_id=control_id)
    if isinstance(command, ResumeCommand):
        return ResumeRequest(control_id=control_id, fallback=command.fallback, when=command.when)
    return SteerRequest(control_id=control_id, text=command.text)


class RunControlSlot:
    """Synchronize command admission with installation of one run session.

    Stop is the only command whose pre-session semantics are unambiguous: it
    means that the eventual session must stop. The slot retains that intent
    until installation and delivers it once; of several, it retains the
    strongest (``escalate_stop``), since a stop only escalates. Other mutating commands require
    an active session, so rejecting them is safer than acknowledging and
    discarding them or guessing their ordering against startup.
    """

    def __init__(self, condition: Condition) -> None:
        """Start ready to retain a stop for the eventual session."""
        self._condition = condition
        self._state = _RunControlState.AWAITING_SESSION
        self._control: RunControl | None = None
        self._stop_pending: PauseNowRequest | None = None
        self._shutdown_pending = False

    def install(self, control: RunControl) -> None:
        """Install the session and deliver one retained stop intent, if any."""
        with self._condition:
            if self._state is not _RunControlState.AWAITING_SESSION:
                message = f"cannot install run control while slot is {self._state.value}"
                raise RuntimeError(message)
            if self._stop_pending is not None:
                # Control methods are synchronous messages to the process-local
                # session. Deliver under the shared re-entrant condition so a
                # concurrent command cannot overtake this retained intent.
                control.submit(self._stop_pending)
                self._stop_pending = None
            if self._shutdown_pending:
                control.stop(StopMode.DRAIN)
                self._shutdown_pending = False
            self._control = control
            self._state = _RunControlState.ACTIVE

    def release(self, control: RunControl) -> None:
        """Close command admission after the installed session ends."""
        with self._condition:
            if self._state is not _RunControlState.ACTIVE or self._control is not control:
                message = "cannot release a run control that is not installed"
                raise RuntimeError(message)
            self._control = None
            self._state = _RunControlState.ENDED

    def stop(self, mode: StopMode) -> bool:
        """Stop the installed run in ``mode``; False, changing nothing, when none is installed.

        The run's control keeps the strongest stop asked for, so a repeat changes nothing
        and a cancel after a drain escalates it.
        """
        with self._condition:
            if self._control is None:
                return False
            self._control.stop(mode)
            return True

    def execute(self, command: ControlProtocolCommand | StopCommand) -> CommandAck:
        """Apply one command or retain the sole command safe before startup."""
        if isinstance(command, StopCommand):
            return self._shutdown()
        request = control_request(command)
        with self._condition:
            control = self._control
            if control is None:
                if self._state is _RunControlState.AWAITING_SESSION and isinstance(
                    request, PauseNowRequest
                ):
                    self._stop_pending = request
                    return CommandAck(action=request.action, status="pending")
                raise RunControlUnavailableError(self._state)
            # Delivered while the slot prevents session release.
            ack = control.submit(request)
        match ack.status, ack.reason:
            case ControlStatus.REFUSED, RefusalReason() as reason:
                refusal = _REFUSALS[reason]()
                raise refusal
            case ControlStatus.CONFLICT, _:
                raise ControlIdConflictError(request.control_id)
            case ControlStatus.PENDING, _:
                return CommandAck(action=ack.action, status="pending")
            case _:
                return CommandAck(action=ack.action, status="consumed")

    def _shutdown(self) -> CommandAck:
        """The registry's process shutdown also reaches hosts without a core control poll."""
        with self._condition:
            if self._control is not None:
                self._control.stop(StopMode.DRAIN)
            elif self._state is _RunControlState.AWAITING_SESSION:
                self._shutdown_pending = True
            else:
                raise RunControlUnavailableError(self._state)
        return CommandAck(action=ControlAction.PAUSE_NOW, status="pending")


@dataclass(frozen=True)
class SubscriptionCheckpoint:
    """One journal state captured atomically for a live subscription batch.

    ``store_id`` names the store the events came from. A subscription compares
    it against the store it bootstrapped from, so it can never mistake a
    replaced log's sequences for a continuation of its own.
    """

    store_id: str
    through_sequence: int
    events: list[RunEvent]
    active_executions: list[ActiveAgentExecution]


class _DesignLogGitEvents:
    """Forward design-projection git warnings to a journal sink.

    The design projection's git access is read-only (``diff_name_status``
    and the patch reader), so the snapshot observations never fire and
    inherit the null no-ops. Warnings are formatted here, at the wiring
    layer, into the tagged text the run journal shows.
    """

    def __init__(self, publish: Callable[[str], None]) -> None:
        self._publish = publish

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        """Publish one tagged line per tracker fault."""
        message = summary if detail is None else f"{summary}: {detail}"
        self._publish(f"[git-tracking] {message}")


class RunApi:
    """Authoritative request API consumed by frontend clients."""

    # lint-waiver: LW-009026 [PLR0913]; inject each request owner explicitly at the API composition boundary.
    def __init__(  # noqa: PLR0913
        self,
        condition: Condition,
        controller: RunController,
        executions: ExecutionTracker,
        journal: WireJournal,
        chat: ChatManager,
        integration: RunIntegrationAdapter,
        *,
        controls: RunControlSlot,
        clock: Clock,
        project_scope: str | None = None,
        threads: Threads,
    ) -> None:
        """Initialize the API with the components that own each request surface."""
        self._condition = condition
        self._controller = controller
        self._executions = executions
        self._journal = journal
        self._chat = chat
        self._integration = integration
        self._controls = controls
        self._clock = clock
        self._project_scope = project_scope
        # Keyed by the attached run so the projection's diff cache survives
        # across requests but never outlives the run it was built for.
        self._design: tuple[str, DesignLog] | None = None
        self._design_lock = threads.lock()
        self._experiment_projection = ExperimentProjection(threads=threads)
        self._journal.add_listener(
            self._observe_experiment_change,
            replay_filter=lambda _header: False,
        )
        self._overview: RunOverview | None = None
        self._measured: PerformanceContext | None = None
        self._journal.add_listener(
            self._observe_run_started,
            replay_filter=lambda header: header.type is EventType.RUN_STARTED,
        )

    def _now(self) -> datetime:
        return utc_datetime(self._clock)

    def decode_request(self, frame: str | bytes) -> ProtocolRequest | Response:
        """Parse one request frame, or the failed response that rejects it, stamped by this API."""
        return decode_request(frame, clock=self._clock)

    def failure(self, request_id: str, client_id: str, error: BaseException) -> Response:
        """Build the failed response for ``error``, addressed to the client that asked."""
        return failure_response(request_id, client_id, error, clock=self._clock)

    def execute(self, request: ProtocolRequest) -> Response:
        """Execute a request and reflect the issuing client on its response."""
        return self._execute(request).model_copy(update={"client_id": request.client_id})

    # lint-waiver: LW-009027 [C901, PLR0911]; exhaustive protocol dispatch keeps each request variant routed to its owning handler.
    def _execute(self, request: ProtocolRequest) -> Response:  # noqa: C901, PLR0911
        """Route one typed request to its owning service."""
        if isinstance(
            request, (PauseCommand, ResumeCommand, SteerCommand, StopCommand, ControlCommand)
        ):
            return self._execute_command(request)
        if isinstance(request, ChatQuery):
            return self._execute_chat(request)
        if isinstance(request, ChatThreadCreateQuery):
            return self._execute_chat_thread_create(request)
        if isinstance(request, HistoryQuery):
            self._journal.record(EventType.STATUS_QUERY, "/history")
            return Response(
                request_id=request.request_id, timestamp=self._now(), events=self.history_events()
            )
        if isinstance(request, PerformanceQuery):
            self._journal.record(EventType.STATUS_QUERY, "/perf")
            return Response(
                request_id=request.request_id,
                timestamp=self._now(),
                performance=self.performance_rounds(),
                performance_context=self.performance_context(),
            )
        if isinstance(request, ExperimentQuery):
            self._journal.record(EventType.STATUS_QUERY, "/experiments")
            attached_run = self._controller.attached_run
            ready = attached_run is not None
            result = (
                self._query_experiments(attached_run, request) if attached_run is not None else None
            )
            return Response(
                request_id=request.request_id,
                timestamp=self._now(),
                experiments=result.entries if result is not None else [],
                experiment_update=result.update if result is not None else None,
                experiments_ready=ready,
            )
        if isinstance(request, IterationQuery):
            iterations = self.iterations(
                expected_store_id=request.expected_store_id,
                parent_id=request.parent_id,
                cursor=request.cursor,
                limit=request.limit,
            )
            return Response(
                request_id=request.request_id,
                timestamp=self._now(),
                iterations=iterations,
                iterations_ready=iterations is not None,
            )
        if isinstance(request, DesignQuery):
            self._journal.record(EventType.STATUS_QUERY, "/design")
            ready = self._controller.attached_run is not None
            return Response(
                request_id=request.request_id,
                timestamp=self._now(),
                design=self.design_rounds() if ready else [],
                design_ready=ready,
            )
        if isinstance(request, DesignPatchQuery):
            # Deliberately not journaled as a STATUS_QUERY: a diff viewer
            # issues one of these per file navigated, and that cadence would
            # spam the run journal without recording anything about the run.
            return Response(
                request_id=request.request_id,
                timestamp=self._now(),
                design_patch=self.design_patch(request.base, request.head, request.path),
            )
        if isinstance(request, SnapshotQuery):
            return Response(
                request_id=request.request_id,
                timestamp=self._now(),
                snapshot=self.snapshot(expected_store_id=request.expected_store_id),
            )
        if isinstance(request, EventsQuery):
            timeout = request.timeout_ms / 1000 if request.timeout_ms else None
            events = (
                self.wait_for_events(
                    request.after_sequence,
                    timeout,
                    request.before_sequence,
                    expected_store_id=request.expected_store_id,
                )
                if timeout is not None
                else self.events(
                    request.after_sequence,
                    request.before_sequence,
                    expected_store_id=request.expected_store_id,
                )
            )
            return Response(request_id=request.request_id, timestamp=self._now(), events=events)
        message = f"Unsupported protocol request: {type(request).__name__}"
        raise TypeError(message)

    def _execute_command(self, request: ControlProtocolCommand | StopCommand) -> Response:
        """Send a command through the runtime's synchronized control slot."""
        if self._controller.is_read_only():
            raise RunReadOnlyError
        ack = self._controls.execute(request)
        return Response(request_id=request.request_id, timestamp=self._now(), ack=ack)

    def _execute_chat(self, request: ChatQuery) -> Response:
        answer, event = self._chat.chat_with_event(request.text, thread_id=request.thread_id)
        return Response(
            request_id=request.request_id,
            timestamp=self._now(),
            chat=ChatResult(question=request.text, answer=answer, thread_id=request.thread_id),
            events=[] if event is None else [event],
        )

    def _execute_chat_thread_create(self, request: ChatThreadCreateQuery) -> Response:
        sequence = self._journal.latest_sequence
        spec = self._chat.create_thread(
            harness=request.harness,
            title=request.title,
        )
        return Response(
            request_id=request.request_id,
            timestamp=self._now(),
            chat_thread=ChatThreadInfo(
                thread_id=spec.thread_id,
                title=spec.title,
                provider=spec.provider,
                model=spec.model,
            ),
            events=self._journal.read(sequence),
        )

    def snapshot(self, *, expected_store_id: str | None = None) -> RunSnapshot:
        """Return state from the expected store, or fail if that sequence space is gone."""
        with self._condition:
            if (
                expected_store_id is not None
                and self._journal.store_id_locked() != expected_store_id
            ):
                raise SnapshotStoreReplacedError
            kind, round_label = self._executions.current_locked()
            return RunSnapshot(
                run_id=self._journal.run_id_locked(),
                project_scope=self._project_scope,
                sequence=self._journal.latest_sequence_locked(),
                status=self._controller.status_locked(),
                agent_kind=kind,
                round_label=round_label,
                active_executions=self._executions.active_locked(),
                chat_threads=[
                    ChatThreadInfo(
                        thread_id=spec.thread_id,
                        title=spec.title,
                        provider=spec.provider,
                        model=spec.model,
                    )
                    for spec in self._chat.threads_locked()
                ],
                last_chat_thread_id=self._chat.last_thread_id_locked(),
                spans=self._integration.trace.spans(),
                overview=self._current_overview(),
            )

    def iterations(
        self,
        *,
        expected_store_id: str | None,
        parent_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> IterationSlice | IterationUnavailable | None:
        """Capture one iteration page from the store generation the stream established."""
        with self._condition:
            if (
                expected_store_id is not None
                and self._journal.store_id_locked() != expected_store_id
            ):
                raise IterationStoreReplacedError
            self._journal.record(EventType.STATUS_QUERY, "/iterations")
            attached_run = self._controller.attached_run_locked()
            if attached_run is None:
                return None
            return page_iterations(
                attached_run.view(), parent_id=parent_id, cursor=cursor, limit=limit
            )

    def subscription_checkpoint(
        self, after_sequence: int, *, store_id: str | None = None, bootstrap_spine: bool = False
    ) -> SubscriptionCheckpoint:
        """Capture events and executions at one subscription sequence boundary.

        ``store_id`` names the store the caller's cursor belongs to. When the
        journal has since attached a different one, that cursor numbers a log
        that is gone, so the checkpoint reports the new identity and reads
        nothing: the caller must bootstrap against the new store rather than
        extend a fold with sequences from another log.
        """
        with self._condition:
            current = self._journal.store_id_locked()
            if store_id is not None and current != store_id:
                return SubscriptionCheckpoint(current, after_sequence, [], [])
            through_sequence, events = self._journal.checkpoint_locked(
                after_sequence, bootstrap_spine=bootstrap_spine
            )
            return SubscriptionCheckpoint(
                store_id=current,
                through_sequence=through_sequence,
                events=events,
                active_executions=self._executions.active_locked(),
            )

    def subscription_bootstrap(
        self, after_sequence: int, tail: int | None, *, store_id: str | None = None
    ) -> SubscriptionBootstrap:
        """Capture one atomic bootstrap state for a new or restarted subscription.

        Appends acquire the same condition, so computing the tail floor and
        reading the checkpoint under one acquisition keeps the replay bounded
        by ``tail`` no matter how many events land during the bootstrap.

        ``store_id`` names the store the caller's ``after_sequence`` belongs to,
        carried by a resume across a dropped connection. When the journal has
        since attached a different one, that cursor numbers a log that is gone,
        so it is dropped and the live store is replayed from its floor: the
        client re-folds from the batch's identity rather than extending a stale
        fold. A cursor beyond the live store's watermark cannot belong to its
        append-only sequence space either, so it is dropped even when the store
        is unnamed or matching. Every other empty or matching id resumes the
        cursor as before, which is also what a fresh dial (no store seen yet)
        and old clients get.
        """
        with self._condition:
            current_store_id = self._journal.store_id_locked()
            latest = self._journal.latest_sequence_locked()
            if (store_id and store_id != current_store_id) or after_sequence > latest:
                after_sequence = 0
            floor = after_sequence if tail is None else max(after_sequence, latest - tail)
            through_sequence, events = self._journal.checkpoint_locked(
                floor, bootstrap_spine=tail is not None
            )
            return SubscriptionBootstrap(
                run_id=self._journal.run_id_locked(),
                store_id=self._journal.store_id_locked(),
                floor=floor,
                through_sequence=through_sequence,
                events=events,
                active_executions=self._executions.active_locked(),
            )

    def events(
        self,
        after_sequence: int = 0,
        before_sequence: int | None = None,
        *,
        expected_store_id: str | None = None,
    ) -> list[RunEvent]:
        """Read one event range from the expected sequence space."""
        if expected_store_id is None:
            return self._journal.read(after_sequence, before_sequence)
        with self._condition:
            return self._fenced_events_locked(expected_store_id, after_sequence, before_sequence)

    def history_events(self) -> list[RunEvent]:
        """Read the canonical event history used by frontend clients."""
        return self._journal.read_history()

    def performance_rounds(self) -> list[PerformanceRound]:
        """Build the recorded round-level performance series.

        Sourced from the agent projection's `rounds`: every field this DTO
        needs (`round_number`, `perf_metric`, `perf_unit`, `passed`,
        `profile_skipped`) is copied verbatim from the run's own round
        history, not re-derived from core state.
        """
        record = self._controller.attached_run
        if record is None:
            return []
        view = record.view()
        projection = agent_projection(view)
        if projection is None:
            return []
        return [
            PerformanceRound(
                round=record.round_number,
                perf_metric=record.perf_metric,
                perf_unit=record.perf_unit,
                passed=record.passed,
                profile_skipped=record.profile_skipped,
            )
            for record in projection.rounds
            if record.perf_metric is not None and record.perf_unit is not None
        ]

    def performance_context(self) -> PerformanceContext | None:
        """Build objective and measurement context for performance rendering.

        Sourced from `vibesys.api`'s `RunView`: `HypothesisView.perf_metric_round`
        carries the round that produced each hypothesis's headline measurement,
        so `build_performance_context` can select the newest one without a
        core-private `HypothesisMeasurement`.
        """
        record = self._controller.attached_run
        if record is None:
            return None
        view = record.view()
        projection = agent_projection(view)
        if projection is None:
            return None
        return build_performance_context(
            view,
            objectives=projection.objectives,
            objective_description=self._objective_description(),
        )

    def experiments(self) -> list[HypothesisEntry]:
        """Build the experiment log for an agent outer loop."""
        record = self._controller.attached_run
        if record is None:
            return []
        return build_experiment_log(record.view())

    def design_rounds(self) -> list[DesignRound]:
        """Project the per-round design log for the attached run.

        Facts (hypothesis/round commits) come from `vibesys.api`'s
        `RunView`; the design log itself only adds what a `RunView` does not
        carry, the workspace's own git history.
        """
        record = self._controller.attached_run
        if record is None:
            return []
        design = self._design_log(record)
        return design.rounds(
            record.view(),
            baseline=record.facts().trusted_input_baseline,
        )

    def design_patch(self, base: str, head: str, path: str) -> DesignPatch | None:
        """Read one file's patch for a published design range, None unattached."""
        record = self._controller.attached_run
        if record is None:
            return None
        return self._design_log(record).patch(base, head, path)

    def _design_log(self, record: RunRecord) -> DesignLog:
        """Return the design projection for one run, building it once.

        The projection caches a git diff per round commit range. Those ranges
        are immutable, so keeping the projection alive across requests is what
        turns a repeated ``query.design`` from one subprocess per round into
        no subprocesses at all. It is rebuilt only if a different run attaches.
        """
        with self._design_lock:
            cached = self._design
            if cached is not None and cached[0] == record.identity:
                return cached[1]
            events = _DesignLogGitEvents(self._publish_git_diagnostic())

            def changes(base: str, head: str) -> tuple[WorkspaceChange, ...] | None:
                try:
                    return record.workspace_changes(base, head)
                except RunRecordReadError as error:
                    events.warning(error.summary, detail=error.detail)
                    return None

            def patch(base: str, head: str, paths: tuple[str, ...]) -> str | None:
                try:
                    return record.workspace_patch(base, head, paths)
                except RunRecordReadError as error:
                    events.warning(error.summary, detail=error.detail)
                    return None

            design = DesignLog(
                changes=changes,
                patch=patch,
            )
            self._design = (record.identity, design)
            return design

    def _publish_git_diagnostic(self) -> Callable[[str], None]:
        """Return a sink that journals each distinct git failure once.

        A failing checkpoint range is retried on every refresh, so without
        deduplication one unreadable round would repeat its line for every
        round of every refresh. Distinct messages stay bounded because the
        set is capped; past the cap the sink stops, having already reported
        every distinct fault it saw.
        """
        reported: set[str] = set()
        limit = 32

        def publish(message: str) -> None:
            if message in reported or len(reported) >= limit:
                return
            reported.add(message)
            self._journal.publish_output("stderr", message, source="design-log")

        return publish

    def wait_for_events(
        self,
        after_sequence: int,
        timeout: float | None = None,
        before_sequence: int | None = None,
        *,
        expected_store_id: str | None = None,
    ) -> list[RunEvent]:
        """Wait for and atomically read events from the expected sequence space."""
        if expected_store_id is None:
            # Preserve the original-store behavior for standalone long-poll
            # callers. A replacement wakes the retired store and this returns
            # that store's final range rather than switching sequence spaces.
            return self._journal.wait_for_events(after_sequence, timeout, before_sequence)
        with self._condition:
            self._require_event_store_locked(expected_store_id)
            initial_store_id = self._journal.store_id_locked()
            # Before attachment there is no durable store to wait on. Preserve
            # the historical immediate empty answer, including for the empty
            # pre-attachment fence.
            if initial_store_id == "":
                return self._fenced_events_locked(
                    expected_store_id, after_sequence, before_sequence
                )
            self._condition.wait_for(
                lambda: (
                    self._journal.store_id_locked() != initial_store_id
                    or self._journal.latest_sequence_locked() > after_sequence
                ),
                timeout,
            )
            return self._fenced_events_locked(expected_store_id, after_sequence, before_sequence)

    def _fenced_events_locked(
        self,
        expected_store_id: str,
        after_sequence: int,
        before_sequence: int | None,
    ) -> list[RunEvent]:
        """Read under the journal condition after enforcing one store fence."""
        events = self._journal.read_fenced_locked(
            expected_store_id, after_sequence, before_sequence
        )
        if events is None:
            raise EventsStoreReplacedError
        return events

    def _require_event_store_locked(self, expected_store_id: str | None) -> None:
        """Reject a cursor whose named event-store generation is gone."""
        if expected_store_id is not None and self._journal.store_id_locked() != expected_store_id:
            raise EventsStoreReplacedError

    def wait_for_change(self, after_sequence: int, timeout: float | None = None) -> bool:
        """Wait until the journal advances beyond the supplied sequence."""
        return self._journal.wait_for_change(after_sequence, timeout)

    @property
    def latest_sequence(self) -> int:
        """Return the latest published journal sequence."""
        return self._journal.latest_sequence

    def _objective_description(self) -> str | None:
        record = self._controller.attached_run
        if record is None:
            return None
        text = record.facts().effective_objective
        return summarize_objective(text) if text is not None else None

    def _query_experiments(
        self,
        record: RunRecord,
        request: ExperimentQuery,
    ) -> ExperimentQueryResult | None:
        """Answer from memory, loading once outside projection locks if needed.

        `ExperimentProjection` consumes `vibesys.api`'s `RunView`.
        `observe_committed_state` below feeds it a `RunView` projected
        in-memory from the state object the run loop just committed, with no
        filesystem read (see `test_service_projects_committed_live_state_
        without_reloading_history` in `tests/server/test_experiments.py`, which
        asserts zero `AgentRunStateStore.load_optional` calls on that path).
        An authoritative ``RunRecord.view`` reload here re-reads durable state;
        that is expected since this branch runs only on a cache miss or race.
        """
        while True:
            projection_id = record.identity
            cached = self._experiment_projection.query(
                record.run_id,
                projection_id,
                request.after,
            )
            if isinstance(cached, ExperimentQueryResult):
                return cached
            view = record.view()
            if agent_projection(view) is None:
                return None
            current = self._controller.attached_run
            if current is not None and current.identity == record.identity:
                installed = self._experiment_projection.install_loaded(
                    record.run_id,
                    projection_id,
                    view,
                    cached,
                )
                if installed is not None:
                    return installed
                continue
            if current is None:
                return None
            record = current

    def observe_committed_state(self, view: RunView, changed_keys: tuple[str, ...] | None) -> None:
        """Incrementally project a state object immediately after its commit.

        Registered as this run's `RunSession.on_committed_view` listener (see
        `server.runtime.ServerRuntime.drive`): the session already filtered
        to `namespace == "agent"` and projected *view* in memory before
        calling this, so there is no `namespace`/raw-state handling left to
        do here.
        """
        record = self._controller.attached_run
        projection = agent_projection(view)
        if record is None or projection is None:
            return
        self._measured = build_performance_context(view, objectives=projection.objectives)
        self._experiment_projection.update(
            record.run_id,
            record.identity,
            view,
            changed_keys=changed_keys,
        )

    def _observe_run_started(self, event: RunEvent) -> None:
        """Keep the overview the run's ``run_started`` event declared (live and on replay)."""
        data = event.data
        if isinstance(data, RunStartedData) and data.overview is not None:
            self._overview = data.overview

    def _current_overview(self) -> RunOverview | None:
        """The declared overview, with the unit and direction the run's measurements add."""
        overview, measured = self._overview, self._measured
        if overview is None or measured is None:
            return overview
        objective = overview.objective
        return overview.model_copy(
            update={
                "objective": objective.model_copy(
                    update={
                        "metric": objective.metric or measured.objective_metric,
                        "unit": measured.objective_unit or objective.unit,
                        "direction": measured.objective_direction or objective.direction,
                    }
                )
            }
        )

    # Keep the previous internal spelling for existing server-side observers.
    _observe_committed_state = observe_committed_state

    def _observe_experiment_change(self, event: RunEvent) -> None:
        data = event.data
        if event.type is not EventType.EXPERIMENTS_CHANGED or data is None:
            return
        if data.kind == "experiments_changed":
            record = self._controller.attached_run
            if record is None or record.run_id != event.run_id:
                return
            self._experiment_projection.invalidate(
                event.run_id,
                record.identity,
                data.revision,
            )
