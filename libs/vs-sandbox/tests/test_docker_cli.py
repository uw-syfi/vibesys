"""The real Docker CLI adapter over the ``vs_sim`` process interface, in virtual time."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import SubprocessDockerCli
from vs_sim.api import (
    DISCARD,
    Capture,
    OutputSink,
    OutputStream,
    ProcessOutputTooLarge,
    ProcessRun,
    ProcessSpec,
    ProcessTimeoutError,
)
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    FakeProcesses,
    ManualClock,
    PipedStart,
    ProcessScript,
)

if TYPE_CHECKING:
    from vs_sim.api import PipedProcess

_TIMEOUTS = st.floats(min_value=0.001, max_value=600, allow_nan=False, allow_infinity=False)


def _docker(script: ProcessScript, clock: ManualClock) -> tuple[SubprocessDockerCli, FakeProcesses]:
    processes = FakeProcesses(lambda _spec: script, pass_time=clock.advance)
    return SubprocessDockerCli(processes=processes, launcher=_no_spawns()), processes


def _no_spawns() -> FakePipedProcessLauncher:
    def unexpected(start: PipedStart) -> PipedProcess:
        message = f"unexpected docker spawn {start.argv!r}"
        raise AssertionError(message)

    return FakePipedProcessLauncher(unexpected)


@given(
    returncode=st.integers(min_value=-20, max_value=255),
    stdout=st.text(max_size=40),
    stderr=st.text(max_size=40),
)
def test_a_finished_command_keeps_its_status_streams_and_arguments(
    returncode: int, stdout: str, stderr: str
) -> None:
    clock = ManualClock()
    script = ProcessScript(returncode, stdout.encode(), stderr.encode(), duration_s=2)
    docker, processes = _docker(script, clock)

    result = docker.run(["docker", "version"], timeout_seconds=30, output=DISCARD)

    assert result.args == ["docker", "version"]
    assert result.returncode == returncode
    assert result.stdout == stdout.replace("\r\n", "\n").replace("\r", "\n")
    assert result.stderr == stderr.replace("\r\n", "\n").replace("\r", "\n")
    assert processes.ran == [ProcessSpec(("docker", "version"))]
    assert clock.now() == 2


# A finished command's streams are decoded as strict UTF-8, as `subprocess.run(text=True)` does.
_UTF8 = st.text(max_size=40).map(str.encode)


@given(stdout=_UTF8, stderr=_UTF8, finishes=st.booleans())
def test_every_byte_a_command_writes_reaches_the_output_whether_it_finishes_or_times_out(
    stdout: bytes, stderr: bytes, *, finishes: bool
) -> None:
    script = ProcessScript(
        stdout=stdout, stderr=stderr, duration_s=1, runs_until_signalled=not finishes
    )
    docker, _ = _docker(script, ManualClock())
    output = Capture(limit=1 << 10)

    with pytest.raises(ProcessTimeoutError) if not finishes else contextlib.nullcontext():
        docker.run(["docker", "run", "-d", "image"], timeout_seconds=30, output=output)

    assert (output.stdout, output.stderr) == (stdout, stderr)


@given(timeout=_TIMEOUTS, stdout=st.binary(max_size=40), stderr=st.binary(max_size=40))
def test_a_timeout_raises_after_the_timeout_with_the_partial_output(
    timeout: float, stdout: bytes, stderr: bytes
) -> None:
    clock = ManualClock()
    script = ProcessScript(stdout=stdout, stderr=stderr, runs_until_signalled=True)
    docker, _ = _docker(script, clock)

    with pytest.raises(ProcessTimeoutError) as raised:
        docker.run(["docker", "info"], timeout_seconds=timeout, output=DISCARD)

    assert raised.value.cmd == ["docker", "info"]
    assert raised.value.timeout == timeout
    assert raised.value.output == (stdout or None)
    assert raised.value.stderr == (stderr or None)
    assert clock.now() == timeout


def test_a_group_the_kill_left_behind_is_a_timeout_that_says_so() -> None:
    clock = ManualClock()
    script = ProcessScript(runs_until_signalled=True, survives_kill=True)
    docker, _ = _docker(script, clock)

    with pytest.raises(ProcessTimeoutError) as raised:
        docker.run(["docker", "rm", "-f", "c"], timeout_seconds=3, output=DISCARD)

    assert raised.value.timeout == 3
    assert raised.value.__notes__ == ["process 1000 survived the kill"]


def test_a_docker_binary_that_cannot_start_raises_its_os_error() -> None:
    missing = FileNotFoundError("docker")

    def script(_spec: ProcessSpec) -> ProcessScript:
        raise missing

    docker = SubprocessDockerCli(
        processes=FakeProcesses(script, pass_time=ManualClock().advance), launcher=_no_spawns()
    )

    with pytest.raises(FileNotFoundError) as raised:
        docker.run(["docker", "version"], timeout_seconds=30, output=DISCARD)

    assert raised.value is missing


class _StoppedForOutput:
    """Processes whose every run was stopped for writing past its cap."""

    def __init__(self, stopped: ProcessOutputTooLarge) -> None:
        self._stopped = stopped

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        del spec, timeout, output
        return self._stopped


@given(
    limit=st.integers(min_value=0, max_value=1 << 30),
    stream=st.sampled_from(OutputStream),
    detail=st.sampled_from(["", "pid 7 survived"]),
)
def test_a_run_stopped_for_too_much_output_raises_naming_the_stream_and_cap(
    limit: int, stream: OutputStream, detail: str
) -> None:
    stopped = ProcessOutputTooLarge(limit, stream, b"", b"", detail)
    docker = SubprocessDockerCli(
        processes=_StoppedForOutput(stopped), launcher=FakePipedProcessLauncher()
    )

    with pytest.raises(
        OSError, match=f"docker logs wrote more than {limit} bytes to {stream}"
    ) as raised:
        docker.run(["docker", "logs"], timeout_seconds=30, output=DISCARD)

    assert getattr(raised.value, "__notes__", []) == ([detail] if detail else [])


@given(arguments=st.lists(st.text(min_size=1, max_size=8), max_size=4))
def test_a_spawn_starts_its_arguments_through_the_launcher_unchanged(arguments: list[str]) -> None:
    started = object()
    launcher = FakePipedProcessLauncher(lambda _start: cast("PipedProcess", started))
    docker = SubprocessDockerCli(
        processes=FakeProcesses(_no_runs, pass_time=ManualClock().advance), launcher=launcher
    )

    process = docker.spawn(["docker", "exec", *arguments])

    assert process is started
    assert launcher.started == [PipedStart(("docker", "exec", *arguments))]


def _no_runs(spec: ProcessSpec) -> ProcessScript:
    message = f"unexpected docker run {spec.argv!r}"
    raise AssertionError(message)
