"""Public contract tests for bounded Docker command execution."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.images import (
    DOCKER_OUTPUT_TAIL_CHARACTERS,
    DOCKER_PROGRESS_LINE_CHARACTERS,
    DockerCommandResult,
    SubprocessDockerBuildRunner,
    TaskImageBuildError,
    build_task_image,
)
from vs_agent.api.testing import FakeDockerBuildRunner, docker_build_result
from vs_sim.api import ProcessTimeoutError
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    ScriptedStreamingCommand,
    SimThreads,
    StreamedRun,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vs_sim.api import Condition, Event, Lock, Worker


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
            self._threads.advance_to(self._threads.now() + self._threads.worker_settle_seconds)
            self._stopped = True
            return
        self._threads.advance_to(self._threads.now() + timeout)


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

    def sleep(self, seconds: float) -> None:
        self._threads.sleep(seconds)

    def now(self) -> float:
        return self._threads.now()

    def advance_to(self, instant: float) -> None:
        self._threads.advance_to(instant)

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
    launcher = FakePipedProcessLauncher(lambda _argv, _cwd: process)
    threads = SimThreads()

    result = threads.run(
        lambda: SubprocessDockerBuildRunner(threads=threads, processes=launcher).run(
            ("docker", "build"),
            cwd=tmp_path,
            timeout=30,
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
    assert launcher.started == [(("docker", "build"), tmp_path)]
    assert process.group_kills == 0
    assert process.stdout.closed
    assert process.stderr.closed


def test_runner_streams_complete_and_unterminated_progress_lines(tmp_path: Path) -> None:
    process = _FakePipedProcess(stderr="first\nsecond")
    launcher = FakePipedProcessLauncher(lambda _argv, _cwd: process)
    threads = SimThreads()
    observed: list[str] = []

    threads.run(
        lambda: SubprocessDockerBuildRunner(threads=threads, processes=launcher).run(
            ("docker", "build"),
            cwd=tmp_path,
            timeout=30,
            on_stderr_line=observed.append,
        )
    )

    assert observed == ["first", "second"]


@pytest.mark.parametrize("implementation", ["bounded-process", "public-fake"])
def test_docker_runner_contract_caps_each_progress_line(
    tmp_path: Path,
    implementation: str,
) -> None:
    long_line = "x" * (DOCKER_PROGRESS_LINE_CHARACTERS + 200)
    observed: list[str] = []
    if implementation == "bounded-process":
        process = _FakePipedProcess(stderr=long_line)
        threads = SimThreads()
        runner = SubprocessDockerBuildRunner(
            threads=threads,
            processes=FakePipedProcessLauncher(lambda _argv, _cwd: process),
        )
        threads.run(
            lambda: runner.run(
                ("docker", "build"),
                cwd=tmp_path,
                timeout=30,
                on_stderr_line=observed.append,
            )
        )
    else:
        FakeDockerBuildRunner(build_result=docker_build_result(stderr=long_line)).run(
            ("docker", "build"),
            cwd=tmp_path,
            timeout=30,
            on_stderr_line=observed.append,
        )

    assert observed == [long_line[:DOCKER_PROGRESS_LINE_CHARACTERS]]


def test_positional_streaming_command_seam_remains_compatible(tmp_path: Path) -> None:
    long_line = "p" * (DOCKER_PROGRESS_LINE_CHARACTERS + 20)
    streaming = ScriptedStreamingCommand(
        lambda _argv, _cwd: StreamedRun(
            returncode=19,
            stdout="stdout",
            stderr_lines=(long_line,),
        )
    )
    observed: list[str] = []

    result = SubprocessDockerBuildRunner(streaming).run(
        ("docker", "build"),
        cwd=tmp_path,
        timeout=30,
        on_stderr_line=observed.append,
    )

    assert result.returncode == 19
    assert result.stdout == "stdout"
    assert observed == [long_line[:DOCKER_PROGRESS_LINE_CHARACTERS]]
    assert streaming.calls == [(("docker", "build"), tmp_path, 30)]


def test_timeout_kills_reaps_and_closes_the_fake_process_group(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    process = _FakePipedProcess(wait_results=(None, -9))
    launcher = FakePipedProcessLauncher(lambda _argv, _cwd: process)
    threads = SimThreads()
    runner = SubprocessDockerBuildRunner(threads=threads, processes=launcher)

    with pytest.raises(TaskImageBuildError, match="timed out after 7 seconds"):
        threads.run(
            lambda: build_task_image(
                dockerfile,
                command_runner=runner,
                timeout=7,
            )
        )

    assert process.wait_timeouts == [7, 5]
    assert process.group_kills == 1
    assert process.stdout.closed
    assert process.stderr.closed


def test_normal_exit_and_inherited_writer_share_one_cleanup_deadline(tmp_path: Path) -> None:
    threads = _StuckThreads()
    process = _FakePipedProcess(wait_results=(0, -9), on_kill=threads.request_kill)
    runner = SubprocessDockerBuildRunner(
        threads=threads,
        processes=FakePipedProcessLauncher(lambda _argv, _cwd: process),
        cleanup_timeout=5,
    )
    started_at = threads.now()

    with pytest.raises(RuntimeError, match="output pipes did not settle"):
        runner.run(("docker", "build"), cwd=tmp_path, timeout=30)

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
        processes=FakePipedProcessLauncher(lambda _argv, _cwd: process),
        cleanup_timeout=5,
    )

    with pytest.raises(ProcessTimeoutError) as caught:
        runner.run(("docker", "build"), cwd=tmp_path, timeout=7)

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
        processes=FakePipedProcessLauncher(lambda _argv, _cwd: process),
        cleanup_timeout=5,
    )

    def exercise() -> None:
        with pytest.raises(ProcessTimeoutError) as caught:
            runner.run(("docker", "build"), cwd=tmp_path, timeout=7)
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
