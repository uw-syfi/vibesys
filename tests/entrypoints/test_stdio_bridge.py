"""The stdio bridge command: arguments, exit status and the stderr report, on simulated I/O.

The relay itself is covered in ``tests/server/test_stdio_bridge.py``; the real process
against a real server in ``tests/e2e/test_stdio_bridge.py``.
"""

from __future__ import annotations

import pytest

from entrypoints.stdio_bridge import bridge
from server.stdio_bridge import EXIT_STATUS, BridgeOutcome, ClientStreams
from server.stdio_bridge_report import BridgeReport
from vs_sim.api.testing import SimNetwork, SimThreads

_ADDRESS = "run.sock"


class _Input:
    """A client that sends ``data`` and then closes its side."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, max_bytes: int) -> bytes:
        chunk, self._data = self._data[:max_bytes], self._data[max_bytes:]
        return chunk


class _Recorder:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data += data


def _bridge(argv: list[str], *, listening: bool) -> tuple[int, bytes]:
    threads = SimThreads()
    network = SimNetwork(threads)
    errors = _Recorder()

    def main() -> int:
        listener = network.listen(_ADDRESS) if listening else None
        try:
            return bridge(
                argv,
                network=network,
                client=ClientStreams(_Input(b'{"type":"command.pause"}\n'), _Recorder()),
                errors=errors,
                threads=threads,
            )
        finally:
            if listener is not None:
                listener.close()

    return threads.run(main), bytes(errors.data)


def test_a_client_that_closes_exits_zero_without_a_report() -> None:
    assert _bridge(["--socket", _ADDRESS], listening=True) == (0, b"")


def test_a_run_that_is_gone_exits_with_its_status_and_one_report_line() -> None:
    status, errors = _bridge(["--socket", _ADDRESS], listening=False)
    assert status == EXIT_STATUS[BridgeOutcome.RUN_GONE]
    assert errors.count(b"\n") == 1
    report = BridgeReport.model_validate_json(errors)
    assert (report.outcome, report.exit_status) == (BridgeOutcome.RUN_GONE, status)


@pytest.mark.parametrize("argv", [[], ["--socket"], ["--socket", "a", "--socket-typo", "b"]])
def test_a_missing_or_malformed_target_is_a_usage_error(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _bridge(argv, listening=True)
    assert raised.value.code == 2
