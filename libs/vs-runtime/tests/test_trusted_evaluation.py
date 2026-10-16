"""Public composition contract for trusted workspace evaluation."""

from __future__ import annotations

import asyncio
import os
import sys
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError

from vs_project.api import Project
from vs_runtime.api import BenchmarkFailureKind
from vs_runtime.api.infrastructure import (
    ModelRequestReconciler,
    ProtocolBenchmarkContract,
    ScalarBenchmarkContract,
    TrustedAccuracyResult,
    TrustedBenchmarkResult,
    TrustedBuild,
    TrustedEvaluationExecutor,
    TrustedEvaluationPlan,
    create_model_request_reconciler,
    create_trusted_evaluation_executor,
)
from vs_runtime.api.testing import FakeModelVolumeProvisioner
from vs_sandbox.api import CommandResult, LocalShellRunner
from vs_sandbox.api.testing import FakeCommandRunner
from vs_sim.api import DISCARD, Capture, OsThreads, SubprocessPipedProcessLauncher
from vs_sim.api.testing import (
    InlineBlockingRunner,
    Sim,
    SimBlockingRunner,
    SimThreads,
    wait_or_fail,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import GitTracker
    from vs_sandbox.api import CommandRunner
    from vs_sim.api import Event, OutputSink


_MARKER = "__VIBESYS_FRAMEWORK_BENCHMARK_JSON__"
_END_MARKER = "__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__"


class _Git:
    def __init__(self, *changes: list[str]) -> None:
        self._changes = deque(changes or ([],))

    def trusted_input_changes(self) -> list[str]:
        if len(self._changes) > 1:
            return self._changes.popleft()
        return self._changes[0]


class _FailedProcessRunner:
    """Run every command through the production-shaped Fake process-result boundary."""

    def __init__(self, stderr: str, *, limit: int) -> None:
        self._stderr = stderr
        self._runner = FakeCommandRunner(max_output_chars=limit)

    @property
    def id(self) -> str:
        return self._runner.id

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: Event | None = None,
        output: OutputSink,
    ) -> CommandResult:
        self._runner.script_process(command, stderr=self._stderr, returncode=1)
        return self._runner.execute(command, timeout=timeout, cancel=cancel, output=output)


def _executor(
    tmp_path: Path,
    plan: TrustedEvaluationPlan,
    sandbox: CommandRunner,
    *,
    git: _Git | None = None,
    model_requests: ModelRequestReconciler | None = None,
) -> TrustedEvaluationExecutor:
    return create_trusted_evaluation_executor(
        plan,
        workspace=tmp_path,
        sandbox=sandbox,
        git=cast("GitTracker", git or _Git()),
        model_requests=model_requests,
        threads=SimThreads(),
        blocking=InlineBlockingRunner(),
    )


def test_accuracy_applies_setup_timeout_and_preserves_streams(tmp_path: Path) -> None:
    sandbox = FakeCommandRunner(
        default_result=CommandResult(
            output="out\nwarning\n",
            exit_code=0,
            stdout="out\n",
            stderr="warning\n",
        )
    )
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            accuracy_command="check",
            accuracy_timeout_seconds=7,
            framework_setup_timeout_seconds=3,
        ),
        sandbox,
    )

    result = asyncio.run(executor.accuracy(output=DISCARD))

    assert result.passed
    assert result.executed
    assert result.stdout == "out\n"
    assert result.stderr == "warning\n"
    assert sandbox.calls[0].command == "check"
    assert sandbox.calls[0].timeout == 10


@pytest.mark.parametrize("stage", ["accuracy", "benchmark"])
@pytest.mark.parametrize("split_at", range(1, len("API_TOKEN")))
def test_failed_local_evaluation_streams_use_the_pre_redacted_bounded_view(
    tmp_path: Path,
    stage: str,
    split_at: int,
) -> None:
    limit = 100
    credential = "API_TOKEN=stream-secret"
    suffix = "z" * (81 + split_at - len(credential) - 1)
    runner = _FailedProcessRunner("p" * 32 + credential + " " + suffix, limit=limit)
    plan = TrustedEvaluationPlan(
        accuracy_command="check",
        benchmark_command="bench",
        benchmark_contract=ScalarBenchmarkContract(output_argument="--output", metric="score"),
    )
    executor = _executor(tmp_path, plan, runner)

    if stage == "accuracy":
        result = asyncio.run(executor.accuracy(output=DISCARD))
    else:
        result = asyncio.run(executor.benchmark(output=DISCARD))

    assert not result.passed
    assert "stream-secret" in result.output
    assert "stream-secret" in result.stderr
    assert "stream-secret" not in result.operator_output
    assert "stream-secret" not in result.operator_stderr
    assert len(result.stderr) <= limit
    assert len(result.operator_stderr) <= limit


