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
import termios
import threading
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from entrypoints.launcher import call_child
from entrypoints.launcher import main as launcher_main
from vibesys.api import boot_trace
from vs_sim.api import (
    LAUNCHER_LIFELINE_ENV,
    InheritedStdioLauncher,
    LoopSignalSource,
    follow_launcher,
)
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
events.write(f"ready {{os.getpid()}} {{os.getpgrp()}}\\n")
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
) -> tuple[int, int]:
    """Signal the launcher once its engine is ready; return the engine's pid and process group.

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
    _, pid, group = ready.split()
    return int(pid), int(group)


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
            pids.append(_drive_engine(environment, launcher_alive, send, number)[0])
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
        (signal.SIGINT, False),
    ],
    ids=[
        "ctrl-c-to-group",
        "hangup-to-group",
        "sigterm-to-launcher",
        "sighup-to-launcher",
        # r3: an operator's `kill -INT` named the launcher, which kept it from the engine.
        "sigint-to-launcher",
    ],
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
    [(signal.SIGINT, True), (signal.SIGHUP, True), (signal.SIGTERM, False), (signal.SIGINT, False)],
    ids=["ctrl-c-to-group", "hangup-to-group", "sigterm-to-launcher", "sigint-to-launcher"],
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
        engine_pid, _ = _drive_engine(environment, launcher_alive, send, number)
        assert launcher.wait(timeout=HANG_GUARD_S) == _ENGINE_STATUS
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=HANG_GUARD_S)
        if engine_pid is not None:
            # At the merge base the engine outlives a SIGTERMed launcher.
            with suppress(ProcessLookupError):
                os.kill(engine_pid, signal.SIGKILL)


@pytest.mark.regression
def test_a_ctrl_c_reaches_the_engine_only_through_the_launcher(tmp_path: Path) -> None:
    """periodic-1330 MED-1: one Ctrl-C reached the engine twice, from the terminal and the launcher.

    The engine leads a session of its own, so a terminal's Ctrl-C (sent to the
    launcher's group) does not reach it directly: it arrives once, as the launcher's copy.
    """
    environment = _write_engine(tmp_path)
    launcher_alive, held_by_launcher = os.pipe()
    # lint-waiver: LW-133001 [S603]; the test runs the real launcher process.
    # > Calling `main` in the test's process would signal pytest itself; a fixed argv is the boundary.
    launcher = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _launcher_script(tmp_path / "stub")],
        cwd=tmp_path,
        env={**os.environ, **environment},
        start_new_session=True,
        pass_fds=(held_by_launcher,),
    )
    os.close(held_by_launcher)
    engine_pid: int | None = None
    try:
        engine_pid, engine_group = _drive_engine(
            environment,
            launcher_alive,
            partial(os.killpg, launcher.pid, signal.SIGINT),
            signal.SIGINT,
        )
        assert launcher.wait(timeout=HANG_GUARD_S) == _ENGINE_STATUS
        assert engine_group == engine_pid != launcher.pid
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=HANG_GUARD_S)
        if engine_pid is not None:
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
    [
        (signal.SIGINT, True),
        (signal.SIGTERM, True),
        (signal.SIGHUP, True),
        (signal.SIGQUIT, True),
        # PR #3209 MED-2: the child leads its own session, so a shell's `fg` (SIGCONT to the
        # launcher's group) reaches it only through the launcher.
        (signal.SIGCONT, True),
    ],
    ids=[
        "sigint-forwarded",
        "sigterm-forwarded",
        "sighup-forwarded",
        "sigquit-forwarded",
        "sigcont-forwarded",
    ],
)
def test_call_child_forwards_every_shutdown_signal(
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


# An engine that hands its launcher's lifeline to the product (``follow_launcher``), then
# reports the first stop signal it receives.
_FOLLOWING_ENGINE = """
import os, signal, sys
handled = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
signal.pthread_sigmask(signal.SIG_BLOCK, handled)
events = open(os.environ["ENGINE_EVENTS"], "w", buffering=1)
from vs_sim.api import follow_launcher
follow_launcher(os.environ)
events.write(f"ready {os.getpid()}\\n")
sys.stdout.write("the engine writes to its terminal\\n")
sys.stdout.flush()
events.write("wrote\\n")
events.write(f"signal {signal.sigwait(handled)}\\n")
sys.exit(7)
"""


class _EngineLines:
    """The following engine's event lines, each awaited under the hang guard only."""

    def __init__(self, events: Path) -> None:
        self._events = os.open(events, os.O_RDONLY | os.O_NONBLOCK)
        self._buffer = b""

    def next_line(self) -> str:
        while b"\n" not in self._buffer:
            readable, _, _ = select.select([self._events], [], [], HANG_GUARD_S)
            assert readable, "the engine went quiet"
            chunk = os.read(self._events, 4096)
            assert chunk, "the engine exited"
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode()

    def close(self) -> None:
        os.close(self._events)


