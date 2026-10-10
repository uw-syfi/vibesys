"""Every implementation of a vs_sim interface passes the same contract, real or simulated."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from vs_sim.api import (
    LoopSignalSource,
    MonotonicClock,
    PidfdProcessSignaller,
    SubprocessLauncher,
    SystemClock,
    ThreadBlockingRunner,
)
from vs_sim.api.testing import (
    HANG_GUARD_S,
    BlockingRunnerContract,
    ClockContract,
    ClockUnderTest,
    FakeProcessLauncher,
    FakeProcessSignaller,
    FakeSignalSource,
    GatedBlockingRunner,
    InlineBlockingRunner,
    ManualClock,
    ProcessLauncherContract,
    ProcessScript,
    ProcessSignallerContract,
    ProcessSignallerUnderTest,
    ProcessUnderTest,
    RunnerUnderTest,
    SignalSourceContract,
    SignalSourceUnderTest,
    SleeperContract,
    SleeperUnderTest,
    VirtualClock,
    run_virtual,
    stop_process,
)

if TYPE_CHECKING:
    from vs_sim.api import ProcessSpec

_REAL_TICK_S = 0.001


def _virtual() -> tuple[VirtualClock, SleeperUnderTest]:
    clock = VirtualClock()
    return clock, SleeperUnderTest(
        clock=clock,
        sleeper=clock,
        run=lambda main: run_virtual(clock, main),
        tick=1.0,
        exact=True,
    )


class TestVirtualClock(ClockContract):
    def clock_under_test(self) -> ClockUnderTest:
        clock, _ = _virtual()
        return ClockUnderTest(
            clock, lambda seconds: run_virtual(clock, clock.sleep(seconds)), exact=True
        )


class TestManualClock(ClockContract):
    def clock_under_test(self) -> ClockUnderTest:
        clock = ManualClock()
        return ClockUnderTest(clock, clock.advance, exact=True)


class TestSystemClock(ClockContract):
    def clock_under_test(self) -> ClockUnderTest:
        return ClockUnderTest(
            SystemClock(), lambda _seconds: asyncio.run(asyncio.sleep(0)), exact=False
        )


class TestMonotonicClock(ClockContract):
    def clock_under_test(self) -> ClockUnderTest:
        return ClockUnderTest(
            MonotonicClock(), lambda _seconds: asyncio.run(asyncio.sleep(0)), exact=False
        )


class TestVirtualClockSleeping(SleeperContract):
    def sleeper_under_test(self) -> SleeperUnderTest:
        return _virtual()[1]


class TestSystemClockSleeping(SleeperContract):
    def sleeper_under_test(self) -> SleeperUnderTest:
        clock = SystemClock()
        return SleeperUnderTest(clock, clock, asyncio.run, tick=_REAL_TICK_S, exact=False)


class TestMonotonicClockSleeping(SleeperContract):
    def sleeper_under_test(self) -> SleeperUnderTest:
        clock = MonotonicClock()
        return SleeperUnderTest(clock, clock, asyncio.run, tick=_REAL_TICK_S, exact=False)


class TestThreadBlockingRunner(BlockingRunnerContract):
    def runner_under_test(self) -> RunnerUnderTest:
        return RunnerUnderTest(ThreadBlockingRunner(), asyncio.run)


class TestInlineBlockingRunner(BlockingRunnerContract):
    def runner_under_test(self) -> RunnerUnderTest:
        return RunnerUnderTest(InlineBlockingRunner(), asyncio.run)


class TestGatedBlockingRunner(BlockingRunnerContract):
    def runner_under_test(self) -> RunnerUnderTest:
        return RunnerUnderTest(GatedBlockingRunner(), asyncio.run)


class TestInlineBlockingRunnerOnTheVirtualLoop(BlockingRunnerContract):
    def runner_under_test(self) -> RunnerUnderTest:
        clock = VirtualClock()
        return RunnerUnderTest(InlineBlockingRunner(), lambda main: run_virtual(clock, main))


class TestFakeSignalSource(SignalSourceContract):
    def source_under_test(self) -> SignalSourceUnderTest:
        source = FakeSignalSource()
        return SignalSourceUnderTest(source, asyncio.run, deliver=source.deliver)


class TestLoopSignalSource(SignalSourceContract):
    def source_under_test(self) -> SignalSourceUnderTest:
        return SignalSourceUnderTest(
            LoopSignalSource(),
            asyncio.run,
            deliver=lambda number: os.kill(os.getpid(), number),
            isolate=True,
        )


def _assert_running(process: subprocess.Popen[bytes]) -> None:
    assert process.poll() is None


def _assert_terminated(process: subprocess.Popen[bytes]) -> None:
    assert process.wait(timeout=HANG_GUARD_S) == -signal.SIGTERM


class TestPidfdProcessSignaller(ProcessSignallerContract):
    def process_signaller_under_test(self) -> ProcessSignallerUnderTest:
        process = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return ProcessSignallerUnderTest(
            PidfdProcessSignaller(),
            process.pid,
            lambda: _assert_running(process),
            lambda: _assert_terminated(process),
            lambda: stop_process(process),
        )


class TestFakeProcessSignaller(ProcessSignallerContract):
    def process_signaller_under_test(self) -> ProcessSignallerUnderTest:
        pid = 1234
        signaller = FakeProcessSignaller({pid})
        return ProcessSignallerUnderTest(
            signaller,
            pid,
            lambda: None if pid in signaller.live_pids else pytest.fail("process was terminated"),
            lambda: (
                None if pid not in signaller.live_pids else pytest.fail("process is still live")
            ),
            lambda: None,
        )


def _python(code: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code, *args)


class TestSubprocessLauncher(ProcessLauncherContract):
    def process_under_test(self) -> ProcessUnderTest:
        return ProcessUnderTest(
            SubprocessLauncher(),
            asyncio.run,
            exit_with_stdout=lambda status, output: _python(
                "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[2])); "
                "sys.exit(int(sys.argv[1]))",
                str(status),
                output.hex(),
            ),
            echo_stdin=_python("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"),
            blocks_until_signalled=_python("import signal; signal.pause()"),
            missing_program=("/nonexistent/vs-sim-no-such-program",),
        )


def _script(spec: ProcessSpec) -> ProcessScript:
    match spec.argv:
        case ("exit", status, output):
            return ProcessScript(
                returncode=int(status), stdout=bytes.fromhex(output), duration_s=2.0
            )
        case ("cat",):
            return ProcessScript(echo_input=True)
        case ("block",):
            return ProcessScript(runs_until_signalled=True)
        case _:
            raise FileNotFoundError(spec.argv[0])


class TestFakeProcessLauncher(ProcessLauncherContract):
    def process_under_test(self) -> ProcessUnderTest:
        clock = VirtualClock()
        return ProcessUnderTest(
            FakeProcessLauncher(clock, _script),
            lambda main: run_virtual(clock, main),
            exit_with_stdout=lambda status, output: ("exit", str(status), output.hex()),
            echo_stdin=("cat",),
            blocks_until_signalled=("block",),
            missing_program=("no-such-program",),
        )