def test_only_the_stage_command_writes_to_the_stage_output(tmp_path: Path) -> None:
    """Each stage's command, and no framework command around it, writes to its sink."""
    printed = f'step 1\n{_MARKER}\n{{"score": 4.5}}\n{_END_MARKER}\n'
    sandbox = FakeCommandRunner(
        default_result=CommandResult(
            output=printed + "warn\n", exit_code=0, stdout=printed, stderr="warn\n"
        )
    )
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            accuracy_command="check",
            benchmark_command="bench",
            benchmark_contract=ScalarBenchmarkContract(
                output_argument="--output-json", metric="score"
            ),
        ),
        sandbox,
    )
    accuracy_output, benchmark_output = Capture(1 << 16), Capture(1 << 16)

    accuracy = asyncio.run(executor.accuracy(output=accuracy_output))
    benchmark = asyncio.run(executor.benchmark(output=benchmark_output))

    assert accuracy.passed
    assert benchmark.passed
    assert benchmark.row == {"score": 4.5}
    # The benchmark's cleanup ran too, and wrote to no stage's stream.
    assert sandbox.calls[-1].command.startswith("rm -f -- ")
    for output in (accuracy_output, benchmark_output):
        assert (output.stdout, output.stderr) == (printed.encode(), b"warn\n")


def test_accuracy_rejects_trusted_input_changes_without_execution(tmp_path: Path) -> None:
    sandbox = FakeCommandRunner()
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(accuracy_command="check"),
        sandbox,
        git=_Git(["vibesys.input.toml"]),
    )

    result = asyncio.run(executor.accuracy(output=DISCARD))

    assert not result.passed
    assert not result.executed
    assert result.failure == "Evaluator-owned files were modified: vibesys.input.toml"
    assert sandbox.calls == []


def test_accuracy_rejects_mutation_during_execution(tmp_path: Path) -> None:
    sandbox = FakeCommandRunner()
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(accuracy_command="check"),
        sandbox,
        git=_Git([], ["API_TOKEN=mutation-secret"]),
    )

    result = asyncio.run(executor.accuracy(output=DISCARD))

    assert not result.passed
    assert result.executed
    assert "API_TOKEN=mutation-secret" in result.output
    assert "mutation-secret" not in result.operator_output


def test_benchmark_keeps_raw_mutation_evidence_and_safe_operator_output(tmp_path: Path) -> None:
    output = f'{_MARKER}\n{{"score": 4.5}}\n{_END_MARKER}\n'
    sandbox = FakeCommandRunner(
        default_result=CommandResult(output=output, exit_code=0, stdout=output)
    )
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench",
            benchmark_contract=ScalarBenchmarkContract(
                output_argument="--output-json",
                metric="score",
            ),
        ),
        sandbox,
        git=_Git([], ["API_TOKEN=mutation-secret"]),
    )

    result = asyncio.run(executor.benchmark(output=DISCARD))

    assert not result.passed
    assert "API_TOKEN=mutation-secret" in result.output
    assert "mutation-secret" not in result.operator_output


def test_scalar_benchmark_decodes_finite_result_and_always_cleans(tmp_path: Path) -> None:
    output = f'noise\n{_MARKER}\n{{"score": 4.5}}\n{_END_MARKER}\n'
    sandbox = FakeCommandRunner(
        default_result=CommandResult(output=output, exit_code=0, stdout=output)
    )
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench",
            benchmark_contract=ScalarBenchmarkContract(
                output_argument="--output-json",
                metric="score",
            ),
        ),
        sandbox,
    )

    result = asyncio.run(executor.benchmark(output=DISCARD))

    assert result.passed
    assert result.row == {"score": 4.5}
    assert "--output-json" in sandbox.calls[0].command
    assert sandbox.calls[-1].command.startswith("rm -f -- /tmp/vibesys-framework-benchmark-")


