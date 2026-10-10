"""Real-system regressions for web gateway port ownership and bind failures."""

from __future__ import annotations

import os
import socket
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.server.support import build_server_parts

from entrypoints.server import main
from server.runtime import WebPortInspector, WebPortState
from server.transport.websocket import WebSocketBindError, WebSocketGateway
from vs_sim.api.testing import HANG_GUARD_S

if TYPE_CHECKING:
    import threading

    from vs_sim.api import Event


def test_server_main_prints_the_bind_authority_and_reason_on_its_first_line(
    tmp_path: Path,
) -> None:
    source = Path("clients/tui/dev/fixtures/framework-events.jsonl")
    replay = tmp_path / "run-events.jsonl"
    replay.write_bytes(source.read_bytes())
    instance = tmp_path / "web-gateway.json"

    with socket.create_server(("127.0.0.1", 0)) as held:
        port = held.getsockname()[1]
        stderr = StringIO()
        with redirect_stderr(stderr), pytest.raises(SystemExit) as exit_status:
            main(
                [
                    "--web",
                    "--web-port",
                    str(port),
                    "--web-instance",
                    str(instance),
                    "--web-reopen",
                    str(replay),
                ]
            )

    assert exit_status.value.code == 1
    first_line = stderr.getvalue().splitlines()[0]
    assert first_line.startswith(f"WebSocket gateway could not bind 127.0.0.1:{port}:")
    assert "address already in use" in first_line.lower()
    assert f"vibesys web stop --port {port}" in stderr.getvalue()


def test_inspector_maps_a_real_same_user_non_gateway_listener() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        observation = WebPortInspector().inspect(port)

    assert observation.state is WebPortState.OTHER
    assert observation.gateway is None
    assert os.getpid() in observation.holder_pids


class _ListenCollision:
    """Take the reuse-address port after bind and before the gateway listens."""

    def __init__(self) -> None:
        self.blocker: socket.socket | None = None

    def wait_for_ready(self, ready: threading.Event) -> bool:
        return ready.wait(HANG_GUARD_S)

    def wait_before_serve(self, stop: threading.Event) -> None:
        del stop

    def wait_before_listener_start(self, stop: Event, bound_port: int) -> None:
        del stop
        blocker = socket.socket()
        blocker.settimeout(HANG_GUARD_S)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", bound_port))
        blocker.listen()
        self.blocker = blocker

    def wait_before_publication(self, stop: threading.Event, bound_port: int) -> None:
        del stop, bound_port

    def close(self) -> None:
        if self.blocker is not None:
            self.blocker.close()


def test_gateway_reports_a_collision_that_occurs_when_the_bound_socket_listens(
    tmp_path: Path,
) -> None:
    parts = build_server_parts(tmp_path / "logs")
    startup = _ListenCollision()
    gateway = WebSocketGateway(parts.api, startup=startup)

    try:
        with pytest.raises(WebSocketBindError) as failure:
            gateway.start()
        assert failure.value.host == "127.0.0.1"
        assert failure.value.port > 0
        assert "address already in use" in str(failure.value).lower()
    finally:
        gateway.close()
        startup.close()
        parts.close()
