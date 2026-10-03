"""Public composition contract for trusted workspace evaluation."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import TYPE_CHECKING, cast

import pytest

from vs_project.api import Project
from vs_runtime.api import BenchmarkFailureKind
from vs_runtime.api.infrastructure import (
    ModelRequestReconciler,
    ProtocolBenchmarkContract,
    ScalarBenchmarkContract,
    TrustedEvaluationExecutor,
    TrustedEvaluationPlan,
    create_model_request_reconciler,
    create_trusted_evaluation_executor,
)
from vs_runtime.api.testing import FakeModelVolumeProvisioner
from vs_sandbox.api import SandboxExecutionResult
from vs_sandbox.api.testing import FakeSandbox

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import GitTracker
    from vs_sandbox.api import Sandbox


_MARKER = "__VIBESYS_FRAMEWORK_BENCHMARK_JSON__"
_END_MARKER = "__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__"


class _Git:
    def __init__(self, *changes: list[str]) -> None:
        self._changes = deque(changes or ([],))

    def trusted_input_changes(self) -> list[str]:
        if len(self._changes) > 1:
            return self._changes.popleft()
        return self._changes[0]


def _executor(
    tmp_path: Path,
    plan: TrustedEvaluationPlan,
    sandbox: Sandbox,
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
    )


def test_accuracy_applies_setup_timeout_and_preserves_streams(tmp_path: Path) -> None:
    sandbox = FakeSandbox(
        default_result=SandboxExecutionResult(
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

    result = asyncio.run(executor.accuracy())

    assert result.passed
    assert result.executed
    assert result.stdout == "out\n"
    assert result.stderr == "warning\n"
    assert sandbox.calls[0].command == "check"
    assert sandbox.calls[0].timeout == 10


def test_accuracy_rejects_trusted_input_changes_without_execution(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(accuracy_command="check"),
        sandbox,
        git=_Git(["vibesys.input.toml"]),
    )

    result = asyncio.run(executor.accuracy())

    assert not result.passed
    assert not result.executed
    assert result.failure == "Evaluator-owned files were modified: vibesys.input.toml"
    assert sandbox.calls == []


def test_accuracy_rejects_mutation_during_execution(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(accuracy_command="check"),
        sandbox,
        git=_Git([], ["OBJECTIVE.md"]),
    )

    result = asyncio.run(executor.accuracy())

    assert not result.passed
    assert result.executed
    assert "changed during accuracy execution: OBJECTIVE.md" in result.output


def test_scalar_benchmark_decodes_finite_result_and_always_cleans(tmp_path: Path) -> None:
    output = f'noise\n{_MARKER}\n{{"score": 4.5}}\n{_END_MARKER}\n'
    sandbox = FakeSandbox(
        default_result=SandboxExecutionResult(output=output, exit_code=0, stdout=output)
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

    result = asyncio.run(executor.benchmark())

    assert result.passed
    assert result.row == {"score": 4.5}
    assert "--output-json" in sandbox.calls[0].command
    assert sandbox.calls[-1].command.startswith("rm -f -- /tmp/vibesys-framework-benchmark-")


class _TruncatingSandbox(FakeSandbox):
    """Return head-truncated benchmark output, then the framed result file on request."""

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: threading.Event | None = None,
    ) -> SandboxExecutionResult:
        del cancel  # every command here finishes at once
        super().execute(command, timeout=timeout)
        if command.startswith("rm -f -- ") and "--output-json" in command:
            head = "evaluator log line\n" * 10
            return SandboxExecutionResult(
                output=head + "\n\n... Output truncated at 100000 characters.",
                exit_code=0,
                truncated=True,
                stdout=head,
            )
        if command.startswith("printf "):
            return SandboxExecutionResult(
                output=f'\n{_MARKER}\n{{"score": 7.25}}\n{_END_MARKER}\n', exit_code=0
            )
        return self.default_result


def test_truncated_benchmark_output_reads_the_result_file_alone(tmp_path: Path) -> None:
    sandbox = _TruncatingSandbox()
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

    result = asyncio.run(executor.benchmark())

    assert result.passed, result.output
    assert result.row == {"score": 7.25}
    output_path = sandbox.calls[0].command.split("--output-json ")[1].split(";")[0]
    assert sandbox.calls[1].command.startswith("printf ")
    assert f"cat {output_path}" in sandbox.calls[1].command
    assert sandbox.calls[-1].command == f"rm -f -- {output_path}"


def test_scalar_benchmark_preserves_legacy_nested_result_shape(tmp_path: Path) -> None:
    output = f'{_MARKER}\n[{{"result": {{"score": 4.5}}}}]\n{_END_MARKER}\n'
    sandbox = FakeSandbox(
        default_result=SandboxExecutionResult(output=output, exit_code=0, stdout=output)
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

    result = asyncio.run(executor.benchmark())

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
    sandbox = FakeSandbox(
        default_result=SandboxExecutionResult(output=output, exit_code=0, stdout=output)
    )
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench",
            benchmark_contract=ProtocolBenchmarkContract(),
        ),
        sandbox,
    )

    result = asyncio.run(executor.benchmark(required_metrics=frozenset({"throughput", "latency"})))

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
    sandbox = FakeSandbox(
        default_result=SandboxExecutionResult(output=output, exit_code=0, stdout=output)
    )
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench",
            benchmark_contract=ProtocolBenchmarkContract(),
        ),
        sandbox,
    )

    result = asyncio.run(executor.benchmark(required_metrics=frozenset({"latency"})))

    assert not result.passed
    assert result.failure is not None
    assert "invalid benchmark result [" in result.failure
    assert "evaluator declares: throughput" in result.failure
    assert result.failure_kind is BenchmarkFailureKind.WORKLOAD


def test_benchmark_timeout_result_is_a_failure_and_cleanup_still_runs(tmp_path: Path) -> None:
    sandbox = FakeSandbox(
        default_result=SandboxExecutionResult(output="benchmark timed out", exit_code=-1)
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

    result = asyncio.run(executor.benchmark())

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
        FakeSandbox(default_result=SandboxExecutionResult(output=output, exit_code=exit_code)),
    )

    result = asyncio.run(executor.benchmark())

    assert not result.passed
    assert reason in (result.failure or "")
    expected = (
        BenchmarkFailureKind.WORKLOAD
        if framed and exit_code is not None and exit_code >= 0
        else BenchmarkFailureKind.INFRASTRUCTURE
    )
    assert result.failure_kind is expected
    if framed:
        assert result.failure_reason == reason


def test_evaluator_disappearing_without_a_record_is_retryable(tmp_path: Path) -> None:
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(
            benchmark_command="bench", benchmark_contract=ProtocolBenchmarkContract()
        ),
        FakeSandbox(
            default_result=SandboxExecutionResult(output=f"{_MARKER}\n\n{_END_MARKER}", exit_code=1)
        ),
    )

    result = asyncio.run(executor.benchmark())

    assert not result.passed
    assert result.failure_kind is BenchmarkFailureKind.INFRASTRUCTURE
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
        cancel: threading.Event | None = None,
    ) -> SandboxExecutionResult:
        del cancel  # every command here finishes at once
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

    result = asyncio.run(executor.benchmark())

    assert not result.passed
    assert result.executed
    assert result.failure == "benchmark command could not be executed: sandbox unavailable"
    assert result.failure_kind is BenchmarkFailureKind.INFRASTRUCTURE
    assert len(sandbox.calls) == 2
    assert sandbox.calls[-1].startswith("rm -f -- /tmp/vibesys-framework-benchmark-")


def test_model_request_failure_prevents_trusted_command(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    project.state.create_project("trusted evaluation")
    project.configuration_root().resolve("models.json", must_exist=False).write_text(
        '[{"id":"blocked/model"}]'
    )
    provisioner = FakeModelVolumeProvisioner()
    reconciler = create_model_request_reconciler(
        provisioner=provisioner,
        environment={"VIBESYS_MODEL_REQUEST_ALLOW": "allowed/"},
    )
    sandbox = FakeSandbox()
    executor = _executor(
        tmp_path,
        TrustedEvaluationPlan(accuracy_command="check"),
        sandbox,
        model_requests=reconciler,
    )

    result = asyncio.run(executor.accuracy())

    assert not result.executed
    assert not result.passed
    assert result.failure is not None
    assert result.failure.startswith("Model-weight request could not be satisfied:")
    assert sandbox.calls == []


class _BlockingSandbox:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()
        self.calls: list[str] = []

    @property
    def id(self) -> str:
        return "blocking"

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: threading.Event | None = None,
    ) -> SandboxExecutionResult:
        del cancel  # every command here finishes at once
        del timeout
        self.calls.append(command)
        self.started.set()
        self.release.wait()
        self.completed.set()
        return SandboxExecutionResult(output="", exit_code=0)


def test_cancellation_drains_owned_accuracy_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        sandbox = _BlockingSandbox()
        executor = _executor(
            tmp_path,
            TrustedEvaluationPlan(accuracy_command="check"),
            sandbox,
        )
        operation = asyncio.create_task(executor.accuracy())
        await asyncio.to_thread(sandbox.started.wait)
        operation.cancel()
        sandbox.release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert sandbox.completed.is_set()

    asyncio.run(scenario())