class _TruncatingRunner(FakeCommandRunner):
    """Return head-truncated benchmark output, then the framed result file on request."""

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: Event | None = None,
        output: OutputSink,
    ) -> CommandResult:
        del cancel  # every command here finishes at once
        super().execute(command, timeout=timeout, output=output)
        if command.startswith("rm -f -- ") and "--output-json" in command:
            head = "evaluator log line\n" * 10
            combined = head + "\n\n... Output truncated at 100000 characters."
            return CommandResult(
                output=combined,
                exit_code=0,
                truncated=True,
                stdout=head,
                diagnostic_output=head + "\n\n... Output truncated at 100000 characters.",
                diagnostic_stdout=head,
            )
        if command.startswith("printf "):
            return CommandResult(
                output=f'\n{_MARKER}\n{{"score": 7.25}}\n{_END_MARKER}\n', exit_code=0
            )
        return self.default_result


def test_truncated_benchmark_output_reads_the_result_file_alone(tmp_path: Path) -> None:
    sandbox = _TruncatingRunner()
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench",
            benchmark_contract=ScalarBenchmarkContract(
                output_argument="--output-json",
                metric="score",
            ),
        ),
        sandbox,
    )

    result = asyncio.run(executor.benchmark(output=DISCARD))

    assert result.passed, result.output
    assert result.row == {"score": 7.25}
    output_path = sandbox.calls[0].command.split("--output-json ")[1].split(";")[0]
    assert sandbox.calls[1].command.startswith("printf ")
    assert f"cat {output_path}" in sandbox.calls[1].command
    assert sandbox.calls[-1].command == f"rm -f -- {output_path}"


_BULKY_BENCHMARK = """
import json, sys
path = sys.argv[sys.argv.index("--output-json") + 1]
size = int(sys.argv[1])
print("evaluator log line " * 20000)
# Place a two-byte character across the 48,000-byte chunk boundary.
pad = "x" * (48_000 - len('{"note": "'))
note = pad[:-1] + "\u00e9" + "y" * max(0, size - 48_000)
with open(path, "w", encoding="utf-8") as file:
    json.dump({"note": note, "score": 7.25}, file, ensure_ascii=False)
"""


def _bulky_executor(
    tmp_path: Path, size: int, *, max_output_chars: int = 100_000
) -> TrustedEvaluationExecutor:
    script = tmp_path / "bench.py"
    script.write_text(_BULKY_BENCHMARK, encoding="utf-8")
    sandbox = LocalShellRunner(
        tmp_path,
        env={"PATH": os.environ["PATH"]},
        max_output_chars=max_output_chars,
        threads=OsThreads(),
        launcher=SubprocessPipedProcessLauncher(),
    )
    return _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command=f"{sys.executable} {script} {size}",
            benchmark_contract=ScalarBenchmarkContract(
                output_argument="--output-json",
                metric="score",
            ),
        ),
        sandbox,
    )


@pytest.mark.parametrize("size", [120_000, 250_000])
def test_result_file_larger_than_the_output_cap_is_read_whole(tmp_path: Path, size: int) -> None:
    result = asyncio.run(_bulky_executor(tmp_path, size).benchmark(output=DISCARD))

    assert result.passed, result.output
    assert result.row == {"score": 7.25}


def test_result_file_that_cannot_be_read_in_chunks_names_its_size(tmp_path: Path) -> None:
    # A cap smaller than one encoded chunk cannot carry the result file.
    result = asyncio.run(
        _bulky_executor(tmp_path, 120_000, max_output_chars=2_000).benchmark(output=DISCARD)
    )

    assert not result.passed
    assert "could not be read back in 48000-byte chunks" in result.output


def test_scalar_benchmark_preserves_legacy_nested_result_shape(tmp_path: Path) -> None:
    output = f'{_MARKER}\n[{{"result": {{"score": 4.5}}}}]\n{_END_MARKER}\n'
    sandbox = FakeCommandRunner(
        default_result=CommandResult(output=output, exit_code=0, stdout=output)
    )
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench",
            benchmark_contract=ScalarBenchmarkContract(
                output_argument="--output-json",
                metric="score",
            ),
        ),
        sandbox,
    )

    result = asyncio.run(executor.benchmark(output=DISCARD))

    assert result.passed
    assert result.row == {"score": 4.5}


