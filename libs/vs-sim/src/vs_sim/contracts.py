"""Contract suites: the cases every implementation of an interface must pass.

A suite is a base class. A test module subclasses it, names the subclass ``Test<Variant>``
and implements the one factory method, so pytest runs every case against that
implementation, real or Fake. The cases drive an implementation only through its
interface, and none of them reads the wall clock to decide an outcome.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vs_sim.child import run_in_child
from vs_sim.gate import Gate
from vs_sim.loopback_http import HttpReply
from vs_sim.output import Capture, OutputStream
from vs_sim.processes import (
    ProcessGroupUnreaped,
    ProcessOutcome,
    ProcessOutputTooLarge,
    ProcessSpec,
    ProcessTimedOut,
    ProcessTimeoutError,
)
from vs_sim.randomness import SeededRandom
from vs_sim.waits import HANG_GUARD_S

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from vs_sim.blocking import BlockingRunner
    from vs_sim.clock import Clock, Sleeper
    from vs_sim.loopback_http import LoopbackHttp
    from vs_sim.probes import CommandProbe
    from vs_sim.processes import ForegroundLauncher, PipedProcess, Processes, ProcessLauncher
    from vs_sim.signals import ProcessSignaller, SignalSource, WriterProcessSignaller
    from vs_sim.streaming import StreamingCommand
from vs_sim.output import DISCARD

type Run = Callable[[Coroutine[Any, Any, Any]], Any]
"""Run a coroutine to completion on the loop the implementation under test belongs to."""

_CASES = 25
"""Seeded cases per property: the same draws every run."""
_PROCESS_CASES = 4
"""Seeded cases per process property: each starts a real interpreter in the real suite."""
_SHORT_TIMEOUT_S = 0.2
"""A timeout a command is meant to outlive."""


@dataclass(frozen=True)
class ClockUnderTest:
    """A clock and the means to move its timeline."""

    clock: Clock
    elapse: Callable[[float], None]
    """Move the timeline forward by about this many seconds."""
    exact: bool
    """Whether ``elapse(d)`` moves ``now`` by exactly ``d``."""


class ClockContract:
    """Cases for :class:`~vs_sim.clock.Clock`. Implement :meth:`clock_under_test`."""

    def clock_under_test(self) -> ClockUnderTest:
        """A fresh clock with its harness."""
        raise NotImplementedError

    def test_time_never_runs_backwards(self) -> None:
        """``now`` never decreases, whatever the timeline does in between."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            subject = self.clock_under_test()
            readings = [subject.clock.now()]
            for _ in range(rng.randint(1, 6)):
                subject.elapse(rng.random() * 3)
                readings.append(subject.clock.now())
            assert readings == sorted(readings), seed

    def test_now_is_a_float_number_of_seconds(self) -> None:
        """A reading is a plain number, so arithmetic on readings is arithmetic on seconds."""
        assert isinstance(self.clock_under_test().clock.now(), int | float)

    def test_elapsing_moves_exactly_when_the_clock_is_exact(self) -> None:
        """An exact clock's difference of readings is the time that elapsed."""
        subject = self.clock_under_test()
        if not subject.exact:
            before = subject.clock.now()
            subject.elapse(0.0)
            assert subject.clock.now() >= before
            return
        for seed in range(_CASES):
            seconds = SeededRandom(seed).random() * 100
            before = subject.clock.now()
            subject.elapse(seconds)
            assert abs(subject.clock.now() - before - seconds) < 1e-6, seed


@dataclass(frozen=True)
class SleeperUnderTest:
    """A sleeping clock and the loop its sleeps run on."""

    clock: Clock
    sleeper: Sleeper
    run: Run
    tick: float
    """A short duration, cheap on this implementation's timeline."""
    exact: bool
    """Whether a sleep of ``d`` moves ``now`` by exactly ``d``."""


class SleeperContract:
    """Cases for :class:`~vs_sim.clock.Sleeper`. Implement :meth:`sleeper_under_test`."""

    def sleeper_under_test(self) -> SleeperUnderTest:
        """A fresh sleeping clock with its harness."""
        raise NotImplementedError

    def test_every_concurrent_sleeper_wakes(self) -> None:
        """Any number of tasks can sleep at once; none waits for another to wake first."""
        for seed in range(_CASES):
            count = SeededRandom(seed).randint(1, 8)
            assert sorted(self._wake_order(count)) == list(range(count)), seed

    def _wake_order(self, count: int) -> list[int]:
        subject = self.sleeper_under_test()
        woke: list[int] = []

        async def sleeper(index: int) -> None:
            await subject.sleeper.sleep(subject.tick)
            woke.append(index)

        async def main() -> None:
            await asyncio.gather(*(sleeper(i) for i in range(count)))

        subject.run(main())
        return woke

    def test_equal_sleeps_wake_in_start_order(self) -> None:
        """Sleepers that started in some order and sleep equally long wake in that order."""
        assert self._wake_order(6) == list(range(6))

    def test_time_never_runs_backwards_across_sleeps(self) -> None:
        """Readings taken after each sleep are in order."""
        subject = self.sleeper_under_test()
        readings: list[float] = []

        async def main() -> None:
            for _ in range(5):
                readings.append(subject.clock.now())
                await subject.sleeper.sleep(subject.tick)
            readings.append(subject.clock.now())

        subject.run(main())
        assert readings == sorted(readings)

    def test_an_exact_sleep_advances_by_its_duration(self) -> None:
        """On a clock whose sleeps are exact, ``now`` after a sleep is ``now`` before plus the duration."""
        subject = self.sleeper_under_test()
        if not subject.exact:
            return
        for seed in range(_CASES):
            seconds = SeededRandom(seed).random() * 100
            before = subject.clock.now()
            subject.run(subject.sleeper.sleep(seconds))
            assert abs(subject.clock.now() - before - seconds) < 1e-6, seed

    def test_a_zero_or_negative_sleep_returns(self) -> None:
        """Sleeping for no time, or for a negative time, is not an error."""
        subject = self.sleeper_under_test()

        async def main() -> None:
            await subject.sleeper.sleep(0.0)
            await subject.sleeper.sleep(-1.0)

        subject.run(main())

    def test_a_cancelled_sleep_leaves_the_sleeper_usable(self) -> None:
        """Cancelling a sleeping task ends it with ``CancelledError`` and later sleeps still work."""
        subject = self.sleeper_under_test()
        outcome: list[str] = []

        async def main() -> None:
            task = asyncio.ensure_future(subject.sleeper.sleep(subject.tick))
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                outcome.append("cancelled")
            await subject.sleeper.sleep(subject.tick)
            outcome.append("slept again")

        subject.run(main())
        assert outcome == ["cancelled", "slept again"]


