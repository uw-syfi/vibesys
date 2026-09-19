"""Transport-neutral request API for frontend clients."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from server.api.design import DesignLog
from server.api.errors import error_response
from server.api.experiments import (
    ExperimentProjection,
    ExperimentQueryResult,
    build_experiment_log,
)
from server.api.performance import (
    build_performance_context,
    metric_directions,
    summarize_objective,
)
from server.api.workspace_git import WorkspacePatchReader
from server.chat.options import build_chat_options
from server.wire import PROTOCOL_VERSION, enums, messages, validate
from server.wire.codec import WireError
from server.wire.v2 import common_pb2, events_pb2, requests_pb2, responses_pb2, snapshot_pb2
from vibesys.loops.agent.hypotheses import reproject_run_evidence
from vibesys.loops.agent.state import AgentRunStateStore
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.run.git_events import NullGitTrackerEvents
from vibesys.run.git_tracker import GitTracker
from vs_project import AgentRunConfiguration, ProjectStateError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel

    from server.chat.manager import ChatManager
    from server.controller import ProjectRunState, RunController
    from server.execution import ExecutionTracker
    from server.integration import RunIntegrationAdapter
    from server.journal import EventJournal
    from vibesys.loops.agent.model import AgentRunState
    from vs_project import Project


@dataclass(frozen=True)
class SubscriptionBootstrap:
    """One journal state captured atomically for a subscription bootstrap."""

    run_id: str
    store_id: str
    floor: int
    through_sequence: int
    events: list[events_pb2.RunEvent]
    active_executions: list[snapshot_pb2.ActiveAgentExecution]


@dataclass(frozen=True)
class SubscriptionCheckpoint:
    """One journal state captured atomically for a live subscription batch.

    ``store_id`` names the store the events came from. A subscription compares
    it against the store it bootstrapped from, so it can never mistake a
    replaced log's sequences for a continuation of its own.
    """

    store_id: str
    through_sequence: int
    events: list[events_pb2.RunEvent]
    active_executions: list[snapshot_pb2.ActiveAgentExecution]


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
        tui_defaults: Callable[[], responses_pb2.TuiDefaults] | None = None,
    ) -> None:
        """Initialize the API with the components that own each request surface."""
        self._condition = condition
        self._controller = controller
        self._executions = executions
        self._journal = journal
        self._chat = chat
        self._integration = integration
        self._tui_defaults_provider = tui_defaults
        self._tui_defaults: responses_pb2.TuiDefaults | None = None
        self._tui_defaults_lock = threading.Lock()
        # Keyed by the attached run so the projection's diff cache survives
        # across requests but never outlives the run it was built for.
        self._design: tuple[tuple[Path, str], DesignLog] | None = None
        self._design_lock = threading.Lock()
        self._experiment_projection = ExperimentProjection()
        self._handlers = self._dispatch_table()
        self._experiment_run_kind: tuple[tuple[Path, str], bool] | None = None
        self._experiment_run_kind_lock = threading.Lock()
        self._journal.add_listener(
            self._observe_experiment_change,
            replay_filter=lambda _header: False,
        )
        self._integration.add_committed_state_listener(self._observe_committed_state)

    def execute(self, request: requests_pb2.Request) -> responses_pb2.Response:
        """Execute one typed request and return its protocol response.

        The response is validated on the way out (finite doubles, no unset
        enums), so a value the contract forbids becomes an error response
        rather than reaching the client.
        """
        body = request.WhichOneof("body")
        handler = self._handlers.get(body) if body is not None else None
        if handler is None:
            raise TypeError(f"Unsupported protocol request body: {body!r}")  # noqa: TRY003  # Names the invalid body.
        response = handler(request)
        try:
            validate.validate_message(response)
        except WireError as error:
            return error_response(request.request_id, error, operation="Response")
        return response

    def _dispatch_table(
        self,
    ) -> dict[str, Callable[[requests_pb2.Request], responses_pb2.Response]]:
        return {
            "pause": self._execute_pause,
            "resume": self._execute_resume,
            "steer": self._execute_steer,
            "stop": self._execute_stop,
            "snapshot": lambda request: _respond(request, snapshot=self.snapshot()),
            "chat": self._execute_chat,
            "chat_thread_create": self._execute_chat_thread_create,
            "chat_options": lambda request: _respond(request, chat_options=self.chat_options()),
            "tui_defaults": lambda request: _respond(request, tui_defaults=self.tui_defaults()),
            "history": self._execute_history,
            "performance": self._execute_performance,
            "experiments": self._execute_experiments,
            "design": self._execute_design,
            "design_patch": self._execute_design_patch,
            "events": self._execute_events,
        }

    def _execute_history(self, request: requests_pb2.Request) -> responses_pb2.Response:
        self._journal.record(events_pb2.EVENT_TYPE_STATUS_QUERY, "/history")
        return _respond(request, events=self.history_events())

    def _execute_performance(self, request: requests_pb2.Request) -> responses_pb2.Response:
        self._journal.record(events_pb2.EVENT_TYPE_STATUS_QUERY, "/perf")
        return _respond(
            request,
            performance=self.performance_rounds(),
            performance_context=self.performance_context(),
        )

    def _execute_experiments(self, request: requests_pb2.Request) -> responses_pb2.Response:
        self._journal.record(events_pb2.EVENT_TYPE_STATUS_QUERY, "/experiments")
        project_run = self._controller.project_run
        result = (
            self._query_experiments(project_run, request.experiments)
            if project_run is not None
            else None
        )
        return _respond(
            request,
            experiments=result.entries if result is not None else [],
            experiment_update=result.update if result is not None else None,
            experiments_ready=project_run is not None,
        )

    def _execute_design(self, request: requests_pb2.Request) -> responses_pb2.Response:
        self._journal.record(events_pb2.EVENT_TYPE_STATUS_QUERY, "/design")
        ready = self._controller.project_run is not None
        return _respond(request, design=self.design_rounds() if ready else [], design_ready=ready)

    def _execute_design_patch(self, request: requests_pb2.Request) -> responses_pb2.Response:
        # Deliberately not journaled as a STATUS_QUERY: a diff viewer
        # issues one of these per file navigated, and that cadence would
        # spam the run journal without recording anything about the run.
        query = request.design_patch
        return _respond(request, design_patch=self.design_patch(query.base, query.head, query.path))

    def _execute_events(self, request: requests_pb2.Request) -> responses_pb2.Response:
        query = request.events
        before = query.before_sequence if query.HasField("before_sequence") else None
        timeout = query.timeout_ms / 1000 if query.timeout_ms else None
        events = (
            self.wait_for_events(query.after_sequence, timeout, before)
            if timeout is not None
            else self.events(query.after_sequence, before)
        )
        return _respond(request, events=events)

    def _execute_pause(self, request: requests_pb2.Request) -> responses_pb2.Response:
        self._controller.pause_after_call()
        return _ack(
            request, responses_pb2.COMMAND_ACTION_PAUSE, responses_pb2.COMMAND_ACK_STATUS_PENDING
        )

    def _execute_resume(self, request: requests_pb2.Request) -> responses_pb2.Response:
        self._controller.resume()
        return _ack(
            request, responses_pb2.COMMAND_ACTION_RESUME, responses_pb2.COMMAND_ACK_STATUS_CONSUMED
        )

    def _execute_stop(self, request: requests_pb2.Request) -> responses_pb2.Response:
        self._controller.stop_after_call()
        return _ack(
            request, responses_pb2.COMMAND_ACTION_STOP, responses_pb2.COMMAND_ACK_STATUS_PENDING
        )

    def _execute_steer(self, request: requests_pb2.Request) -> responses_pb2.Response:
        self._controller.steer(request.steer.text)
        return _ack(
            request, responses_pb2.COMMAND_ACTION_STEER, responses_pb2.COMMAND_ACK_STATUS_PENDING
        )

    def _execute_chat(self, request: requests_pb2.Request) -> responses_pb2.Response:
        query = request.chat
        thread_id = query.thread_id if query.HasField("thread_id") else None
        answer, event = self._chat.chat_with_event(query.text, thread_id=thread_id)
        return _respond(
            request,
            chat=responses_pb2.ChatResult(question=query.text, answer=answer, thread_id=thread_id),
            events=[] if event is None else [event],
        )

    def _execute_chat_thread_create(self, request: requests_pb2.Request) -> responses_pb2.Response:
        query = request.chat_thread_create
        sequence = self._journal.latest_sequence
        spec = self._chat.create_thread(
            driver=(
                enums.text(requests_pb2.ChatDriver, query.driver)
                if query.HasField("driver")
                else None
            ),
            provider=query.provider if query.HasField("provider") else None,
            model=query.model if query.HasField("model") else None,
            title=query.title if query.HasField("title") else None,
        )
        return _respond(
            request,
            chat_thread=_thread_info(spec),
            events=self._journal.read(sequence),
        )

    def chat_options(self) -> responses_pb2.ChatOptions | None:
        """Return the agent choices available for experiment chat."""
        settings = self._chat.run_settings
        return None if settings is None else build_chat_options(settings)

    def tui_defaults(self) -> responses_pb2.TuiDefaults | None:
        """Load and cache defaults for the interactive setup form."""
        if self._tui_defaults_provider is None:
            return None
        with self._tui_defaults_lock:
            if self._tui_defaults is None:
                self._tui_defaults = self._tui_defaults_provider()
            return self._tui_defaults

    def snapshot(self) -> snapshot_pb2.RunSnapshot:
        """Return a consistent snapshot of run and frontend-facing state."""
        with self._condition:
            kind, round_label = self._executions.current_locked()
            return snapshot_pb2.RunSnapshot(
                protocol_version=PROTOCOL_VERSION,
                run_id=self._journal.run_id_locked(),
                sequence=self._journal.latest_sequence_locked(),
                status=enums.number(common_pb2.RunStatus, self._controller.status_locked()),
                agent_kind=kind,
                round_label=round_label,
                active_executions=self._executions.active_locked(),
                chat_threads=[_thread_info(spec) for spec in self._chat.threads_locked()],
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

    def events(
        self, after_sequence: int = 0, before_sequence: int | None = None
    ) -> list[events_pb2.RunEvent]:
        """Read journal events within the requested sequence bounds."""
        return self._journal.read(after_sequence, before_sequence)

    def history_events(self) -> list[events_pb2.RunEvent]:
        """Read the canonical event history used by frontend clients."""
        return self._journal.read_history()

    def performance_rounds(self) -> list[responses_pb2.PerformanceRound]:
        """Build the recorded round-level performance series."""
        state = self._agent_run_state()
        if state is None:
            return []
        return [
            responses_pb2.PerformanceRound(
                round=record.round_number,
                perf_metric=record.perf_metric,
                perf_unit=record.perf_unit,
                passed=record.passed,
                profile_skipped=record.profile_skipped,
            )
            for record in state.rounds
            if record.perf_metric is not None and record.perf_unit is not None
        ]

    def performance_context(self) -> responses_pb2.PerformanceContext | None:
        """Build objective and measurement context for performance rendering."""
        project_run = self._controller.project_run
        if project_run is None:
            return None
        manifest = project_run.project.state.load_run(project_run.run_id)
        if not isinstance(manifest.configuration, AgentRunConfiguration):
            return None
        return build_performance_context(
            self._agent_run_state(),
            objectives=manifest.configuration.objectives,
            objective_description=self._objective_description(),
        )

    def experiments(self) -> list[responses_pb2.HypothesisEntry]:
        """Build the experiment log for an agent outer loop."""
        state = self._agent_run_state()
        return [] if state is None else build_experiment_log(state)

    def design_rounds(self) -> list[responses_pb2.DesignRound]:
        """Project the per-round design log for the attached run."""
        project_run = self._controller.project_run
        state = self._agent_run_state()
        if project_run is None or state is None:
            return []
        design = self._design_log(project_run.project.root, project_run.run_id)
        manifest = project_run.project.state.load_run(project_run.run_id)
        return design.rounds(state, baseline=manifest.trusted_input_baseline)

    def design_patch(self, base: str, head: str, path: str) -> responses_pb2.DesignPatch | None:
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
    ) -> list[events_pb2.RunEvent]:
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
        request: requests_pb2.ExperimentQuery,
    ) -> ExperimentQueryResult | None:
        """Answer from memory, loading once outside projection locks if needed."""
        while self._is_agent_run(project_run.project, project_run.run_id):
            projection_id = self._experiment_projection_id(project_run)
            cached = self._experiment_projection.query(
                project_run.run_id,
                projection_id,
                request.after if request.HasField("after") else None,
            )
            if isinstance(cached, ExperimentQueryResult):
                return cached
            state = self._required_agent_run_state(project_run, reproject=True)
            current = self._controller.project_run
            if current is not None and self._same_project_run(current, project_run):
                installed = self._experiment_projection.install_loaded(
                    project_run.run_id,
                    projection_id,
                    state,
                    cached,
                )
                if installed is not None:
                    return installed
                continue
            if current is None:
                return None
            project_run = current
        return None

    def _agent_run_state(
        self,
        project_run: ProjectRunState | None = None,
        *,
        reproject: bool = True,
    ) -> AgentRunState | None:
        project_run = project_run or self._controller.project_run
        if project_run is None:
            return None
        if not self._is_agent_run(project_run.project, project_run.run_id):
            return None
        portable = project_run.project.state.portable_namespace(project_run.run_id, "agent")
        store = AgentRunStateStore(portable)
        state = store.load_optional()
        if state is None:
            from vibesys.run.state import RunStateNamespace  # noqa: PLC0415

            local = project_run.project.state.local_namespace(
                project_run.run_id, RunStateNamespace.AGENT
            )
            manifest = project_run.project.state.load_run(project_run.run_id)
            configuration = cast("AgentRunConfiguration", manifest.configuration)
            # Unified state predating the persisted metric space: the run
            # manifest records the axes but no tolerance, so legacy rounds are
            # ordered exactly, which is what they were ordered by when written.
            return store.migrate_legacy(
                rounds=project_run.project.state.load_rounds(project_run.run_id),
                local_namespace=local,
                legacy_space=MetricSpace(
                    objectives=tuple(
                        Objective(name=name, direction=direction)
                        for name, direction in metric_directions(configuration.objectives).items()
                    )
                ),
            )
        # The run's own space and each round's own comparison travel with the
        # state, so the read path needs no measurement configuration of its own.
        return reproject_run_evidence(state) if reproject else state

    def _required_agent_run_state(
        self,
        project_run: ProjectRunState,
        *,
        reproject: bool,
    ) -> AgentRunState:
        state = self._agent_run_state(project_run, reproject=reproject)
        if state is None:
            raise RuntimeError(  # noqa: TRY003
                "attached run does not have agent experiment state"
            )
        return state

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

    def _observe_committed_state(
        self,
        namespace: str,
        project_root: Path,
        run_id: str,
        state: BaseModel,
        changed_keys: tuple[str, ...] | None,
    ) -> None:
        """Incrementally project a state object immediately after its commit."""
        if namespace != "agent":
            return
        project_run = self._controller.project_run
        if (
            project_run is None
            or project_run.run_id != run_id
            or project_run.project.root != project_root
        ):
            return
        agent_state = cast("AgentRunState", state)
        self._experiment_projection.update(
            run_id,
            self._experiment_projection_id(project_run),
            agent_state,
            changed_keys=changed_keys,
        )

    def _observe_experiment_change(self, event: events_pb2.RunEvent) -> None:
        if event.type != events_pb2.EVENT_TYPE_EXPERIMENTS_CHANGED:
            return
        if event.WhichOneof("data") != "experiments_changed":
            return
        project_run = self._controller.project_run
        if project_run is None or project_run.run_id != event.run_id:
            return
        data = event.experiments_changed
        self._experiment_projection.invalidate(
            event.run_id,
            self._experiment_projection_id(project_run),
            data.revision if data.HasField("revision") else None,
        )


def _respond(request: requests_pb2.Request, **sections: Any) -> responses_pb2.Response:  # noqa: ANN401
    """Build a successful response; ``None`` sections stay absent."""
    return responses_pb2.Response(
        protocol_version=PROTOCOL_VERSION,
        request_id=request.request_id,
        timestamp=messages.now(),
        ok=True,
        **sections,
    )


def _ack(
    request: requests_pb2.Request,
    action: responses_pb2.CommandAction.ValueType,
    status: responses_pb2.CommandAckStatus.ValueType,
) -> responses_pb2.Response:
    return _respond(request, ack=responses_pb2.CommandAck(action=action, status=status))


def _thread_info(spec: events_pb2.ChatThreadCreatedData) -> snapshot_pb2.ChatThreadInfo:
    """Project a recorded thread's identity onto its wire form."""
    return snapshot_pb2.ChatThreadInfo(
        thread_id=spec.thread_id,
        title=spec.title,
        driver=spec.driver,
        provider=spec.provider,
        model=spec.model,
    )
