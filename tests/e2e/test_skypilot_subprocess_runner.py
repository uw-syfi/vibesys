"""``SubprocessCommandRunner`` against a real child process.

The unit tests in ``tests/vibesys/skypilot/test_runner.py`` drive ``SkyPilotJobRunner`` through
a scripted command runner. What only a real process shows is here: both output pipes are
drained while a sink fails, and timeout cleanup kills descendants that inherited those pipes.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.skypilot import SubprocessCommandRunner
from vs_sim.api import PipedProcess, ProcessTimeoutError, SubprocessPipedProcessLauncher

if TYPE_CHECKING:
    from io import TextIOBase
    from pathlib import Path


class _ReadyProcess:
    """Synchronize the command timeout after a real child reports readiness."""

    def __init__(self, process: PipedProcess, ready: threading.Event) -> None:
        self._process = process
        self._ready = ready
        self._synchronized = False

    @property
    def stdout(self) -> TextIOBase:
        return self._process.stdout

    @property
    def stderr(self) -> TextIOBase:
        return self._process.stderr

    def wait(self, timeout: float | None) -> int:
        if not self._synchronized:
            self._synchronized = True
            self._ready.wait()
        return self._process.wait(timeout)

    def kill_group(self) -> None:
        self._process.kill_group()

    def cancel_output(self) -> None:
        self._process.cancel_output()


class _ReadyProcessLauncher:
    def __init__(self, ready: threading.Event) -> None:
        self._launcher = SubprocessPipedProcessLauncher()
        self._ready = ready

    def start(self, argv: tuple[str, ...], *, cwd: Path | None = None) -> _ReadyProcess:
        return _ReadyProcess(self._launcher.start(argv, cwd=cwd), self._ready)


class _ReadySink:
    """Observe streamed readiness through the cancellable sink contract."""

    def __init__(self, ready: threading.Event) -> None:
        self.lines: list[str] = []
        self._ready = ready

    def __call__(self, line: str) -> None:
        self.lines.append(line)
        if line == "ready\n":
            self._ready.set()

    def cancel(self) -> None:
        """The callback never blocks, so it needs no active interruption."""


def test_subprocess_runner_drains_pipes_and_propagates_sink_failure() -> None:
    calls = 0

    def broken_sink(_: str) -> None:
        nonlocal calls
        calls += 1
        raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        SubprocessCommandRunner().run(
            (
                sys.executable,
                "-c",
                "import sys; [print(i) for i in range(1000)]; print('err', file=sys.stderr)",
            ),
            stdout_sink=broken_sink,
        )

    assert calls == 1


def test_subprocess_runner_captures_both_streams_and_the_exit_status() -> None:
    result = SubprocessCommandRunner().run(
        (
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        )
    )

    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")


@pytest.mark.skipif(os.name != "posix", reason="VibeSys publishes only POSIX builds")
@pytest.mark.timeout(20)
def test_subprocess_timeout_kills_descendants_that_inherit_output_pipes(tmp_path: Path) -> None:
    survived = tmp_path / "descendant-survived"
    program = f"""
import os
import pathlib
import signal

read_fd, write_fd = os.pipe()
child = os.fork()
if child == 0:
    os.close(write_fd)
    os.read(read_fd, 1)
    pathlib.Path({str(survived)!r}).write_text("survived", encoding="utf-8")
    os._exit(0)
os.close(read_fd)
print("ready", flush=True)
signal.pause()
"""
    ready = threading.Event()
    stdout = _ReadySink(ready)

    with pytest.raises(ProcessTimeoutError):
        SubprocessCommandRunner(processes=_ReadyProcessLauncher(ready)).run(
            (sys.executable, "-c", program), timeout=0, stdout_sink=stdout
        )

    assert stdout.lines == ["ready\n"]
    assert not survived.exists()
