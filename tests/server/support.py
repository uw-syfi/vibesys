"""Test composition helpers for independently owned server components."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from launch import open_run_store
from server.api.service import RunApi
from server.chat.manager import ChatManager
from server.controller import RunController
from server.execution import AgentExecutionRequest, ExecutionHandle, ExecutionTracker
from server.integration import RunIntegrationAdapter
from server.journal import WireJournal
from server.read_model import RunInspector
from vibesys.api import CoreEventType
from vibesys.api.metrics import MetricSpace
from vibesys.orchestration.agent_options import (
    AgentOrchestrationOptions,
)
from vibesys.run.event_journal import EventJournal as CoreEventJournal
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api.infrastructure import (
    RunControlChannel,
    RunControlTransition,
    create_run_control_channel,
)
from vs_sim.api import OsThreads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.chat.factory import ChatAgentBuilder
    from server.run_attachment import AgentSelection
    from server.settings import InteractiveSetupDefaults
    from vibesys.api import RunRecord, RunView
    from vs_project.api import Project
    from vs_sim.api import Condition, Threads


class _ControlBridge:
    """Adapt a `RunControlChannel` to the `vibesys.api.RunControl` shape.

    Mirrors what `vibesys.api._session._LocalRunSession`'s `steer`/`pause`/
    `resume`/`stop` methods do in production: translate the `RunControl`
    protocol's names onto the channel's writer-side methods. `RunApi`'s
    `session_provider` returns this so `_execute_command` can route through
    it exactly as it would a live session.
    """

    def __init__(self, channel: RunControlChannel) -> None:
        self._channel = channel

    def steer(self, text: str) -> None:
        self._channel.queue_steer(text)

    def pause(self) -> None:
        self._channel.request_pause()

    def resume(self) -> None:
        self._channel.resume()

    def resume_with_fallback(self) -> None:
        self._channel.resume_with_fallback()

    def stop(self) -> None:
        self._channel.request_stop()


def _record_control_transition(
    events: CoreEventJournal, transition: RunControlTransition
) -> object:
    """Adapt runtime transitions to the core journal in test composition."""
    return events.emit(
        CoreEventType(transition.kind.value),
        transition.text,
        agent_kind=transition.agent_kind,
        round_label=transition.round_label,
        execution_id=transition.execution_id,
    )


def agent_descriptor(
    *,
    metric_space: MetricSpace | None = None,
) -> OrchestrationDescriptor:
    """Build the one active manifest descriptor for server agent fixtures."""
    options = AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=3,
        max_retries_per_round=1,
        judge_every=1,
        official_eval_every=1,
        metric_space=metric_space or MetricSpace(),
    )
    return OrchestrationDescriptor(
        id="single-agent",
        config_version=1,
        options=options.model_dump(mode="json"),
    )


def auxiliary_agent_providers() -> tuple[str, ...]:
    """Return stable provider facts for server composition tests."""
    return ("claude", "codex", "gemini", "opencode")


def run_record(project: Project, run_id: str) -> RunRecord:
    """Open the semantic record used by server-facing integration tests."""
    return open_run_store(project).get_record(run_id)


@dataclass(frozen=True)
class ServerParts:
    """Explicitly composed server components used by focused tests."""

    condition: Condition
    journal: WireJournal
    executions: ExecutionTracker
    controller: RunController
    chat: ChatManager
    integration: RunIntegrationAdapter
    api: RunApi
    core_events: CoreEventJournal
    control: RunControlChannel

    def start_execution(
        self,
        *args: str,
        participates_in_run_control: bool = True,
        emit_lifecycle: bool = True,
        agent_selection: AgentSelection | None = None,
    ) -> ExecutionHandle:
        """Build a lifecycle request for terse controller-focused tests."""
        if len(args) not in (3, 4):
            message = "start_execution expects kind, round, prompt, and optional system prompt"
            raise TypeError(message)
        kind, round_label, user_prompt = args[:3]
        system_prompt = args[3] if len(args) == 4 else ""
        return self.controller.start_agent_execution(
            AgentExecutionRequest(
                kind=kind,
                round_label=round_label,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                participates_in_run_control=participates_in_run_control,
                emit_lifecycle=emit_lifecycle,
                provider=agent_selection.provider if agent_selection is not None else None,
                model=agent_selection.model if agent_selection is not None else None,
            )
        )

    def attach(
        self,
        log_dir: Path,
        *,
        record: RunRecord | None = None,
    ) -> None:
        """Attach the integration and core event journal to durable state.

        Mirrors what `vibesys.run.integration.LocalRunIntegration.attach`
        does for its own `events` journal in production: this harness has no
        session, so `core_events` needs its own attach call to write
        ``core-events.jsonl`` under *log_dir*.
        """
        self.integration.attach(log_dir, record=record)
        self.core_events.attach(
            log_dir,
            record.run_id if record is not None else log_dir.parent.name,
        )

    def close(self) -> None:
        """Release subscriptions owned by the integration adapter."""
        self.integration.close()

    def publish_committed_view(
        self, view: RunView, changed_keys: tuple[str, ...] | None = None
    ) -> None:
        """Feed a projected view to the API as `RunSession.on_committed_view` would.

        Production wires this through `ServerRuntime.drive`
        (`session.on_committed_view(self.api.observe_committed_state)`); this
        harness has no session, so it calls the same method directly.
        """
        self.api.observe_committed_state(view, changed_keys)


def build_server_parts(
    log_dir: Path | None = None,
    *,
    record: RunRecord | None = None,
    tui_defaults: Callable[[], InteractiveSetupDefaults] | None = None,
    chat_agent_builder: ChatAgentBuilder | None = None,
    threads: Threads | None = None,
) -> ServerParts:
    """Compose real server components and optionally attach durable state.

    ``threads`` supplies every lock, condition and event the components create; pass a
    ``SimThreads`` to run them on the simulator.
    """
    threads = threads or OsThreads()
    condition = threads.condition(threads.rlock())
    journal = WireJournal(condition, threads=threads)
    executions = ExecutionTracker(condition, journal)
    controller = RunController(condition, journal, executions)
    chat = ChatManager(
        condition,
        journal,
        run_status=controller.run_status,
        threads=threads,
    )
    journal.add_listener(chat.apply_replayed_event, replay_filter=chat.replay_filter)
    if chat_agent_builder is None:
        integration = RunIntegrationAdapter(controller, executions, journal, chat)
    else:
        integration = RunIntegrationAdapter(
            controller,
            executions,
            journal,
            chat,
            chat_agent_builder=chat_agent_builder,
        )
    chat.set_fallback_answer(RunInspector(integration).answer)
    core_events = CoreEventJournal()
    core_events.subscribe(integration.project_event)
    control = create_run_control_channel(
        lambda transition: _record_control_transition(core_events, transition)
    )
    control_bridge = _ControlBridge(control)
    api = RunApi(
        condition,
        controller,
        executions,
        journal,
        chat,
        integration,
        session_provider=lambda: control_bridge,
        tui_defaults=tui_defaults,
        threads=threads,
    )
    parts = ServerParts(
        condition=condition,
        journal=journal,
        executions=executions,
        controller=controller,
        chat=chat,
        integration=integration,
        api=api,
        core_events=core_events,
        control=control,
    )
    if log_dir is not None:
        parts.attach(log_dir, record=record)
    return parts


# Bounds a wait that always ends when the thing waited on happens; reaching it
# means a deadlock, so raising it can only turn a failure into a pass.
DEADLOCK_GUARD_S = 30.0


class Task[T]:
    """A call running on its own worker thread, whose outcome the test collects with `result`.

    Replaces a one-worker executor: it spawns through the test's `Threads`, so on the
    simulator the call is a simulated thread and `result` costs no wall time.
    """

    def __init__(self, threads: Threads, call: Callable[[], T], *, name: str = "task") -> None:
        """Start *call* on a new worker."""
        self._outcome: list[T] = []
        self._error: list[BaseException] = []

        def body() -> None:
            try:
                self._outcome.append(call())
            except BaseException as error:  # noqa: BLE001  # LW-100901 [BLE001]; the call's exception is handed to whoever collects the result.
                self._error.append(error)

        self._worker = threads.spawn(body, name=name)

    def result(self) -> T:
        """Wait for the call to end and return its value, or raise what it raised."""
        self._worker.join(DEADLOCK_GUARD_S)
        assert not self._worker.is_alive(), f"{self._worker.name} did not end"
        if self._error:
            raise self._error[0]
        return self._outcome[0]


class FakeSettleWindow:
    """A `SettleWindow` whose window ends only when the test says so.

    `wait_for` holds the tracker's condition until the predicate holds or
    `elapse` ends the window. `await_window` blocks until the tracker has
    opened the given window, so a test can order its steps against the
    tracker's without sleeping.
    """

    def __init__(self) -> None:
        """Start with no window opened."""
        self._opened = 0
        self._ended = 0
        self._elapsed = False
        self._condition: Condition | None = None
        self._window_opened = threading.Condition()

    def wait_for(
        self,
        condition: Condition,
        predicate: Callable[[], bool],
        seconds: float,
    ) -> bool:
        """Block until *predicate* holds or `elapse` ends this window."""
        del seconds
        self._condition = condition
        with self._window_opened:
            self._opened += 1
            self._window_opened.notify_all()
        condition.wait_for(lambda: predicate() or self._elapsed)
        self._elapsed = False
        held = predicate()
        with self._window_opened:
            self._ended += 1
            self._window_opened.notify_all()
        return held

    @property
    def windows_opened(self) -> int:
        """How many settle windows the tracker has opened so far."""
        with self._window_opened:
            return self._opened

    def await_window(self, number: int) -> None:
        """Block until the tracker has opened its *number*-th window."""
        with self._window_opened:
            assert self._window_opened.wait_for(
                lambda: self._opened >= number, timeout=DEADLOCK_GUARD_S
            ), f"settle window {number} never opened"

    def await_window_end(self, number: int) -> None:
        """Block until the tracker's *number*-th window has ended, either way."""
        with self._window_opened:
            assert self._window_opened.wait_for(
                lambda: self._ended >= number, timeout=DEADLOCK_GUARD_S
            ), f"settle window {number} never ended"

    def elapse(self) -> None:
        """End the open window as if its seconds had passed."""
        condition = self._condition
        assert condition is not None, "no settle window has opened"
        with condition:
            self._elapsed = True
            condition.notify_all()


def run_at_paused_boundary(parts: ServerParts, work: Callable[[], None]) -> threading.Thread:
    """Start *work* on a thread and return once it has announced it is parked.

    A thread that reaches the pause boundary while the run is paused records
    one more `PAUSED` core event before it parks, so seeing that event is the
    handoff that replaces a guessed sleep. The thread cannot leave the boundary
    until the test resumes or stops the run, whether or not it has finished
    parking.
    """
    reached = threading.Event()
    unsubscribe = parts.core_events.subscribe(
        lambda event: reached.set() if event.type is CoreEventType.PAUSED else None
    )
    thread = threading.Thread(target=work)
    try:
        thread.start()
        assert reached.wait(timeout=DEADLOCK_GUARD_S), "the thread never reached the pause boundary"
    finally:
        unsubscribe()
    return thread