def test_protocol_benchmark_returns_full_row_and_declarations(tmp_path: Path) -> None:
    records = (
        '{"kind":"hello","protocol":2,"metrics":'
        '{"throughput":{"direction":"max","unit":"req/s"},'
        '"latency":{"direction":"min","unit":"ms"}}}\n'
        '{"kind":"result","values":{"throughput":8,"latency":3}}'
    )
    output = f"{_MARKER}\n{records}\n{_END_MARKER}\n"
    sandbox = FakeCommandRunner(
        default_result=CommandResult(output=output, exit_code=0, stdout=output)
    )
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench",
            benchmark_contract=ProtocolBenchmarkContract(),
        ),
        sandbox,
    )

    result = asyncio.run(
        executor.benchmark(required_metrics=frozenset({"throughput", "latency"}), output=DISCARD)
    )

    assert result.passed
    assert result.row == {"throughput": 8.0, "latency": 3.0}
    assert result.metrics["throughput"].direction == "max"
    assert result.metrics["throughput"].unit == "req/s"


def test_protocol_rejection_names_reason_and_declared_metrics(tmp_path: Path) -> None:
    records = (
        '{"kind":"hello","protocol":2,"metrics":'
        '{"throughput":{"direction":"max"}}}\n'
        '{"kind":"result","values":{"throughput":8}}'
    )
    output = f"{_MARKER}\n{records}\n{_END_MARKER}\n"
    sandbox = FakeCommandRunner(
        default_result=CommandResult(output=output, exit_code=0, stdout=output)
    )
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench",
            benchmark_contract=ProtocolBenchmarkContract(),
        ),
        sandbox,
    )

    result = asyncio.run(
        executor.benchmark(required_metrics=frozenset({"latency"}), output=DISCARD)
    )

    assert not result.passed
    assert result.failure is not None
    assert "invalid benchmark result [" in result.failure
    assert "evaluator declares: throughput" in result.failure
    assert result.failure_kind is BenchmarkFailureKind.WORKLOAD


def test_benchmark_timeout_result_is_a_failure_and_cleanup_still_runs(tmp_path: Path) -> None:
    sandbox = FakeCommandRunner(
        default_result=CommandResult(output="benchmark timed out", exit_code=-1)
    )
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench",
            benchmark_timeout_seconds=4,
            framework_setup_timeout_seconds=2,
            benchmark_contract=ProtocolBenchmarkContract(),
        ),
        sandbox,
    )

    result = asyncio.run(executor.benchmark(output=DISCARD))

    assert not result.passed
    assert result.executed
    assert result.failure == "benchmark timed out"
    assert result.failure_kind is BenchmarkFailureKind.INFRASTRUCTURE
    assert sandbox.calls[0].timeout == 6
    assert sandbox.calls[-1].command.startswith("rm -f -- /tmp/vibesys-framework-benchmark-")


@pytest.mark.parametrize("exit_code", [None, -15, -1, 0, 1, 2])
@pytest.mark.parametrize("framed", [False, True])
def test_failure_kind_requires_a_completed_trusted_benchmark_frame(
    tmp_path: Path, exit_code: int | None, *, framed: bool
) -> None:
    reason = "engine cannot satisfy the workload contract"
    records = '{"kind":"error","message":"' + reason + '"}'
    output = f"{_MARKER}\n{records}\n{_END_MARKER}" if framed else reason
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench", benchmark_contract=ProtocolBenchmarkContract()
        ),
        FakeCommandRunner(default_result=CommandResult(output=output, exit_code=exit_code)),
    )

    result = asyncio.run(executor.benchmark(output=DISCARD))

    assert not result.passed
    assert reason in (result.failure or "")
    if exit_code is None or exit_code < 0:
        expected = BenchmarkFailureKind.INFRASTRUCTURE
    elif framed:
        expected = BenchmarkFailureKind.WORKLOAD
    else:
        # It exited but wrote no result record: the process may have been killed.
        expected = BenchmarkFailureKind.AMBIGUOUS
    assert result.failure_kind is expected
    if framed:
        assert result.failure_reason == reason


