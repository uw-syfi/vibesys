"""``SubprocessPipedProcessLauncher`` against real processes.

Every wait here is bounded by ``HANG_GUARD_S`` only to stop a hang; each
result is decided by what the child wrote or how it was signalled, never by
elapsed time.
"""

from __future__ import annotations

import signal
import sys

import pytest

from vs_sim.api import ProcessTimeoutError, SubprocessPipedProcessLauncher
from vs_sim.api.testing import HANG_GUARD_S


def _python(source: str) -> tuple[str, ...]:
    return (sys.executable, "-c", source)


def test_merged_output_keeps_the_order_the_child_wrote_both_streams() -> None:
    process = SubprocessPipedProcessLauncher().start(
        _python(
            "import sys\n"
            "for i in range(3):\n"
            "    sys.stdout.write(f'out{i}\\n'); sys.stdout.flush()\n"
            "    sys.stderr.write(f'err{i}\\n'); sys.stderr.flush()\n"
        ),
        merge_stderr=True,
    )

    output = process.stdout.read()

    assert output == "out0\nerr0\nout1\nerr1\nout2\nerr2\n"
    assert process.stderr.readline() == ""
    assert process.stderr.read() == ""
    assert process.wait(HANG_GUARD_S) == 0


def test_env_replaces_the_callers_environment() -> None:
    process = SubprocessPipedProcessLauncher().start(
        _python("import os, sys; sys.stdout.write(repr(sorted(os.environ.items())))"),
        env={"ONLY": "this"},
    )

    output = process.stdout.read()

    assert process.wait(HANG_GUARD_S) == 0
    assert "('ONLY', 'this')" in output
    assert "PATH" not in output


def test_a_bounded_read_returns_unterminated_output_without_waiting_for_eof() -> None:
    process = SubprocessPipedProcessLauncher().start(
        _python("import signal, sys; sys.stdout.write('abc'); sys.stdout.flush(); signal.pause()")
    )
    try:
        assert process.stdout.read(2) == "ab"
        assert process.stdout.read(10) == "c"
    finally:
        process.kill_group()
    assert process.wait(HANG_GUARD_S) == -signal.SIGKILL
    assert process.stdout.read(10) == ""


def test_a_timed_wait_does_not_hold_back_a_signal_from_another_caller() -> None:
    process = SubprocessPipedProcessLauncher().start(
        _python(
            # Blocked, then taken with sigwait: a handler followed by signal.pause() loses
            # a SIGTERM that lands after the handler check and before pause() blocks.
            "import signal, sys\n"
            "signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])\n"
            "print('ready', flush=True)\n"
            "signal.sigwait([signal.SIGTERM])\n"
            "sys.exit(3)\n"
        )
    )
    assert process.stdout.readline() == "ready\n"

    with pytest.raises(ProcessTimeoutError):
        process.wait(0)
    process.terminate_group()

    assert process.wait(HANG_GUARD_S) == 3


def test_terminate_group_reaches_descendants_and_signals_after_reaping_are_no_ops() -> None:
    process = SubprocessPipedProcessLauncher().start(
        _python(
            "import signal, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'])\n"
            "signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])\n"
            "print('ready', flush=True)\n"
            "signal.sigwait([signal.SIGTERM])\n"
            "sys.exit(7 if child.wait() == -signal.SIGTERM else 1)\n"
        )
    )
    assert process.stdout.readline() == "ready\n"

    process.terminate_group()

    assert process.wait(HANG_GUARD_S) == 7
    process.terminate_group()
    process.kill_group()
    assert process.wait(None) == 7