@dataclass(frozen=True)
class RunnerUnderTest:
    """A blocking-call runner and the loop it runs on."""

    runner: BlockingRunner
    run: Run


class BlockingRunnerContract:
    """Cases for :class:`~vs_sim.blocking.BlockingRunner`. Implement :meth:`runner_under_test`."""

    def runner_under_test(self) -> RunnerUnderTest:
        """A fresh runner with its harness."""
        raise NotImplementedError

    def test_returns_the_result_of_one_call_with_its_arguments(self) -> None:
        """The function is called once, with the given arguments, and its value comes back."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            left, right = rng.randint(-50, 50), rng.randint(-50, 50)
            result, calls = self._add(left, right)
            assert result == left + right, seed
            assert calls == [(left, right)], seed

    def _add(self, left: int, right: int) -> tuple[int, list[tuple[int, int]]]:
        subject = self.runner_under_test()
        calls: list[tuple[int, int]] = []

        def add(a: int, *, b: int) -> int:
            calls.append((a, b))
            return a + b

        async def main() -> int:
            return await subject.runner.run(add, left, b=right)

        return subject.run(main()), calls

    def test_an_exception_of_the_function_propagates_unchanged(self) -> None:
        """The caller sees the function's own exception, not a wrapper."""
        subject = self.runner_under_test()
        failure = KeyError("missing")

        def fail() -> None:
            raise failure

        async def main() -> None:
            await subject.runner.run(fail)

        raised: BaseException | None = None
        try:
            subject.run(main())
        except KeyError as error:
            raised = error
        assert raised is failure, "the function's own exception did not reach the caller"

    def test_other_tasks_run_while_the_caller_waits(self) -> None:
        """A call in flight does not stop the loop: a sibling task that is already ready still runs."""
        subject = self.runner_under_test()
        order: list[str] = []

        def work() -> str:
            order.append("work")
            return "done"

        async def sibling() -> None:
            order.append("sibling")

        async def main() -> str:
            other = asyncio.ensure_future(sibling())
            result = await subject.runner.run(work)
            await other
            return result

        assert subject.run(main()) == "done"
        assert sorted(order) == ["sibling", "work"]


@dataclass(frozen=True)
class SignalSourceUnderTest:
    """A signal source and the means to make a signal arrive."""

    source: SignalSource
    run: Run
    deliver: Callable[[signal.Signals], object]
    """Make ``number`` arrive; its handler runs on the loop, possibly after this returns."""
    isolate: bool = False
    """Run each case in a child process: delivery changes process-wide state."""


class SignalSourceContract:
    """Cases for :class:`~vs_sim.signals.SignalSource`. Implement :meth:`source_under_test`."""

    number = signal.SIGUSR1

    def source_under_test(self) -> SignalSourceUnderTest:
        """A fresh source with its harness."""
        raise NotImplementedError

    def _case(self, case: Callable[[SignalSourceUnderTest], object]) -> None:
        if self.source_under_test().isolate:
            run_in_child(lambda: case(self.source_under_test()))
        else:
            case(self.source_under_test())

    def test_a_delivered_signal_runs_its_handler_once(self) -> None:
        """Each arrival of the signal calls the installed handler once."""

        def case(subject: SignalSourceUnderTest) -> None:
            arrivals = Gate()
            count = 0

            def handler() -> None:
                nonlocal count
                count += 1
                arrivals.open()

            async def main() -> None:
                subject.source.add_handler(self.number, handler)
                subject.deliver(self.number)
                await arrivals.wait()
                subject.source.remove_handler(self.number)

            subject.run(main())
            assert count == 1

        self._case(case)

    def test_removing_a_handler_reports_whether_there_was_one(self) -> None:
        """``remove_handler`` is true once per installed handler."""

        def case(subject: SignalSourceUnderTest) -> None:
            async def main() -> list[bool]:
                subject.source.add_handler(self.number, lambda: None)
                return [
                    subject.source.remove_handler(self.number),
                    subject.source.remove_handler(self.number),
                ]

            assert subject.run(main()) == [True, False]

        self._case(case)

    def test_a_new_handler_replaces_the_old_one(self) -> None:
        """Only the most recently installed handler hears the signal."""

        def case(subject: SignalSourceUnderTest) -> None:
            heard: list[str] = []
            arrived = Gate()

            def second() -> None:
                heard.append("second")
                arrived.open()

            async def main() -> None:
                subject.source.add_handler(self.number, lambda: heard.append("first"))
                subject.source.add_handler(self.number, second)
                subject.deliver(self.number)
                await arrived.wait()
                subject.source.remove_handler(self.number)

            subject.run(main())
            assert heard == ["second"]

        self._case(case)


