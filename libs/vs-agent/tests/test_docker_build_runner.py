"""Public contract tests for bounded Docker command execution."""

from __future__ import annotations

import contextlib
import tempfile
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.images import (
    DOCKER_OUTPUT_TAIL_CHARACTERS,
    DockerCommandResult,
    ImageBuildTimeoutError,
    SubprocessDockerBuildRunner,
    TaskImageBuildError,
    agent_image,
    build_task_image,
)
from vs_agent.api.testing import FakeDockerBuildRunner, docker_build_result
from vs_sim.api import DISCARD, Capture, OutputStream, ProcessTimeoutError
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    ManualClock,
    PipedStart,
    ScriptedStreamingCommand,
    SimThreads,
    StreamedRun,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from vs_sim.api import Condition, Event, Lock, OutputSink, ThreadLocal, Worker


class _FakePipedProcess:
    """Scripted implementation of the shared synchronous process boundary."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        wait_results: Sequence[int | None | BaseException] = (0,),
        kill_error: BaseException | None = None,
        on_kill: Callable[[], None] | None = None,
    ) -> None:
        if not wait_results:
            message = "a fake piped process needs at least one wait result"
            raise ValueError(message)
        self.stdout = StringIO(stdout)
        self.stderr = StringIO(stderr)
        self._wait_results = tuple(wait_results)
        self._wait_index = 0
        self._kill_error = kill_error
        self._on_kill = on_kill
        self.wait_timeouts: list[float | None] = []
        self.group_kills = 0
        self.output_cancellations = 0

    def wait(self, timeout: float | None) -> int:
        """Return or raise the next scripted outcome; ``None`` means timeout."""
        self.wait_timeouts.append(timeout)
        index = min(self._wait_index, len(self._wait_results) - 1)
        self._wait_index += 1
        result = self._wait_results[index]
        if isinstance(result, BaseException):
            raise result
        if result is None:
            if timeout is None:
                raise AssertionError
            raise ProcessTimeoutError(("docker",), timeout)
        return result

    def kill_group(self) -> None:
        """Record the kill and apply its scripted effect."""
        self.group_kills += 1
        if self._on_kill is not None:
            self._on_kill()
        if self._kill_error is not None:
            raise self._kill_error

    def terminate_group(self) -> None:
        """These runners stop a process group only with SIGKILL."""
        raise AssertionError

    def cancel_output(self) -> None:
        """Record that active reads were asked to return."""
        self.output_cancellations += 1


class _StuckWorker:
    """A drain worker controlled by the test's virtual clock."""

    def __init__(self, threads: _StuckThreads, name: str) -> None:
        self._threads = threads
        self.name = name
        self._stopped = False
        self.join_timeouts: list[float | None] = []
        self.join_phases: list[bool] = []

    def is_alive(self) -> bool:
        return not self._stopped

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)
        self.join_phases.append(self._threads.kill_requested)
        if not self.is_alive() or timeout is None:
            return
        if self._threads.kill_requested and timeout >= self._threads.worker_settle_seconds:
            self._threads.advance(self._threads.worker_settle_seconds)
            self._stopped = True
            return
        self._threads.advance(timeout)


class _StuckThreads:
    """Deterministic threads whose drains finish only after group kill."""

    def __init__(self) -> None:
        self._threads = SimThreads()
        self.workers: list[_StuckWorker] = []
        self.kill_requested = False
        self.worker_settle_seconds = 0.25

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        del target, daemon
        worker = _StuckWorker(self, name)
        self.workers.append(worker)
        return worker

    def lock(self) -> Lock:
        return self._threads.lock()

    def rlock(self) -> Lock:
        return self._threads.rlock()

    def condition(self, lock: Lock | None = None) -> Condition:
        return self._threads.condition(lock)

    def event(self) -> Event:
        return self._threads.event()

    def local[T](self, initial: Callable[[], T]) -> ThreadLocal[T]:
        return self._threads.local(initial)

    def sleep(self, seconds: float) -> None:
        self._threads.sleep(seconds)

    def now(self) -> float:
        return self._threads.now()

    def run_async[T](self, main: Coroutine[object, object, T]) -> T:
        return self._threads.run_async(main)

    def advance(self, seconds: float) -> None:
        self._threads.advance(seconds)

    def request_kill(self) -> None:
        self.kill_requested = True


