"""Explicit composition root for the frontend server process."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import ExitStack, suppress
from functools import partial
from typing import TYPE_CHECKING, Protocol, TypeVar

from server.api.service import RunApi, RunControlSlot
from server.chat.manager import ChatManager
from server.controller import RunController
from server.diagnostics import (
    Diagnostic,
    DiagnosticRetryability,
    DiagnosticScope,
    DiagnosticSeverity,
    exception_to_diagnostic,
)
from server.events import (
    ConfigurationFailedData,
    EventStatus,
    EventType,
    QuotaPausedData,
    RunEndingData,
    RunInterruptedData,
    RunStartupProgressData,
    RunStatusChangedData,
    ServerReadyData,
    StartupState,
)
from server.execution import ExecutionTracker
from server.host_api import (
    FailureReason,
    PauseCause,
    RunEndingFact,
    RunEndOutcome,
    StartupFact,
)
from server.idle_watch import IdleWatch
from server.integration import RunIntegrationAdapter
from server.journal import WireJournal
from server.read_model import RunInspector
from server.transport.discovery import (
    CAPABILITY_ROTATION_HEADER as CAPABILITY_ROTATION_HEADER,  # noqa: PLC0414  # lint-waiver: LW-936006 [PLC0414]; re-export the lifecycle HTTP contract through the runtime composition boundary; a wrapper would duplicate a constant and a direct entrypoint-to-transport import would bypass the boundary.
)
from server.transport.discovery import (
    UrlWebHealthProbe as UrlWebHealthProbe,  # noqa: PLC0414  # lint-waiver: LW-257802 [PLC0414]; re-export the gateway health probe through the runtime composition boundary so the entrypoint that builds the real one does not import the transport directly
)
from server.transport.discovery import (
    WebDiscovery as WebDiscovery,  # noqa: PLC0414  # lint-waiver: LW-257803 [PLC0414]; re-export gateway discovery through the runtime composition boundary so entrypoints build it with real roles and tests with Fakes
)
from server.transport.discovery import (
    WebGatewayListener as WebGatewayListener,  # noqa: PLC0414  # lint-waiver: LW-102902 [PLC0414]; re-export verified port identity through the runtime composition boundary for operator entrypoints
)
from server.transport.discovery import (
    WebInstanceClaim as WebInstanceClaim,  # noqa: PLC0414  # lint-waiver: LW-101062 [PLC0414]; re-export discovery locking through the allowed runtime composition boundary
)
from server.transport.discovery import (
    WebInstanceHold as WebInstanceHold,  # noqa: PLC0414  # lint-waiver: LW-101115 [PLC0414]; re-export the discovery instance hold through the allowed runtime composition boundary
)
from server.transport.discovery import (
    WebInstanceRecord as WebInstanceRecord,  # noqa: PLC0414  # lint-waiver: LW-101061 [PLC0414]; re-export the discovery record through the allowed runtime composition boundary
)
from server.transport.discovery import (
    WebPortInspector as WebPortInspector,  # noqa: PLC0414  # lint-waiver: LW-102903 [PLC0414]; re-export Linux port discovery through the runtime composition boundary for operator entrypoints
)
from server.transport.discovery import (
    WebPortObservation as WebPortObservation,  # noqa: PLC0414  # lint-waiver: LW-102904 [PLC0414]; re-export the port observation contract through the runtime composition boundary for operator entrypoints
)
from server.transport.discovery import (
    WebPortState as WebPortState,  # noqa: PLC0414  # lint-waiver: LW-102905 [PLC0414]; re-export port classification through the runtime composition boundary for operator entrypoints
)
from server.transport.subscriptions import DeliveryWatermark, SubscriptionTracker
from server.transport.unix_jsonl import UnixJsonlServer
from server.transport.websocket import (
    WEBSOCKET_CLOSE_TIMEOUT_SECONDS as WEBSOCKET_CLOSE_TIMEOUT_SECONDS,  # noqa: PLC0414  # lint-waiver: LW-107301 [PLC0414]; expose the transport's canonical close bound through the runtime boundary so launchers can outlast it without restating it
)
from server.transport.websocket import (
    WebSocketBindError as WebSocketBindError,  # noqa: PLC0414  # lint-waiver: LW-102901 [PLC0414]; re-export the operator-facing bind failure through the runtime composition boundary so entrypoints do not import a concrete transport
)
from server.transport.websocket import WebSocketGateway
from server.transport.websocket import (
    browser_origin as browser_origin,  # noqa: PLC0414  # lint-waiver: LW-101108 [PLC0414]; re-export the browser-origin parser through the allowed runtime composition boundary, so the launcher validates `--web-origin` against the one definition the gateway enforces
)
from vibesys.api import ConfigurationError, RunStopped, StopMode
from vs_sim.api import Signals

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.events import RunEvent
    from server.host_api import RunStatus
    from vibesys.api import RunHandle, RunReader, RunRequest, RunResult, Runs, RunSession
    from vs_project.api import OperatorKey
    from vs_sim.api import Platform


_LOG = logging.getLogger(__name__)
_RunValueT = TypeVar("_RunValueT")
DETACHED_DRAIN_TIMEOUT_SECONDS = 2.0
"""How long a detached server that is exiting waits for clients to read its last events."""
READER_IDLE_SECONDS = 60.0
"""How long a read-only server waits with no client connected before it ends."""
_SHUTDOWN_POLL_SECONDS = 0.1
"""How often a detached or read-only server waiting for its shutdown looks at its clients."""
_TERMINAL_EVENT_TYPES = frozenset(
    {EventType.RUN_FINISHED, EventType.RUN_FAILED, EventType.RUN_INTERRUPTED}
)
_TERMINATING = frozenset({Signals.SIGTERM, Signals.SIGHUP})
"""The signals that end a run now (the launcher terminating it), unlike a Ctrl-C's stop."""
_FAILURE_EVENT_TYPES = frozenset(
    {EventType.RUN_FAILED, EventType.RUN_INTERRUPTED, EventType.CONFIGURATION_FAILED}
)


