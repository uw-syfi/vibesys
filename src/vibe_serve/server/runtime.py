"""Headless supervision server runtime."""

from __future__ import annotations

import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vibe_serve.server.events import (
    EventStatus,
    EventType,
    RunInterruptedData,
    RunStartedData,
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
    outer_loop: str,
    input_path: str,
    max_rounds: int,
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
    supervisor.record(
        EventType.SERVER_READY,
        status=EventStatus.ACTIVE,
        data=ServerReadyData(),
    )
    try:
        with SupervisionSocketServer(socket_path, service):
            supervisor.record(
                EventType.RUN_STARTED,
                status=EventStatus.ACTIVE,
                data=RunStartedData(
                    outer_loop=outer_loop,
                    input=input_path,
                    max_rounds=max_rounds,
                ),
            )
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
            except BaseException as exc:
                supervisor.finish(exc)
                raise
            supervisor.finish()
            return value
    finally:
        REGISTRY.deactivate(supervisor)
        signal.signal(signal.SIGTERM, previous_sigterm)
