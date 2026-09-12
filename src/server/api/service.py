"""Transport-neutral request API for frontend clients."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.api.design import DesignLog
from server.api.experiments import build_experiment_log
from server.api.performance import (
    build_performance_context,
    metric_directions,
    summarize_objective,
)
from server.api.protocol import (
    ChatOptionsQuery,
    ChatQuery,
    ChatResult,
    ChatThreadCreateQuery,
    ChatThreadInfo,
    CommandAck,
    DesignQuery,
    DesignRound,
    EventsQuery,
    ExperimentQuery,
    HistoryQuery,
    HypothesisEntry,
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
    TuiDefaultsQuery,
)
from server.chat.options import ChatOptions, build_chat_options
from server.events import EventType, RunEvent
from vibesys.loops.agent.hypotheses import reproject_run_evidence
from vibesys.loops.agent.state import AgentRunStateStore
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.run.git_events import NullGitTrackerEvents
from vibesys.run.git_tracker import GitTracker
from vs_project import ProjectStateError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.chat.manager import ChatManager
    from server.controller import RunController
    from server.execution import ActiveAgentExecution, ExecutionTracker
    from server.integration import RunIntegrationAdapter
    from server.journal import EventJournal
    from server.settings import InteractiveSetupDefaults
    from vibesys.loops.agent.model import AgentRunState


@dataclass(frozen=True)
class SubscriptionBootstrap:
    """One journal state captured atomically for a subscription bootstrap."""

    run_id: str
    floor: int
    through_sequence: int
    events: list[RunEvent]
    active_executions: list[ActiveAgentExecution]


class _DesignLogGitEvents(NullGitTrackerEvents):
    """Forward design-projection tracker warnings to a journal sink.

    The design tracker is read-only (``diff_name_status`` only), so the
    snapshot observations never fire and inherit the null no-ops. Warnings
    are formatted here, at the wiring layer, into the tagged text the run
    journal shows.
    """

    def __init__(self, publish: Callable[[str], None]) -> None:
        self._publish = publish

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        """Publish one tagged line per tracker fault."""
        message = summary if detail is None else f"{summary}: {detail}"
        self._publish(f"[git-tracking] {message}")


class RunApi:
    """Authoritative request API consumed by frontend clients."""

    def __init__(  # noqa: PLR0913  # Explicit dependencies define the API boundary.
        self,
        condition: threading.Condition,
        controller: RunController,
        executions: ExecutionTracker,
        journal: EventJournal,
        chat: ChatManager,
        integration: RunIntegrationAdapter,
        *,
        tui_defaults: Callable[[], InteractiveSetupDefaults] | None = None,
    ) -> None:
        """Initialize the API with the components that own each request surface."""
        self._condition = condition
        self._controller = controller
        self._executions = executions
        self._journal = journal
        self._chat = chat
        self._integration = integration
        self._tui_defaults_provider = tui_defaults
        self._tui_defaults: InteractiveSetupDefaults | None = None
        self._tui_defaults_lock = threading.Lock()
        # Keyed by the attached run so the projection's diff cache survives
        # across requests but never outlives the run it was built for.
        self._design: tuple[tuple[Path, str], DesignLog] | None = None
        self._design_lock = threading.Lock()

    def execute(self, request: ProtocolRequest) -> Response:  # noqa: C901, PLR0911
        """Execute one typed request and return its protocol response."""
        if isinstance(request, (PauseCommand, ResumeCommand, SteerCommand)):
            return self._execute_command(request)
        if isinstance(request, ChatQuery):
            return self._execute_chat(request)
        if isinstance(request, ChatThreadCreateQuery):
            return self._execute_chat_thread_create(request)
        if isinstance(request, ChatOptionsQuery):
            return Response(request_id=request.request_id, chat_options=self.chat_options())
        if isinstance(request, TuiDefaultsQuery):
            return Response(request_id=request.request_id, tui_defaults=self.tui_defaults())
        if isinstance(request, HistoryQuery):
            self._journal.record(EventType.STATUS_QUERY, "/history")
            return Response(request_id=request.request_id, events=self.history_events())
        if isinstance(request, PerformanceQuery):
            self._journal.record(EventType.STATUS_QUERY, "/perf")
            return Response(
                request_id=request.request_id,
                performance=self.performance_rounds(),
                performance_context=self.performance_context(),
            )
        if isinstance(request, ExperimentQuery):
            self._journal.record(EventType.STATUS_QUERY, "/experiments")
            ready = self._controller.project_run is not None
            return Response(
                request_id=request.request_id,
                experiments=self.experiments() if ready else [],
                experiments_ready=ready,
            )
        if isinstance(request, DesignQuery):
            self._journal.record(EventType.STATUS_QUERY, "/design")
            ready = self._controller.project_run is not None
            return Response(
                request_id=request.request_id,
                design=self.design_rounds() if ready else [],
                design_ready=ready,
            )
        if isinstance(request, SnapshotQuery):
            return Response(request_id=request.request_id, snapshot=self.snapshot())
        if isinstance(request, EventsQuery):
            timeout = request.timeout_ms / 1000 if request.timeout_ms else None
            events = (
                self.wait_for_events(request.after_sequence, timeout, request.before_sequence)
                if timeout is not None
                else self.events(request.after_sequence, request.before_sequence)
            )
            return Response(request_id=request.request_id, events=events)
        raise TypeError(  # noqa: TRY003  # Include the invalid protocol model in the error.
            f"Unsupported protocol request: {type(request).__name__}"
        )

    def _execute_command(self, request: PauseCommand | ResumeCommand | SteerCommand) -> Response:
        if isinstance(request, PauseCommand):
            self._controller.pause_after_call()
            ack = CommandAck(action="pause", status="pending")
        elif isinstance(request, ResumeCommand):
            self._controller.resume()
            ack = CommandAck(action="resume", status="consumed")
        else:
            self._controller.steer(request.text)
            ack = CommandAck(action="steer", status="pending")
        return Response(request_id=request.request_id, ack=ack)

    def _execute_chat(self, request: ChatQuery) -> Response:
        sequence = self._journal.latest_sequence
        answer = self._chat.chat(request.text, thread_id=request.thread_id)
        return Response(
            request_id=request.request_id,
            chat=ChatResult(question=request.text, answer=answer, thread_id=request.thread_id),
            events=self._journal.read(sequence),
        )

    def _execute_chat_thread_create(self, request: ChatThreadCreateQuery) -> Response:
        sequence = self._journal.latest_sequence
        spec = self._chat.create_thread(
            driver=request.driver,
            provider=request.provider,
            model=request.model,
            title=request.title,
        )
        return Response(
            request_id=request.request_id,
            chat_thread=ChatThreadInfo(
                thread_id=spec.thread_id,
                title=spec.title,
                driver=spec.driver,
                provider=spec.provider,
                model=spec.model,
            ),
            events=self._journal.read(sequence),
        )

    def chat_options(self) -> ChatOptions | None:
        """Return the agent choices available for experiment chat."""
        settings = self._chat.run_settings
        return None if settings is None else build_chat_options(settings)

    def tui_defaults(self) -> InteractiveSetupDefaults | None:
        """Load and cache defaults for the interactive setup form."""
        if self._tui_defaults_provider is None:
            return None
        with self._tui_defaults_lock:
            if self._tui_defaults is None:
                self._tui_defaults = self._tui_defaults_provider()
            return self._tui_defaults

    def snapshot(self) -> RunSnapshot:
        """Return a consistent snapshot of run and frontend-facing state."""
        with self._condition:
            kind, round_label = self._executions.current_locked()
            return RunSnapshot(
                run_id=self._journal.run_id_locked(),
                sequence=self._journal.latest_sequence_locked(),
                status=self._controller.status_locked(),
                agent_kind=kind,
                round_label=round_label,
                active_executions=self._executions.active_locked(),
                chat_threads=[
                    ChatThreadInfo(
                        thread_id=spec.thread_id,
                        title=spec.title,
                        driver=spec.driver,
                        provider=spec.provider,
                        model=spec.model,
                    )
                    for spec in self._chat.threads_locked()
                ],
            )

    def subscription_checkpoint(
        self, after_sequence: int, *, bootstrap_spine: bool = False
    ) -> tuple[int, list[RunEvent], list[ActiveAgentExecution]]:
        """Capture events and executions at one subscription sequence boundary."""
        with self._condition:
            through_sequence, events = self._journal.checkpoint_locked(
                after_sequence, bootstrap_spine=bootstrap_spine
            )
            return through_sequence, events, self._executions.active_locked()

    def subscription_bootstrap(
        self, after_sequence: int, tail: int | None
    ) -> SubscriptionBootstrap:
        """Capture one atomic bootstrap state for a new or restarted subscription.

        Appends acquire the same condition, so computing the tail floor and
        reading the checkpoint under one acquisition keeps the replay bounded
        by ``tail`` no matter how many events land during the bootstrap.
        """
        with self._condition:
            latest = self._journal.latest_sequence_locked()
            floor = after_sequence if tail is None else max(after_sequence, latest - tail)
            through_sequence, events = self._journal.checkpoint_locked(
                floor, bootstrap_spine=tail is not None
            )
            return SubscriptionBootstrap(
                run_id=self._journal.run_id_locked(),
                floor=floor,
                through_sequence=through_sequence,
                events=events,
                active_executions=self._executions.active_locked(),
            )

    def events(self, after_sequence: int = 0, before_sequence: int | None = None) -> list[RunEvent]:
        """Read journal events within the requested sequence bounds."""
        return self._journal.read(after_sequence, before_sequence)

    def history_events(self) -> list[RunEvent]:
        """Read the canonical event history used by frontend clients."""
        return self._journal.read_history()

    def performance_rounds(self) -> list[PerformanceRound]:
        """Build the recorded round-level performance series."""
        state = self._agent_run_state()
        if state is None:
            return []
        return [
            PerformanceRound(
                round=record.round_number,
                perf_metric=record.perf_metric,
                perf_unit=record.perf_unit,
                passed=record.passed,
                profile_skipped=record.profile_skipped,
            )
            for record in state.rounds
            if record.perf_metric is not None and record.perf_unit is not None
        ]

    def performance_context(self) -> PerformanceContext | None:
        """Build objective and measurement context for performance rendering."""
        project_run = self._controller.project_run
        if project_run is None:
            return None
        manifest = project_run.project.state.load_run(project_run.run_id)
        if manifest.configuration.outer_loop != "agent":
            return None
        return build_performance_context(
            self._agent_run_state(),
            objectives=manifest.configuration.objectives,
            objective_description=self._objective_description(),
        )

    def experiments(self) -> list[HypothesisEntry]:
        """Build the experiment log for an agent outer loop."""
        state = self._agent_run_state()
        return [] if state is None else build_experiment_log(state)

    def design_rounds(self) -> list[DesignRound]:
        """Project the per-round design log for the attached run."""
        project_run = self._controller.project_run
        state = self._agent_run_state()
        if project_run is None or state is None:
            return []
        design = self._design_log(project_run.project.root, project_run.run_id)
        manifest = project_run.project.state.load_run(project_run.run_id)
        return design.rounds(state, baseline=manifest.trusted_input_baseline)

    def _design_log(self, workspace: Path, run_id: str) -> DesignLog:
        """Return the design projection for one run, building it once.

        The projection caches a git diff per round commit range. Those ranges
        are immutable, so keeping the projection alive across requests is what
        turns a repeated ``query.design`` from one subprocess per round into
        no subprocesses at all. It is rebuilt only if a different run attaches.
        """
        with self._design_lock:
            cached = self._design
            if cached is not None and cached[0] == (workspace, run_id):
                return cached[1]
            tracker = GitTracker(
                workspace,
                run_id=run_id,
                events=_DesignLogGitEvents(self._publish_git_diagnostic()),
            )
            design = DesignLog(workspace=workspace, diff=tracker.diff_name_status)
            self._design = ((workspace, run_id), design)
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
    ) -> list[RunEvent]:
        """Wait for and read events newer than the supplied sequence."""
        return self._journal.wait_for_events(after_sequence, timeout, before_sequence)

    def wait_for_change(self, after_sequence: int, timeout: float | None = None) -> bool:
        """Wait until the journal advances beyond the supplied sequence."""
        return self._journal.wait_for_change(after_sequence, timeout)

    @property
    def latest_sequence(self) -> int:
        """Return the latest published journal sequence."""
        return self._journal.latest_sequence

    def _objective_description(self) -> str | None:
        project_run = self._controller.project_run
        if project_run is None:
            return None
        try:
            runtime = project_run.project.state.portable_namespace(project_run.run_id, "runtime")
            document = runtime.external_directory() / "effective-objective.md"
            if not document.is_file():
                return None
            text = document.read_text(encoding="utf-8")
        except (OSError, ProjectStateError):
            return None
        return summarize_objective(text)

    def _agent_run_state(self) -> AgentRunState | None:
        project_run = self._controller.project_run
        if project_run is None:
            return None
        manifest = project_run.project.state.load_run(project_run.run_id)
        if manifest.configuration.outer_loop != "agent":
            return None
        portable = project_run.project.state.portable_namespace(project_run.run_id, "agent")
        store = AgentRunStateStore(portable)
        state = store.load_optional()
        if state is None:
            from vibesys.run.state import RunStateNamespace  # noqa: PLC0415

            local = project_run.project.state.local_namespace(
                project_run.run_id, RunStateNamespace.AGENT
            )
            # Unified state predating the persisted metric space: the run
            # manifest records the axes but no tolerance, so legacy rounds are
            # ordered exactly, which is what they were ordered by when written.
            return store.migrate_legacy(
                rounds=project_run.project.state.load_rounds(project_run.run_id),
                local_namespace=local,
                legacy_space=MetricSpace(
                    objectives=tuple(
                        Objective(name=name, direction=direction)
                        for name, direction in metric_directions(
                            manifest.configuration.objectives
                        ).items()
                    )
                ),
            )
        # The run's own space and each round's own comparison travel with the
        # state, so the read path needs no measurement configuration of its own.
        return reproject_run_evidence(state)