@dataclass(frozen=True)
class ProcessSignallerUnderTest:
    """A process signaller and observable lifetime for one stable process."""

    signaller: ProcessSignaller
    pid: int
    assert_live: Callable[[], None]
    assert_terminated: Callable[[], None]
    close: Callable[[], None]


class ProcessSignallerContract:
    """Cases for :class:`~vs_sim.signals.ProcessSignaller`. Implement one factory."""

    def process_signaller_under_test(self) -> ProcessSignallerUnderTest:
        """Return a fresh signaller and one process identity it can open."""
        raise NotImplementedError

    def _case(self, case: Callable[[ProcessSignallerUnderTest], None]) -> None:
        subject = self.process_signaller_under_test()
        try:
            case(subject)
        finally:
            subject.close()

    def test_a_current_identity_is_terminated(self) -> None:
        """A true revalidation sends SIGTERM to the stable process."""

        def case(subject: ProcessSignallerUnderTest) -> None:
            assert subject.signaller.terminate_if_current(subject.pid, lambda: True) is True
            subject.assert_terminated()

        self._case(case)

    def test_a_process_exists_until_it_is_terminated(self) -> None:
        """The live process exists; once terminated and gone, it does not."""

        def case(subject: ProcessSignallerUnderTest) -> None:
            assert subject.signaller.exists(subject.pid)
            assert subject.signaller.terminate_if_current(subject.pid, lambda: True) is True
            subject.assert_terminated()
            assert not subject.signaller.exists(subject.pid)

        self._case(case)

    def test_a_changed_identity_is_not_terminated(self) -> None:
        """A false revalidation leaves the stable process running."""

        def case(subject: ProcessSignallerUnderTest) -> None:
            assert subject.signaller.terminate_if_current(subject.pid, lambda: False) is False
            subject.assert_live()

        self._case(case)

    def test_a_revalidation_failure_is_propagated_without_termination(self) -> None:
        """An identity-check error propagates and leaves the stable process running."""

        class RevalidationError(RuntimeError):
            pass

        def case(subject: ProcessSignallerUnderTest) -> None:
            def fail() -> bool:
                raise RevalidationError

            try:
                subject.signaller.terminate_if_current(subject.pid, fail)
            except RevalidationError:
                pass
            else:  # pragma: no cover - every implementation must propagate the callback error.
                raise AssertionError
            subject.assert_live()

        self._case(case)


@dataclass(frozen=True)
class LockOwnershipUnderTest:
    """One exclusive flock owned by a process and a way to release it."""

    signaller: WriterProcessSignaller
    pid: int
    path: Path
    release: Callable[[], None]
    close: Callable[[], None]


class LockOwnershipContract:
    """Exclusive flock ownership is specific to a process and an inode."""

    def lock_ownership_under_test(self) -> LockOwnershipUnderTest:
        """Return a fresh owner with one held exclusive flock."""
        raise NotImplementedError

    def test_only_the_exact_process_and_file_prove_ownership(self) -> None:
        """An unrelated inode cannot authorize signalling the same numeric PID."""
        subject = self.lock_ownership_under_test()
        try:
            assert subject.signaller.owns_lock(subject.pid, subject.path)
            assert not subject.signaller.owns_lock(subject.pid, subject.path.with_name("other"))
        finally:
            subject.close()

    def test_release_removes_the_ownership_proof(self) -> None:
        """A released flock no longer identifies the process as its owner."""
        subject = self.lock_ownership_under_test()
        try:
            subject.release()
            assert not subject.signaller.owns_lock(subject.pid, subject.path)
        finally:
            subject.close()


@dataclass(frozen=True)
class ProcessUnderTest:
    """A process launcher and the commands it can be asked to run.

    Each command is an argv the implementation understands: a real launcher gets a Python
    one-liner, a Fake gets an argv its script recognises.
    """

    launcher: ProcessLauncher
    run: Run
    exit_with_stdout: Callable[[int, bytes], tuple[str, ...]]
    """A command that writes the bytes to stdout and exits with the status."""
    echo_stdin: tuple[str, ...]
    """A command that writes its stdin to stdout and exits 0."""
    blocks_until_signalled: tuple[str, ...]
    """A command that never ends by itself."""
    missing_program: tuple[str, ...]
    """A command whose program does not exist."""


