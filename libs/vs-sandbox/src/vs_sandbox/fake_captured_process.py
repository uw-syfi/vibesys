"""A captured process-group runner on simulated time for public boundary tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from vs_sandbox.fake_stoppable_process import FakeStoppableProcess, StoppableScript
from vs_sandbox.process_execution import (
    DEFAULT_PROCESS_SETTLE_SECONDS,
    KILL,
    CapturedProcessRequest,
    CapturedProcessResult,
    OwnedProcessRunner,
    StopSignal,
    raise_pipe_close_failures,
)
from vs_sim.api import Threads

if TYPE_CHECKING:
    from collections.abc import Callable


class _DrivenThreads(Threads, Protocol):
    """Thread effects whose deterministic test driver can run one main call."""

    def run[T](self, main: Callable[[], T]) -> T:
        """Drive *main* and its simulated waits to completion."""
        ...


@dataclass(frozen=True, slots=True)
class CapturedProcessFaults:
    """Deterministic failures a captured-process Fake can expose."""

    initial_wait_failure: BaseException | None = None
    initial_wait_reaps_leader: bool = False
    signal_failure: BaseException | None = None
    settle_wait_failure: BaseException | None = None
    stdin_close_failure: BaseException | None = None
    stdout_close_failure: BaseException | None = None
    stderr_close_failure: BaseException | None = None
    kill_settles: bool = True


class FakeCapturedProcess:
    """A captured process with observable cleanup and deterministic failures."""

    def __init__(
        self,
        threads: Threads,
        script: StoppableScript,
        faults: CapturedProcessFaults,
        *,
        pipe_stdin: bool,
    ) -> None:
        """Wrap a simulated stoppable process with captured-process effects."""
        self._threads = threads
        self._script = script
        self._faults = faults
        self._pipe_stdin = pipe_stdin
        self._process = FakeStoppableProcess(threads, script)
        self._wait_calls = 0
        self._leader_reaped = False
        self.signals: list[tuple[StopSignal, float]] = []
        self.close_attempted = False
        self.close_attempts: list[str] = []
        self.closed = False

    @property
    def returncode(self) -> int:
        """Return the simulated leader's status."""
        return self._process.returncode

    @property
    def timeout_output(self) -> tuple[bytes | None, bytes | None]:
        """Return subprocess-compatible raw partial streams."""
        stdout = self._script.stdout.encode() or None
        stderr = self._script.stderr.encode() or None
        return stdout, stderr

    def wait(self, timeout: float | None) -> tuple[str, str] | None:
        """Apply the configured wait effect, then wait on simulated time."""
        self._wait_calls += 1
        if self._wait_calls == 1 and self._faults.initial_wait_failure is not None:
            self._leader_reaped = self._faults.initial_wait_reaps_leader
            raise self._faults.initial_wait_failure
        if self._wait_calls > 1 and self._faults.settle_wait_failure is not None:
            raise self._faults.settle_wait_failure
        result = self._process.wait(timeout)
        if result is not None:
            self._leader_reaped = True
        return result

    def signal_group(self, signal_number: StopSignal) -> None:
        """Record a group signal, fail it, ignore kill, or apply it."""
        self.signals.append((signal_number, self._threads.now()))
        if self._faults.signal_failure is not None:
            raise self._faults.signal_failure
        if signal_number is KILL and not self._faults.kill_settles:
            return
        self._process.signal_group(signal_number)

    def signal_group_if_unreaped(self, signal_number: StopSignal) -> bool:
        """Signal only while the simulated leader identity remains reserved."""
        if self._leader_reaped:
            return False
        self.signal_group(signal_number)
        return True

    def close(self) -> None:
        """Attempt every simulated pipe close and report failures in stream order."""
        self.close_attempted = True
        failures: list[BaseException] = []
        streams = [
            ("stdout", self._faults.stdout_close_failure),
            ("stderr", self._faults.stderr_close_failure),
        ]
        if self._pipe_stdin:
            streams.insert(0, ("stdin", self._faults.stdin_close_failure))
        for name, failure in streams:
            self.close_attempts.append(name)
            if failure is not None:
                failures.append(failure)
        self.closed = not failures
        raise_pipe_close_failures(failures)


@dataclass
class _FakeCapturedProcessLauncher:
    """Give the real ownership policy one already-scripted process."""

    process: FakeCapturedProcess
    requests: list[CapturedProcessRequest] = field(default_factory=list)

    def start(self, request: CapturedProcessRequest) -> FakeCapturedProcess:
        self.requests.append(request)
        return self.process


class FakeCapturedProcessRunner:
    """Run each captured command against a scripted process on virtual time.

    The Fake records requests and created processes. Its wait,
    timeout, group signal, reap, and close behavior is the production
    :class:`OwnedProcessRunner` policy applied to
    :class:`FakeStoppableProcess`, rather than canned timeout results.
    """

    def __init__(
        self,
        script: StoppableScript | None = None,
        *,
        threads: _DrivenThreads,
        settle_seconds: float = DEFAULT_PROCESS_SETTLE_SECONDS,
        faults: CapturedProcessFaults | None = None,
    ) -> None:
        """Use *script* for every call on one deterministic simulated clock."""
        self._script = script or StoppableScript()
        self._threads = threads
        self._settle_seconds = settle_seconds
        self._faults = faults or CapturedProcessFaults()
        self.calls: list[CapturedProcessRequest] = []
        self.processes: list[FakeCapturedProcess] = []

    def run(self, request: CapturedProcessRequest) -> CapturedProcessResult:
        """Execute one request using the scripted process and virtual clock."""
        process = FakeCapturedProcess(
            self._threads,
            self._script,
            self._faults,
            pipe_stdin=request.stdin is not None,
        )
        launcher = _FakeCapturedProcessLauncher(process)
        runner = OwnedProcessRunner(
            processes=launcher,
            threads=self._threads,
            settle_seconds=self._settle_seconds,
        )

        def execute() -> CapturedProcessResult:
            return runner.run(request)

        self.calls.append(request)
        self.processes.append(process)
        return self._threads.run(execute)


__all__ = ["CapturedProcessFaults", "FakeCapturedProcess", "FakeCapturedProcessRunner"]
