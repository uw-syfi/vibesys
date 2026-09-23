"""Transport-neutral request API for frontend clients."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from server.api.design import DesignLog
from server.api.experiments import (
    ExperimentProjection,
    ExperimentQueryResult,
    build_experiment_log,
)
from server.api.performance import (
    build_performance_context,
    summarize_objective,
)
from server.api.protocol import (
    ChatOptionsQuery,
    ChatQuery,
    ChatResult,
    ChatThreadCreateQuery,
    ChatThreadInfo,
    CommandAck,
    DesignPatch,
    DesignPatchQuery,
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
    StopCommand,
    TuiDefaultsQuery,
)
from server.api.workspace_git import WorkspacePatchReader
from server.chat.options import ChatOptions, build_chat_options
from server.events import EventType, RunEvent
from vibesys.api import open_run_store
from vs_project.api import (
    AgentRunConfiguration,
    GitTracker,
    NullGitTrackerEvents,
    ProjectStateError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.chat.manager import ChatManager
    from server.controller import ProjectRunState, RunController
    from server.execution import ActiveAgentExecution, ExecutionTracker
    from server.integration import RunIntegrationAdapter
    from server.journal import EventJournal
    from server.settings import InteractiveSetupDefaults
    from vibesys.api import RunControl, RunView
    from vs_project.api import Project


@dataclass(frozen=True)
class SubscriptionBootstrap:
    """One journal state captured atomically for a subscription bootstrap."""

    run_id: str
    store_id: str
    floor: int
    through_sequence: int
    events: list[RunEvent]
    active_executions: list[ActiveAgentExecution]


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


class _DesignLogGitEvents(NullGitTrackerEvents):
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

    def __init__(  # noqa: PLR0913  # Explicit dependencies define the API boundary.
        self,
        condition: threading.Condition,
        controller: RunController,
        executions: ExecutionTracker,
        journal: EventJournal,
        chat: ChatManager,
        integration: RunIntegrationAdapter,
        *,
        session_provider: Callable[[], RunControl | None],
        tui_defaults: Callable[[], InteractiveSetupDefaults] | None = None,
    ) -> None:
        """Initialize the API with the components that own each request surface."""
        self._condition = condition
        self._controller = controller
        self._executions = executions
        self._journal = journal
        self._chat = chat
        self._integration = integration
        self._session_provider = session_provider
        self._tui_defaults_provider = tui_defaults
        self._tui_defaults: InteractiveSetupDefaults | None = None
        self._tui_defaults_lock = threading.Lock()
        # Keyed by the attached run so the projection's diff cache survives
        # across requests but never outlives the run it was built for.
        self._design: tuple[tuple[Path, str], DesignLog] | None = None
        self._design_lock = threading.Lock()
        self._experiment_projection = ExperimentProjection()
        self._experiment_run_kind: tuple[tuple[Path, str], bool] | None = None
        self._experiment_run_kind_lock = threading.Lock()
        self._journal.add_listener(
            self._observe_experiment_change,
            replay_filter=lambda _header: False,
        )

    def execute(self, request: ProtocolRequest) -> Response:  # noqa: C901, PLR0911
        """Execute one typed request and return its protocol response."""
        if isinstance(request, (PauseCommand, ResumeCommand, SteerCommand, StopCommand)):
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
            project_run = self._controller.project_run
            ready = project_run is not None
            result = (
                self._query_experiments(project_run, request) if project_run is not None else None
            )
            return Response(
                request_id=request.request_id,
                experiments=result.entries if result is not None else [],
                experiment_update=result.update if result is not None else None,
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
        if isinstance(request, DesignPatchQuery):
            # Deliberately not journaled as a STATUS_QUERY: a diff viewer
            # issues one of these per file navigated, and that cadence would
            # spam the run journal without recording anything about the run.
            return Response(
                request_id=request.request_id,
                design_patch=self.design_patch(request.base, request.head, request.path),
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

    def _execute_command(
        self, request: PauseCommand | ResumeCommand | SteerCommand | StopCommand
    ) -> Response:
        """Send a control message to the live run's session, if one is running.

        Routes through `self._session_provider()` (a `vibesys.api.RunControl`)
        rather than a channel this class owns: once a session's
        `vibesys.run.run_control.RunControlChannel` is private to that
        session (`vibesys.context._RunContext.invoke` reads it at each
        invocation boundary; the controller's status/journal mirror its
        events, see `server.integration.RunIntegrationAdapter
        ._project_control_event`), there is no run-scoped channel to hold
        onto between requests. No run live is a graceful no-op: the command
        still acknowledges, there is just nothing to steer.
        """
        control = self._session_provider()
        if isinstance(request, PauseCommand):
            if control is not None:
                control.pause()
            ack = CommandAck(action="pause", status="pending")
        elif isinstance(request, ResumeCommand):
            if control is not None:
                control.resume()
            ack = CommandAck(action="resume", status="consumed")
        elif isinstance(request, StopCommand):
            if control is not None:
                control.stop()
            ack = CommandAck(action="stop", status="pending")
        else:
            if control is not None:
                control.steer(request.text)
            ack = CommandAck(action="steer", status="pending")
        return Response(request_id=request.request_id, ack=ack)

    def _execute_chat(self, request: ChatQuery) -> Response:
        answer, event = self._chat.chat_with_event(request.text, thread_id=request.thread_id)
        return Response(
            request_id=request.request_id,
            chat=ChatResult(question=request.text, answer=answer, thread_id=request.thread_id),
            events=[] if event is None else [event],
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
        fold. An empty or matching id resumes the cursor as before, which is
        also what a fresh dial (no store seen yet) and old clients get.
        """
        with self._condition:
            if store_id and store_id != self._journal.store_id_locked():
                after_sequence = 0
            latest = self._journal.latest_sequence_locked()
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

    def events(self, after_sequence: int = 0, before_sequence: int | None = None) -> list[RunEvent]:
        """Read journal events within the requested sequence bounds."""
        return self._journal.read(after_sequence, before_sequence)

    def history_events(self) -> list[RunEvent]:
        """Read the canonical event history used by frontend clients."""
        return self._journal.read_history()

    def performance_rounds(self) -> list[PerformanceRound]:
        """Build the recorded round-level performance series.

        Sourced from `vibesys.api`'s `RunView.rounds`: every field this DTO
        needs (`round_number`, `perf_metric`, `perf_unit`, `passed`,
        `profile_skipped`) is copied verbatim from the run's own round
        history, not re-derived from core state.
        """
        project_run = self._controller.project_run
        if project_run is None:
            return []
        run_view = open_run_store(project_run.project).get_run(project_run.run_id)
        return [
            PerformanceRound(
                round=record.round_number,
                perf_metric=record.perf_metric,
                perf_unit=record.perf_unit,
                passed=record.passed,
                profile_skipped=record.profile_skipped,
            )
            for record in run_view.rounds
            if record.perf_metric is not None and record.perf_unit is not None
        ]

    def performance_context(self) -> PerformanceContext | None:
        """Build objective and measurement context for performance rendering.

        Sourced from `vibesys.api`'s `RunView`: `HypothesisView.perf_metric_round`
        carries the round that produced each hypothesis's headline measurement,
        so `build_performance_context` can select the newest one without a
        core-private `HypothesisMeasurement`.
        """
        project_run = self._controller.project_run
        if project_run is None:
            return None
        manifest = project_run.project.state.load_run(project_run.run_id)
        if not isinstance(manifest.configuration, AgentRunConfiguration):
            return None
        run_view = open_run_store(project_run.project).get_run(project_run.run_id)
        return build_performance_context(
            run_view,
            objectives=manifest.configuration.objectives,
            objective_description=self._objective_description(),
        )

    def experiments(self) -> list[HypothesisEntry]:
        """Build the experiment log for an agent outer loop."""
        project_run = self._controller.project_run
        if project_run is None:
            return []
        run_view = open_run_store(project_run.project).get_run(project_run.run_id)
        return build_experiment_log(run_view)

    def design_rounds(self) -> list[DesignRound]:
        """Project the per-round design log for the attached run.

        Facts (hypothesis/round commits) come from `vibesys.api`'s
        `RunView`; the design log itself only adds what a `RunView` does not
        carry, the workspace's own git history.
        """
        project_run = self._controller.project_run
        if project_run is None:
            return []
        run_view = open_run_store(project_run.project).get_run(project_run.run_id)
        design = self._design_log(project_run.project.root, project_run.run_id)
        manifest = project_run.project.state.load_run(project_run.run_id)
        return design.rounds(run_view, baseline=manifest.trusted_input_baseline)

    def design_patch(self, base: str, head: str, path: str) -> DesignPatch | None:
        """Read one file's patch for a published design range, None unattached."""
        project_run = self._controller.project_run
        if project_run is None:
            return None
        design = self._design_log(project_run.project.root, project_run.run_id)
        return design.patch(base, head, path)

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
            events = _DesignLogGitEvents(self._publish_git_diagnostic())
            tracker = GitTracker(workspace, run_id=run_id, events=events)
            reader = WorkspacePatchReader(workspace, warning=events.warning)
            design = DesignLog(
                workspace=workspace,
                diff=tracker.diff_name_status,
                patch=reader.diff_patch,
            )
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

    def _query_experiments(
        self,
        project_run: ProjectRunState,
        request: ExperimentQuery,
    ) -> ExperimentQueryResult | None:
        """Answer from memory, loading once outside projection locks if needed.

        `ExperimentProjection` consumes `vibesys.api`'s `RunView`.
        `_observe_committed_state` below feeds it a `RunView` projected
        in-memory from the state object the run loop just committed, with no
        filesystem read (see `test_service_projects_committed_live_state_
        without_reloading_history` in `tests/server/test_experiments.py`, which
        asserts zero `AgentRunStateStore.load_optional` calls on that path).
        An authoritative reload here, by contrast, goes through
        `vibesys.api.open_run_store`, which always re-reads from disk; that
        is expected since this branch only runs once per cache miss or race.
        """
        while self._is_agent_run(project_run.project, project_run.run_id):
            projection_id = self._experiment_projection_id(project_run)
            cached = self._experiment_projection.query(
                project_run.run_id,
                projection_id,
                request.after,
            )
            if isinstance(cached, ExperimentQueryResult):
                return cached
            view = open_run_store(project_run.project).get_run(project_run.run_id)
            current = self._controller.project_run
            if current is not None and self._same_project_run(current, project_run):
                installed = self._experiment_projection.install_loaded(
                    project_run.run_id,
                    projection_id,
                    view,
                    cached,
                )
                if installed is not None:
                    return installed
                continue
            if current is None:
                return None
            project_run = current
        return None

    def _is_agent_run(self, project: Project, run_id: str) -> bool:
        key = (project.root, run_id)
        with self._experiment_run_kind_lock:
            cached = self._experiment_run_kind
            if cached is not None and cached[0] == key:
                return cached[1]
        is_agent = project.state.load_run(run_id).configuration.outer_loop == "agent"
        with self._experiment_run_kind_lock:
            self._experiment_run_kind = (key, is_agent)
        return is_agent

    @staticmethod
    def _same_project_run(left: ProjectRunState, right: ProjectRunState) -> bool:
        return left.project.root == right.project.root and left.run_id == right.run_id

    @staticmethod
    def _experiment_projection_id(project_run: ProjectRunState) -> str:
        identity = f"{project_run.project.root.resolve()}\0{project_run.run_id}"
        return sha256(identity.encode()).hexdigest()[:16]

    def _observe_committed_state(self, view: RunView, changed_keys: tuple[str, ...] | None) -> None:
        """Incrementally project a state object immediately after its commit.

        Registered as this run's `RunSession.on_committed_view` listener (see
        `server.runtime.ServerRuntime.drive`): the session already filtered
        to `namespace == "agent"` and projected *view* in memory before
        calling this, so there is no `namespace`/raw-state handling left to
        do here.
        """
        project_run = self._controller.project_run
        if project_run is None:
            return
        self._experiment_projection.update(
            project_run.run_id,
            self._experiment_projection_id(project_run),
            view,
            changed_keys=changed_keys,
        )

    def _observe_experiment_change(self, event: RunEvent) -> None:
        data = event.data
        if event.type is not EventType.EXPERIMENTS_CHANGED or data is None:
            return
        if data.kind == "experiments_changed":
            project_run = self._controller.project_run
            if project_run is None or project_run.run_id != event.run_id:
                return
            projection_id = self._experiment_projection_id(project_run)
            self._experiment_projection.invalidate(
                event.run_id,
                projection_id,
                data.revision,
            )