class ProcessLauncherContract:
    """Cases for :class:`~vs_sim.processes.ProcessLauncher`. Implement :meth:`process_under_test`."""

    def process_under_test(self) -> ProcessUnderTest:
        """A fresh launcher with its harness."""
        raise NotImplementedError

    def _outcome(
        self, subject: ProcessUnderTest, spec: ProcessSpec
    ) -> Coroutine[Any, Any, ProcessOutcome]:
        async def main() -> ProcessOutcome:
            process = await subject.launcher.start(spec)
            return await process.wait()

        return main()

    def test_status_and_output_come_back(self) -> None:
        """The outcome carries the exit status and what the process wrote."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            subject = self.process_under_test()
            status = rng.randint(0, 100)
            output = bytes(rng.randint(32, 126) for _ in range(rng.randint(0, 20)))
            spec = ProcessSpec(subject.exit_with_stdout(status, output))
            outcome = subject.run(self._outcome(subject, spec))
            assert (outcome.returncode, outcome.stdout) == (status, output), seed

    def test_input_reaches_the_process(self) -> None:
        """The spec's input is the process's stdin."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            subject = self.process_under_test()
            data = bytes(rng.randint(32, 126) for _ in range(rng.randint(0, 20)))
            spec = ProcessSpec(subject.echo_stdin, input=data)
            assert subject.run(self._outcome(subject, spec)).stdout == data, seed

    def test_terminate_and_kill_end_a_blocked_process_with_a_signal_status(self) -> None:
        """A process that never ends by itself ends when signalled, with ``-signal`` as its status."""
        for stop, expected in (("terminate", -signal.SIGTERM), ("kill", -signal.SIGKILL)):
            assert self._signalled_status(stop) == expected, stop

    def _signalled_status(self, stop: str) -> int:
        subject = self.process_under_test()

        async def main() -> int:
            process = await subject.launcher.start(ProcessSpec(subject.blocks_until_signalled))
            getattr(process, stop)()
            return (await process.wait()).returncode

        return subject.run(main())

    def test_signalling_a_finished_process_changes_nothing(self) -> None:
        """``terminate`` and ``kill`` after the process ended leave its outcome as it was."""
        subject = self.process_under_test()

        async def main() -> tuple[Any, Any]:
            process = await subject.launcher.start(ProcessSpec(subject.exit_with_stdout(3, b"x")))
            first = await process.wait()
            process.terminate()
            process.kill()
            return first, await process.wait()

        first, second = subject.run(main())
        assert (first.returncode, first.stdout) == (second.returncode, second.stdout) == (3, b"x")

    def test_a_missing_program_fails_to_start(self) -> None:
        """Starting a program that does not exist raises ``OSError`` instead of returning a process."""
        subject = self.process_under_test()

        async def main() -> None:
            await subject.launcher.start(ProcessSpec(subject.missing_program))

        try:
            subject.run(main())
        except OSError:
            return
        message = "starting a missing program did not raise OSError"
        raise AssertionError(message)


@dataclass(frozen=True)
class ProcessesUnderTest:
    """A :class:`~vs_sim.processes.Processes` and commands with known outcomes for it.

    A real implementation gets Python one-liners, a Fake gets argvs its script recognises.
    """

    processes: Processes
    exit_with: Callable[[int, bytes, bytes], tuple[str, ...]]
    """A command that writes the first bytes to stdout, the second to stderr, and exits."""
    echo_stdin: tuple[str, ...]
    """A command that writes its stdin to stdout and exits 0."""
    print_cwd_and_env: Callable[[str], tuple[str, ...]]
    """A command that prints its working directory, a newline, and the named variable."""
    outlives_timeout: Callable[[bytes], tuple[str, ...]]
    """A command that writes the bytes to stdout, then never ends, with a descendant in its group."""
    survives_kill: tuple[str, ...]
    """A command that leaves a descendant outside its group holding its output open."""
    missing_program: tuple[str, ...]
    """A command whose program does not exist."""
    now: Callable[[], float]
    """The time the implementation's runs take, in seconds."""
    release: Callable[[], None] = lambda: None
    """Ends what a run of ``survives_kill`` reported unreaped left running."""


