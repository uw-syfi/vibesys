"""Test composition helpers for independently owned server components."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support.real_git import REAL_GIT

from launch import open_run_store
from server.api.service import RunApi, RunControlSlot
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
from vibesys.run.integration import CoreRunControlEvents
from vs_project.api import OperatorDialer, OperatorKey, OrchestrationDescriptor
from vs_runtime.api.infrastructure import (
    ChannelRunControl,
    RunControlChannel,
    create_run_control_channel,
)
from vs_sim.api import OsThreads, Platform
from vs_sim.api.testing import (
    FakeProcesses,
    FakeProcessSignaller,
    ManualClock,
    SimBlockingRunner,
    SimNetwork,
    SimThreads,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.chat.factory import ChatAgentBuilder
    from server.host_api import (
        FailureReason,
        PauseCause,
        RunEndingFact,
        RunStatus,
        StartupFact,
    )
    from server.run_attachment import AgentSelection
    from vibesys.api import RunRecord, RunView
    from vs_project.api import Project
    from vs_sim.api import Condition, Network, ProcessSpec, Threads, Worker
    from vs_sim.api.testing import ProcessScript

REQUEST_TIME = datetime(2026, 1, 1, tzinfo=UTC)
"""The stamp a test gives a request or response it builds."""
CONTROL_KEY = OperatorKey("5" * 64)
"""The operator key every test server demands and every test client presents."""
ANY_SOCKET_ROOT = Path("/")
"""A dialer root that contains every socket, for tests not about where the socket lives.

``libs/vs-project/tests/test_operator_key.py`` pins that a dialer refuses a socket outside
its root; the transport and lifecycle tests here bind theirs in temporary directories.
"""


def operator_dialer(network: Network) -> OperatorDialer:
    """The operator's dialer over ``network``: it presents :data:`CONTROL_KEY` anywhere."""
    return OperatorDialer(network, CONTROL_KEY, root=ANY_SOCKET_ROOT)


class RecordingObserver:
    """A ``ServerLifecycleObserver`` that notes each fact it is told, in order, in ``facts``."""

    def __init__(self) -> None:
        """Start with no facts."""
        self.facts: list[object] = []

    def resolved(self, *, project_root: Path, task: str) -> None:
        """The invocation resolved; not noted."""
        del project_root, task

    def listening(self) -> None:
        """Note ``"listening"``."""
        self.facts.append("listening")

    def observe(
        self,
        *,
        status: RunStatus | None = None,
        failure: FailureReason | None = None,
        startup: StartupFact | None = None,
    ) -> None:
        """Note the one fact given."""
        self.facts.append(status or failure or startup)

    def pausing_for(self, cause: PauseCause) -> None:
        """Note the pause's cause."""
        self.facts.append(cause)

    def ending(self, fact: RunEndingFact) -> None:
        """Note the run's ending."""
        self.facts.append(fact)

    def terminated(self) -> None:
        """Note ``"terminated"``."""
        self.facts.append("terminated")


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
        metric_space=metric_space or MetricSpace(),
    )
    return OrchestrationDescriptor(
        id="multi-agent",
        config_version=1,
        options=options.model_dump(mode="json"),
    )


def auxiliary_agent_providers() -> tuple[str, ...]:
    """Return stable provider facts for server composition tests."""
    return ("claude", "codex", "gemini", "opencode")


def run_record(project: Project, run_id: str) -> RunRecord:
    """Open the semantic record used by server-facing integration tests."""
    return open_run_store(project, git=REAL_GIT, threads=SimThreads()).get_record(run_id)


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
    operator_control: ChannelRunControl
    clock: ManualClock

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
        """Attach the integration and core event journal to durable state, and mark the run ready.

        Mirrors what `vibesys.run.integration.LocalRunIntegration.attach`
        does for its own `events` journal in production: this harness has no
        session, so `core_events` needs its own attach call to write
        ``core-events.jsonl`` under *log_dir*. Like a session's readiness
        (`RunIntegrationAdapter.handle_run_ready`), it then moves the run out
        of ``starting``.
        """
        run_id = record.run_id if record is not None else HARNESS_RUN_ID
        self.integration.attach(log_dir, run_id=run_id, record=record)
        self.core_events.attach(log_dir, run_id)
        self.controller.ready()

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
    chat_agent_builder: ChatAgentBuilder | None = None,
    threads: Threads | None = None,
) -> ServerParts:
    """Compose real server components and optionally attach durable state.

    ``threads`` supplies every lock, condition and event the components create; pass a
    ``SimThreads`` to run them on the simulator.
    """
    threads = threads or OsThreads()
    clock = ManualClock()
    condition = threads.condition(threads.rlock())
    journal = WireJournal(condition, threads=threads, clock=clock)
    executions = ExecutionTracker(condition, journal, clock=clock, threads=threads)
    controller = RunController(condition, journal, executions)
    chat = ChatManager(
        condition,
        journal,
        run_status=controller.run_status,
        threads=threads,
    )
    journal.add_listener(chat.apply_replayed_event, replay_filter=chat.replay_filter)
    if chat_agent_builder is None:
        integration = RunIntegrationAdapter(
            controller, executions, journal, chat, clock=ManualClock(), threads=threads
        )
    else:
        integration = RunIntegrationAdapter(
            controller,
            executions,
            journal,
            chat,
            chat_agent_builder=chat_agent_builder,
            clock=ManualClock(),
            threads=threads,
        )
    chat.set_fallback_answer(RunInspector(integration).answer)
    core_events = CoreEventJournal(clock=clock, threads=OsThreads())
    core_events.subscribe(integration.project_event)
    control = create_run_control_channel(CoreRunControlEvents(core_events), threads=OsThreads())
    control_bridge = ChannelRunControl(control, threads)
    controls = RunControlSlot(condition)
    controls.install(control_bridge)
    api = RunApi(
        condition,
        controller,
        executions,
        journal,
        chat,
        integration,
        controls=controls,
        clock=clock,
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
        operator_control=control_bridge,
        clock=clock,
    )
    if log_dir is not None:
        parts.attach(log_dir, record=record)
    return parts


HARNESS_RUN_ID = "harness-run"
"""The run a harness without a ``RunRecord`` journals as."""


# Bounds a wait that always ends when the thing waited on happens; reaching it
# means a deadlock, so raising it can only turn a failure into a pass.
DEADLOCK_GUARD_S = 30.0


def wait_for_worker(worker: Worker) -> None:
    """Join *worker*, failing the test if it is still running after the deadlock guard."""
    worker.join(DEADLOCK_GUARD_S)
    assert not worker.is_alive(), f"{worker.name} did not end"


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
        wait_for_worker(self._worker)
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


def _no_process(spec: ProcessSpec) -> ProcessScript:
    message = f"a server test started no process, but {spec.argv!r} ran"
    raise AssertionError(message)


def sim_platform(threads: SimThreads, *, network: Network | None = None) -> Platform:
    """A wholly simulated ``Platform`` on ``threads``: no part of it is real.

    Its clock is a ``ManualClock`` (event stamps only), blocking calls and sockets run on
    ``threads``, no process may start, and no process ID is live.
    """
    return Platform(
        clock=ManualClock(),
        threads=threads,
        blocking=SimBlockingRunner(threads),
        processes=FakeProcesses(_no_process, threads.sleep),
        network=SimNetwork(threads) if network is None else network,
        signaller=FakeProcessSignaller(set()),
    )