def test_runner_drains_large_multiline_and_unterminated_streams_into_bounded_tails(
    tmp_path: Path,
) -> None:
    repetitions = DOCKER_OUTPUT_TAIL_CHARACTERS
    stdout_line = "stdout-line\n"
    stdout_end = "stdout-end\n"
    stderr_character = "e"
    stderr_end = "stderr-end"
    stdout = stdout_line * repetitions + stdout_end
    stderr = stderr_character * (repetitions * 2) + stderr_end
    process = _FakePipedProcess(
        stdout=stdout,
        stderr=stderr,
        wait_results=(23,),
    )
    launcher = FakePipedProcessLauncher(lambda _start: process)
    threads = SimThreads()

    result = threads.run(
        lambda: SubprocessDockerBuildRunner(threads=threads, processes=launcher).run(
            ("docker", "build"),
            cwd=tmp_path,
            timeout=30,
            output=DISCARD,
        )
    )

    expected_stdout_characters = len(stdout_line) * repetitions + len(stdout_end)
    expected_stderr_characters = repetitions * 2 + len(stderr_end)
    assert result.returncode == 23
    assert len(result.stdout) == DOCKER_OUTPUT_TAIL_CHARACTERS
    assert len(result.stderr) == DOCKER_OUTPUT_TAIL_CHARACTERS
    assert result.stdout.endswith(stdout_end)
    assert result.stderr.endswith(stderr_end)
    assert "\n" in result.stdout
    assert "\n" not in result.stderr
    assert result.stdout_omitted_characters == (
        expected_stdout_characters - DOCKER_OUTPUT_TAIL_CHARACTERS
    )
    assert result.stderr_omitted_characters == (
        expected_stderr_characters - DOCKER_OUTPUT_TAIL_CHARACTERS
    )
    assert result.stdout_truncated
    assert result.stderr_truncated
    diagnostic = result.failure_diagnostic("missing")
    assert "earlier Docker output characters truncated" in diagnostic
    assert diagnostic.endswith(stderr_end)
    assert len(diagnostic) <= 1000
    assert launcher.started == [PipedStart(("docker", "build"), tmp_path)]
    assert process.group_kills == 0
    assert process.stdout.closed
    assert process.stderr.closed


_OUTPUT_TEXT = st.text(st.characters(blacklist_categories=("Cs",)), max_size=200)


@pytest.mark.parametrize("implementation", ["bounded-process", "public-fake"])
@given(stdout=_OUTPUT_TEXT, stderr=_OUTPUT_TEXT)
def test_docker_runner_contract_writes_every_byte_of_both_streams_to_the_output(
    implementation: str, stdout: str, stderr: str
) -> None:
    captured = Capture(limit=1 << 20)
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        if implementation == "bounded-process":
            process = _FakePipedProcess(stdout=stdout, stderr=stderr)
            threads = SimThreads()
            runner = SubprocessDockerBuildRunner(
                threads=threads,
                processes=FakePipedProcessLauncher(lambda _start: process),
            )
            result = threads.run(
                lambda: runner.run(("docker", "build"), cwd=cwd, timeout=30, output=captured)
            )
        else:
            result = FakeDockerBuildRunner(
                build_result=docker_build_result(stdout=stdout, stderr=stderr)
            ).run(("docker", "build"), cwd=cwd, timeout=30, output=captured)

    assert captured.stdout == stdout.encode()
    assert captured.stderr == stderr.encode()
    assert (result.stdout, result.stderr) == (stdout, stderr)


def test_a_failing_output_sink_does_not_stall_the_drain_and_fails_the_command(
    tmp_path: Path,
) -> None:
    class _Broken:
        def write(self, stream: OutputStream, data: bytes) -> None:
            del stream, data
            message = "sink broke"
            raise RuntimeError(message)

    process = _FakePipedProcess(stdout="a\n" * 100, stderr="b\n" * 100)
    threads = SimThreads()
    runner = SubprocessDockerBuildRunner(
        threads=threads, processes=FakePipedProcessLauncher(lambda _start: process)
    )

    with pytest.raises(RuntimeError, match="sink broke"):
        threads.run(
            lambda: runner.run(("docker", "build"), cwd=tmp_path, timeout=30, output=_Broken())
        )

    assert process.stdout.closed
    assert process.stderr.closed


def test_positional_streaming_command_seam_remains_compatible(tmp_path: Path) -> None:
    streaming = ScriptedStreamingCommand(
        lambda _argv, _cwd: StreamedRun(
            returncode=19,
            stdout="stdout",
            stderr_lines=("progress",),
        )
    )
    captured = Capture(limit=1024)

    result = SubprocessDockerBuildRunner(streaming).run(
        ("docker", "build"),
        cwd=tmp_path,
        timeout=30,
        output=captured,
    )

    assert result.returncode == 19
    assert result.stdout == "stdout"
    assert b"progress" in captured.stderr
    assert streaming.calls == [(("docker", "build"), tmp_path, 30)]