class ProcessesContract:
    """Cases for :class:`~vs_sim.processes.Processes`. Implement :meth:`processes_under_test`."""

    def processes_under_test(self, *, cleanup_timeout: float) -> ProcessesUnderTest:
        """A fresh implementation that waits ``cleanup_timeout`` for a killed group to end."""
        raise NotImplementedError

    def test_status_and_both_streams_come_back(self) -> None:
        """The outcome carries the exit status, zero or not, and what each stream received."""
        subject = self.processes_under_test(cleanup_timeout=HANG_GUARD_S)
        for seed in range(_PROCESS_CASES):
            rng = SeededRandom(seed)
            status = rng.randint(0, 100)
            out, err = (_random_bytes(rng) for _ in "oe")
            outcome = subject.processes.run(
                ProcessSpec(subject.exit_with(status, out, err)),
                timeout=HANG_GUARD_S,
                output=DISCARD,
            )
            assert outcome == ProcessOutcome(status, out, err), seed

    def test_output_gets_every_byte_the_outcome_holds(self) -> None:
        """What a run writes reaches ``output`` too, and the outcome is the same without it."""
        subject = self.processes_under_test(cleanup_timeout=HANG_GUARD_S)
        for seed in range(_PROCESS_CASES):
            rng = SeededRandom(seed)
            out, err = (_random_bytes(rng) for _ in "oe")
            spec = ProcessSpec(subject.exit_with(1, out, err))
            seen = Capture(len(out) + len(err))
            outcome = subject.processes.run(spec, timeout=HANG_GUARD_S, output=seen)
            assert outcome == ProcessOutcome(1, out, err), seed
            assert (seen.stdout, seen.stderr) == (out, err), seed

    def test_output_gets_what_a_stopped_run_wrote_past_its_cap(self) -> None:
        """A run stopped at the cap or the timeout still forwarded what it read, cap or not."""
        subject = self.processes_under_test(cleanup_timeout=HANG_GUARD_S)
        written = b"more than four"
        seen = Capture(len(written))
        spec = ProcessSpec(subject.outlives_timeout(written), max_output_bytes=4)
        run = subject.processes.run(spec, timeout=HANG_GUARD_S, output=seen)
        assert isinstance(run, ProcessOutputTooLarge), run
        assert seen.stdout.startswith(run.stdout), (seen.stdout, run)
        assert len(seen.stdout) > len(run.stdout), (seen.stdout, run)
        timed_out = Capture(len(written))
        run = subject.processes.run(
            ProcessSpec(subject.outlives_timeout(written)),
            timeout=_SHORT_TIMEOUT_S,
            output=timed_out,
        )
        assert isinstance(run, ProcessTimedOut), run
        assert timed_out.stdout == run.stdout, (timed_out.stdout, run)

    def test_input_reaches_the_process(self) -> None:
        """The spec's input is the process's stdin."""
        subject = self.processes_under_test(cleanup_timeout=HANG_GUARD_S)
        for seed in range(_PROCESS_CASES):
            data = _random_bytes(SeededRandom(seed))
            spec = ProcessSpec(subject.echo_stdin, input=data)
            assert subject.processes.run(
                spec, timeout=HANG_GUARD_S, output=DISCARD
            ) == ProcessOutcome(0, data, b""), seed

    def test_the_command_runs_in_the_given_directory_and_environment(self) -> None:
        """``cwd`` and ``env`` are the process's directory and its whole environment."""
        subject = self.processes_under_test(cleanup_timeout=HANG_GUARD_S)
        cwd = Path(__file__).resolve().parent
        spec = ProcessSpec(
            subject.print_cwd_and_env("VS_SIM_CONTRACT"), cwd=cwd, env={"VS_SIM_CONTRACT": "x y"}
        )
        outcome = subject.processes.run(spec, timeout=HANG_GUARD_S, output=DISCARD)
        assert outcome == ProcessOutcome(0, f"{cwd}\nx y".encode(), b"")

    def test_a_command_that_outlives_the_timeout_times_out_once_its_group_is_gone(self) -> None:
        """Past the timeout the whole group is killed, and what it wrote comes back."""
        subject = self.processes_under_test(cleanup_timeout=HANG_GUARD_S)
        written = b"partial"
        run = subject.processes.run(
            ProcessSpec(subject.outlives_timeout(written)),
            timeout=_SHORT_TIMEOUT_S,
            output=DISCARD,
        )
        assert isinstance(run, ProcessTimedOut), run
        assert run.timeout == _SHORT_TIMEOUT_S
        assert written.startswith(run.stdout), run

    def test_a_run_that_times_out_takes_at_least_its_timeout(self) -> None:
        """A caller that retries until a deadline sees each timed-out run use up its timeout."""
        subject = self.processes_under_test(cleanup_timeout=HANG_GUARD_S)
        started = subject.now()
        run = subject.processes.run(
            ProcessSpec(subject.outlives_timeout(b"")),
            timeout=_SHORT_TIMEOUT_S,
            output=DISCARD,
        )
        elapsed = subject.now() - started
        assert isinstance(run, ProcessTimedOut), run
        assert elapsed >= _SHORT_TIMEOUT_S, elapsed

    def test_a_group_the_kill_cannot_end_is_reported_unreaped_not_timed_out(self) -> None:
        """A descendant that escaped the group and holds the output open is a distinct result.

        A run that times out before the command started that descendant is a plain timeout,
        so the timeout doubles until the command got that far.
        """
        subject = self.processes_under_test(cleanup_timeout=_SHORT_TIMEOUT_S)
        timeout = _SHORT_TIMEOUT_S
        run = subject.processes.run(
            ProcessSpec(subject.survives_kill), timeout=timeout, output=DISCARD
        )
        while isinstance(run, ProcessTimedOut) and timeout < HANG_GUARD_S:
            timeout *= 2
            run = subject.processes.run(
                ProcessSpec(subject.survives_kill), timeout=timeout, output=DISCARD
            )
        if isinstance(run, ProcessGroupUnreaped):
            subject.release()
        assert isinstance(run, ProcessGroupUnreaped), run
        assert run.timeout == timeout
        assert run.detail

    def test_output_up_to_the_cap_comes_back_and_more_is_cut_and_reported(self) -> None:
        """Each stream is kept up to the spec's cap; a stream past it stops the run, named."""
        subject = self.processes_under_test(cleanup_timeout=HANG_GUARD_S)
        for seed in range(_PROCESS_CASES):
            rng = SeededRandom(seed)
            limit = rng.randint(0, 12)
            out, err = (_random_bytes(rng) for _ in "oe")
            spec = ProcessSpec(subject.exit_with(3, out, err), max_output_bytes=limit)
            run = subject.processes.run(spec, timeout=HANG_GUARD_S, output=DISCARD)
            if len(out) <= limit and len(err) <= limit:
                assert run == ProcessOutcome(3, out, err), seed
                continue
            assert isinstance(run, ProcessOutputTooLarge), (seed, run)
            assert run.limit == limit, seed
            assert run.stream in {
                stream
                for stream, written in ((OutputStream.STDOUT, out), (OutputStream.STDERR, err))
                if len(written) > limit
            }, seed
            assert out[:limit].startswith(run.stdout), seed
            assert err[:limit].startswith(run.stderr), seed
            assert not run.detail, seed

    def test_a_command_writing_past_the_cap_is_stopped_before_its_timeout(self) -> None:
        """A writer that would never end is killed at the cap, not left to run to its timeout."""
        subject = self.processes_under_test(cleanup_timeout=HANG_GUARD_S)
        written = b"more than four"
        spec = ProcessSpec(subject.outlives_timeout(written), max_output_bytes=4)
        run = subject.processes.run(spec, timeout=HANG_GUARD_S, output=DISCARD)
        assert run == ProcessOutputTooLarge(4, OutputStream.STDOUT, written[:4], b""), run

    def test_a_negative_cap_is_refused(self) -> None:
        """A spec cannot ask for a negative output cap."""
        subject = self.processes_under_test(cleanup_timeout=HANG_GUARD_S)
        try:
            ProcessSpec(subject.echo_stdin, max_output_bytes=-1)
        except ValueError:
            return
        message = "a negative max_output_bytes was accepted"
        raise AssertionError(message)

    def test_a_missing_program_fails_to_start(self) -> None:
        """A program that does not exist raises ``OSError`` instead of returning an outcome."""
        subject = self.processes_under_test(cleanup_timeout=HANG_GUARD_S)
        try:
            subject.processes.run(
                ProcessSpec(subject.missing_program), timeout=HANG_GUARD_S, output=DISCARD
            )
        except OSError:
            return
        message = "running a missing program did not raise OSError"
        raise AssertionError(message)


