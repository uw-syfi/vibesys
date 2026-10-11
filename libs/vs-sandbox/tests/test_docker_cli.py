"""Public Docker CLI process-ownership regressions on simulated time."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import SubprocessDockerCli
from vs_sandbox.api.testing import (
    CapturedProcessFaults,
    FakeCapturedProcessRunner,
    StoppableScript,
)

# test-isolation: compare real Popen close failures with the public Fake through the shared owner because OS streams cannot deterministically fail close.
from vs_sandbox.process_execution import (
    CapturedProcessRequest,
    OwnedProcessRunner,
    PopenProcess,
)
from vs_sim.api import ProcessTimeoutError
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    import subprocess


class _FailingCloseStream(StringIO):
    def __init__(self, name: str, attempts: list[str]) -> None:
        super().__init__()
        self._name = name
        self._attempts = attempts

    def close(self) -> None:
        self._attempts.append(self._name)
        super().close()
        message = f"{self._name} close failed"
        raise OSError(message)


@dataclass
class _ScriptedTextPopen:
    stdin: StringIO
    stdout: StringIO
    stderr: StringIO
    communication_error: BaseException
    returncode: int | None = 0

    def communicate(
        self,
        input: str | None = None,  # noqa: A002  # lint-waiver: LW-194306 [A002]; this deterministic Popen boundary must accept the real adapter's `input=` keyword; **kwargs would hide the callable contract and a wrapper would only rename this one test seam.
        timeout: float | None = None,
    ) -> tuple[str, str]:
        del input, timeout
        raise self.communication_error

    def poll(self) -> int | None:
        return self.returncode


@dataclass
class _SingleProcessLauncher:
    process: PopenProcess

    def start(self, request: CapturedProcessRequest) -> PopenProcess:
        assert list(request.argv) == ["docker", "version"]
        assert request.stdin == "request"
        return self.process


@given(
    timeout=st.floats(min_value=0, max_value=600, allow_nan=False, allow_infinity=False),
    stdout=st.text(max_size=40),
    stderr=st.text(max_size=40),
)
def test_timeout_kills_settles_and_closes_the_owned_process_group(
    timeout: float,
    stdout: str,
    stderr: str,
) -> None:
    processes = FakeCapturedProcessRunner(
        StoppableScript(stdout=stdout, stderr=stderr, runs_for=None, term_delay=None),
        threads=SimThreads(),
    )
    docker = SubprocessDockerCli(processes=processes)

    with pytest.raises(ProcessTimeoutError) as raised:
        docker.run(["docker", "info"], timeout_seconds=timeout)

    process = processes.processes[0]
    assert raised.value.cmd == ["docker", "info"]
    assert raised.value.timeout == timeout
    assert raised.value.output == (stdout.encode() or None)
    assert raised.value.stderr == (stderr.encode() or None)
    assert [number.name for number, _at in process.signals] == ["SIGKILL"]
    assert process.closed
    assert processes.calls[0].start_new_session


@given(
    returncode=st.integers(min_value=-20, max_value=255),
    stdout=st.text(max_size=40),
    stderr=st.text(max_size=40),
)
def test_normal_completion_preserves_raw_status_complete_streams_and_arguments(
    returncode: int,
    stdout: str,
    stderr: str,
) -> None:
    processes = FakeCapturedProcessRunner(
        StoppableScript(stdout=stdout, stderr=stderr, returncode=returncode),
        threads=SimThreads(),
    )

    result = SubprocessDockerCli(processes=processes).run(["docker", "version"], timeout_seconds=30)

    assert result.args == ["docker", "version"]
    assert result.returncode == returncode
    assert result.stdout == stdout
    assert result.stderr == stderr
    assert processes.processes[0].signals == []
    assert processes.processes[0].closed


def test_interruption_kills_and_settles_an_unreaped_process_group() -> None:
    interruption = KeyboardInterrupt("interrupted wait")
    processes = FakeCapturedProcessRunner(
        StoppableScript(runs_for=None),
        threads=SimThreads(),
        faults=CapturedProcessFaults(initial_wait_failure=interruption),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        SubprocessDockerCli(processes=processes).run(["docker", "version"], timeout_seconds=30)

    process = processes.processes[0]
    assert raised.value is interruption
    assert [number.name for number, _at in process.signals] == ["SIGKILL"]
    assert process.close_attempted
    assert process.closed


def test_failure_after_leader_reap_never_signals_a_stale_process_group() -> None:
    decode_failure = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    processes = FakeCapturedProcessRunner(
        threads=SimThreads(),
        faults=CapturedProcessFaults(
            initial_wait_failure=decode_failure,
            initial_wait_reaps_leader=True,
        ),
    )

    with pytest.raises(UnicodeDecodeError) as raised:
        SubprocessDockerCli(processes=processes).run(["docker", "version"], timeout_seconds=30)

    process = processes.processes[0]
    assert raised.value is decode_failure
    assert process.signals == []
    assert process.close_attempted
    assert process.closed


def test_interruption_preserves_ordered_group_and_pipe_cleanup_failures() -> None:
    interruption = KeyboardInterrupt("interrupted wait")
    processes = FakeCapturedProcessRunner(
        StoppableScript(runs_for=None),
        threads=SimThreads(),
        faults=CapturedProcessFaults(
            initial_wait_failure=interruption,
            signal_failure=OSError("signal failed"),
            stdout_close_failure=OSError("stdout close failed"),
            stderr_close_failure=OSError("stderr close failed"),
        ),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        SubprocessDockerCli(processes=processes).run(["docker", "version"], timeout_seconds=30)

    process = processes.processes[0]
    assert raised.value is interruption
    assert [number.name for number, _at in process.signals] == ["SIGKILL"]
    assert process.close_attempts == ["stdout", "stderr"]
    assert raised.value.__notes__ == [
        "process group cleanup also failed: signal failed",
        "process pipe cleanup also failed: stdout close failed; stderr close failed",
    ]


def test_timeout_preserves_its_contract_when_the_group_does_not_settle() -> None:
    processes = FakeCapturedProcessRunner(
        StoppableScript(stdout="partial", runs_for=None),
        threads=SimThreads(),
        faults=CapturedProcessFaults(kill_settles=False),
    )

    with pytest.raises(ProcessTimeoutError) as raised:
        SubprocessDockerCli(processes=processes).run(["docker", "version"], timeout_seconds=3)

    process = processes.processes[0]
    assert raised.value.output == b"partial"
    assert raised.value.__notes__ == ["process group did not settle within 5 seconds"]
    assert [number.name for number, _at in process.signals] == ["SIGKILL"]
    assert process.close_attempted
    assert process.closed


@pytest.mark.parametrize(
    ("faults", "note"),
    [
        pytest.param(
            CapturedProcessFaults(signal_failure=OSError("signal failed")),
            "process group cleanup also failed: signal failed",
            id="signal",
        ),
        pytest.param(
            CapturedProcessFaults(settle_wait_failure=RuntimeError("wait failed")),
            "process group cleanup also failed: wait failed",
            id="settle-wait",
        ),
    ],
)
def test_timeout_preserves_its_contract_when_group_cleanup_fails(
    faults: CapturedProcessFaults,
    note: str,
) -> None:
    processes = FakeCapturedProcessRunner(
        StoppableScript(runs_for=None),
        threads=SimThreads(),
        faults=faults,
    )

    with pytest.raises(ProcessTimeoutError) as raised:
        SubprocessDockerCli(processes=processes).run(["docker", "version"], timeout_seconds=3)

    process = processes.processes[0]
    assert raised.value.__notes__ == [note]
    assert [number.name for number, _at in process.signals] == ["SIGKILL"]
    assert process.close_attempted
    assert process.closed


def test_timeout_preserves_its_contract_when_pipe_closure_fails() -> None:
    processes = FakeCapturedProcessRunner(
        StoppableScript(runs_for=None),
        threads=SimThreads(),
        faults=CapturedProcessFaults(stdout_close_failure=OSError("close failed")),
    )

    with pytest.raises(ProcessTimeoutError) as raised:
        SubprocessDockerCli(processes=processes).run(["docker", "version"], timeout_seconds=3)

    process = processes.processes[0]
    assert raised.value.__notes__ == ["process pipe cleanup also failed: close failed"]
    assert process.close_attempted
    assert not process.closed


@pytest.mark.parametrize("adapter", ["fake", "subprocess"])
def test_fake_and_real_adapter_attempt_every_pipe_and_report_the_same_close_failure(
    adapter: str,
) -> None:
    attempts: list[str] = []
    interruption = KeyboardInterrupt("cancelled")
    request = CapturedProcessRequest(
        argv=("docker", "version"),
        stdin="request",
        timeout=30,
    )
    fake_processes: FakeCapturedProcessRunner | None = None
    if adapter == "fake":
        fake_processes = FakeCapturedProcessRunner(
            threads=SimThreads(),
            faults=CapturedProcessFaults(
                initial_wait_failure=interruption,
                initial_wait_reaps_leader=True,
                stdin_close_failure=OSError("stdin close failed"),
                stdout_close_failure=OSError("stdout close failed"),
                stderr_close_failure=OSError("stderr close failed"),
            ),
        )
        subject = fake_processes
    else:
        popen = _ScriptedTextPopen(
            stdin=_FailingCloseStream("stdin", attempts),
            stdout=_FailingCloseStream("stdout", attempts),
            stderr=_FailingCloseStream("stderr", attempts),
            communication_error=interruption,
        )
        process = PopenProcess(cast("subprocess.Popen[str]", popen))
        subject = OwnedProcessRunner(processes=_SingleProcessLauncher(process))

    with pytest.raises(KeyboardInterrupt) as raised:
        subject.run(request)

    if fake_processes is not None:
        attempts = fake_processes.processes[0].close_attempts
    assert attempts == ["stdin", "stdout", "stderr"]
    assert raised.value is interruption
    assert raised.value.__notes__ == [
        "process pipe cleanup also failed: "
        "stdin close failed; stdout close failed; stderr close failed"
    ]