def test_timeout_kills_reaps_and_closes_the_fake_process_group(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    process = _FakePipedProcess(wait_results=(None, -9))
    launcher = FakePipedProcessLauncher(lambda _start: process)
    threads = SimThreads()
    runner = SubprocessDockerBuildRunner(threads=threads, processes=launcher)

    with pytest.raises(TaskImageBuildError, match="budget of 7 seconds ran out"):
        threads.run(
            lambda: build_task_image(
                dockerfile,
                command_runner=runner,
                timeout=7,
                clock=ManualClock(),
                output=DISCARD,
            )
        )

    first_wait = process.wait_timeouts[0]
    assert first_wait is not None
    assert 0 < first_wait <= 7
    assert process.wait_timeouts[1:] == [5]
    assert process.group_kills == 1
    assert process.stdout.closed
    assert process.stderr.closed


def test_normal_exit_and_inherited_writer_share_one_cleanup_deadline(tmp_path: Path) -> None:
    threads = _StuckThreads()
    process = _FakePipedProcess(wait_results=(0, -9), on_kill=threads.request_kill)
    runner = SubprocessDockerBuildRunner(
        threads=threads,
        processes=FakePipedProcessLauncher(lambda _start: process),
        cleanup_timeout=5,
    )
    started_at = threads.now()

    with pytest.raises(RuntimeError, match="output pipes did not settle"):
        runner.run(("docker", "build"), cwd=tmp_path, timeout=30, output=DISCARD)

    assert threads.now() - started_at <= 5
    assert process.wait_timeouts == [30, 2.5]
    assert process.group_kills == 1
    assert process.stdout.closed
    assert process.stderr.closed
    assert all(not worker.is_alive() for worker in threads.workers)
    assert all(
        any(
            after_kill and timeout is not None and timeout > 0
            for after_kill, timeout in zip(worker.join_phases, worker.join_timeouts, strict=True)
        )
        for worker in threads.workers
    )


def test_runtime_kill_failure_does_not_skip_remaining_cleanup(tmp_path: Path) -> None:
    threads = _StuckThreads()
    process = _FakePipedProcess(
        wait_results=(None, -9),
        kill_error=RuntimeError("injected kill failed"),
        on_kill=threads.request_kill,
    )
    runner = SubprocessDockerBuildRunner(
        threads=threads,
        processes=FakePipedProcessLauncher(lambda _start: process),
        cleanup_timeout=5,
    )

    with pytest.raises(ProcessTimeoutError) as caught:
        runner.run(("docker", "build"), cwd=tmp_path, timeout=7, output=DISCARD)

    assert any("RuntimeError" in note for note in caught.value.__notes__)
    assert process.output_cancellations == 1
    assert process.wait_timeouts == [7, 5]
    assert all(not worker.is_alive() for worker in threads.workers)
    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize(
    ("kill_error", "wait_results", "cleanup_error"),
    [
        (PermissionError("denied"), (None, -9), "PermissionError"),
        (KeyboardInterrupt(), (None, -9), "KeyboardInterrupt"),
        (None, (None, OSError("reap failed")), "OSError"),
    ],
)
def test_timeout_preserves_original_error_when_cleanup_fails(
    tmp_path: Path,
    kill_error: BaseException | None,
    wait_results: Sequence[int | None | BaseException],
    cleanup_error: str,
) -> None:
    process = _FakePipedProcess(wait_results=wait_results, kill_error=kill_error)
    threads = SimThreads()
    runner = SubprocessDockerBuildRunner(
        threads=threads,
        processes=FakePipedProcessLauncher(lambda _start: process),
        cleanup_timeout=5,
    )

    def exercise() -> None:
        with pytest.raises(ProcessTimeoutError) as caught:
            runner.run(("docker", "build"), cwd=tmp_path, timeout=7, output=DISCARD)
        assert any(cleanup_error in note for note in caught.value.__notes__)

    threads.run(exercise)

    assert process.stdout.closed
    assert process.stderr.closed
    assert process.output_cancellations == 1
    assert process.wait_timeouts == [7, 5]


@given(
    retained=st.text(
        alphabet=st.characters(categories=("L", "N", "P", "Z")),
        max_size=2000,
    ),
    omitted=st.integers(min_value=1, max_value=10_000_000),
)
def test_truncated_failure_diagnostics_are_marked_and_bounded(
    retained: str,
    omitted: int,
) -> None:
    result = DockerCommandResult(
        args=("docker", "build"),
        returncode=1,
        stdout="",
        stderr=retained,
        stderr_omitted_characters=omitted,
    )

    diagnostic = result.failure_diagnostic("docker build failed")

    assert len(diagnostic) <= 1000
    assert str(omitted) in diagnostic
    assert "earlier Docker output characters truncated" in diagnostic


_IMAGE_ID = "sha256:" + "b" * 64
_STALLED_BUILD_OUTPUT = (
    "#6 [ 2/4] RUN apt-get update\n"
    "#7 [ 3/4] RUN npm install -g @anthropic-ai/claude-code\n"
    "#7 12.3 npm warn network ETIMEDOUT while fetching the tarball\n"
)


def test_an_agent_image_build_that_never_finishes_is_stopped_with_its_output() -> None:
    """The stalled build's process group is killed and the error names the step and output."""
    stalled = _FakePipedProcess(stderr=_STALLED_BUILD_OUTPUT, wait_results=(None, -9))

    def start(request: PipedStart) -> _FakePipedProcess:
        if request.argv[1] == "build":
            return stalled
        if request.argv[-1].startswith("vibesys-agent-build:"):
            return _FakePipedProcess(wait_results=(1,))  # not built yet, so the build runs
        return _FakePipedProcess(stdout=_IMAGE_ID)

    threads = SimThreads()
    runner = SubprocessDockerBuildRunner(threads=threads, processes=FakePipedProcessLauncher(start))

    with pytest.raises(ImageBuildTimeoutError) as caught:
        threads.run(
            lambda: agent_image(
                "python:3.12-bookworm",
                command_runner=runner,
                timeout=240,
                clock=ManualClock(),
                output=DISCARD,
            )
        )

    assert stalled.group_kills == 1
    assert stalled.wait_timeouts[0] == 240
    assert caught.value.budget_seconds == 240
    assert caught.value.step == "step 3/4: RUN npm install -g @anthropic-ai/claude-code"
    assert "ETIMEDOUT while fetching the tarball" in str(caught.value)
    assert "step 3/4: RUN npm install" in str(caught.value)
    assert isinstance(caught.value, TaskImageBuildError)


@given(tail=st.text(min_size=1, max_size=9000).filter(lambda text: text.strip() != ""))
def test_a_stopped_task_build_reports_the_output_tail_and_running_step(tail: str) -> None:
    class StallingRunner:
        def run(
            self,
            argv: Sequence[str],
            *,
            cwd: Path,
            timeout: float,
            output: OutputSink,
        ) -> DockerCommandResult:
            del cwd
            output.write(OutputStream.STDERR, b"#4 [2/9] RUN pip install torch\n")
            raise ProcessTimeoutError(tuple(argv), timeout, stderr=tail.encode())

    with tempfile.TemporaryDirectory() as directory:
        dockerfile = Path(directory) / "Dockerfile"
        dockerfile.write_text("FROM scratch\n", encoding="utf-8")
        with pytest.raises(ImageBuildTimeoutError) as caught:
            build_task_image(
                dockerfile,
                command_runner=StallingRunner(),
                timeout=30,
                clock=ManualClock(),
                output=DISCARD,
            )

    assert caught.value.step == "step 2/9: RUN pip install torch"
    assert caught.value.output == tail.strip()[-4000:]
    assert tail.strip()[-4000:] in str(caught.value)


class _BudgetedRunner:
    """Docker commands that each take scripted time on a shared manual clock.

    A command that would outlive its ``timeout`` consumes exactly that long
    and raises, as a real runner that killed it would.
    """

    def __init__(self, clock: ManualClock, durations: Sequence[float]) -> None:
        self.clock = clock
        self._durations = durations
        self.timeouts: list[float] = []
        self.starts: list[float] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        output: OutputSink,
    ) -> DockerCommandResult:
        del cwd, output
        duration = self._durations[min(len(self.timeouts), len(self._durations) - 1)]
        self.timeouts.append(timeout)
        self.starts.append(self.clock.now())
        if duration >= timeout:
            self.clock.advance(timeout)
            raise ProcessTimeoutError(tuple(argv), timeout)
        self.clock.advance(duration)
        return DockerCommandResult(tuple(argv), 0, _IMAGE_ID, "")


@given(
    budget=st.floats(min_value=0.5, max_value=100_000, allow_nan=False),
    durations=st.lists(st.floats(min_value=0, max_value=100_000, allow_nan=False), min_size=1),
)
def test_no_docker_command_is_given_more_than_the_budget_that_remains(
    budget: float, durations: list[float]
) -> None:
    """Whatever the commands do, the build ends within the caller's limit."""
    clock = ManualClock()
    runner = _BudgetedRunner(clock, durations)

    with contextlib.suppress(ImageBuildTimeoutError):
        agent_image(
            "python:3.12-bookworm",
            command_runner=runner,
            timeout=budget,
            clock=clock,
            output=DISCARD,
        )

    assert runner.timeouts
    for started, timeout in zip(runner.starts, runner.timeouts, strict=True):
        assert 0 < timeout <= budget - started + 1e-9
    assert clock.now() <= budget + 1e-9
