"""Headless supervision server runtime."""

from __future__ import annotations

import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vibe_serve.errors import ConfigurationError
from vibe_serve.server.events import (
    ConfigurationFailedData,
    EventStatus,
    EventType,
    RunInterruptedData,
    ServerReadyData,
)
from vibe_serve.server.registry import REGISTRY
from vibe_serve.server.service import SupervisionService
from vibe_serve.server.supervisor import RunSupervisor
from vibe_serve.server.transport import SupervisionSocketServer


def run_server(
    run: Callable[[], Any],
    *,
    socket_path: Path,
) -> Any:
    """Run a headless backend that exposes supervision over a Unix socket."""
    supervisor = RunSupervisor()
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupt_from_launcher(signum: int, frame: object) -> None:
        del signum, frame
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt_from_launcher)
    service = SupervisionService(supervisor)
    REGISTRY.activate(supervisor)
    supervisor.attach(socket_path.parent)
    supervisor.record(
        EventType.SERVER_READY,
        status=EventStatus.ACTIVE,
        data=ServerReadyData(),
    )
    try:
        with SupervisionSocketServer(socket_path, service) as server:
            if not server.wait_for_subscriber(timeout=30.0):
                raise RuntimeError("Timed out waiting for a supervision client")
            try:
                value = run()
            except KeyboardInterrupt:
                supervisor.record(
                    EventType.RUN_INTERRUPTED,
                    status=EventStatus.FAILED,
                    data=RunInterruptedData(reason="launcher_terminated", signal="SIGTERM"),
                )
                supervisor.finish(RuntimeError("Run interrupted by launcher"))
                raise
            except ConfigurationError as exc:
                diagnostic = exc.diagnostic
                supervisor.record(
                    EventType.CONFIGURATION_FAILED,
                    diagnostic.message,
                    status=EventStatus.FAILED,
                    data=ConfigurationFailedData(
                        code=diagnostic.code,
                        stage=diagnostic.stage,
                        message=diagnostic.message,
                        usage=diagnostic.usage,
                        exit_code=diagnostic.exit_code,
                    ),
                )
                supervisor.finish(exc)
                server.wait_for_subscriber_disconnect()
                raise
            except BaseException as exc:
                supervisor.finish(exc)
                server.wait_for_subscriber_disconnect()
                raise
            supervisor.finish()
            server.wait_for_subscriber_disconnect()
            return value
    finally:
        REGISTRY.deactivate(supervisor)
        signal.signal(signal.SIGTERM, previous_sigterm)
