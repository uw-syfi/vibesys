"""The ``vibesys`` launcher never ends the engine it started before it exits.

r14: Ctrl-C reached both the launcher and its headless engine. The launcher's
``subprocess.call`` SIGKILLed the engine 0.25 seconds later, so the engine's
teardown never cancelled the run's Slurm job.

The launcher runs in a subprocess with a stand-in ``entrypoints.headless``
first on ``PYTHONPATH``, so ``main(["--headless"])`` starts the stand-in. The
stand-in reports each signal it receives, then exits with
:data:`_ENGINE_STATUS` only once the test releases it.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path

import pytest

from entrypoints.launcher import call_child

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
_LAUNCHER = (
    "import sys\n"
    f"sys.path.insert(0, {str(_SOURCE)!r})\n"
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
    fake = tmp_path / "fake" / "entrypoints"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("", encoding="utf-8")
    (fake / "headless.py").write_text(_ENGINE, encoding="utf-8")
    events, release = tmp_path / "events", tmp_path / "release"
    os.mkfifo(events)
    os.mkfifo(release)
    environment = {
        **os.environ,
        "PYTHONPATH": str(fake.parent),
        "ENGINE_EVENTS": str(events),
        "ENGINE_RELEASE": str(release),
    }
    launcher_alive, held_by_launcher = os.pipe()
    # lint-waiver: LW-731103 [S603]; the test runs the real launcher process.
    # > Calling `main` in-process would put the launcher in the test's process
    # > group and signal pytest itself; a fixed argv here is the boundary.
    launcher = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _LAUNCHER],
        cwd=tmp_path,
        env=environment,
        start_new_session=True,
        # The launcher starts the engine without it (close_fds).
        pass_fds=(held_by_launcher,),
    )
    os.close(held_by_launcher)
    engine = _Engine(events, launcher_alive)
    engine_pid: int | None = None
    try:
        ready = engine.next_event()
        assert ready is not None
        assert ready.startswith("ready ")
        engine_pid = int(ready.split()[1])
        if to_group:
            os.killpg(launcher.pid, number)
        else:
            os.kill(launcher.pid, number)

        # The engine sees the signal while the launcher still waits for it.
        assert engine.next_event() == f"signal {int(number)}"
        with release.open("wb", buffering=0) as stream:
            stream.write(b"x")
        assert launcher.wait() == _ENGINE_STATUS
    finally:
        engine.close()
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait()
        if engine_pid is not None:
            # At the merge base the engine outlives a SIGTERMed launcher.
            with suppress(ProcessLookupError):
                os.kill(engine_pid, signal.SIGKILL)


# In-process: the child signals this process (its launcher) itself, so the
# test needs no timing. These run `call_child` on pytest's main thread, where
# its handlers apply, and prove pytest is neither interrupted nor terminated.
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


@pytest.mark.parametrize(
    ("number", "forwarded"),
    [(signal.SIGINT, False), (signal.SIGTERM, True), (signal.SIGHUP, True)],
    ids=["sigint-waits", "sigterm-forwarded", "sighup-forwarded"],
)
def test_call_child_waits_through_sigint_and_forwards_termination(
    number: signal.Signals, *, forwarded: bool
) -> None:
    status = call_child(
        [
            sys.executable,
            "-c",
            _SIGNAL_PARENT,
            number.name,
            "forwarded" if forwarded else "waited",
        ]
    )

    assert status == _ENGINE_STATUS


def test_call_child_reports_a_signal_death_as_a_shell_does() -> None:
    status = call_child(
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
    )

    assert status == 128 + signal.SIGKILL


def test_call_child_from_a_worker_thread_only_waits() -> None:
    statuses: list[int] = []
    worker = threading.Thread(
        target=lambda: statuses.append(call_child([sys.executable, "-c", "raise SystemExit(3)"]))
    )
    worker.start()
    worker.join()

    assert statuses == [3]