def test_evaluator_disappearing_without_a_record_is_retried_once(tmp_path: Path) -> None:
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench", benchmark_contract=ProtocolBenchmarkContract()
        ),
        FakeCommandRunner(
            default_result=CommandResult(output=f"{_MARKER}\n\n{_END_MARKER}", exit_code=1)
        ),
    )

    result = asyncio.run(executor.benchmark(output=DISCARD))

    assert not result.passed
    assert result.failure_kind is BenchmarkFailureKind.AMBIGUOUS
    assert result.failure_reason is None


class _FailingSandbox:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def id(self) -> str:
        return "failing"

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: Event | None = None,
        output: OutputSink,
    ) -> CommandResult:
        del cancel, output  # every command here finishes at once and prints nothing
        del timeout
        self.calls.append(command)
        message = "sandbox unavailable"
        raise RuntimeError(message)


def test_benchmark_execution_error_is_typed_and_cleanup_failure_is_suppressed(
    tmp_path: Path,
) -> None:
    sandbox = _FailingSandbox()
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench",
            benchmark_contract=ProtocolBenchmarkContract(),
        ),
        sandbox,
    )

    result = asyncio.run(executor.benchmark(output=DISCARD))

    assert not result.passed
    assert result.executed
    assert result.failure == "benchmark command could not be executed: sandbox unavailable"
    assert result.failure_kind is BenchmarkFailureKind.INFRASTRUCTURE
    assert len(sandbox.calls) == 2
    assert sandbox.calls[-1].startswith("rm -f -- /tmp/vibesys-framework-benchmark-")


def test_model_request_failure_prevents_trusted_command(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    project.state.create_project("trusted evaluation", now=datetime(2026, 1, 1, tzinfo=UTC))
    project.configuration_root().resolve("models.json", must_exist=False).write_text(
        '[{"id":"blocked/model"}]'
    )
    provisioner = FakeModelVolumeProvisioner()
    reconciler = create_model_request_reconciler(
        provisioner=provisioner,
        environment={"VIBESYS_MODEL_REQUEST_ALLOW": "allowed/"},
    )
    sandbox = FakeCommandRunner()
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(accuracy_command="check"),
        sandbox,
        model_requests=reconciler,
    )

    result = asyncio.run(executor.accuracy(output=DISCARD))

    assert not result.executed
    assert not result.passed
    assert result.failure is not None
    assert result.failure.startswith("Model-weight request could not be satisfied:")
    assert sandbox.calls == []


def _accuracy_on_threads(
    tmp_path: Path, sandbox: CommandRunner, threads: SimThreads
) -> TrustedEvaluationExecutor:
    """An accuracy-only executor whose blocking commands run on simulated ``threads``."""
    return create_trusted_evaluation_executor(
        TrustedEvaluationPlan(accuracy_command="check"),
        workspace=tmp_path,
        sandbox=sandbox,
        git=cast("GitTracker", _Git()),
        threads=threads,
        blocking=SimBlockingRunner(threads),
    )


class _BlockingSandbox:
    def __init__(self, threads: SimThreads) -> None:
        self.started = threads.event()
        self.release = threads.event()
        self.completed = threads.event()
        self.calls: list[str] = []

    @property
    def id(self) -> str:
        return "blocking"

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: Event | None = None,
        output: OutputSink,
    ) -> CommandResult:
        del cancel, output  # every command here finishes at once and prints nothing
        del timeout
        self.calls.append(command)
        self.started.set()
        wait_or_fail(self.release, "the held execution release")
        self.completed.set()
        return CommandResult(output="", exit_code=0)


def test_cancellation_drains_owned_accuracy_execution(tmp_path: Path, sim: Sim) -> None:
    threads = sim.threads()
    sandbox = _BlockingSandbox(threads)
    executor = _accuracy_on_threads(tmp_path, sandbox, threads)

    async def scenario() -> None:
        operation = asyncio.create_task(executor.accuracy(output=DISCARD))
        await SimBlockingRunner(threads).run(sandbox.started.wait)
        operation.cancel()
        sandbox.release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert sandbox.completed.is_set()

    sim.run(scenario(), threads=threads)


class _CancellableSandbox(_BlockingSandbox):
    """A command that runs until its cancel event stops it, as a real runner's does."""

    def __init__(self, threads: SimThreads) -> None:
        super().__init__(threads)
        self.stopped_by_cancel = False

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: Event | None = None,
        output: OutputSink,
    ) -> CommandResult:
        del timeout, output
        assert cancel is not None
        self.calls.append(command)
        self.started.set()
        wait_or_fail(cancel, "the cancel event of the cancelled evaluation")
        self.stopped_by_cancel = True
        self.completed.set()
        return CommandResult(output="", exit_code=130)