def _random_bytes(rng: SeededRandom) -> bytes:
    return bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 20)))


@dataclass(frozen=True)
class ForegroundUnderTest:
    """A foreground launcher and the commands it can be asked to run."""

    launcher: ForegroundLauncher
    run: Run
    exit_with: Callable[[int], tuple[str, ...]]
    """A command that exits with the status."""
    blocks_until_signalled: tuple[str, ...]
    """A command that never ends by itself and has no signal handlers."""
    missing_program: tuple[str, ...]
    """A command whose program does not exist."""


class ForegroundLauncherContract:
    """Cases for :class:`~vs_sim.processes.ForegroundLauncher`. Implement :meth:`foreground_under_test`."""

    def foreground_under_test(self) -> ForegroundUnderTest:
        """A fresh launcher with its harness."""
        raise NotImplementedError

    def test_the_exit_status_comes_back(self) -> None:
        """``wait`` returns the status the child exited with."""
        for seed in range(_CASES):
            status = SeededRandom(seed).randint(0, 100)
            subject = self.foreground_under_test()

            async def main(subject: ForegroundUnderTest = subject, status: int = status) -> int:
                child = await subject.launcher.start(subject.exit_with(status))
                return await child.wait()

            assert subject.run(main()) == status, seed

    def test_a_signal_ends_a_child_without_a_handler_with_a_signal_status(self) -> None:
        """A child that never ends by itself ends when signalled, with ``-signal`` as its status."""
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGKILL):
            subject = self.foreground_under_test()

            async def main(
                subject: ForegroundUnderTest = subject, number: signal.Signals = number
            ) -> int:
                child = await subject.launcher.start(subject.blocks_until_signalled)
                child.send_signal(number)
                return await child.wait()

            assert subject.run(main()) == -number, number

    def test_signalling_a_finished_child_changes_nothing(self) -> None:
        """``send_signal`` after the child ended leaves its status as it was."""
        subject = self.foreground_under_test()

        async def main() -> tuple[int, int]:
            child = await subject.launcher.start(subject.exit_with(3))
            first = await child.wait()
            child.send_signal(signal.SIGTERM)
            return first, await child.wait()

        assert subject.run(main()) == (3, 3)

    def test_a_missing_program_fails_to_start(self) -> None:
        """Starting a program that does not exist raises ``OSError`` instead of returning a child."""
        subject = self.foreground_under_test()

        async def main() -> None:
            await subject.launcher.start(subject.missing_program)

        try:
            subject.run(main())
        except OSError:
            return
        message = "starting a missing program did not raise OSError"
        raise AssertionError(message)


@dataclass(frozen=True)
class ProbeUnderTest:
    """A probe and commands with known outcomes for it."""

    probe: CommandProbe
    exit_with_stdout: Callable[[int, str], tuple[str, ...]]
    """A command that writes the text to stdout and exits with the status."""
    blocks_forever: tuple[str, ...]
    """A command that never ends by itself."""
    missing_program: tuple[str, ...]
    """A command whose program does not exist."""


