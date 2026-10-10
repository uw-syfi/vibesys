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
    InheritedStdioLauncher,
    LoopSignalSource,
    MonotonicClock,
    PidfdProcessSignaller,
    ProbeResult,
    SubprocessLauncher,
    SubprocessProbe,
    SystemClock,
    ThreadBlockingRunner,
    run_foreground,
)
from vs_sim.api.testing import (
    HANG_GUARD_S,
    BlockingRunnerContract,
    ClockContract,
    ClockUnderTest,
    CommandProbeContract,
    FakeForegroundLauncher,
    FakeProcessLauncher,
    FakeProcessSignaller,
    FakeSignalSource,
    ForegroundLauncherContract,
    ForegroundScript,
    ForegroundUnderTest,
    GatedBlockingRunner,
    InlineBlockingRunner,
    ManualClock,
    ProbeUnderTest,
    ProcessLauncherContract,
    ProcessScript,
    ProcessSignallerContract,
    ProcessSignallerUnderTest,
    ProcessUnderTest,
    RunnerUnderTest,
    ScriptedProbe,
    SignalSourceContract,
    SignalSourceUnderTest,
    SleeperContract,
    SleeperUnderTest,
    VirtualClock,
    run_virtual,
    stop_process,
)

if TYPE_CHECKING:
    from pathlib import Path

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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pidfds are Linux system calls")
class TestPidfdProcessSignallerThroughDirectSyscalls(ProcessSignallerContract):
    """The fallback for interpreters built without pidfd wrappers (uv's standalone builds)."""

    def process_signaller_under_test(self) -> ProcessSignallerUnderTest:
        process = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return ProcessSignallerUnderTest(
            PidfdProcessSignaller(direct_syscalls=True),
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


class TestInheritedStdioLauncher(ForegroundLauncherContract):
    def foreground_under_test(self) -> ForegroundUnderTest:
        return ForegroundUnderTest(
            InheritedStdioLauncher(),
            asyncio.run,
            exit_with=lambda status: _python("import sys; sys.exit(int(sys.argv[1]))", str(status)),
            blocks_until_signalled=_python("import signal; signal.pause()"),
            missing_program=("/nonexistent/vs-sim-no-such-program",),
        )


def _foreground_script(argv: tuple[str, ...]) -> ForegroundScript:
    match argv:
        case ("exit", status):
            return ForegroundScript(returncode=int(status), exits_immediately=True)
        case ("block",):
            return ForegroundScript()
        case _:
            raise FileNotFoundError(argv[0])


class TestFakeForegroundLauncher(ForegroundLauncherContract):
    def foreground_under_test(self) -> ForegroundUnderTest:
        return ForegroundUnderTest(
            FakeForegroundLauncher(_foreground_script),
            asyncio.run,
            exit_with=lambda status: ("exit", str(status)),
            blocks_until_signalled=("block",),
            missing_program=("no-such-program",),
        )


class TestSubprocessProbe(CommandProbeContract):
    def probe_under_test(self) -> ProbeUnderTest:
        return ProbeUnderTest(
            SubprocessProbe(),
            exit_with_stdout=lambda status, text: _python(
                "import sys; sys.stdout.write(sys.argv[2]); sys.exit(int(sys.argv[1]))",
                str(status),
                text,
            ),
            blocks_forever=_python("import signal; signal.pause()"),
            missing_program=("/nonexistent/vs-sim-no-such-program",),
        )


def _probe_script(argv: tuple[str, ...]) -> ProbeResult | None:
    match argv:
        case ("exit", status, text):
            return ProbeResult(int(status), text)
        case _:
            return None


class TestScriptedProbe(CommandProbeContract):
    def probe_under_test(self) -> ProbeUnderTest:
        return ProbeUnderTest(
            ScriptedProbe(_probe_script),
            exit_with_stdout=lambda status, text: ("exit", str(status), text),
            blocks_forever=("block",),
            missing_program=("no-such-program",),
        )


def test_inherited_stdio_launcher_starts_the_child_in_the_requested_directory(
    tmp_path: Path,
) -> None:
    here = _python(
        "import os, sys; sys.exit(0 if os.getcwd() == sys.argv[1] else 1)", str(tmp_path.resolve())
    )

    inside = asyncio.run(run_foreground(InheritedStdioLauncher(), here, cwd=tmp_path))
    outside = asyncio.run(run_foreground(InheritedStdioLauncher(), here))

    assert (inside, outside) == (0, 1)


def test_fake_foreground_launcher_records_how_a_child_was_started(tmp_path: Path) -> None:
    launcher = FakeForegroundLauncher(
        lambda _argv: ForegroundScript(returncode=4, exits_immediately=True)
    )

    status = asyncio.run(run_foreground(launcher, ["tool", "--flag"], env={"K": "v"}, cwd=tmp_path))

    child = launcher.children[0]
    assert (status, child.argv, child.env, child.cwd) == (
        4,
        ("tool", "--flag"),
        {"K": "v"},
        tmp_path,
    )