def test_cancelling_an_evaluation_stops_its_running_command(tmp_path: Path, sim: Sim) -> None:
    threads = sim.threads()
    sandbox = _CancellableSandbox(threads)
    executor = _accuracy_on_threads(tmp_path, sandbox, threads)

    async def scenario() -> None:
        operation = asyncio.create_task(executor.accuracy(output=DISCARD))
        await SimBlockingRunner(threads).run(sandbox.started.wait)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert sandbox.stopped_by_cancel

    sim.run(scenario(), threads=threads)


@pytest.mark.parametrize("model", [TrustedAccuracyResult, TrustedBenchmarkResult])
def test_a_trusted_result_names_whose_fault_a_failure_is_exactly_when_it_failed(
    model: type[TrustedAccuracyResult] | type[TrustedBenchmarkResult],
) -> None:
    model(executed=True, passed=True)
    model(
        executed=True,
        passed=False,
        failure_kind=BenchmarkFailureKind.WORKLOAD,
        operator_failure="failed",
    )
    with pytest.raises(ValidationError, match="failure kind"):
        model(executed=True, passed=False)
    with pytest.raises(ValidationError, match="failure kind"):
        model(executed=True, passed=True, failure_kind=BenchmarkFailureKind.INFRASTRUCTURE)


_BUILD_PLAN = TrustedEvaluationPlan(
    build=TrustedBuild(command="make lib.so", timeout_seconds=60, outputs=("lib.so",)),
    accuracy_command="check",
)


def test_a_build_runs_under_the_confinement_never_the_trusted_runner(tmp_path: Path) -> None:
    trusted = FakeCommandRunner()
    confinement = FakeCommandRunner(default_result=CommandResult(output="built\n", exit_code=0))
    executor = _executor(tmp_path, _BUILD_PLAN, trusted)

    result = asyncio.run(executor.build(confinement=confinement, output=DISCARD))

    assert (result.passed, result.executed) == (True, True)
    assert [(call.command, call.timeout) for call in confinement.calls] == [("make lib.so", 60)]
    assert trusted.calls == []


def test_a_build_with_no_confinement_fails_closed_and_runs_nothing(tmp_path: Path) -> None:
    trusted = FakeCommandRunner()
    executor = _executor(tmp_path, _BUILD_PLAN, trusted)

    result = asyncio.run(executor.build(confinement=None, output=DISCARD))

    assert (result.passed, result.executed) == (False, False)
    assert result.failure is not None
    assert "no agent confinement" in result.failure
    assert trusted.calls == []


@pytest.mark.parametrize(
    ("changes", "executed"),
    [
        ((["task/vibesys.input.toml"],), False),
        (([], ["task/vibesys.input.toml"]), True),
    ],
    ids=["before", "during"],
)
def test_a_build_that_touches_evaluator_owned_files_fails(
    tmp_path: Path, changes: tuple[list[str], ...], *, executed: bool
) -> None:
    confinement = FakeCommandRunner(default_result=CommandResult(output="", exit_code=0))
    executor = _executor(tmp_path, _BUILD_PLAN, FakeCommandRunner(), git=_Git(*changes))

    result = asyncio.run(executor.build(confinement=confinement, output=DISCARD))

    assert (result.passed, result.executed) == (False, executed)
    assert result.failure is not None
    assert "task/vibesys.input.toml" in result.failure
    assert len(confinement.calls) == int(executed)


@pytest.mark.parametrize(("exit_code", "status"), [(2, "exited with 2"), (124, "timed out")])
def test_a_failed_build_says_how_it_ended(tmp_path: Path, exit_code: int, status: str) -> None:
    confinement = FakeCommandRunner(
        default_result=CommandResult(output="error: linker\n", exit_code=exit_code)
    )
    executor = _executor(tmp_path, _BUILD_PLAN, FakeCommandRunner())

    result = asyncio.run(executor.build(confinement=confinement, output=DISCARD))

    assert not result.passed
    assert result.failure is not None
    assert result.failure.startswith(f"Candidate build {status}.")
    assert "error: linker" in result.failure