class CommandProbeContract:
    """Cases for :class:`~vs_sim.probes.CommandProbe`. Implement :meth:`probe_under_test`."""

    def probe_under_test(self) -> ProbeUnderTest:
        """A fresh probe with its harness."""
        raise NotImplementedError

    def test_status_and_output_come_back(self) -> None:
        """The result carries the exit status, zero or not, and what the command printed."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            subject = self.probe_under_test()
            status = rng.randint(0, 100)
            text = "".join(chr(rng.randint(32, 126)) for _ in range(rng.randint(0, 20)))
            result = subject.probe.run(subject.exit_with_stdout(status, text), timeout_seconds=30)
            assert result is not None, seed
            assert (result.returncode, result.stdout) == (status, text), seed

    def test_a_missing_program_is_unavailable(self) -> None:
        """A program that does not exist gives ``None`` instead of raising."""
        subject = self.probe_under_test()
        assert subject.probe.run(subject.missing_program, timeout_seconds=30) is None

    def test_a_command_that_outlives_the_timeout_is_unavailable(self) -> None:
        """A command still running at the timeout gives ``None``."""
        subject = self.probe_under_test()
        assert subject.probe.run(subject.blocks_forever, timeout_seconds=0.2) is None


@dataclass(frozen=True)
class PipedUnderTest:
    """Piped process groups with known behaviour, and how to run a case beside them.

    A real launcher starts a Python one-liner for each; a scripted process is driven
    by its script. Every case body runs inside ``drive``: the call itself for real
    processes, ``SimThreads.run`` for scripted ones, whose reads block on simulated
    threads.
    """

    writes_then_exits: Callable[[str, str, int], PipedProcess]
    """``(stdout, stderr, status)``: writes stdout, then stderr, then exits with the status."""
    merged_writes_then_exits: Callable[[str, str, int], PipedProcess]
    """The same, started with stderr merged into stdout."""
    blocks_until_signalled: Callable[[], PipedProcess]
    """A group that writes nothing and never ends by itself."""
    descendant_holds_output: Callable[[], PipedProcess]
    """A leader that exits 0 at once while a descendant keeps the pipes.

    The descendant writes ``ready`` only after the leader has exited, so a case that
    reads it knows the leader's status is already fixed.
    """
    drive: Callable[[Callable[[], object]], object]


_PIPED_TEXT = "ab \n"


class PipedProcessContract:
    """Cases for :class:`~vs_sim.processes.PipedProcess`. Implement :meth:`piped_under_test`.

    The cases that matter most to a caller that stops a group: a signal reaches the
    whole group only while its leader is unreaped, so a descendant still holding a
    pipe after the leader exited can be stopped until the leader is waited for, and
    a signal after that changes nothing.
    """

    def piped_under_test(self) -> PipedUnderTest:
        """A fresh harness."""
        raise NotImplementedError

    def test_output_and_status_come_back(self) -> None:
        """Both pipes carry what the group wrote, to end of file, and ``wait`` its status."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            subject = self.piped_under_test()
            out = "".join(rng.choice(_PIPED_TEXT) for _ in range(rng.randint(0, 20)))
            err = "".join(rng.choice(_PIPED_TEXT) for _ in range(rng.randint(0, 20)))
            status = rng.randint(0, 100)

            def case(
                subject: PipedUnderTest = subject,
                out: str = out,
                err: str = err,
                status: int = status,
            ) -> object:
                process = subject.writes_then_exits(out, err, status)
                return process.stdout.read(), process.stderr.read(), process.wait(None)

            assert subject.drive(case) == (out, err, status), seed

    def test_merged_output_keeps_the_write_order_and_stderr_is_at_end_of_file(self) -> None:
        """Started merged, stdout carries stdout then stderr, and stderr reads as empty."""
        subject = self.piped_under_test()

        def case() -> object:
            process = subject.merged_writes_then_exits("out\n", "err\n", 0)
            return process.stdout.read(), process.stderr.read(), process.wait(None)

        assert subject.drive(case) == ("out\nerr\n", "", 0)

    def test_a_timed_wait_on_a_running_group_times_out_and_a_group_signal_ends_it(self) -> None:
        """``wait`` raises ``ProcessTimeoutError`` while the leader runs; a signal then ends it."""
        for stop, expected in (
            ("terminate_group", -signal.SIGTERM),
            ("kill_group", -signal.SIGKILL),
        ):
            subject = self.piped_under_test()

            def case(subject: PipedUnderTest = subject, stop: str = stop) -> object:
                process = subject.blocks_until_signalled()
                try:
                    process.wait(0)
                except ProcessTimeoutError:
                    timed_out = True
                else:
                    timed_out = False
                getattr(process, stop)()
                return timed_out, process.wait(None)

            assert subject.drive(case) == (True, expected), stop

    def test_a_signal_after_the_leader_is_reaped_changes_nothing(self) -> None:
        """Once ``wait`` returned, group signals are no-ops and ``wait`` repeats the status."""
        subject = self.piped_under_test()

        def case() -> object:
            process = subject.writes_then_exits("", "", 3)
            first = process.wait(None)
            process.terminate_group()
            process.kill_group()
            return first, process.wait(None)

        assert subject.drive(case) == (3, 3)

    def test_a_signal_before_reaping_reaches_a_descendant_that_holds_the_output(self) -> None:
        """A leader that exited but is unreaped still lets a group signal end its descendants.

        The descendant's pipe then reaches end of file with nothing more written, and
        ``wait`` reports the leader's own status.
        """
        subject = self.piped_under_test()

        def case() -> object:
            process = subject.descendant_holds_output()
            ready = process.stdout.readline()
            process.terminate_group()
            return ready, process.stdout.read(), process.wait(None)

        assert subject.drive(case) == ("ready\n", "", 0)

    def test_cancelled_output_reads_return_while_the_group_still_runs(self) -> None:
        """``cancel_output`` makes reads return ``""`` without signalling the group."""
        subject = self.piped_under_test()

        def case() -> object:
            process = subject.blocks_until_signalled()
            process.cancel_output()
            read = process.stdout.read(), process.stderr.read()
            process.kill_group()
            return read, process.wait(None)

        assert subject.drive(case) == (("", ""), -signal.SIGKILL)