def _following_launcher(
    base: Path, *, terminal: int | None, engine: str = _FOLLOWING_ENGINE
) -> tuple[subprocess.Popen[bytes], dict[str, str]]:
    """Start the real launcher, as a session of its own, over the following engine.

    With *terminal* (a pty's follower side), the launcher takes it as its controlling
    terminal and its standard streams, and so is the terminal's foreground process group.
    """
    environment = _write_engine(base)
    (base / "fake" / "entrypoints" / "headless.py").write_text(engine, encoding="utf-8")
    script = _launcher_script(base / "stub")
    if terminal is not None:
        script = "import fcntl, termios\nfcntl.ioctl(0, termios.TIOCSCTTY, 0)\n" + script
    # lint-waiver: LW-320901 [S603]; the test runs the real launcher process.
    # > Calling `main` in the test's process would signal pytest itself; a fixed argv is the boundary.
    launcher = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=base,
        env={**os.environ, **environment},
        start_new_session=True,
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
    )
    return launcher, environment


def _end(launcher: subprocess.Popen[bytes], engine_pid: int | None) -> None:
    if launcher.poll() is None:
        launcher.kill()
        launcher.wait(timeout=HANG_GUARD_S)
    if engine_pid is not None:
        with suppress(ProcessLookupError):
            os.kill(engine_pid, signal.SIGKILL)


@pytest.mark.regression
def test_an_engine_whose_launcher_is_killed_hangs_up(tmp_path: Path) -> None:
    """PR #3209 MED-1: SIGKILL of the launcher left its engine running, unreachable.

    The engine leads its own session, so no terminal signal reaches it once its
    launcher is gone. It hears the launcher's death and hangs up (SIGHUP), as a closed
    terminal used to tell it.
    """
    launcher, environment = _following_launcher(tmp_path, terminal=None)
    engine_pid: int | None = None
    lines = _EngineLines(Path(environment["ENGINE_EVENTS"]))
    try:
        ready = lines.next_line()
        assert ready.startswith("ready ")
        engine_pid = int(ready.split()[1])
        assert lines.next_line() == "wrote"
        launcher.kill()
        launcher.wait(timeout=HANG_GUARD_S)
        assert lines.next_line() == f"signal {int(signal.SIGHUP)}"
    finally:
        lines.close()
        _end(launcher, engine_pid)


@pytest.mark.regression
def test_an_engine_writes_its_terminal_under_stty_tostop(tmp_path: Path) -> None:
    """PR #3209 MED-2: with `stty tostop`, the engine stopped at its first terminal write.

    The engine was a background process group on the launcher's terminal, so the kernel
    stopped it with SIGTTOU when ``tostop`` was set. In a session of its own the terminal
    is not its controlling terminal: it writes on, and still hears the launcher's Ctrl-C.
    """
    leader, follower = os.openpty()
    attributes = termios.tcgetattr(follower)
    attributes[3] |= termios.TOSTOP
    termios.tcsetattr(follower, termios.TCSANOW, attributes)
    launcher, environment = _following_launcher(tmp_path, terminal=follower)
    os.close(follower)
    engine_pid: int | None = None
    lines = _EngineLines(Path(environment["ENGINE_EVENTS"]))
    try:
        ready = lines.next_line()
        assert ready.startswith("ready ")
        engine_pid = int(ready.split()[1])
        assert lines.next_line() == "wrote"
        os.killpg(launcher.pid, signal.SIGINT)
        assert lines.next_line() == f"signal {int(signal.SIGINT)}"
        assert launcher.wait(timeout=HANG_GUARD_S) == _ENGINE_STATUS
    finally:
        lines.close()
        _end(launcher, engine_pid)
        os.close(leader)


# A command that turns off its terminal's echo, as ssh does to read a password or a 2FA
# code from ``/dev/tty``; it reports whether it could reach a terminal at all.
_ECHO_OFF = """
import os, termios
try:
    tty = os.open("/dev/tty", os.O_RDWR)
except OSError:
    print("no-terminal")
else:
    attributes = termios.tcgetattr(tty)
    attributes[3] &= ~termios.ECHO
    termios.tcsetattr(tty, termios.TCSANOW, attributes)
    print("echo-off")
"""

