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
from vs_sim.api import DISCARD, Capture
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
        process = PipedStoppableProcess(scripted, threads=threads, output=DISCARD)
        scripted.write(stdout, stderr)
        scripted.exit(status)
        return process.wait(None), process.returncode

    def normalized(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")

    assert threads.run(case) == ((normalized(stdout), normalized(stderr)), status)


@settings(max_examples=examples(pr=30, nightly=200))
@given(
    writes=st.lists(st.tuples(_TEXT, _TEXT), max_size=6),
    ending=st.sampled_from(("exit", "timeout")),
)
def test_every_drained_chunk_reaches_the_output_sink_raw_and_in_order(
    writes: list[tuple[str, str]], ending: str
) -> None:
    """The sink sees each stream exactly as written, line endings included, on any exit."""
    threads = SimThreads()
    output = Capture(1 << 16)

    def case() -> None:
        scripted = ScriptedPipedProcess(threads)
        process = PipedStoppableProcess(scripted, threads=threads, output=output)
        for stdout, stderr in writes:
            scripted.write(stdout, stderr)
        if ending == "timeout":
            wait_stoppable(process, timeout=1, cancel=None, grace_seconds=1, threads=threads)
        else:
            scripted.exit(0)
            process.wait(None)

    threads.run(case)

    assert output.stdout == "".join(stdout for stdout, _ in writes).encode()
    assert output.stderr == "".join(stderr for _, stderr in writes).encode()


def test_output_reaches_the_sink_while_the_process_still_runs() -> None:
    threads = SimThreads()
    output = Capture(1 << 16)

    def case() -> tuple[bytes, bytes, object]:
        scripted = ScriptedPipedProcess(threads)
        process = PipedStoppableProcess(scripted, threads=threads, output=output)
        scripted.write("step 1\n", "warming up\n")
        running = process.wait(5)
        seen = (output.stdout, output.stderr)
        scripted.exit(0)
        process.wait(5)
        return *seen, running

    assert threads.run(case) == (b"step 1\n", b"warming up\n", None)


def test_a_wait_on_a_running_process_returns_none_until_it_exits() -> None:
    threads = SimThreads()

    def case() -> tuple[object, object]:
        scripted = ScriptedPipedProcess(threads)
        process = PipedStoppableProcess(scripted, threads=threads, output=DISCARD)
        scripted.write("out", "err")
        early = process.wait(5)
        scripted.exit(0)
        return early, process.wait(5)

    assert threads.run(case) == (None, ("out", "err"))


def test_the_returncode_is_unknown_before_the_leader_exits() -> None:
    threads = SimThreads()

    def case() -> None:
        process = PipedStoppableProcess(
            ScriptedPipedProcess(threads), threads=threads, output=DISCARD
        )
        with pytest.raises(RuntimeError, match="has not exited"):
            _ = process.returncode

    threads.run(case)


def test_each_stop_signal_becomes_its_group_signal() -> None:
    threads = SimThreads()

    def case() -> list[str]:
        scripted = ScriptedPipedProcess(threads, survives_terminate=True)
        process = PipedStoppableProcess(scripted, threads=threads, output=DISCARD)
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
        process = PipedStoppableProcess(scripted, threads=threads, output=DISCARD)
        scripted.write("started\n")
        scripted.exit(0, close_output=False)
        outcome = wait_stoppable(process, timeout=10, cancel=None, grace_seconds=1, threads=threads)
        return outcome.stopped, scripted.signals, scripted.refused, scripted.reaped

    stop, signals, refused, reaped = threads.run(case)

    assert stop is ProcessStop.TIMEOUT
    assert signals[0] == "terminate"
    assert refused == []
    assert reaped