@dataclass(frozen=True)
class StreamingUnderTest:
    """A streaming command, a directory to run in, and commands with known outcomes."""

    command: StreamingCommand
    cwd: Path
    exits_with: Callable[[int, str, tuple[str, ...]], tuple[str, ...]]
    """A command that writes each stderr line, then the stdout text, and exits with the status."""
    prints_cwd: tuple[str, ...]
    """A command that writes its working directory to stdout and exits 0."""
    blocks_forever: tuple[str, ...]
    """A command that never ends by itself."""
    missing_program: tuple[str, ...]
    """A command whose program does not exist."""


_LINE_CHARACTERS = "".join(chr(code) for code in range(32, 127))
_STREAMED_BYTES = 1 << 20
"""More than any case writes, so the capture keeps everything."""


def _text(rng: SeededRandom, longest: int) -> str:
    return "".join(rng.choice(_LINE_CHARACTERS) for _ in range(rng.randint(0, longest)))


class StreamingCommandContract:
    """Cases for :class:`~vs_sim.streaming.StreamingCommand`. Implement :meth:`streaming_under_test`."""

    def streaming_under_test(self) -> StreamingUnderTest:
        """A fresh command with its harness."""
        raise NotImplementedError

    def test_output_streams_in_order_and_the_result_keeps_status_and_both_streams(self) -> None:
        """Every byte reaches the sink once, in order, per stream; the result holds all of it."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            subject = self.streaming_under_test()
            status = rng.randint(0, 100)
            stdout = _text(rng, 20)
            lines = tuple(_text(rng, 12) for _ in range(rng.randint(0, 5)))
            argv = subject.exits_with(status, stdout, lines)
            seen = Capture(_STREAMED_BYTES)

            result = subject.command.run(argv, cwd=subject.cwd, timeout_seconds=30, output=seen)

            stderr = "".join(f"{line}\n" for line in lines)
            assert (seen.stdout, seen.stderr) == (stdout.encode(), stderr.encode()), seed
            assert result.args == argv, seed
            assert (result.returncode, result.stdout) == (status, stdout), seed
            assert result.stderr == stderr, seed

    def test_the_command_runs_in_the_given_directory(self) -> None:
        """``cwd`` is the command's working directory."""
        subject = self.streaming_under_test()
        result = subject.command.run(
            subject.prints_cwd,
            cwd=subject.cwd,
            timeout_seconds=30,
            output=DISCARD,
        )
        assert result.returncode == 0
        assert Path(result.stdout).resolve() == subject.cwd.resolve()

    def test_a_missing_program_raises_file_not_found(self) -> None:
        """A program that does not exist raises instead of returning a status."""
        subject = self.streaming_under_test()
        raised = False
        try:
            subject.command.run(
                subject.missing_program,
                cwd=subject.cwd,
                timeout_seconds=30,
                output=DISCARD,
            )
        except FileNotFoundError:
            raised = True
        assert raised, "a missing program did not raise FileNotFoundError"

    def test_a_command_that_outlives_the_timeout_raises_timeout_expired(self) -> None:
        """A command still running at the timeout is killed and ``TimeoutExpired`` is raised."""
        subject = self.streaming_under_test()
        raised: subprocess.TimeoutExpired | None = None
        try:
            subject.command.run(
                subject.blocks_forever,
                cwd=subject.cwd,
                timeout_seconds=0.2,
                output=DISCARD,
            )
        except subprocess.TimeoutExpired as error:
            raised = error
        assert raised is not None, "a command past its timeout did not raise TimeoutExpired"
        assert (raised.cmd, raised.timeout) == (subject.blocks_forever, 0.2)


@dataclass(frozen=True)
class LoopbackHttpUnderTest:
    """A loopback HTTP client and the way a test puts a server behind a port."""

    http: LoopbackHttp
    serve: Callable[[HttpReply], tuple[int, Callable[[], None]]]
    """Start a server answering every GET with the reply; its port and how to stop it."""
    unused_port: Callable[[], int]
    """A port nothing listens on."""


class LoopbackHttpContract:
    """Cases for :class:`~vs_sim.loopback_http.LoopbackHttp`. Implement one factory."""

    def loopback_http_under_test(self) -> LoopbackHttpUnderTest:
        """A fresh client with its harness."""
        raise NotImplementedError

    def test_a_served_reply_comes_back_with_its_status_and_body(self) -> None:
        """Success and error statuses both count as an answer."""
        subject = self.loopback_http_under_test()
        for reply in (HttpReply(200, b"vibesys-ok\n"), HttpReply(404, b"missing")):
            port, stop = subject.serve(reply)
            try:
                got = subject.http.get(port, "/health?token=t", timeout=HANG_GUARD_S, max_bytes=64)
            finally:
                stop()
            assert got == reply

    def test_the_body_is_read_up_to_its_bound(self) -> None:
        """A longer body is cut at ``max_bytes``."""
        subject = self.loopback_http_under_test()
        port, stop = subject.serve(HttpReply(200, b"x" * 100))
        try:
            got = subject.http.get(port, "/", timeout=HANG_GUARD_S, max_bytes=10)
        finally:
            stop()
        assert got == HttpReply(200, b"x" * 10)

    def test_a_port_with_no_server_answers_nothing(self) -> None:
        """No listener is ``None``, not an error."""
        subject = self.loopback_http_under_test()
        assert subject.http.get(subject.unused_port(), "/", timeout=1.0, max_bytes=64) is None
