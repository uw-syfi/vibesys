"""The stdio bridge process against a real Unix server: the conformance runner ``stdio-bridge``.

Each connection is a real ``python -m entrypoints.stdio_bridge`` process whose stdin and
stdout stand in for an ssh exec channel. The scenarios in
``tests/conformance/runners/stdio-bridge.json`` replay through it unchanged, so the relay
is held to the same corpus as the Unix transport it fronts. The deterministic tests of
the relay's ends and deadlines are in ``tests/server/test_stdio_bridge.py``.
"""

from __future__ import annotations

import json
import queue
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from typing import IO, TYPE_CHECKING, Any, cast

import pytest
from tests.conformance.frame_matching import assert_frame_matches
from tests.conformance.replay import load_scenario, run_steps, runner_group
from tests.server.support import DEADLOCK_GUARD_S, ServerParts, build_server_parts

from server.stdio_bridge import EXIT_STATUS, BridgeOutcome
from server.stdio_bridge_report import BridgeReport
from server.transport.unix_jsonl import UnixJsonlServer

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

_HISTORY_EVENTS = 100
_RUNNER = "stdio-bridge"


class _BridgeConnection:
    """One protocol connection through one bridge process."""

    def __init__(self, socket_path: Path) -> None:
        # lint-waiver: LW-178504 [S603]; the bridge must be its own process, as over ssh.
        # > The argv is fixed apart from a test-owned socket path; an in-process call would
        # > skip the entrypoint's stdio, exit status and stderr contract under test.
        self.process = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "entrypoints.stdio_bridge", "--socket", str(socket_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._lines: queue.Queue[bytes] = queue.Queue()
        stdout = cast("IO[bytes]", self.process.stdout)
        self._reader = threading.Thread(target=self._read, args=(stdout,), daemon=True)
        self._reader.start()

    def _read(self, stdout: IO[bytes]) -> None:
        for line in stdout:
            self._lines.put(line)
        self._lines.put(b"")

    def send(self, frame: Mapping[str, Any]) -> None:
        stdin = cast("IO[bytes]", self.process.stdin)
        stdin.write(json.dumps(frame).encode() + b"\n")
        stdin.flush()

    def receive(self) -> dict[str, Any]:
        line = self._lines.get(timeout=DEADLOCK_GUARD_S)
        assert line, f"the bridge ended before the expected frame: {self.finish()}"
        assert line.endswith(b"\n"), "a frame crossed the bridge without its newline"
        return cast("dict[str, Any]", json.loads(line))

    def close(self) -> None:
        stdin = cast("IO[bytes]", self.process.stdin)
        if not stdin.closed:
            stdin.close()

    def finish(self) -> tuple[int, bytes]:
        """Close stdin (the client going away), then wait; the exit status and stderr."""
        self.close()
        return self.wait_for_exit()

    def wait_for_exit(self) -> tuple[int, bytes]:
        """Wait for the process to end on its own, leaving stdin open; status and stderr.

        The bound only guards against a hang.
        """
        status = self.process.wait(timeout=DEADLOCK_GUARD_S)
        stderr = cast("IO[bytes]", self.process.stderr).read()
        return status, stderr


@contextmanager
def _bridged(parts: ServerParts, socket_path: Path) -> Iterator[_BridgeConnection]:
    with UnixJsonlServer(socket_path, parts.api):
        connection = _BridgeConnection(socket_path)
        try:
            yield connection
        finally:
            # Closing stdin is the client going away: a clean end, status 0, no report.
            assert connection.finish() == (EXIT_STATUS[BridgeOutcome.CLIENT_CLOSED], b"")


@pytest.mark.parametrize("scenario_name", runner_group(_RUNNER, "bootstrap"))
def test_bootstrap_scenarios_cross_the_bridge(
    tmp_path: Path, socket_dir: Path, scenario_name: str
) -> None:
    parts = build_server_parts(tmp_path / "logs")
    for index in range(_HISTORY_EVENTS):
        parts.journal.publish_output("stdout", f"conformance-{index}")
    try:
        with _bridged(parts, socket_dir / "bridge.sock") as connection:
            batch = run_steps(connection, load_scenario(scenario_name))[-1]
        expected_floor = (
            parts.api.latest_sequence - 50 if scenario_name == "tail-bootstrap-spine-prepend" else 0
        )
        assert batch["history_after_sequence"] == expected_floor
    finally:
        parts.close()


@pytest.mark.parametrize("scenario_name", runner_group(_RUNNER, "control"))
def test_control_scenarios_cross_the_bridge(
    tmp_path: Path, socket_dir: Path, scenario_name: str
) -> None:
    parts = build_server_parts(tmp_path / "logs")
    try:
        with _bridged(parts, socket_dir / "bridge.sock") as connection:
            assert len(run_steps(connection, load_scenario(scenario_name))) == 1
    finally:
        parts.close()


@pytest.mark.parametrize("scenario_name", runner_group(_RUNNER, "tail-overflow"))
def test_tail_overflow_rebootstrap_crosses_the_bridge(
    tmp_path: Path, socket_dir: Path, scenario_name: str
) -> None:
    steps = load_scenario(scenario_name)["steps"]
    parts = build_server_parts(tmp_path / "logs")
    try:
        with _bridged(parts, socket_dir / "bridge.sock") as connection:
            connection.send(steps[0]["frame"])
            assert_frame_matches(connection.receive(), steps[1]["expect"])
            assert_frame_matches(connection.receive(), steps[2]["expect"])
            # One complete over-tail burst under the stream loop's condition, as in the
            # server runner, so the loop cannot send an ordinary continuation first.
            with parts.condition:
                for index in range(11):
                    parts.journal.publish_output("stdout", f"overflow-{index}")
            assert_frame_matches(connection.receive(), steps[3]["expect"])
    finally:
        parts.close()


def test_a_missing_socket_exits_as_run_gone_with_one_report_line(socket_dir: Path) -> None:
    connection = _BridgeConnection(socket_dir / "absent.sock")
    status, stderr = connection.finish()
    assert status == EXIT_STATUS[BridgeOutcome.RUN_GONE]
    report = BridgeReport.model_validate_json(stderr)
    assert report.outcome is BridgeOutcome.RUN_GONE
    assert report.exit_status == status
    assert stderr.count(b"\n") == 1


def test_a_server_close_relays_the_last_frame_then_exits_as_server_closed(
    socket_dir: Path,
) -> None:
    path = socket_dir / "closing.sock"
    final = {"type": "protocol_error", "message": "stream failed"}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen()
        listener.settimeout(DEADLOCK_GUARD_S)
        connection = _BridgeConnection(path)
        server, _ = listener.accept()
        with server:
            server.sendall(json.dumps(final).encode() + b"\n")
    assert connection.receive() == final
    # Stdin stays open until the bridge has ended: closing it here would race the bridge's
    # read of the server's close, and either end could win (status 0 or 3).
    status, stderr = connection.wait_for_exit()
    connection.close()
    assert status == EXIT_STATUS[BridgeOutcome.SERVER_CLOSED]
    assert BridgeReport.model_validate_json(stderr).outcome is BridgeOutcome.SERVER_CLOSED
