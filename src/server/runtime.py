"""Explicit composition root for the frontend server process."""

from __future__ import annotations

import signal
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from server.api.service import RunApi
from server.chat.manager import ChatManager
from server.controller import RunController, RunStopped
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
from server.journal import EventJournal
from server.read_model import RunInspector
from server.transport.unix_jsonl import UnixJsonlServer
from vibesys.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.settings import InteractiveSetupDefaults


_TERMINAL_EVENT_TYPES = frozenset(
    {EventType.RUN_FINISHED, EventType.RUN_FAILED, EventType.RUN_INTERRUPTED}
)


class ServerRuntime:
    """Compose and run one frontend-facing JSONL server."""

    def __init__(
        self,
        *,
        socket_path: Path,
        tui_defaults: Callable[[], InteractiveSetupDefaults] | None = None,
    ) -> None:
        """Compose all server components around one shared condition."""
        self.socket_path = socket_path
        self.condition = threading.Condition(threading.RLock())
        self.journal = EventJournal(self.condition)
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
            tui_defaults=tui_defaults,
        )
        self.chat.enable_terminal_retention()

    def run(self, run: Callable[[], Any]) -> Any:  # noqa: ANN401, PLR0915
        """Serve requests while executing ``run`` in the calling thread."""
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def interrupt_from_launcher(signum: int, frame: object) -> None:
            del signum, frame
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupt_from_launcher)
        self.controller.attach(self.socket_path.parent)
        self.journal.record(
            EventType.SERVER_READY,
            status=EventStatus.ACTIVE,
            data=ServerReadyData(),
        )
        run_error: BaseException | None = None
        try:
            with UnixJsonlServer(self.socket_path, self.api) as transport:
                if not transport.wait_for_subscriber(timeout=30.0):
                    raise RuntimeError("Timed out waiting for a server client")  # noqa: TRY003, TRY301
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
                    transport.wait_for_subscriber_disconnect()
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
                    transport.wait_for_subscriber_disconnect()
                    raise
                except BaseException as exc:
                    self.controller.finish(
                        exc,
                        record_event=not self._terminal_recorded_after(terminal_cursor),
                    )
                    transport.wait_for_subscriber_disconnect()
                    raise
                self.controller.finish(
                    record_event=not self._terminal_recorded_after(terminal_cursor)
                )
                transport.wait_for_subscriber_disconnect()
                return value
        except BaseException as exc:
            run_error = exc
            raise
        finally:
            try:
                self.integration.close()
            except BaseException as cleanup_error:  # optional presentation cleanup  # noqa: BLE001
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
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)

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
