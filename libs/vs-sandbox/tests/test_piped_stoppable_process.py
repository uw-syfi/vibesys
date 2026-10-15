"""``PipedStoppableProcess`` over ``vs_sim``'s scripted piped process, on simulated threads.

The real launcher under the same adapter passes the stoppable-process contract in
``tests/e2e``. Here a scripted process shows what only the adapter decides: how
drained output is joined and normalized, when a wait gives up, which group signal
each stop signal becomes, and that a pipe held open past the leader's exit keeps
the group stoppable.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# test-isolation: the adapter is internal to the command runners; this is its deterministic check.
from vs_sandbox.process_execution import (
    KILL,
    TERMINATE,
    PipedStoppableProcess,
    ProcessStop,
    wait_stoppable,
)
from vs_sim.api.testing import ScriptedPipedProcess, SimThreads, examples

_TEXT = st.text(alphabet="ab\r\n", max_size=30)


@settings(max_examples=examples(pr=30, nightly=200))
@given(stdout=_TEXT, stderr=_TEXT, status=st.integers(-64, 255))
def test_an_exited_process_reports_all_output_with_universal_newlines(
    stdout: str, stderr: str, status: int
) -> None:
    threads = SimThreads()

    def case() -> tuple[tuple[str, str] | None, int]:
        scripted = ScriptedPipedProcess(threads)
        process = PipedStoppableProcess(scripted, threads=threads)
        scripted.write(stdout, stderr)
        scripted.exit(status)
        return process.wait(None), process.returncode

    def normalized(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")

    assert threads.run(case) == ((normalized(stdout), normalized(stderr)), status)


def test_a_wait_on_a_running_process_returns_none_until_it_exits() -> None:
    threads = SimThreads()

    def case() -> tuple[object, object]:
        scripted = ScriptedPipedProcess(threads)
        process = PipedStoppableProcess(scripted, threads=threads)
        scripted.write("out", "err")
        early = process.wait(5)
        scripted.exit(0)
        return early, process.wait(5)

    assert threads.run(case) == (None, ("out", "err"))


def test_the_returncode_is_unknown_before_the_leader_exits() -> None:
    threads = SimThreads()

    def case() -> None:
        process = PipedStoppableProcess(ScriptedPipedProcess(threads), threads=threads)
        with pytest.raises(RuntimeError, match="has not exited"):
            _ = process.returncode

    threads.run(case)


def test_each_stop_signal_becomes_its_group_signal() -> None:
    threads = SimThreads()

    def case() -> list[str]:
        scripted = ScriptedPipedProcess(threads, survives_terminate=True)
        process = PipedStoppableProcess(scripted, threads=threads)
        process.signal_group(TERMINATE)
        process.signal_group(KILL)
        process.wait(None)
        return scripted.signals

    assert threads.run(case) == ["terminate", "kill"]


def test_a_pipe_held_past_the_leaders_exit_leaves_the_group_stoppable() -> None:
    """A descendant holding stdout after the leader exited is still killed by a stop.

    Reaping the leader ends the group's signals, so the adapter must not reap it
    while a pipe is open: otherwise the timeout's kill is refused and the
    descendant outlives the call.
    """
    threads = SimThreads()

    def case() -> tuple[ProcessStop | None, list[str], list[str], bool]:
        scripted = ScriptedPipedProcess(threads)
        process = PipedStoppableProcess(scripted, threads=threads)
        scripted.write("started\n")
        scripted.exit(0, close_output=False)
        outcome = wait_stoppable(process, timeout=10, cancel=None, grace_seconds=1, threads=threads)
        return outcome.stopped, scripted.signals, scripted.refused, scripted.reaped

    stop, signals, refused, reaped = threads.run(case)

    assert stop is ProcessStop.TIMEOUT
    assert signals[0] == "terminate"
    assert refused == []
    assert reaped
