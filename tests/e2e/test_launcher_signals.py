"""The ``vibesys`` launcher, with real processes and signals, never ends the engine it started before it exits.

r14: Ctrl-C reached both the launcher and its headless engine. The launcher's
``subprocess.call`` SIGKILLed the engine 0.25 seconds later, so the engine's
teardown never cancelled the run's Slurm job.

The launcher runs with a stand-in ``entrypoints.headless`` first on ``PYTHONPATH``, so
``main(["--headless"])`` starts the stand-in. The stand-in reports each signal it receives, then
exits with :data:`_ENGINE_STATUS` only once the test releases it. Every signal and delivery runs
the launcher in a forked child of the test (its own session); one real launcher process per signal
proves the console entry point's wiring.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from entrypoints.launcher import call_child
from entrypoints.launcher import main as launcher_main
from vibesys.api import boot_trace
from vs_sim.api import InheritedStdioLauncher, LoopSignalSource
from vs_sim.api.testing import HANG_GUARD_S, join_or_fail, run_in_child

if TYPE_CHECKING:
    from collections.abc import Callable

# The launcher imports the boot trace on first use; loading it here lets every forked child share it.
_BOOT_TRACE = boot_trace
_SOURCE = Path(__file__).resolve().parents[2] / "src"
_ENGINE_STATUS = 7
_ENGINE = f"""
import os, signal, sys
handled = {{signal.SIGINT, signal.SIGTERM, signal.SIGHUP}}
signal.pthread_sigmask(signal.SIG_BLOCK, handled)
release = os.open(os.environ["ENGINE_RELEASE"], os.O_RDWR)
events = open(os.environ["ENGINE_EVENTS"], "w", buffering=1)
events.write(f"ready {{os.getpid()}}\\n")
events.write(f"signal {{signal.sigwait(handled)}}\\n")
os.read(release, 1)
sys.exit({_ENGINE_STATUS})
"""
_BOOT_TRACE_STUB = """
class boot_trace:
    BootTrace = staticmethod(lambda **_clocks: None)
    install = staticmethod(lambda _trace: None)
    mark_launch = staticmethod(lambda: None)
    child_env = staticmethod(dict)
"""


def _launcher_script(stub_root: Path) -> str:
    return (
        "import sys\n"
        f"sys.path.insert(0, {str(_SOURCE)!r})\n"
        f"sys.path.insert(0, {str(stub_root)!r})\n"
        "from entrypoints.launcher import main\n"
        "sys.exit(main(['--headless']))\n"
    )


class _Engine:
    """The stand-in engine's event stream, read while the launcher lives."""

    def __init__(self, events: Path, launcher_alive: int) -> None:
        # Reaches end-of-file when the launcher, its only writer, exits.
        self._launcher = launcher_alive
        # Opening blocks until the engine opens its end for writing.
        self._events = events.open(encoding="utf-8")

    def next_event(self) -> str | None:
        """Return the engine's next line, or ``None`` once the launcher exited."""
        readable, _, _ = select.select([self._events, self._launcher], [], [])
        if self._events in readable:
            return self._events.readline().strip()
        return None

    def close(self) -> None:
        self._events.close()
        os.close(self._launcher)


def _write_engine(base: Path) -> dict[str, str]:
    """Put the stand-in engine first on ``PYTHONPATH``; return the environment it needs."""
    fake = base / "fake" / "entrypoints"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("", encoding="utf-8")
    (fake / "headless.py").write_text(_ENGINE, encoding="utf-8")
    # The real launcher process would import the whole engine for its boot trace, which the
    # stand-in engine and this test do not use.
    stub = base / "stub" / "vibesys"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text("", encoding="utf-8")
    (stub / "api.py").write_text(_BOOT_TRACE_STUB, encoding="utf-8")
    events, release = base / "events", base / "release"
    os.mkfifo(events)
    os.mkfifo(release)
    return {
        "PYTHONPATH": str(fake.parent),
        "ENGINE_EVENTS": str(events),
        "ENGINE_RELEASE": str(release),
    }


def _drive_engine(
    environment: dict[str, str],
    launcher_alive: int,
    send: Callable[[], None],
    number: signal.Signals,
) -> int:
    """Signal the launcher once its engine is ready; return the engine's pid.

    Asserts the engine sees the signal while the launcher still waits, then releases the
    engine. ``launcher_alive`` reaches end-of-file when the launcher exits.
    """
    engine = _Engine(Path(environment["ENGINE_EVENTS"]), launcher_alive)
    try:
        ready = engine.next_event()
        assert ready is not None
        assert ready.startswith("ready ")
        send()
        # The engine sees the signal while the launcher still waits for it.
        assert engine.next_event() == f"signal {int(number)}"
        with Path(environment["ENGINE_RELEASE"]).open("wb", buffering=0) as stream:
            stream.write(b"x")
    finally:
        engine.close()
    return int(ready.split()[1])