def _project_scope(project_root: Path) -> str:
    """Return an opaque stable namespace for machine-local client state."""
    return hashlib.sha256(project_root.expanduser().resolve().as_posix().encode()).hexdigest()


def _ending_fact(data: RunEndingData) -> RunEndingFact:
    """The record's view of a run's ending: core's end reason is one of the record's outcomes."""
    return RunEndingFact(
        outcome=None if data.reason is None else RunEndOutcome(data.reason.value),
        summary=data.summary,
        resumable=data.resumable,
        rounds_finished=data.rounds_finished,
    )


class _Once:
    """Run a callback on its first call only; later calls do nothing."""

    def __init__(self, callback: Callable[[], object]) -> None:
        self._callback: Callable[[], object] | None = callback

    def __call__(self) -> None:
        callback, self._callback = self._callback, None
        if callback is not None:
            callback()


class ServerLifecycleObserver(Protocol):
    """Told the facts a registry record advertises: the server's milestones and its run's phase."""

    def resolved(self, *, project_root: Path, task: str) -> None:
        """The invocation resolved its authoritative project and selected task."""
        ...

    def listening(self) -> None:
        """The control socket accepts connections."""
        ...

    def observe(
        self,
        *,
        status: RunStatus | None = None,
        failure: FailureReason | None = None,
        startup: StartupFact | None = None,
    ) -> None:
        """The run's status changed, it failed for ``failure``, or its startup progressed."""
        ...

    def pausing_for(self, cause: PauseCause) -> None:
        """The run is pausing, or paused, for ``cause`` rather than an operator's pause."""
        ...

    def ending(self, fact: RunEndingFact) -> None:
        """How the run's durable record stood as the run ended."""
        ...

    def terminated(self) -> None:
        """The server was terminated (a force stop) before its run reached a safe point."""
        ...


def failure_reason(event: RunEvent) -> FailureReason | None:
    """Why the run failed, if ``event`` is the failure that ended it."""
    diagnostic = event.diagnostic
    if event.type not in _FAILURE_EVENT_TYPES or diagnostic is None:
        return None
    stage = (
        event.data.stage
        if isinstance(event.data, ConfigurationFailedData)
        else diagnostic.scope.value
    )
    return FailureReason(code=diagnostic.code, stage=stage, message=diagnostic.summary)


