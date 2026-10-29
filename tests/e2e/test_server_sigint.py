"""A real SIGINT, even twice, stops a foreground server's run through its cooperative stop.

Review periodic-1330 MED-1: one Ctrl-C on ``vibesys --web`` reached the server child twice,
once from the terminal and once forwarded by the launcher. The server ran its run under
``asyncio.run`` with the default SIGINT handling, so the first SIGINT cancelled the run's
driver (a drain) and the second raised ``KeyboardInterrupt`` inside that drain: the run
ended by hard cancellation and was journaled as "launcher_terminated (SIGTERM)".

The server runs through the public ``entrypoints.server.main`` on a fake run service, in a
forked child of the test (``run_in_child``), so no signal reaches the pytest process. The
fake run sends its own process SIGINT twice, as a terminal and a launcher did.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from entrypoints import cli
from entrypoints.server import main
from server.host_api import RUN_EVENTS_FILE
from vibesys.api import PauseNowRequest, RunResult, RunStopped, StopMode
from vibesys.api.testing import FakeRuns
from vs_sim.api.testing import run_in_child

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.api import Runs


class _SelfInterruptingRun:
    """A run that sends its process the given signals, then ends once each SIGINT's stop reaches it.

    Stops arrive on the event loop (``stop``) or from the server's signal or transport
    threads (``submit``); either wakes the run through the loop, thread-safely. The run
    waits for every SIGINT's stop, not only the first: a run that ended at the first would
    release its control, and a later SIGINT, finding no run to stop, would end the server
    as SIGTERM does, depending on how the signal threads were scheduled.
    """

    def __init__(self, signals: tuple[signal.Signals, ...]) -> None:
        self._signals = signals
        self._awaited = signals.count(signal.SIGINT)
        self.stops: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped: asyncio.Event | None = None

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def on_committed_view(self, listener: Callable[..., None]) -> None:
        del listener

    def on_ready(self, listener: Callable[..., None]) -> None:
        del listener

    async def await_result(self) -> RunResult:
        self._loop = asyncio.get_running_loop()
        self._stopped = asyncio.Event()
        for number in self._signals:
            os.kill(os.getpid(), number)
            # Let each signal's handler run before the next arrives, as a terminal's
            # Ctrl-C and the launcher's forwarded copy did, a few milliseconds apart.
            await asyncio.sleep(0)
        await self._stopped.wait()
        raise RunStopped

    def _stop(self, mode: StopMode) -> None:
        self.stops.append(mode.value)
        if len(self.stops) < self._awaited:
            return
        if self._loop is not None and self._stopped is not None:
            self._loop.call_soon_threadsafe(self._stopped.set)

    def stop(self, mode: StopMode = StopMode.DRAIN) -> None:
        self._stop(mode)

    def submit(self, request: object) -> None:
        if isinstance(request, PauseNowRequest):
            self._stop(StopMode.CANCEL)


def _serve_interrupted_run(
    base: Path, signals: tuple[signal.Signals, ...]
) -> tuple[str, list[str], list[dict[str, object]]]:
    """Serve one foreground run that is sent ``signals``; return how it all ended.

    Call through ``run_in_child``: it sets the environment, swaps request building and
    takes real signals. Returns how ``main`` ended, the stop modes the run received, and
    the kinds of every journaled interruption.
    """
    os.environ["XDG_RUNTIME_DIR"] = str(base)
    # A detached server's child: no client to wait for, and it exits when the run ends.
    os.environ["VIBESYS_DETACHED_CHILD"] = "1"
    socket_dir = base / "vibesys" / "run"
    socket_dir.mkdir(parents=True)
    # test-isolation: the child swaps CLI request building for a fixed request, as
    # tests/e2e/foreground_run_child.py does; the server runtime and its signals are real.
    cli.parse_cli_invocation = lambda _argv: object()  # ty: ignore[invalid-assignment]  # LW-133002 [invalid-assignment]; the child replaces CLI parsing with a fixed fake
    cli.build_run_request = lambda _invocation: SimpleNamespace(  # ty: ignore[invalid-assignment]  # LW-133003 [invalid-assignment]; the child replaces request building with a fixed fake
        resume=None, resolved_run_id="run-1", resolved_project_root=None
    )
    run = _SelfInterruptingRun(signals)
    runs = FakeRuns(
        lambda _request, _publish: run,
        identity=lambda _request: "run-1",
        is_resume=lambda _request: False,
    )
    try:
        main(
            ["--control-socket", str(socket_dir / "control.sock"), "--detach", "--local"],
            runs=cast("Runs", runs),
        )
    except KeyboardInterrupt:
        ended = "interrupted"
    except SystemExit as exit_:
        ended = f"exit {exit_.code}"
    else:
        ended = "returned"
    interruptions = [
        event["data"]
        for line in (socket_dir / RUN_EVENTS_FILE).read_text(encoding="utf-8").splitlines()
        if (event := json.loads(line)).get("type") == "run_interrupted"
    ]
    return ended, run.stops, interruptions


@pytest.mark.regression
def test_two_sigints_stop_a_foreground_servers_run_once_and_cancel_its_work(
    tmp_path: Path,
) -> None:
    """The run takes a stop that cancels its work in flight; nothing hard-cancels it."""
    ended, stops, interruptions = run_in_child(
        lambda: _serve_interrupted_run(tmp_path, (signal.SIGINT, signal.SIGINT))
    )

    assert ended == "returned"
    assert stops
    assert set(stops) == {StopMode.CANCEL.value}
    assert interruptions == []


@pytest.mark.regression
@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP], ids=["sigterm", "sighup"])
def test_a_launchers_termination_ends_the_run_now_and_is_journaled_by_its_signal(
    tmp_path: Path, number: signal.Signals
) -> None:
    """periodic-1330 MED-1: every interruption was journaled as SIGTERM, and SIGHUP killed
    the server with no teardown; each now ends the run through cleanup, named by its signal.
    """
    ended, _stops, interruptions = run_in_child(lambda: _serve_interrupted_run(tmp_path, (number,)))

    assert ended == "interrupted"
    assert interruptions == [
        {"kind": "run_interrupted", "reason": "launcher_terminated", "signal": number.name}
    ]