def _launch_in_child(
    environment: dict[str, str], number: signal.Signals, *, to_group: bool
) -> tuple[int, str | None]:
    """Run ``main(["--headless"])`` here, as its own session, while a thread signals it.

    Call through ``run_in_child``: it changes the session, the environment and signal state.
    Returns the launcher's status and what the driving thread asserted, if it failed.
    """
    os.setsid()
    os.environ.update(environment)
    launcher_alive, held_by_launcher = os.pipe()
    pids: list[int] = []
    failures: list[str] = []

    def send() -> None:
        # The group is this session: the launcher, its engine, and nothing of the test's.
        if to_group:
            os.killpg(os.getpgrp(), number)
        else:
            os.kill(os.getpid(), number)

    def drive() -> None:
        try:
            pids.append(_drive_engine(environment, launcher_alive, send, number))
        except AssertionError as error:
            failures.append(repr(error))

    driver = threading.Thread(target=drive)
    driver.start()
    try:
        status = launcher_main(["--headless"])
    finally:
        # The launcher's end of the pipe: the driver sees end-of-file once it has exited.
        os.close(held_by_launcher)
        join_or_fail(driver)
        for pid in pids:
            # At the merge base the engine outlives a SIGTERMed launcher.
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    return status, failures[0] if failures else None


@pytest.mark.parametrize(
    ("number", "to_group"),
    [
        (signal.SIGINT, True),
        (signal.SIGHUP, True),
        (signal.SIGTERM, False),
        (signal.SIGHUP, False),
    ],
    ids=["ctrl-c-to-group", "hangup-to-group", "sigterm-to-launcher", "sighup-to-launcher"],
)
def test_the_launcher_waits_for_its_engine_to_finish_tearing_down(
    tmp_path: Path, number: signal.Signals, *, to_group: bool
) -> None:
    environment = _write_engine(tmp_path)

    status, failure = run_in_child(
        partial(_launch_in_child, environment, number, to_group=to_group)
    )

    assert failure is None
    assert status == _ENGINE_STATUS


@pytest.mark.parametrize(
    ("number", "to_group"),
    [(signal.SIGINT, True), (signal.SIGHUP, True), (signal.SIGTERM, False)],
    ids=["ctrl-c-to-group", "hangup-to-group", "sigterm-to-launcher"],
)
def test_the_launcher_process_waits_for_its_engine_to_finish_tearing_down(
    tmp_path: Path, number: signal.Signals, *, to_group: bool
) -> None:
    """One real launcher process per signal: the console entry point's own wiring."""
    environment = _write_engine(tmp_path)
    launcher_alive, held_by_launcher = os.pipe()
    # lint-waiver: LW-731103 [S603]; the test runs the real launcher process.
    # > Calling `main` in the test's process would signal pytest itself; a fixed argv is the boundary.
    launcher = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _launcher_script(tmp_path / "stub")],
        cwd=tmp_path,
        env={**os.environ, **environment},
        start_new_session=True,
        # The launcher starts the engine without it (close_fds).
        pass_fds=(held_by_launcher,),
    )
    os.close(held_by_launcher)
    engine_pid: int | None = None

    def send() -> None:
        if to_group:
            os.killpg(launcher.pid, number)
        else:
            os.kill(launcher.pid, number)

    try:
        engine_pid = _drive_engine(environment, launcher_alive, send, number)
        assert launcher.wait(timeout=HANG_GUARD_S) == _ENGINE_STATUS
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=HANG_GUARD_S)
        if engine_pid is not None:
            # At the merge base the engine outlives a SIGTERMed launcher.
            with suppress(ProcessLookupError):
                os.kill(engine_pid, signal.SIGKILL)


# `call_child` on a real child that signals its launcher itself (a signal reaches the
# process that runs `call_child`, so each case runs in a forked child of the test).
_SIGNAL_PARENT = """
import os, signal, sys
number = signal.Signals[sys.argv[1]]
forwarded = sys.argv[2] == "forwarded"
signal.pthread_sigmask(signal.SIG_BLOCK, {number})
os.kill(os.getppid(), number)
if forwarded:
    signal.sigwait({number})
sys.exit(7)
"""


def _call_signalling_child(name: str, mode: str) -> int:
    return _call_real_child([sys.executable, "-c", _SIGNAL_PARENT, name, mode])


def _call_real_child(argv: list[str]) -> int:
    return call_child(argv, children=InheritedStdioLauncher(), signals=LoopSignalSource())


@pytest.mark.parametrize(
    ("number", "forwarded"),
    [(signal.SIGINT, False), (signal.SIGTERM, True), (signal.SIGHUP, True)],
    ids=["sigint-waits", "sigterm-forwarded", "sighup-forwarded"],
)
def test_call_child_waits_through_sigint_and_forwards_termination(
    number: signal.Signals, *, forwarded: bool
) -> None:
    status = run_in_child(
        partial(_call_signalling_child, number.name, "forwarded" if forwarded else "waited")
    )

    assert status == _ENGINE_STATUS


def test_call_child_reports_a_signal_death_as_a_shell_does() -> None:
    status = _call_real_child(
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
    )

    assert status == 128 + signal.SIGKILL


def test_call_child_from_a_worker_thread_only_waits() -> None:
    statuses: list[int] = []
    worker = threading.Thread(
        target=lambda: statuses.append(
            _call_real_child([sys.executable, "-c", "raise SystemExit(3)"])
        )
    )
    worker.start()
    join_or_fail(worker)

    assert statuses == [3]