class ServerRuntime:
    """Compose and run one frontend-facing JSONL server.

    Lifetime: an attached server (no ``detach``) waits for its first client and
    ends with it. A detached web server outlives the run until ``shutdown``, so
    a browser can keep reading it. A detached server without the web gateway
    exits when the run ends: nothing it serves outlives the run, and the run is
    reopened read-only from stored state.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-101040 [PLR0913]; the composition root accepts one explicit option per transport and lifetime concern
        self,
        *,
        socket_path: Path,
        control_key: OperatorKey,
        runs: Runs,
        run_id: str | None = None,
        project_root: Path | None = None,
        web: bool = False,
        web_port: int = 0,
        web_assets: Path | None = None,
        web_build_id: str | None = None,
        web_origins: tuple[str, ...] = (),
        instance_path: Path | None = None,
        detach: bool = False,
        read_only_log: Path | None = None,
        open_reader: Callable[[], RunReader] | None = None,
        reader_idle_seconds: float | None = None,
        observer: ServerLifecycleObserver | None = None,
        platform: Platform,
    ) -> None:
        """Compose all server components around one shared condition.

        ``platform`` is the process's time, threads, blocking runner and sockets: its
        clock counts seconds since the epoch and stamps every journaled event and
        execution start; its threads make every lock, condition, event and worker
        thread the server creates; its network binds the Unix control socket and its
        blocking runner runs the web gateway's synchronous requests.

        ``control_key`` is the operator key a client of the control socket must present
        before anything is served.

        ``run_id`` names the run every journaled event belongs to, from the
        first; only a read-only reopen (``read_only_log``) takes it from the
        recorded events instead.

        A read-only server opens the finished run for conversation through ``open_reader`` when
        it is given one; without it, chat on the run answers from its recorded events only.

        A read-only server without the web gateway ends once no client has held a
        connection for ``reader_idle_seconds`` (one nobody connects to ends that long
        after it listened); without it, it serves until ``shutdown``.
        """
        clock = platform.clock
        self.platform = platform
        self.threads = platform.threads
        self.network = platform.network
        self.runs = runs
        self.socket_path = socket_path
        self._control_key = control_key
        self.web = web
        self.web_port = web_port
        self.web_assets = web_assets
        self.web_build_id = web_build_id
        self.web_origins = web_origins
        self.instance_path = instance_path
        self.detach = detach
        self.read_only_log = read_only_log
        self._open_reader = open_reader
        self._reader_idle_seconds = reader_idle_seconds
        self.observer = observer
        self._shutdown = self.threads.event()
        self._shutdown_signal: Signals | None = None
        # Set once the Unix socket accepts connections: how a same-process
        # client learns it can dial without polling the filesystem.
        self.transport_listening = self.threads.event()
        self.condition = self.threads.condition(self.threads.rlock())
        self._clock = clock
        self.journal = WireJournal(self.condition, clock=clock, threads=self.threads)
        self.executions = ExecutionTracker(
            self.condition, self.journal, clock=clock, threads=self.threads
        )
        self.controller = RunController(self.condition, self.journal, self.executions)
        if read_only_log is not None:
            self._attach_journal = partial(self.controller.attach_read_only, read_only_log)
        elif run_id is not None:
            self._attach_journal = partial(
                self.controller.attach, socket_path.parent, run_id=run_id
            )
        else:
            message = "a server that drives a run must be given its run id"
            raise ValueError(message)
        self.chat = ChatManager(
            self.condition,
            self.journal,
            run_status=self.controller.run_status,
            threads=self.threads,
        )
        self.journal.add_listener(
            self.chat.apply_replayed_event,
            replay_filter=self.chat.replay_filter,
        )
        self.integration = RunIntegrationAdapter(
            self.controller,
            self.executions,
            self.journal,
            self.chat,
            clock=clock,
            threads=self.threads,
        )
        self.chat.set_fallback_answer(RunInspector(self.integration).answer)
        self.controls = RunControlSlot(self.condition)
        self.api = RunApi(
            self.condition,
            self.controller,
            self.executions,
            self.journal,
            self.chat,
            self.integration,
            controls=self.controls,
            clock=clock,
            project_scope=_project_scope(project_root) if project_root is not None else None,
            threads=self.threads,
        )
        self.chat.enable_terminal_retention()
        if observer is not None:
            self.journal.add_listener(
                partial(_report_phase_facts, observer), replay_filter=lambda _header: False
            )
        self.session: RunSession | None = None
        self.handle: RunHandle | None = None

    def drive(self, request: RunRequest) -> RunResult:
        """Start *request* through the injected run service and project its events."""

        async def execute() -> RunResult:
            handle = (
                self.runs.resume(request)
                if request.resume is not None
                else self.runs.start(request)
            )
            session = handle.session
            session.on_committed_view(self.api.observe_committed_state)

            session.on_ready(partial(self.integration.handle_run_ready, session))
            with self.condition:
                self.handle = handle
                self.session = session
            control_installed = False
            try:
                self.controls.install(session)
                control_installed = True
                try:
                    async for event in handle.events():
                        self.integration.project_event(event)
                    return await handle.result()
                except asyncio.CancelledError as cancellation:
                    handle.stop()
                    try:
                        await handle.result()
                    finally:
                        raise cancellation
            finally:
                if control_installed:
                    self.controls.release(session)
                with self.condition:
                    self.handle = None
                    self.session = None

        return self.threads.run_async(execute())

    def run(self, run: Callable[[], _RunValueT]) -> _RunValueT | None:
        """Serve requests while executing ``run`` in the calling thread."""
        self._attach_journal()
        if self._open_reader is not None:
            self._attach_chat_reader(self._open_reader)
        if self.read_only_log is None:
            self.journal.record(
                EventType.SERVER_READY,
                status=EventStatus.ACTIVE,
                data=ServerReadyData(),
            )
        run_error: BaseException | None = None
        try:
            subscriptions = SubscriptionTracker(threads=self.threads)
            with ExitStack() as transports:
                transport = transports.enter_context(
                    UnixJsonlServer(
                        self.socket_path,
                        self.api,
                        threads=self.threads,
                        network=self.network,
                        subscriptions=subscriptions,
                        key=self._control_key,
                    )
                )
                # Registered after the transport, so it runs before the
                # transport closes, on every exit path: a run that raised
                # (a configuration or run failure) or ended leaves its
                # clients the event that says why before they see the close.
                drain = _Once(partial(self._drain_subscribers, transport))
                transports.callback(drain)
                self.transport_listening.set()
                if self.observer is not None:
                    self.observer.listening()
                web_transport = (
                    transports.enter_context(
                        WebSocketGateway(
                            self.api,
                            assets_dir=self.web_assets,
                            web_build_id=self.web_build_id,
                            port=self.web_port,
                            allowed_origins=self.web_origins,
                            subscriptions=subscriptions,
                            instance_path=self.instance_path,
                            clock=self._clock,
                            threads=self.threads,
                            request_runner=self.platform.blocking,
                        )
                    )
                    if self.web
                    else None
                )
                if web_transport is not None:
                    # Again after the gateway, so its browsers drain before it
                    # closes too; the callback above then finds it done.
                    transports.callback(drain)
                    print(f"VibeSys web UI: {web_transport.url}", flush=True)  # noqa: T201  # lint-waiver: LW-101006 [T201]; expose the capability URL to the interactive launcher user
                if not self._detachable_mode:
                    self._wait_for_subscriber(transport)
                if self.read_only_log is not None:
                    self._wait_for_detached_shutdown(transport)
                    return None
                return self._execute_run(transport, run)
        except BaseException as exc:
            run_error = exc
            raise
        finally:
            try:
                self.integration.close()
            except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-009025 [BLE001]; optional cleanup must not replace a run failure or interrupt, so record it as a note instead.
                message = (
                    "Experiment chat cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
                if run_error is not None:
                    run_error.add_note(message)
                else:
                    with suppress(BaseException):
                        self.journal.publish_output(
                            "stderr",
                            f"{message}\n",
                            source="experiment-chat",
                        )

    def _attach_chat_reader(self, open_reader: Callable[[], RunReader]) -> None:
        """Give the finished run's chat an agent; a run that cannot be opened keeps its summary answers."""
        try:
            reader = open_reader()
        except Exception:  # noqa: BLE001  # lint-waiver: LW-101051 [BLE001]; a finished run that cannot be opened for conversation is still served for reading, so the failure is logged, not raised
            _LOG.warning("chat cannot read the finished run", exc_info=True)
            return
        self.integration.attach_reader(reader)

    @property
    def _detachable_mode(self) -> bool:
        """Whether the server lifetime is independent of its subscribers."""
        return self.detach or self.read_only_log is not None

    def _drain_subscribers(self, transport: UnixJsonlServer) -> None:
        """Let attached clients read the run's last events before the socket closes.

        A server that exits with its run (detached, or attached and failing)
        would otherwise let a client see the connection close before the
        terminal status that explains it. The wait is bounded: a client that
        stopped reading cannot hold the server open. A server that already
        waited for its clients to leave finds no stream to wait for. The
        shared tracker closes subscription admission atomically with this
        wait, so a reconnect cannot arrive outside the drained set.
        """
        with self.condition:
            watermark = DeliveryWatermark(
                store_id=self.journal.store_id_locked(),
                sequence=self.journal.latest_sequence_locked(),
            )
        transport.wait_until_delivered(watermark, DETACHED_DRAIN_TIMEOUT_SECONDS)

    def interrupt(self) -> bool:
        """Stop the run on Ctrl-C, cancelling its work in flight; False when no run is installed.

        The stop goes through the run's control, the one authority for its stop, as an
        operator's cancelling stop does; a repeated Ctrl-C changes nothing. The server
        then leaves once the run has stopped, without waiting for its clients to go.
        """
        if not self.controls.stop(StopMode.CANCEL):
            return False
        self.shutdown(Signals.SIGINT)
        return True

    def shutdown(self, signal: Signals | None = None) -> None:
        """Request a clean shutdown of a detached or read-only server.

        *signal* names the signal that asked for it. The first one that ends the run
        (SIGTERM or SIGHUP, ``_TERMINATING``) names how a run it interrupts ended; a Ctrl-C
        (SIGINT), which only asked the run to stop, names it when nothing stronger came.
        """
        with self.condition:
            held = self._shutdown_signal
            if held is None or (held not in _TERMINATING and signal in _TERMINATING):
                self._shutdown_signal = signal
        self._shutdown.set()

    def _wait_for_detached_shutdown(self, transport: UnixJsonlServer) -> None:
        if self._shutdown.is_set():
            return
        if not self._detachable_mode:
            transport.wait_for_subscriber_disconnect()
            return
        if not self.web and self.read_only_log is None:
            return
        idle = (
            None
            if self._reader_idle_seconds is None
            else IdleWatch.begin(self._reader_idle_seconds, self.threads.now())
        )
        while not self._shutdown.wait(timeout=_SHUTDOWN_POLL_SECONDS):
            if idle is None:
                continue
            now = self.threads.now()
            idle = idle.observe(transport.client_count(), now)
            if idle.expired(now):
                return

    @staticmethod
    def _wait_for_subscriber(transport: UnixJsonlServer) -> None:
        """Require a frontend client before starting the backend run."""
        if not transport.wait_for_subscriber(timeout=30.0):
            message = "Timed out waiting for a server client"
            raise RuntimeError(message)

    def _execute_run(
        self,
        transport: UnixJsonlServer,
        run: Callable[[], _RunValueT],
    ) -> _RunValueT | None:
        """Run the backend and order its terminal status against the journal."""
        terminal_cursor = self.journal.latest_sequence
        try:
            value = run()
        except KeyboardInterrupt:
            self._finish_after_launcher_interrupt(terminal_cursor)
            raise
        except RunStopped:
            # The stop already landed: the controller is STOPPED and
            # the journal ends with that terminal status change, so
            # there is no terminal event to add. ``finish`` is
            # absorbed by the ended status; it is called so the
            # journal cannot end on a live status even if a stop ever
            # unwinds before landing. Returning, not re-raising, is
            # what makes an operator stop a clean backend exit.
            self.controller.finish(record_event=False)
            self._wait_for_detached_shutdown(transport)
            return None
        except ConfigurationError as exc:
            configuration_diagnostic = exc.diagnostic
            event_diagnostic = Diagnostic(
                code=configuration_diagnostic.code,
                summary=configuration_diagnostic.message,
                detail=(
                    f"Stage: {configuration_diagnostic.stage}\n"
                    f"Exit code: {configuration_diagnostic.exit_code}"
                ),
                hint=configuration_diagnostic.usage,
                scope=DiagnosticScope.CONFIGURATION,
                severity=DiagnosticSeverity.FATAL,
                retryability=DiagnosticRetryability.NEVER,
            )
            self.controller.finish(
                exc,
                record_event=False,
                diagnostic=event_diagnostic,
            )
            self.journal.record(
                EventType.CONFIGURATION_FAILED,
                event_diagnostic.summary,
                status=EventStatus.FAILED,
                data=ConfigurationFailedData(
                    code=configuration_diagnostic.code,
                    stage=configuration_diagnostic.stage,
                    message=event_diagnostic.summary,
                    usage=event_diagnostic.hint,
                    exit_code=configuration_diagnostic.exit_code,
                ),
                diagnostic=event_diagnostic,
            )
            raise
        except BaseException as exc:
            self.controller.finish(
                exc,
                record_event=not self._terminal_recorded_after(terminal_cursor),
            )
            raise
        self.controller.finish(record_event=not self._terminal_recorded_after(terminal_cursor))
        self._wait_for_detached_shutdown(transport)
        return value

    def _finish_after_launcher_interrupt(self, terminal_cursor: int) -> None:
        """End a run a signal interrupted, recording the interruption once.

        SIGTERM and SIGHUP are the launcher terminating the run; any other cause (a Ctrl-C
        with no run installed to stop) is an interruption, named by its signal if any.
        """
        with self.condition:
            signal = self._shutdown_signal
        reason = "launcher_terminated" if signal in _TERMINATING else "interrupted"
        launcher_error = RuntimeError(reason if signal is None else f"{reason} ({signal.name})")
        event_diagnostic = exception_to_diagnostic(
            launcher_error,
            scope=DiagnosticScope.RUN,
            operation="Run",
            summary="Run interrupted",
            code="interrupted",
            severity=DiagnosticSeverity.FATAL,
            retryability=DiagnosticRetryability.NEVER,
        )
        terminal_recorded = self._terminal_recorded_after(terminal_cursor)
        # End the run before its terminal event is recorded, so no
        # snapshot can report `running` at a sequence that already
        # contains that event.
        self.controller.finish(
            launcher_error,
            record_event=False,
            diagnostic=event_diagnostic,
        )
        if not terminal_recorded:
            self.journal.record(
                EventType.RUN_INTERRUPTED,
                status=EventStatus.FAILED,
                data=RunInterruptedData(
                    reason=reason,
                    signal=None if signal is None else signal.name,
                ),
                diagnostic=event_diagnostic,
            )

    def _terminal_recorded_after(self, sequence: int) -> bool:
        """Return whether the run callback already emitted its terminal event."""
        return any(event.type in _TERMINAL_EVENT_TYPES for event in self.journal.read(sequence))


def _report_phase_facts(observer: ServerLifecycleObserver, event: RunEvent) -> None:
    """Tell ``observer`` the lifecycle facts in one journaled event."""
    data = event.data
    if isinstance(data, RunStatusChangedData):
        observer.observe(status=data.status)
    elif isinstance(data, RunStartupProgressData):
        # Only a stage's start and its steps move the run through its stages; a wait or a
        # finish says nothing new about where the run is.
        if data.state is StartupState.RUNNING:
            observer.observe(startup=StartupFact(stage=data.stage, progress=data.progress))
    elif isinstance(data, QuotaPausedData) and data.condition != "unavailable":
        # An outage holds only its own turn; the run is not pausing for it.
        observer.pausing_for(PauseCause.QUOTA)
    elif isinstance(data, RunEndingData):
        observer.ending(_ending_fact(data))
    elif isinstance(data, RunInterruptedData):
        observer.terminated()
    failure = failure_reason(event)
    if failure is not None:
        observer.observe(failure=failure)
