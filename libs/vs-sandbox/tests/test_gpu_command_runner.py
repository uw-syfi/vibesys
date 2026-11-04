"""The trusted GPU command runner shares agent-GPU policy and keeps tighter deadlines."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

import pytest
from tests.support.command_runner_contract import assert_preset_cancellation_contract

from vs_sandbox.api import (
    CaptureExecutionIdentity,
    CaptureExecutionKind,
    CommandExecutionSettledError,
    CommandStopUnconfirmedError,
)
from vs_sandbox.api.slurm import (
    AgentGpuConfig,
    GpuCaptureRecovery,
    GpuCommand,
    GpuCommandRunner,
    GpuCommands,
    GpuJobOutcome,
    gpu_capture_execution_identity,
)
from vs_sim.api import Capture, OutputStream
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vs_sandbox.api import CommandResult
    from vs_sandbox.api.slurm import GpuJobRequest
    from vs_sim.api import Event, Threads, Worker


class _SetFailureEvent:
    def __init__(self, delegate: Event) -> None:
        self._delegate = delegate

    def is_set(self) -> bool:
        return self._delegate.is_set()

    def set(self) -> None:
        raise _DeadlineSignalError

    def clear(self) -> None:
        self._delegate.clear()

    def wait(self, timeout: float | None = None) -> bool:
        return self._delegate.wait(timeout)


class _JoinFailureWorker:
    def __init__(self, delegate: Worker) -> None:
        self._delegate = delegate

    @property
    def name(self) -> str:
        return self._delegate.name

    def is_alive(self) -> bool:
        return self._delegate.is_alive()

    def join(self, timeout: float | None = None) -> None:
        del timeout
        raise _DeadlineJoinError


class _DeadlineSignalError(OSError):
    """Injected failure to signal deadline-worker completion."""


class _DeadlineJoinError(OSError):
    """Injected failure to join the deadline worker."""


class _DeadlineCleanupFaultThreads:
    """Delegate the simulator and fault one deadline-cleanup boundary."""

    def __init__(self, delegate: SimThreads, fault: str) -> None:
        self._delegate = delegate
        self._fault = fault
        self._events = 0

    def event(self) -> Event:
        self._events += 1
        event = self._delegate.event()
        return _SetFailureEvent(event) if self._fault == "set" and self._events == 1 else event

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        worker = self._delegate.spawn(target, name=name, daemon=daemon)
        if self._fault == "join" and name == "vibesys-gpu-command-deadline":
            return _JoinFailureWorker(worker)
        return worker

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _Confinement:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def wrap(self, workspace: Path, argv: Sequence[str]) -> list[str]:
        self.calls.append((workspace, tuple(argv)))
        return ["confined", str(workspace), *argv]


class _Launcher:
    def __init__(
        self,
        threads: Threads,
        *,
        hold: bool = False,
        termination_confirmed: bool = True,
        script: tuple[bytes, ...] | BaseException = (b"job output\n",),
        settled_failure: BaseException | None = None,
    ) -> None:
        self._hold = hold
        self._threads = threads
        self._termination_confirmed = termination_confirmed
        self._script = script
        self._settled_failure = settled_failure
        self.requests: list[GpuJobRequest] = []
        self.commands: list[GpuCommand] = []
        self.cancelled = threads.event()
        self.reconciled: list[str] = []
        self.reconcile_result = True

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> GpuJobOutcome:
        self.requests.append(request)
        self.commands.append(command)
        chunks = (b"job output\n",) if isinstance(self._script, BaseException) else self._script
        for chunk in chunks:
            write(chunk)
        if isinstance(self._script, BaseException):
            raise self._script
        if self._settled_failure is not None:
            return GpuJobOutcome(
                1,
                self._termination_confirmed,
                self._settled_failure,
                command.job_name,
            )
        if self._hold:
            while not cancel.wait(1.0):
                pass
            self.cancelled.set()
            return GpuJobOutcome(143, self._termination_confirmed, job_name=command.job_name)
        return GpuJobOutcome(0, self._termination_confirmed, job_name=command.job_name)

    def reconcile(self, job_name: str) -> bool:
        self.reconciled.append(job_name)
        return self.reconcile_result


class _FailingLauncher(_Launcher):
    """Return a command failure after streaming the configured merged output."""

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> GpuJobOutcome:
        outcome = super().run(request, command, write=write, cancel=cancel)
        return GpuJobOutcome(
            7,
            outcome.termination_confirmed,
            outcome.failure,
            outcome.job_name,
        )


def _runner(
    tmp_path: Path,
    threads: Threads,
    *,
    launcher: _Launcher,
    confinement: _Confinement,
    max_output_characters: int = 100_000,
) -> GpuCommandRunner:
    config = AgentGpuConfig(
        partitions=("main",),
        max_gpus=4,
        max_time_minutes=90,
        default_time_minutes=30,
        job_name_prefix="vibesys-test-run",
    )
    commands = GpuCommands(
        config,
        confinement,
        {"PATH": "/operator/bin", "TOKEN": "secret"},
        launcher=launcher,
    )
    return GpuCommandRunner(
        commands,
        tmp_path,
        threads=threads,
        max_output_characters=max_output_characters,
    )


def test_capture_uses_the_shared_confinement_environment_and_launcher(tmp_path: Path) -> None:
    threads = SimThreads()
    launcher = _Launcher(threads)
    confinement = _Confinement()
    runner = _runner(tmp_path, threads, launcher=launcher, confinement=confinement)
    output = Capture(1000)

    result = threads.run(lambda: runner.execute("profile --out result", timeout=61, output=output))

    assert (result.exit_code, result.output) == (0, "job output\n")
    assert (result.stdout, result.stderr) == ("job output\n", "")
    assert output.kept(OutputStream.STDOUT) == b"job output\n"
    assert launcher.requests[0].model_dump() == {"gpus": 1, "time_minutes": 2}
    command = launcher.commands[0]
    assert command.job_name is not None
    assert re.fullmatch(r"vibesys-test-run-capture-[0-9a-f]{8}", command.job_name)
    assert len(command.job_name) <= 49
    assert command.cwd == tmp_path.resolve()
    assert command.env == {"PATH": "/operator/bin"}
    assert command.job_name == runner.id
    root, wrapped = confinement.calls[0]
    assert root == tmp_path.resolve()
    assert wrapped[-3:] == ("/bin/sh", "-c", "profile --out result")


def test_reconcile_uses_the_durable_run_scoped_job_identity(tmp_path: Path) -> None:
    threads = SimThreads()
    launcher = _Launcher(threads)
    runner = _runner(tmp_path, threads, launcher=launcher, confinement=_Confinement())

    execution = CaptureExecutionIdentity(
        kind=CaptureExecutionKind.SLURM_GPU,
        execution_id=runner.id,
    )
    assert runner.reconcile(execution)
    assert launcher.reconciled == [runner.id]
    assert not runner.reconcile(
        CaptureExecutionIdentity(
            kind=CaptureExecutionKind.SLURM_GPU,
            execution_id="another-runs-job",
        )
    )


def test_run_scoped_recovery_validates_the_exact_candidate_path_before_scheduler_io(
    tmp_path: Path,
) -> None:
    threads = SimThreads()
    launcher = _Launcher(threads)
    config = AgentGpuConfig(
        partitions=("main",),
        max_gpus=4,
        max_time_minutes=90,
        job_name_prefix="vibesys-test-run",
    )
    recovery = GpuCaptureRecovery(config, launcher)
    candidate = tmp_path / "candidate"
    expected = gpu_capture_execution_identity(config, candidate)

    assert recovery.reconcile(candidate, expected)
    assert launcher.reconciled == [expected.execution_id]
    assert not recovery.reconcile(tmp_path / "other-candidate", expected)
    assert launcher.reconciled == [expected.execution_id]


def test_exact_seconds_timeout_cancels_a_rounded_up_slurm_allocation(tmp_path: Path) -> None:
    threads = SimThreads()
    launcher = _Launcher(threads, hold=True)
    runner = _runner(tmp_path, threads, launcher=launcher, confinement=_Confinement())
    output = Capture(1000)

    result = threads.run(lambda: runner.execute("profile", timeout=61, output=output))

    assert launcher.requests[0].time_minutes == 2
    assert launcher.cancelled.is_set()
    assert result.exit_code == 124
    assert "timed out after 61 seconds" in result.stderr
    assert output.kept(OutputStream.STDOUT) == b"job output\n"
    assert output.kept(OutputStream.STDERR) == b""


def test_caller_cancellation_stops_the_gpu_job_and_settles_the_runner(tmp_path: Path) -> None:
    threads = SimThreads()
    launcher = _Launcher(threads, hold=True)
    runner = _runner(tmp_path, threads, launcher=launcher, confinement=_Confinement())
    cancel = threads.event()
    output = Capture(1000)

    def program() -> CommandResult:
        threads.spawn(lambda: (threads.sleep(3), cancel.set()), name="cancel-capture")
        return runner.execute("profile", timeout=600, cancel=cancel, output=output)

    result = threads.run(program)

    assert launcher.cancelled.is_set()
    assert result.cancelled
    assert result.exit_code == 143
    assert "was cancelled" in result.stderr
    assert output.kept(OutputStream.STDERR) == b""


def test_a_preset_cancel_never_authorizes_or_submits_a_gpu_job(tmp_path: Path) -> None:
    threads = SimThreads()
    launcher = _Launcher(threads)
    confinement = _Confinement()
    runner = _runner(tmp_path, threads, launcher=launcher, confinement=confinement)
    result = threads.run(lambda: assert_preset_cancellation_contract(runner, threads, "profile"))

    assert result.cancelled
    assert result.exit_code == 143
    assert launcher.requests == []
    assert confinement.calls == []


def test_gpu_output_is_bounded_without_hiding_streamed_output(tmp_path: Path) -> None:
    threads = SimThreads()
    launcher = _Launcher(threads)
    runner = _runner(
        tmp_path,
        threads,
        launcher=launcher,
        confinement=_Confinement(),
        max_output_characters=8,
    )
    output = Capture(1000)

    result = threads.run(lambda: runner.execute("profile", output=output))

    assert result.truncated
    assert len(result.output) <= 8
    assert output.kept(OutputStream.STDOUT) == b"job output\n"


def test_failed_gpu_output_keeps_the_merged_prefix_and_rolling_tail(tmp_path: Path) -> None:
    threads = SimThreads()
    raw = b"BEGIN-" + b"x" * 100 + b"-TAIL"
    launcher = _FailingLauncher(threads, script=(raw[:60], raw[60:]))
    runner = _runner(
        tmp_path,
        threads,
        launcher=launcher,
        confinement=_Confinement(),
        max_output_characters=40,
    )
    output = Capture(1000)

    result = threads.run(lambda: runner.execute("profile", output=output))

    assert result.truncated
    assert result.stdout.startswith("BEGIN-")
    assert result.stderr.endswith("-TAIL")
    assert result.output == result.stdout + result.stderr
    assert output.kept(OutputStream.STDOUT) == raw


def test_gpu_diagnostics_redact_a_long_secret_before_prefix_tail_retention(
    tmp_path: Path,
) -> None:
    threads = SimThreads()
    secret = b"s" * 240
    raw = b"BEGIN-" + b"x" * 80 + b" API_TOKEN=" + secret + b" END\n"
    launcher = _FailingLauncher(
        threads,
        script=(raw[:90], raw[90:100], raw[100:250], raw[250:]),
    )
    runner = _runner(
        tmp_path,
        threads,
        launcher=launcher,
        confinement=_Confinement(),
        max_output_characters=120,
    )
    output = Capture(1_000)

    result = threads.run(lambda: runner.execute("profile", output=output))

    assert result.truncated
    assert "s" * 20 in result.output
    assert "API_TOKEN=[REDACTED]" in result.diagnostic_output
    assert "s" * 20 not in result.diagnostic_output
    assert output.kept(OutputStream.STDOUT) == raw


def test_an_untyped_post_submission_failure_is_unconfirmed_and_bounded(
    tmp_path: Path,
) -> None:
    threads = SimThreads()
    launcher = _Launcher(threads, script=OSError("srun unavailable"))
    runner = _runner(tmp_path, threads, launcher=launcher, confinement=_Confinement())
    output = Capture(1000)

    with pytest.raises(CommandStopUnconfirmedError) as raised:
        threads.run(lambda: runner.execute("profile", timeout=30, output=output))

    result = raised.value.result
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("job output\n")
    assert "OSError" in result.stderr
    assert "srun unavailable" in result.stderr
    assert output.kept(OutputStream.STDOUT) == b"job output\n"
    assert output.kept(OutputStream.STDERR) == b""


def test_a_base_exception_after_submission_is_still_an_unconfirmed_stop(tmp_path: Path) -> None:
    threads = SimThreads()
    launcher = _Launcher(threads, script=KeyboardInterrupt("launcher interrupted"))
    runner = _runner(tmp_path, threads, launcher=launcher, confinement=_Confinement())

    with pytest.raises(CommandStopUnconfirmedError) as raised:
        threads.run(lambda: runner.execute("profile", timeout=30, output=Capture(1000)))

    assert "KeyboardInterrupt" in raised.value.result.stderr


@pytest.mark.parametrize("fault", ["set", "join"])
def test_deadline_cleanup_faults_preserve_an_unconfirmed_scheduler_outcome(
    tmp_path: Path, fault: str
) -> None:
    """Neither ``finished.set`` nor timer ``join`` may replace stop ownership."""
    simulator = SimThreads()
    faulting = cast("Threads", _DeadlineCleanupFaultThreads(simulator, fault))
    launcher = _Launcher(simulator, termination_confirmed=False)
    runner = _runner(tmp_path, faulting, launcher=launcher, confinement=_Confinement())

    with pytest.raises(CommandStopUnconfirmedError) as raised:
        simulator.run(lambda: runner.execute("profile", timeout=30, output=Capture(1000)))

    cause = raised.value.__cause__
    expected = _DeadlineSignalError if fault == "set" else _DeadlineJoinError
    assert isinstance(cause, expected)


@pytest.mark.parametrize("fault", ["set", "join"])
def test_deadline_cleanup_fault_after_confirmed_outcome_is_typed_as_settled(
    tmp_path: Path, fault: str
) -> None:
    simulator = SimThreads()
    faulting = cast("Threads", _DeadlineCleanupFaultThreads(simulator, fault))
    launcher = _Launcher(simulator, termination_confirmed=True)
    runner = _runner(tmp_path, faulting, launcher=launcher, confinement=_Confinement())

    with pytest.raises(CommandExecutionSettledError) as raised:
        simulator.run(lambda: runner.execute("profile", timeout=30, output=Capture(1_000)))

    expected = _DeadlineSignalError if fault == "set" else _DeadlineJoinError
    assert isinstance(raised.value.cause, expected)


def test_a_confirmed_launcher_failure_is_a_settled_bounded_result(tmp_path: Path) -> None:
    threads = SimThreads()
    launcher = _Launcher(threads, settled_failure=OSError("srun relay failed"))
    runner = _runner(tmp_path, threads, launcher=launcher, confinement=_Confinement())
    result = threads.run(lambda: runner.execute("profile", timeout=30, output=Capture(1000)))

    assert result.exit_code == 1
    assert "srun relay failed" in result.stderr


def test_an_unconfirmed_gpu_stop_raises_with_the_bounded_result(tmp_path: Path) -> None:
    threads = SimThreads()
    launcher = _Launcher(threads, hold=True, termination_confirmed=False)
    runner = _runner(tmp_path, threads, launcher=launcher, confinement=_Confinement())

    with pytest.raises(CommandStopUnconfirmedError) as raised:
        threads.run(lambda: runner.execute("profile", timeout=3, output=Capture(1000)))

    assert raised.value.result.exit_code == 124
    assert "timed out after 3 seconds" in raised.value.result.stderr
