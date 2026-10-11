from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import ProcessTimeoutError, SubprocessCapturedTextProcessLauncher
from vs_sim.api.testing import FakeCapturedTextProcessLauncher

# test-isolation: this test pins the internal timeout-cleanup ordering whose unreaped-leader invariant prevents PGID reuse.
from vs_slurm.owned_process import OwnedProcessRunner

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


@dataclass
class _ScriptedCapturedProcess:
    events: list[object]
    output: tuple[str, str] | None = None
    returncode: int = 0
    reap_times_out: bool = False
    communication_error: BaseException | None = None
    kill_error: BaseException | None = None
    wait_error: BaseException | None = None
    close_failures: tuple[tuple[str, BaseException], ...] = ()
    close_attempts: list[str] | None = None
    command: tuple[str, ...] = ("transport",)

    def communicate(self, input_text: str | None, timeout: float) -> tuple[str, str]:
        self.events.append(("communicate", input_text, timeout))
        if self.communication_error is not None:
            raise self.communication_error
        if self.output is not None:
            return self.output
        raise ProcessTimeoutError(
            self.command,
            timeout,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    def kill_group(self) -> None:
        self.events.append("kill_group")
        if self.kill_error is not None:
            raise self.kill_error

    def wait(self, timeout: float) -> int:
        self.events.append(("wait", timeout))
        if self.wait_error is not None:
            raise self.wait_error
        if self.reap_times_out:
            raise ProcessTimeoutError(self.command, timeout)
        return self.returncode

    def close(self) -> None:
        self.events.append("close")
        failures: list[BaseException] = []
        for stream, failure in self.close_failures:
            if self.close_attempts is not None:
                self.close_attempts.append(stream)
            failures.append(failure)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            message = "captured process pipes could not be closed"
            raise BaseExceptionGroup(message, failures)


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
    pid: int = 1
    returncode: int | None = 0

    def communicate(
        self, input_text: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        del input_text, timeout
        raise self.communication_error

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0


def _launcher(
    process: _ScriptedCapturedProcess,
    *,
    pipe_stdin: bool = True,
) -> FakeCapturedTextProcessLauncher:
    expected_pipe_stdin = pipe_stdin

    def make(
        argv: tuple[str, ...],
        *,
        pipe_stdin: bool,
        cwd: Path | None,
        env: Mapping[str, str] | None,
    ) -> _ScriptedCapturedProcess:
        assert (argv, pipe_stdin, cwd, env) == (
            process.command,
            expected_pipe_stdin,
            None,
            None,
        )
        return process

    return FakeCapturedTextProcessLauncher(make)


@given(
    cleanup_timeout=st.floats(
        min_value=0,
        max_value=300,
        allow_nan=False,
        allow_infinity=False,
    ),
    transport_timeout=st.floats(
        min_value=0,
        max_value=300,
        allow_nan=False,
        allow_infinity=False,
    ),
    stdin=st.one_of(st.none(), st.text(max_size=40)),
)
def test_every_timeout_uses_the_fixed_cleanup_budget_after_group_kill(
    cleanup_timeout: float,
    transport_timeout: float,
    stdin: str | None,
) -> None:
    events: list[object] = []
    process = _ScriptedCapturedProcess(events)
    runner = OwnedProcessRunner(
        _launcher(process, pipe_stdin=stdin is not None),
        cleanup_timeout=cleanup_timeout,
    )

    with pytest.raises(ProcessTimeoutError):
        runner.run(process.command, stdin=stdin, timeout=transport_timeout)

    assert events == [
        ("communicate", stdin, transport_timeout),
        "kill_group",
        ("wait", cleanup_timeout),
        "close",
    ]


def test_timeout_signals_the_unreaped_group_before_bounded_wait_and_pipe_close() -> None:
    events: list[object] = []
    process = _ScriptedCapturedProcess(events)
    runner = OwnedProcessRunner(_launcher(process), cleanup_timeout=3.0)

    with pytest.raises(ProcessTimeoutError) as raised:
        runner.run(process.command, stdin="request", timeout=7.0)

    assert raised.value.output == b"partial stdout"
    assert raised.value.stderr == b"partial stderr"
    assert events == [
        ("communicate", "request", 7.0),
        "kill_group",
        ("wait", 3.0),
        "close",
    ]


def test_cleanup_timeout_does_not_replace_the_transport_timeout() -> None:
    events: list[object] = []
    process = _ScriptedCapturedProcess(events, reap_times_out=True)
    runner = OwnedProcessRunner(_launcher(process), cleanup_timeout=2.0)

    with pytest.raises(ProcessTimeoutError) as raised:
        runner.run(process.command, stdin="request", timeout=5.0)

    assert events[-2:] == [("wait", 2.0), "close"]
    assert raised.value.__notes__ == [
        "process-group reap also failed: Command '('transport',)' timed out after 2.0 seconds"
    ]


def test_every_cleanup_failure_preserves_an_interruption_and_remaining_attempts() -> None:
    events: list[object] = []
    interruption = KeyboardInterrupt("cancelled")
    process = _ScriptedCapturedProcess(
        events,
        communication_error=interruption,
        kill_error=RuntimeError("kill failed"),
        wait_error=ValueError("wait failed"),
        close_failures=(("stdin", OSError("close failed")),),
    )
    runner = OwnedProcessRunner(_launcher(process), cleanup_timeout=4.0)

    with pytest.raises(KeyboardInterrupt) as raised:
        runner.run(process.command, stdin="request", timeout=6.0)

    assert raised.value is interruption
    assert events == [
        ("communicate", "request", 6.0),
        "kill_group",
        ("wait", 4.0),
        "close",
    ]
    assert raised.value.__notes__ == [
        "process-group cleanup also failed: kill failed",
        "process-group reap also failed: wait failed",
        "process pipe cleanup also failed: close failed",
    ]


@pytest.mark.parametrize("adapter", ["fake", "subprocess"])
def test_fake_and_real_adapter_attempt_every_pipe_and_report_the_same_close_failure(
    adapter: str,
) -> None:
    attempts: list[str] = []
    interruption = KeyboardInterrupt("cancelled")
    failures = tuple(
        (name, OSError(f"{name} close failed")) for name in ("stdin", "stdout", "stderr")
    )
    if adapter == "fake":
        process = _ScriptedCapturedProcess(
            [],
            communication_error=interruption,
            close_failures=failures,
            close_attempts=attempts,
        )
        launcher = _launcher(process)
    else:
        popen = _ScriptedTextPopen(
            stdin=_FailingCloseStream("stdin", attempts),
            stdout=_FailingCloseStream("stdout", attempts),
            stderr=_FailingCloseStream("stderr", attempts),
            communication_error=interruption,
        )

        def make_process(
            argv: tuple[str, ...],
            *,
            pipe_stdin: bool,
            cwd: Path | None,
            env: Mapping[str, str] | None,
        ) -> _ScriptedTextPopen:
            assert (argv, pipe_stdin, cwd, env) == (("transport",), True, None, None)
            return popen

        launcher = SubprocessCapturedTextProcessLauncher(make_process)

    runner = OwnedProcessRunner(launcher)

    with pytest.raises(KeyboardInterrupt) as raised:
        runner.run(("transport",), stdin="request", timeout=6.0)

    assert attempts == ["stdin", "stdout", "stderr"]
    assert raised.value is interruption
    assert raised.value.__notes__ == [
        "process pipe cleanup also failed: "
        "stdin close failed; stdout close failed; stderr close failed"
    ]


def test_normal_completion_preserves_status_utf8_streams_and_pipe_cleanup() -> None:
    events: list[object] = []
    process = _ScriptedCapturedProcess(
        events,
        output=("snowman ☃\n", "diagnostic λ\n"),
        returncode=23,
    )
    runner = OwnedProcessRunner(_launcher(process))

    result = runner.run(process.command, stdin="request", timeout=11.0)

    assert (result.argv, result.returncode, result.stdout, result.stderr) == (
        process.command,
        23,
        "snowman ☃\n",
        "diagnostic λ\n",
    )
    assert events == [("communicate", "request", 11.0), "close"]
