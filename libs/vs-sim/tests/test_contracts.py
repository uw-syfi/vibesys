"""Every implementation of a vs_sim interface passes the same contract, real or simulated."""

from __future__ import annotations

import asyncio
import http.server
import os
import signal
import socket
import subprocess
import sys
import tempfile
from functools import partial
from io import BytesIO, StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_sim.api import (
    HttpReply,
    InheritedStdioLauncher,
    LoopSignalSource,
    MonotonicClock,
    OsProcesses,
    OsThreads,
    PidfdProcessSignaller,
    ProbeResult,
    ProcessesProbe,
    SubprocessLauncher,
    SubprocessProbe,
    SystemClock,
    ThreadBlockingRunner,
    UrllibLoopbackHttp,
    run_foreground,
)
from vs_sim.api.testing import (
    HANG_GUARD_S,
    BlockingRunnerContract,
    ClockContract,
    ClockUnderTest,
    CommandProbeContract,
    FakeForegroundLauncher,
    FakeLoopbackHttp,
    FakeProcesses,
    FakeProcessLauncher,
    FakeProcessSignaller,
    FakeSignalSource,
    ForegroundLauncherContract,
    ForegroundScript,
    ForegroundUnderTest,
    GatedBlockingRunner,
    InlineBlockingRunner,
    LoopbackHttpContract,
    LoopbackHttpUnderTest,
    ManualClock,
    ProbeUnderTest,
    ProcessesContract,
    ProcessesUnderTest,
    ProcessLauncherContract,
    ProcessScript,
    ProcessSignallerContract,
    ProcessSignallerUnderTest,
    ProcessUnderTest,
    RunnerUnderTest,
    ScriptedProbe,
    ScriptedStreamingCommand,
    SignalSourceContract,
    SignalSourceUnderTest,
    SleeperContract,
    SleeperUnderTest,
    StreamedRun,
    StreamingCommandContract,
    StreamingUnderTest,
    VirtualClock,
    run_in_child,
    run_virtual,
    stop_process,
)

# test-isolation: inject between Popen and private wrapper construction to verify ownership.
from vs_sim.processes import _start_piped_process

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import IO

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


class TestManualClockSleeping(SleeperContract):
    def sleeper_under_test(self) -> SleeperUnderTest:
        clock = ManualClock()
        return SleeperUnderTest(clock, clock, asyncio.run, tick=1.0, exact=True)


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


def _restores_the_earlier_handler() -> bool:
    def earlier(_number: int, _frame: object) -> None:
        return None

    signal.signal(signal.SIGUSR2, earlier)
    source = LoopSignalSource()

    async def scope() -> bool:
        source.add_handler(signal.SIGUSR2, lambda: None)
        source.add_handler(signal.SIGUSR2, lambda: None)
        return source.remove_handler(signal.SIGUSR2)

    removed = asyncio.run(scope())
    return removed and signal.getsignal(signal.SIGUSR2) is earlier


def _off_main_thread_installs_nothing() -> tuple[bool, object]:
    source = LoopSignalSource()
    before = signal.getsignal(signal.SIGUSR2)
    outcome: list[bool] = []

    async def scope() -> None:
        source.add_handler(signal.SIGUSR2, lambda: None)
        outcome.append(source.remove_handler(signal.SIGUSR2))

    worker = OsThreads().spawn(lambda: asyncio.run(scope()), name="off-main")
    worker.join(HANG_GUARD_S)
    return (outcome == [False], signal.getsignal(signal.SIGUSR2) is before)


class TestLoopSignalSourceScope:
    """The real source leaves the process's signal dispositions as it found them."""

    def test_removing_restores_the_handler_installed_before(self) -> None:
        assert run_in_child(_restores_the_earlier_handler)

    def test_off_the_main_thread_it_installs_nothing(self) -> None:
        assert run_in_child(_off_main_thread_installs_nothing) == (True, True)


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


class _StartedPipedGroup:
    """A deterministic owned group used to fault wrapper construction after start."""

    def __init__(self) -> None:
        self.stdout = BytesIO(b"stdout")
        self.stderr = BytesIO(b"stderr")
        self.group_kills = 0
        self.wait_timeouts: list[float | None] = []

    def terminate_group(self) -> None:
        raise AssertionError

    def kill_group(self) -> None:
        self.group_kills += 1

    def wait(self, timeout: float | None) -> int:
        self.wait_timeouts.append(timeout)
        return -signal.SIGKILL


