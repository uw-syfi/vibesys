"""The real Docker CLI adapter over the ``vs_sim`` process interface, in virtual time."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import SubprocessDockerCli
from vs_sim.api import ProcessSpec, ProcessTimeoutError
from vs_sim.api.testing import FakeProcesses, ManualClock, ProcessScript

_TIMEOUTS = st.floats(min_value=0.001, max_value=600, allow_nan=False, allow_infinity=False)


def _docker(script: ProcessScript, clock: ManualClock) -> tuple[SubprocessDockerCli, FakeProcesses]:
    processes = FakeProcesses(lambda _spec: script, pass_time=clock.advance)
    return SubprocessDockerCli(processes=processes), processes


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

    result = docker.run(["docker", "version"], timeout_seconds=30)

    assert result.args == ["docker", "version"]
    assert result.returncode == returncode
    assert result.stdout == stdout.replace("\r\n", "\n").replace("\r", "\n")
    assert result.stderr == stderr.replace("\r\n", "\n").replace("\r", "\n")
    assert processes.ran == [ProcessSpec(("docker", "version"))]
    assert clock.now() == 2


@given(timeout=_TIMEOUTS, stdout=st.binary(max_size=40), stderr=st.binary(max_size=40))
def test_a_timeout_raises_after_the_timeout_with_the_partial_output(
    timeout: float, stdout: bytes, stderr: bytes
) -> None:
    clock = ManualClock()
    script = ProcessScript(stdout=stdout, stderr=stderr, runs_until_signalled=True)
    docker, _ = _docker(script, clock)

    with pytest.raises(ProcessTimeoutError) as raised:
        docker.run(["docker", "info"], timeout_seconds=timeout)

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
        docker.run(["docker", "rm", "-f", "c"], timeout_seconds=3)

    assert raised.value.timeout == 3
    assert raised.value.__notes__ == ["process 1000 survived the kill"]


def test_a_docker_binary_that_cannot_start_raises_its_os_error() -> None:
    missing = FileNotFoundError("docker")

    def script(_spec: ProcessSpec) -> ProcessScript:
        raise missing

    docker = SubprocessDockerCli(processes=FakeProcesses(script, pass_time=ManualClock().advance))

    with pytest.raises(FileNotFoundError) as raised:
        docker.run(["docker", "version"], timeout_seconds=30)

    assert raised.value is missing