# A following engine that runs ``_ECHO_OFF`` as a run's own command (``OsProcesses``, as
# the Slurm transport runs ssh) and reports how it ended.
_PROMPTING_ENGINE = f"""
import os, signal, sys
handled = {{signal.SIGINT, signal.SIGTERM, signal.SIGHUP}}
signal.pthread_sigmask(signal.SIG_BLOCK, handled)
events = open(os.environ["ENGINE_EVENTS"], "w", buffering=1)
from vs_sim.api import DISCARD, OsProcesses, ProcessOutcome, ProcessSpec, follow_launcher
follow_launcher(os.environ)
events.write(f"ready {{os.getpid()}}\\n")
run = OsProcesses().run(
    ProcessSpec(argv=(sys.executable, "-c", {_ECHO_OFF!r})), timeout={HANG_GUARD_S / 2}, output=DISCARD
)
ended = run.stdout.decode().strip() if isinstance(run, ProcessOutcome) else type(run).__name__
events.write(f"command {{ended}}\\n")
events.write(f"signal {{signal.sigwait(handled)}}\\n")
sys.exit(7)
"""


@pytest.mark.regression
def test_no_command_a_run_starts_can_turn_its_terminals_echo_off(tmp_path: Path) -> None:
    """PR #3209 recheck LOW-2: an ssh prompt left the operator's terminal with echo off.

    The run child ignored SIGTTOU (for ``stty tostop``) and its commands inherited that, so
    a command that opened ``/dev/tty`` to prompt (ssh asking for a password or a 2FA code)
    could turn echo off from the background, then stop at its read. No command a run
    starts may reach the operator's terminal: it fails to open one, and echo stays on.
    """
    leader, follower = os.openpty()
    launcher, environment = _following_launcher(
        tmp_path, terminal=follower, engine=_PROMPTING_ENGINE
    )
    os.close(follower)
    engine_pid: int | None = None
    lines = _EngineLines(Path(environment["ENGINE_EVENTS"]))
    try:
        ready = lines.next_line()
        assert ready.startswith("ready ")
        engine_pid = int(ready.split()[1])
        assert lines.next_line() == "command no-terminal"
        assert termios.tcgetattr(leader)[3] & termios.ECHO
        os.killpg(launcher.pid, signal.SIGINT)
        assert lines.next_line() == f"signal {int(signal.SIGINT)}"
        assert launcher.wait(timeout=HANG_GUARD_S) == _ENGINE_STATUS
    finally:
        lines.close()
        _end(launcher, engine_pid)
        os.close(leader)


def _lifeline_reaches_a_grandchild() -> tuple[int, bool]:
    """Follow a lifeline, then start a command that inherits every inheritable descriptor.

    Call through ``run_in_child``: it blocks SIGHUP and hangs this process up. Returns the
    command's status (0: it held the lifeline) and whether this process then hung up.
    """
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGHUP})
    read, held = os.pipe()
    # ``dup2`` leaves its copy inheritable, as ``pass_fds`` leaves it in a launched child.
    given = os.dup2(read, read + 16)
    os.close(read)
    follow_launcher({LAUNCHER_LIFELINE_ENV: str(given)})
    # lint-waiver: LW-191001 [S603]; the test runs a fixed probe of its own descriptors.
    # > A Fake launcher cannot show what a real exec inherits, which is the property here.
    probe = subprocess.run(  # noqa: S603
        [sys.executable, "-c", f"import os; os.fstat({given})"],
        close_fds=False,
        capture_output=True,
        check=False,
        timeout=HANG_GUARD_S,
    )
    os.close(held)
    return probe.returncode, signal.sigtimedwait({signal.SIGHUP}, HANG_GUARD_S) is not None


@pytest.mark.regression
def test_no_command_the_run_starts_inherits_its_lifeline() -> None:
    """PR #3209 recheck LOW-1: a command started with ``close_fds=False`` held the lifeline.

    Only the launcher holds the write end, so a leaked read end never delays the hang-up,
    but a long-lived command kept a descriptor that is not its own.
    """
    status, hung_up = run_in_child(_lifeline_reaches_a_grandchild, timeout_s=3 * HANG_GUARD_S)

    assert status != 0
    assert hung_up


def _hang_up_with_a_closed_lifeline() -> bool:
    """Follow a lifeline that was closed before its watcher could close it; whether it hung up."""
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGHUP})
    given, held = os.pipe()
    os.close(given)
    os.close(held)
    follow_launcher({LAUNCHER_LIFELINE_ENV: str(given)})
    return signal.sigtimedwait({signal.SIGHUP}, HANG_GUARD_S) is not None


@pytest.mark.regression
def test_a_lifeline_closed_before_its_end_still_hangs_up() -> None:
    """PR #3209 recheck LOW-3: closing an already closed lifeline raised before the SIGHUP.

    Whatever ends the watcher's read, end-of-file or a descriptor gone bad, the process
    hangs up: it can no longer tell that its launcher lives.
    """
    assert run_in_child(_hang_up_with_a_closed_lifeline, timeout_s=3 * HANG_GUARD_S)