class _OwnedOutput(StringIO):
    """A text wrapper that owns its binary input, matching the real wrapper contract."""

    def __init__(self, source: IO[bytes]) -> None:
        super().__init__()
        self._source = source

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        self._source.close()
        super().close()


class _FailingOutputFactory:
    def __init__(self, fail_at: int) -> None:
        self._fail_at = fail_at
        self.calls = 0
        self.opened: list[_OwnedOutput] = []

    def __call__(self, source: IO[bytes]) -> StringIO:
        self.calls += 1
        if self.calls == self._fail_at:
            message = "injected output wrapper failure"
            raise ValueError(message)
        output = _OwnedOutput(source)
        self.opened.append(output)
        return output


@pytest.mark.parametrize("fail_at", [1, 2])
def test_piped_process_start_rolls_back_every_resource_when_output_wrapping_fails(
    fail_at: int,
) -> None:
    process = _StartedPipedGroup()
    starts: list[tuple[tuple[str, ...], Path | None]] = []
    outputs = _FailingOutputFactory(fail_at)

    def start(argv: tuple[str, ...], cwd: Path | None) -> _StartedPipedGroup:
        starts.append((argv, cwd))
        return process

    with pytest.raises(ValueError, match="injected output wrapper failure"):
        _start_piped_process(("sky", "status"), None, start, outputs)

    assert starts == [(("sky", "status"), None)]
    assert process.group_kills == 1
    assert process.wait_timeouts == [5.0]
    assert process.stdout.closed
    assert process.stderr.closed
    assert all(output.closed for output in outputs.opened)


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


class TestProcessesProbe(TestSubprocessProbe):
    def probe_under_test(self) -> ProbeUnderTest:
        subject = super().probe_under_test()
        return ProbeUnderTest(
            ProcessesProbe(OsProcesses()),
            subject.exit_with_stdout,
            subject.blocks_forever,
            subject.missing_program,
        )


class TestOsProcesses(ProcessesContract):
    def processes_under_test(self, *, cleanup_timeout: float) -> ProcessesUnderTest:
        held = Path(tempfile.mkdtemp()) / "held"
        held.touch()
        return ProcessesUnderTest(
            OsProcesses(cleanup_timeout=cleanup_timeout),
            exit_with=lambda status, out, err: _python(
                "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[2])); "
                "sys.stderr.buffer.write(bytes.fromhex(sys.argv[3])); sys.exit(int(sys.argv[1]))",
                str(status),
                out.hex(),
                err.hex(),
            ),
            echo_stdin=_python("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"),
            print_cwd_and_env=lambda name: _python(
                "import os, sys; sys.stdout.write(os.getcwd() + chr(10) + os.environ[sys.argv[1]])",
                name,
            ),
            # The grandchild in the leader's group also holds stdout open, so the call returns
            # only because the whole group is killed.
            outlives_timeout=lambda written: _python(
                "import subprocess, sys, signal; "
                "sys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); sys.stdout.flush(); "
                "subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()']); "
                "signal.pause()",
                written.hex(),
            ),
            # The leader exits at once; its grandchild, in a session of its own, holds stdout
            # while the file exists (at most for the hang guard).
            survives_kill=_python(
                "import subprocess, sys; subprocess.Popen([sys.executable, '-c', "
                "'import os, sys, time; deadline = time.monotonic() + float(sys.argv[2])\\n"
                "while os.path.exists(sys.argv[1]) and time.monotonic() < deadline: time.sleep(0.05)', "
                "sys.argv[1], sys.argv[2]], start_new_session=True)",
                str(held),
                str(HANG_GUARD_S),
            ),
            missing_program=("/nonexistent/vs-sim-no-such-program",),
            now=OsThreads().now,
            release=held.unlink,
        )


