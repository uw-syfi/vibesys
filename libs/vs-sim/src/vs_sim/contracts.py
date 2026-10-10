"""Contract suites: the cases every implementation of an interface must pass.

A suite is a base class. A test module subclasses it, names the subclass ``Test<Variant>``
and implements the one factory method, so pytest runs every case against that
implementation, real or Fake. The cases drive an implementation only through its
interface, and none of them reads the wall clock to decide an outcome.
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vs_sim.child import run_in_child
from vs_sim.gate import Gate
from vs_sim.processes import ProcessSpec
from vs_sim.randomness import SeededRandom

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from vs_sim.blocking import BlockingRunner
    from vs_sim.clock import Clock, Sleeper
    from vs_sim.probes import CommandProbe
    from vs_sim.processes import ForegroundLauncher, ProcessLauncher, ProcessOutcome
    from vs_sim.signals import ProcessSignaller, SignalSource

type Run = Callable[[Coroutine[Any, Any, Any]], Any]
"""Run a coroutine to completion on the loop the implementation under test belongs to."""

_CASES = 25
"""Seeded cases per property: the same draws every run."""


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
