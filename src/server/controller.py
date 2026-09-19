"""Run lifecycle and human-control coordination."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any

from server.diagnostics import Diagnostic, DiagnosticScope, DiagnosticSeverity
from server.events import EventStatus, EventType, RunStatusChangedData
from server.run_lifecycle import RunStatus, RunTrigger, transition

if TYPE_CHECKING:
    import threading

    from server.execution import ExecutionHandle, ExecutionTracker
    from server.journal import EventJournal
    from vs_project import Project, StateSnapshot


class RunStopped(BaseException):
    """The operator's ``/stop`` landed: unwind the run callable.

    Raised on the run's own thread at the invocation boundary where the stop
    lands, after the controller has already reached ``STOPPED`` and journaled
    the terminal status change. Derives from ``BaseException``, like
    ``KeyboardInterrupt``, so an ``except Exception`` inside loop code cannot
    absorb the unwind on its way to :meth:`server.runtime.ServerRuntime.run`.
    """


@dataclass(frozen=True)
class ProjectRunState:
    """Typed access to one attached canonical project run."""

    project: Project
    run_id: str

    def history_snapshots(self) -> tuple[StateSnapshot, ...]:
        """Return portable history snapshots relevant to frontend queries."""
        return tuple(
            self.project.state.portable_namespace(self.run_id, namespace).snapshot()
            for namespace in ("agent", "plain", "evolve")
        )


class RunController:
    """Coordinate run state, pause boundaries, steering, and the ended state."""

    def __init__(
        self,
        condition: threading.Condition,
        journal: EventJournal,
        executions: ExecutionTracker,
    ) -> None:
        """Initialize control state over the shared server condition."""
        self._condition = condition
        self._journal = journal
        self._executions = executions
        self._pending_steer: list[str] = []
        self._status: RunStatus = RunStatus.STARTING
        self._project_run: ProjectRunState | None = None

    @property
    def project_run(self) -> ProjectRunState | None:
        """Return the canonical project run attached to this controller."""
        with self._condition:
            return self._project_run

    @property
    def current_round(self) -> str | None:
        """Return the current controlled execution round."""
        return self._executions.current_round

    def attach(
        self,
        log_dir: Path,
        *,
        project: Project | None = None,
        run_id: str | None = None,
    ) -> None:
        """Attach durable run storage and optional canonical project state."""
        if project is not None and run_id is None:
            raise ValueError("run_id is required when project is provided")  # noqa: TRY003
        with self._condition:
            if project is not None and run_id is not None:
                self._project_run = ProjectRunState(project, run_id)
            self._journal.attach(log_dir, run_id=run_id)
            self._apply_locked(RunTrigger.ATTACHED)

    def _apply_locked(
        self,
        trigger: RunTrigger,
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        execution_id: str | None = None,
    ) -> RunStatus:
        """Apply one lifecycle trigger and publish the change it caused.

        The caller must already hold ``self._condition``. Mutating the status
        and recording its event under the same lock is what keeps event order
        and state order the same: a snapshot taken at any sequence agrees with
        the fold of the events up to that sequence. A trigger the current
        status absorbs changes nothing and publishes nothing.
        """
        previous = self._status
        current = transition(previous, trigger)
        if current is previous:
            return current
        self._status = current
        self._journal.record(
            EventType.RUN_STATUS_CHANGED,
            data=RunStatusChangedData(status=current, previous=previous),
            agent_kind=agent_kind,
            round_label=round_label,
            execution_id=execution_id,
        )
        self._condition.notify_all()
        return current

    def pause_after_call(self) -> None:
        """Request a pause after the current controlled invocation finishes."""
        with self._condition:
            self._apply_locked(RunTrigger.PAUSE_REQUESTED)
            self._journal.record(EventType.CONTROL, "/pause", status=EventStatus.PENDING)

    def stop_after_call(self) -> None:
        """Request a stop at the next controlled invocation boundary."""
        with self._condition:
            self._apply_locked(RunTrigger.STOP_REQUESTED)
            self._journal.record(EventType.CONTROL, "/stop", status=EventStatus.PENDING)

    def resume(self) -> None:
        """Resume controlled invocations and clear a pending pause or stop."""
        with self._condition:
            self._apply_locked(RunTrigger.RESUMED)
            self._journal.record(EventType.CONTROL, "/resume", status=EventStatus.CONSUMED)

    def steer(self, text: str) -> None:
        """Queue operator guidance for the next controlled invocation."""
        with self._condition:
            self._pending_steer.append(text)
            self._journal.record(EventType.CONTROL, f"/steer: {text}", status=EventStatus.PENDING)

    def start_agent_execution(  # noqa: PLR0913
        self,
        kind: str,
        round_label: str,
        user_prompt: str,
        system_prompt: str = "",
        *,
        consume_steering: bool = True,
        participates_in_run_control: bool = True,
        emit_lifecycle: bool = True,
        driver: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> ExecutionHandle:
        """Enter one invocation boundary, applying pause, stop, and steering state."""
        with self._condition:
            while participates_in_run_control and self._status is RunStatus.PAUSED:
                self._condition.wait()
            if participates_in_run_control:
                self._land_stop_at_entry_locked()
            steering = (
                self._pending_steer if consume_steering and participates_in_run_control else []
            )
            if consume_steering and participates_in_run_control:
                self._pending_steer = []
            effective_prompt = _with_steering(user_prompt, steering)
            execution = self._executions.start_locked(
                kind,
                round_label,
                effective_prompt,
                system_prompt,
                participates_in_run_control=participates_in_run_control,
                emit_lifecycle=emit_lifecycle,
                driver=driver,
                provider=provider,
                model=model,
            )
            if steering:
                self._journal.record(
                    EventType.CONTROL,
                    "/steer",
                    status=EventStatus.CONSUMED,
                    agent_kind=kind,
                    round_label=round_label,
                    execution_id=execution.execution_id,
                )
            return execution

    def before_agent(
        self, kind: str, round_label: str, user_prompt: str, system_prompt: str = ""
    ) -> str:
        """Compatibility boundary returning only the effective prompt."""
        execution = self.start_agent_execution(kind, round_label, user_prompt, system_prompt)
        self._executions.remember_legacy(execution.execution_id)
        return execution.user_prompt

    def after_agent(
        self,
        kind: str,
        round_label: str,
        *,
        result: Any = None,  # noqa: ANN401
        error: BaseException | None = None,
        execution_id: str | None = None,
    ) -> None:
        """Finish an invocation and apply any pending pause transition."""
        del kind, round_label
        resolved_id = self._executions.resolve_legacy(execution_id)
        if resolved_id is None:
            # A finish with no execution to match: the compatibility boundary
            # was never entered on this thread. It is still an invocation
            # boundary, so a pending pause or stop lands here like anywhere else.
            with self._condition:
                self._reach_invocation_boundary_locked()
            return
        with self._condition:
            active, controlled = self._executions.finish_locked(
                resolved_id, result=result, error=error
            )
            if active is None:
                return
            if controlled:
                self._reach_invocation_boundary_locked(
                    agent_kind=active.agent_kind,
                    round_label=active.round_label,
                    execution_id=resolved_id,
                )
        self._executions.clear_legacy(resolved_id)

    def _reach_invocation_boundary_locked(
        self,
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        execution_id: str | None = None,
    ) -> None:
        """Apply the invocation boundary, landing a pending pause or stop.

        A stop that lands here does not raise: ``after_agent`` runs while the
        invocation is still finalizing, so the unwind is deferred to the next
        ``start_agent_execution`` entry, which finds ``STOPPED`` and raises.
        """
        reached = self._apply_locked(
            RunTrigger.INVOCATION_FINISHED,
            agent_kind=agent_kind,
            round_label=round_label,
            execution_id=execution_id,
        )
        if reached not in (RunStatus.PAUSED, RunStatus.STOPPED):
            return
        self._journal.record(
            EventType.CONTROL,
            "/pause" if reached is RunStatus.PAUSED else "/stop",
            status=EventStatus.CONSUMED,
            agent_kind=agent_kind,
            round_label=round_label,
            execution_id=execution_id,
        )

    def _land_stop_at_entry_locked(self) -> None:
        """Land a pending stop at the entry side of the invocation boundary.

        A stop requested while the run was paused, or between invocations,
        has no controlled call in flight to finish, so the entry to the next
        one is the boundary where it lands: no further agent call starts.
        Raising :class:`RunStopped` on the run's own thread is what unwinds
        the run callable to ``ServerRuntime.run`` without touching loop code.
        """
        if self._status is RunStatus.STOPPING:
            self._apply_locked(RunTrigger.INVOCATION_FINISHED)
            self._journal.record(EventType.CONTROL, "/stop", status=EventStatus.CONSUMED)
        if self._status is RunStatus.STOPPED:
            raise RunStopped

    def status(self) -> str:
        """Return a compact human-readable run status."""
        with self._condition:
            state = self.status_locked()
            kind, round_label = self._executions.current_locked()
        return f"{state} · {kind or 'starting'} · {round_label or 'no round yet'}"

    def status_locked(self) -> RunStatus:
        """Return run status while the caller holds the shared condition."""
        return self._status

    def run_status(self) -> RunStatus:
        """Return the run status token, including whether the run has ended."""
        with self._condition:
            return self._status

    def settle(self, trigger: RunTrigger) -> bool:
        """End the run without a terminal event, reporting whether it did.

        The projection of a core terminal event calls this before appending
        that event, so the status change is ordered ahead of it: a snapshot at
        any sequence that contains the terminal event already reports an ended
        status. ``finish`` then finds the run ended and records nothing twice.
        """
        with self._condition:
            return self._settle_locked(trigger)

    def _settle_locked(self, trigger: RunTrigger) -> bool:
        """End the run exactly once, interrupting controlled work first."""
        if self._status.has_ended:
            return False
        self._executions.interrupt_controlled_locked()
        # An ended status is not PAUSED, so ending a paused run releases the
        # thread parked at the pause wait instead of leaving it to re-wait.
        self._apply_locked(trigger)
        return True

    def finish(
        self,
        error: BaseException | None = None,
        *,
        record_event: bool = True,
        diagnostic: Diagnostic | None = None,
    ) -> None:
        """Transition the run to its ended state exactly once."""
        if not self.settle(RunTrigger.FAILED if error else RunTrigger.COMPLETED):
            return
        event_diagnostic = diagnostic
        if error is not None and event_diagnostic is not None:
            event_diagnostic = event_diagnostic.model_copy(
                update={"severity": DiagnosticSeverity.FATAL}
            )
        try:
            if not record_event:
                return
            if error is not None and event_diagnostic is None:
                self._journal.record_terminal_failure(
                    EventType.RUN_FAILED,
                    error,
                    scope=DiagnosticScope.RUN,
                    operation="Run",
                    severity=DiagnosticSeverity.FATAL,
                )
                return
            self._journal.record(
                EventType.RUN_FAILED if error else EventType.RUN_FINISHED,
                event_diagnostic.summary if event_diagnostic else "",
                status=EventStatus.FAILED if error else EventStatus.COMPLETED,
                diagnostic=event_diagnostic,
            )
        finally:
            self._journal.clear_diagnostics()


def _with_steering(user_prompt: str, messages: list[str]) -> str:
    if not messages:
        return user_prompt
    block = "\n".join(f"- {message}" for message in messages)
    return (
        f"{user_prompt.rstrip()}\n\n"
        "## Operator steering (live)\n\n"
        "The operator sent the following instruction(s) for this invocation. "
        "Treat them as high-priority guidance for the work you do now:\n\n"
        f"{block}\n"
    )