def _processes_script(spec: ProcessSpec) -> ProcessScript:
    match spec.argv:
        case ("exit", status, out, err):
            return ProcessScript(int(status), bytes.fromhex(out), bytes.fromhex(err))
        case ("cat",):
            return ProcessScript(echo_input=True)
        case ("pwd-env", name):
            env = spec.env or {}
            return ProcessScript(stdout=f"{spec.cwd}\n{env[name]}".encode())
        case ("block", written):
            return ProcessScript(stdout=bytes.fromhex(written), runs_until_signalled=True)
        case ("escape",):
            return ProcessScript(runs_until_signalled=True, survives_kill=True)
        case _:
            raise FileNotFoundError(spec.argv[0])


class TestFakeProcesses(ProcessesContract):
    def processes_under_test(self, *, cleanup_timeout: float) -> ProcessesUnderTest:
        del cleanup_timeout
        clock = ManualClock()
        return ProcessesUnderTest(
            FakeProcesses(_processes_script, pass_time=clock.advance),
            exit_with=lambda status, out, err: ("exit", str(status), out.hex(), err.hex()),
            echo_stdin=("cat",),
            print_cwd_and_env=lambda name: ("pwd-env", name),
            outlives_timeout=lambda written: ("block", written.hex()),
            survives_kill=("escape",),
            missing_program=("no-such-program",),
            now=clock.now,
        )


class TestProcessesProbeOverFakeProcesses(CommandProbeContract):
    def probe_under_test(self) -> ProbeUnderTest:
        return ProbeUnderTest(
            ProcessesProbe(FakeProcesses(_processes_script, pass_time=ManualClock().advance)),
            exit_with_stdout=lambda status, text: ("exit", str(status), text.encode().hex(), ""),
            blocks_forever=("block", ""),
            missing_program=("no-such-program",),
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


def _streaming_script(argv: tuple[str, ...], cwd: Path) -> StreamedRun | None:
    match argv:
        case ("exit", status, stdout, *lines):
            return StreamedRun(int(status), stdout, tuple(lines))
        case ("pwd",):
            return StreamedRun(stdout=str(cwd))
        case ("block",):
            return StreamedRun(blocks=True)
        case _:
            return None


class TestScriptedStreamingCommand(StreamingCommandContract):
    @pytest.fixture(autouse=True)
    def _directory(self, tmp_path: Path) -> None:
        self._cwd = tmp_path

    def streaming_under_test(self) -> StreamingUnderTest:
        return StreamingUnderTest(
            ScriptedStreamingCommand(_streaming_script),
            cwd=self._cwd,
            exits_with=lambda status, stdout, lines: ("exit", str(status), stdout, *lines),
            prints_cwd=("pwd",),
            blocks_forever=("block",),
            missing_program=("no-such-program",),
        )


class _Reply(http.server.BaseHTTPRequestHandler):
    reply: HttpReply

    def do_GET(self) -> None:
        self.send_response(self.reply.status)
        self.send_header("Content-Length", str(len(self.reply.body)))
        self.end_headers()
        self.wfile.write(self.reply.body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # LW-257801 [A002]; the standard library names this parameter, and an override must keep its name.
        del format, args


def _serve_real(reply: HttpReply) -> tuple[int, Callable[[], None]]:
    handler = type("_Handler", (_Reply,), {"reply": reply})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    # A short poll lets ``shutdown`` return at once instead of after the default half second.
    worker = OsThreads().spawn(partial(server.serve_forever, 0.005), name="http-contract")

    def stop() -> None:
        server.shutdown()
        server.server_close()
        worker.join(HANG_GUARD_S)

    return server.server_address[1], stop


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.settimeout(HANG_GUARD_S)
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TestUrllibLoopbackHttp(LoopbackHttpContract):
    def loopback_http_under_test(self) -> LoopbackHttpUnderTest:
        return LoopbackHttpUnderTest(UrllibLoopbackHttp(), _serve_real, _unused_port)


class TestFakeLoopbackHttp(LoopbackHttpContract):
    def loopback_http_under_test(self) -> LoopbackHttpUnderTest:
        http_client = FakeLoopbackHttp()
        ports = iter(range(40_000, 41_000))

        def serve(reply: HttpReply) -> tuple[int, Callable[[], None]]:
            port = next(ports)
            http_client.serve(port, lambda _target: reply)
            return port, lambda: http_client.stop(port)

        return LoopbackHttpUnderTest(http_client, serve, lambda: 39_999)
