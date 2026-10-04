"""Explicit composition root for the frontend server process."""

from __future__ import annotations

import asyncio
import threading
from contextlib import ExitStack, suppress
from typing import TYPE_CHECKING, TypeVar

from server.api.service import RunApi
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
    RunInterruptedData,
    ServerReadyData,
)
from server.execution import ExecutionTracker
from server.integration import RunIntegrationAdapter
from server.journal import WireJournal
from server.read_model import RunInspector
from server.transport.discovery import (
    CAPABILITY_ROTATION_HEADER as CAPABILITY_ROTATION_HEADER,  # noqa: PLC0414  # lint-waiver: LW-936006 [PLC0414]; re-export the lifecycle HTTP contract through the runtime composition boundary; a wrapper would duplicate a constant and a direct entrypoint-to-transport import would bypass the boundary.
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
from server.transport.subscriptions import SubscriptionTracker
from server.transport.unix_jsonl import UnixJsonlServer
from server.transport.websocket import WebSocketGateway
from server.transport.websocket import (
    browser_origin as browser_origin,  # noqa: PLC0414  # lint-waiver: LW-101108 [PLC0414]; re-export the browser-origin parser through the allowed runtime composition boundary, so the launcher validates `--web-origin` against the one definition the gateway enforces
)
from vibesys.api import ConfigurationError, RunStopped

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.settings import InteractiveSetupDefaults
    from vibesys.api import RunHandle, RunRequest, RunResult, Runs, RunSession


_RunValueT = TypeVar("_RunValueT")
_TERMINAL_EVENT_TYPES = frozenset(
    {EventType.RUN_FINISHED, EventType.RUN_FAILED, EventType.RUN_INTERRUPTED}
)


class ServerRuntime:
    """Compose and run one frontend-facing JSONL server."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-101040 [PLR0913]; the composition root accepts one explicit option per transport and lifetime concern
        self,
        *,
        socket_path: Path,
        runs: Runs,
        tui_defaults: Callable[[], InteractiveSetupDefaults] | None = None,
        web: bool = False,
        web_port: int = 0,
        web_assets: Path | None = None,
        web_origins: tuple[str, ...] = (),
        instance_path: Path | None = None,
        detach: bool = False,
        read_only_log: Path | None = None,
    ) -> None:
        """Compose all server components around one shared condition."""
        self.runs = runs
        self.socket_path = socket_path
        self.web = web
        self.web_port = web_port
        self.web_assets = web_assets
        self.web_origins = web_origins
        self.instance_path = instance_path
        self.detach = detach
        self.read_only_log = read_only_log
        self._shutdown = threading.Event()
        self.condition = threading.Condition(threading.RLock())
        self.journal = WireJournal(self.condition)
        self.executions = ExecutionTracker(self.condition, self.journal)
        self.controller = RunController(self.condition, self.journal, self.executions)
        self.chat = ChatManager(
            self.condition,
            self.journal,
            run_status=self.controller.run_status,
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
        )
        self.chat.set_fallback_answer(RunInspector(self.integration).answer)
        self.api = RunApi(
            self.condition,
            self.controller,
            self.executions,
            self.journal,
            self.chat,
            self.integration,
            session_provider=lambda: self.session,
            tui_defaults=tui_defaults,
        )
        self.chat.enable_terminal_retention()
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
            session.on_ready(lambda ready: self.integration.handle_run_ready(session, ready))
            with self.condition:
                self.handle = handle
                self.session = session
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
                with self.condition:
                    self.handle = None
                    self.session = None

        return asyncio.run(execute())

    def run(self, run: Callable[[], _RunValueT]) -> _RunValueT | None:
        """Serve requests while executing ``run`` in the calling thread."""
        if self.read_only_log is not None:
            self.controller.attach_read_only(self.read_only_log)
        else:
            self.controller.attach(self.socket_path.parent)
            self.journal.record(
                EventType.SERVER_READY,
                status=EventStatus.ACTIVE,
                data=ServerReadyData(),
            )
        run_error: BaseException | None = None
        try:
            subscriptions = SubscriptionTracker()
            with ExitStack() as transports:
                transport = transports.enter_context(
                    UnixJsonlServer(self.socket_path, self.api, subscriptions)
                )
                web_transport = (
                    transports.enter_context(
                        WebSocketGateway(
                            self.api,
                            assets_dir=self.web_assets,
                            port=self.web_port,
                            allowed_origins=self.web_origins,
                            subscriptions=subscriptions,
                            instance_path=self.instance_path,
                        )
                    )
                    if self.web
                    else None
                )
                if web_transport is not None:
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

    @property
    def _detachable_mode(self) -> bool:
        """Whether the server lifetime is independent of its subscribers."""
        return self.detach or self.read_only_log is not None

    def shutdown(self) -> None:
        """Request a clean shutdown of a detached or read-only server."""
        self._shutdown.set()

    def _wait_for_detached_shutdown(self, transport: UnixJsonlServer) -> None:
        if not self._detachable_mode:
            transport.wait_for_subscriber_disconnect()
            return
        while not self._shutdown.wait(timeout=0.1):
            pass

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
        """End a run the launcher terminated, recording the interruption once."""
        launcher_error = RuntimeError("launcher_terminated (SIGTERM)")
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
                    reason="launcher_terminated",
                    signal="SIGTERM",
                ),
                diagnostic=event_diagnostic,
            )

    def _terminal_recorded_after(self, sequence: int) -> bool:
        """Return whether the run callback already emitted its terminal event."""
        return any(event.type in _TERMINAL_EVENT_TYPES for event in self.journal.read(sequence))
