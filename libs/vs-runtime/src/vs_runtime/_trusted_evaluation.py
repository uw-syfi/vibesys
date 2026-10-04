"""Private trusted accuracy and benchmark execution for one workspace."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import shlex
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from vs_evaluator_protocol.api import (
    ErrorRecord,
    Hello,
    Measurement,
    PartialMeasurement,
    ProtocolError,
    check_objectives,
    parse_records,
    read_measurement,
)
from vs_runtime._model_requests import ModelRequestError
from vs_runtime.contracts import BenchmarkFailureKind
from vs_sandbox.api import SandboxExecutionResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_project.api import GitTracker
    from vs_runtime.api.infrastructure import ModelRequestReconciler
    from vs_sandbox.api import Sandbox

_BENCHMARK_OUTPUT_PREFIX = "/tmp/vibesys-framework-benchmark-"  # noqa: S108  # lint-waiver: LW-837218 [S108]; remote sandbox bridges allowlist this fixed transport path and every nonce is removed after use.
_BENCHMARK_MARKER = "__VIBESYS_FRAMEWORK_BENCHMARK_JSON__"
_BENCHMARK_END_MARKER = "__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__"


def build_trusted_benchmark_command(
    command: str,
    contract: TrustedBenchmarkContract,
    output_path: str,
) -> str:
    """Frame one benchmark command for the authoritative result decoder.

    The result file is printed whether or not the command succeeded, so a
    failed evaluator's `error` record reaches the decoder too; the framed
    command still exits with the benchmark's own status. A missing file frames
    nothing, which the decoder rejects for a passing run.
    """
    path = shlex.quote(output_path)
    return (
        f"rm -f -- {path}"
        f" && {{ {command}"
        f" {shlex.quote(contract.output_argument)} {path};"
        " status=$?;"
        f" printf '\\n{_BENCHMARK_MARKER}\\n';"
        f" cat {path} 2>/dev/null;"
        f" printf '\\n{_BENCHMARK_END_MARKER}\\n';"
        ' (exit "$status"); }'
    )


def _framed_result_command(output_path: str) -> str:
    """Print only an existing benchmark result file between the decoder's markers."""
    return (
        f"printf '\\n{_BENCHMARK_MARKER}\\n'"
        f" && cat {shlex.quote(output_path)}"
        f" && printf '\\n{_BENCHMARK_END_MARKER}\\n'"
    )


class ScalarBenchmarkContract(BaseModel):
    """One legacy scalar JSON result contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["scalar"] = "scalar"
    output_argument: str = Field(min_length=1)
    metric: str = Field(min_length=1)


class ProtocolBenchmarkContract(BaseModel):
    """One versioned evaluator result-stream contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["protocol"] = "protocol"
    version: Literal[2] = 2
    output_argument: str = "--vs-output"


type TrustedBenchmarkContract = ScalarBenchmarkContract | ProtocolBenchmarkContract


class TrustedEvaluationPlan(BaseModel):
    """Resolved commands and transport contract for one workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accuracy_command: str | None = Field(default=None, min_length=1)
    accuracy_timeout_seconds: int | None = Field(default=None, gt=0)
    profile_command: str | None = Field(default=None, min_length=1)
    profile_timeout_seconds: int | None = Field(default=None, gt=0)
    benchmark_command: str | None = Field(default=None, min_length=1)
    benchmark_timeout_seconds: int | None = Field(default=None, gt=0)
    framework_setup_timeout_seconds: int = Field(default=0, ge=0)
    benchmark_contract: TrustedBenchmarkContract | None = Field(
        default=None,
        discriminator="kind",
    )

    def execution_budget_seconds(
        self, stages: tuple[Literal["accuracy", "benchmark", "profile", "framework_setup"], ...]
    ) -> int:
        """Sum declared timeouts for exactly the stages the evaluation will run.

        Missing stage bounds are an error. Callers include framework_setup
        when their execution plan runs it; no runtime default is guessed.
        """
        if not stages or len(stages) != len(set(stages)):
            message = "stages must be nonempty and unique"
            raise ValueError(message)
        timeouts = {
            "accuracy": self.accuracy_timeout_seconds,
            "benchmark": self.benchmark_timeout_seconds,
            "profile": self.profile_timeout_seconds,
            "framework_setup": self.framework_setup_timeout_seconds,
        }
        budget = 0
        for stage in stages:
            timeout = timeouts.get(stage)
            if timeout is None:
                message = f"stages.{stage}: declared timeout_seconds is required"
                raise ValueError(message)
            budget += timeout
        return budget

    def suspension_deadline_s(
        self,
        submitted_at_s: float,
        stages: tuple[Literal["accuracy", "benchmark", "profile", "framework_setup"], ...],
        queue_allowance_seconds: int,
    ) -> float:
        """Derive an absolute deadline from supplied time and declared bounds."""
        if not math.isfinite(submitted_at_s) or submitted_at_s < 0:
            message = "submitted_at_s must be finite and nonnegative"
            raise ValueError(message)
        if type(queue_allowance_seconds) is not int or queue_allowance_seconds <= 0:
            message = "queue_allowance_seconds must be a positive integer"
            raise ValueError(message)
        budget = self.execution_budget_seconds(stages)
        try:
            deadline = submitted_at_s + queue_allowance_seconds + budget
        except OverflowError as error:
            message = "suspension deadline must be finite"
            raise ValueError(message) from error
        if not math.isfinite(deadline):
            message = "suspension deadline must be finite"
            raise ValueError(message)
        return deadline


class TrustedMetricDeclaration(BaseModel):
    """Protocol-declared metadata for one measured metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: Literal["max", "min"] | None = None
    unit: str | None = None
    required: bool = True


class TrustedAccuracyResult(BaseModel):
    """Policy-neutral result of one trusted accuracy command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str | None = None
    executed: bool
    passed: bool
    output: str = ""
    failure: str | None = None
    stdout: str = ""
    stderr: str = ""
    provisioned_volumes: tuple[str, ...] = ()


class TrustedBenchmarkResult(BaseModel):
    """Policy-neutral result of one trusted benchmark command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str | None = None
    executed: bool
    passed: bool
    output: str = ""
    failure: str | None = None
    failure_kind: BenchmarkFailureKind | None = None
    # The validated evaluator error record's message, without transport framing.
    failure_reason: str | None = None
    stdout: str = ""
    stderr: str = ""
    row: Mapping[str, FiniteFloat] | None = None
    metrics: Mapping[str, TrustedMetricDeclaration] = Field(default_factory=dict)
    # What a failed run measured before it stopped, as its evaluator reported it.
    partial_measurement: PartialMeasurement | None = None
    provisioned_volumes: tuple[str, ...] = ()


class TrustedEvaluationExecutor(Protocol):
    """Serialize trusted evaluation effects for one workspace."""

    async def accuracy(self, *, command_override: str | None = None) -> TrustedAccuracyResult:
        """Run the configured accuracy command and enforce input integrity."""
        ...

    async def benchmark(
        self,
        *,
        command_override: str | None = None,
        required_metrics: frozenset[str] = frozenset(),
    ) -> TrustedBenchmarkResult:
        """Run and decode the configured benchmark result contract."""
        ...


async def _drain(task: asyncio.Task[object]) -> None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:  # noqa: BLE001  # lint-waiver: LW-837216 [BLE001]; cancellation must drain the owned worker outcome regardless of its exception type.
            break


class RuntimeTrustedEvaluation:
    """Concrete trusted evaluation owner for one workspace and sandbox."""

    def __init__(
        self,
        plan: TrustedEvaluationPlan,
        workspace: Path,
        sandbox: Sandbox,
        git: GitTracker,
        model_requests: ModelRequestReconciler | None,
    ) -> None:
        self._plan = plan
        self._workspace = workspace
        self._sandbox = sandbox
        self._git = git
        self._model_requests = model_requests
        self._lock = asyncio.Lock()

    async def accuracy(self, *, command_override: str | None = None) -> TrustedAccuracyResult:
        """Run one accuracy operation without overlapping workspace mutation."""
        async with self._lock:
            return await self._run_sync(partial(self._accuracy, command_override))

    async def benchmark(
        self,
        *,
        command_override: str | None = None,
        required_metrics: frozenset[str] = frozenset(),
    ) -> TrustedBenchmarkResult:
        """Run one benchmark operation without overlapping workspace mutation."""
        async with self._lock:
            return await self._run_sync(
                partial(self._benchmark, command_override, required_metrics)
            )

    async def _run_sync[Result](
        self,
        operation: Callable[[threading.Event], Result],
    ) -> Result:
        """Run *operation* in a worker; cancelling the caller stops its command.

        On cancellation the operation's cancel event stops the sandbox command
        (and, on Slurm, cancels its job); the worker is drained before the
        cancellation propagates, so no command outlives this call.
        """
        cancel = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(operation, cancel))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            cancel.set()
            await _drain(task)
            if error := task.exception():
                cancelled.add_note(f"trusted evaluation also failed: {error}")
            raise

    def _provision(self) -> tuple[tuple[str, ...], str | None]:
        if self._model_requests is None:
            return (), None
        try:
            volumes = self._model_requests.reconcile(self._workspace, log=lambda _message: None)
        except ModelRequestError as error:
            return (), f"Model-weight request could not be satisfied: {error}"
        return volumes, None

    def _accuracy(
        self, command_override: str | None, cancel: threading.Event
    ) -> TrustedAccuracyResult:
        command = self._plan.accuracy_command
        volumes, failure = self._provision()
        if failure is not None:
            return TrustedAccuracyResult(
                command=command,
                executed=False,
                passed=False,
                output=failure,
                failure=failure,
            )
        if changed := self._git.trusted_input_changes():
            failure = "Evaluator-owned files were modified: " + ", ".join(changed)
            return TrustedAccuracyResult(
                command=command,
                executed=False,
                passed=False,
                output=failure,
                failure=failure,
            )
        if command is None:
            return TrustedAccuracyResult(
                executed=False,
                passed=True,
                provisioned_volumes=volumes,
            )
        result, execution_failure = self._execute(
            command_override or command,
            timeout=self._timeout(self._plan.accuracy_timeout_seconds),
            label="accuracy",
            cancel=cancel,
        )
        output = execution_failure or result.output.strip()
        passed = execution_failure is None and result.exit_code == 0
        if changed := self._git.trusted_input_changes():
            mutation = "Evaluator-owned files changed during accuracy execution: " + ", ".join(
                changed
            )
            output = f"{output}\n{mutation}".strip()
            passed = False
        return TrustedAccuracyResult(
            command=command,
            executed=True,
            passed=passed,
            output=output,
            failure=None if passed else output,
            stdout=result.stdout,
            stderr=result.stderr,
            provisioned_volumes=volumes,
        )

    def _benchmark(
        self,
        command_override: str | None,
        required_metrics: frozenset[str],
        cancel: threading.Event,
    ) -> TrustedBenchmarkResult:
        contract = self._plan.benchmark_contract
        command = self._plan.benchmark_command
        volumes, failure = self._provision()
        if failure is not None:
            return TrustedBenchmarkResult(
                command=command,
                executed=False,
                passed=False,
                output=failure,
                failure=failure,
                failure_kind=BenchmarkFailureKind.INFRASTRUCTURE,
            )
        if contract is None:
            return TrustedBenchmarkResult(
                executed=False,
                passed=True,
                provisioned_volumes=volumes,
            )
        if command is None:
            failure = "Benchmark result contract is configured without a benchmark command."
            return TrustedBenchmarkResult(
                executed=False,
                passed=False,
                output=failure,
                failure=failure,
                failure_kind=BenchmarkFailureKind.WORKLOAD,
            )
        if changed := self._git.trusted_input_changes():
            failure = "Evaluator-owned files were modified: " + ", ".join(changed)
            return TrustedBenchmarkResult(
                command=command,
                executed=False,
                passed=False,
                output=failure,
                failure=failure,
                failure_kind=BenchmarkFailureKind.WORKLOAD,
            )
        output_path = f"{_BENCHMARK_OUTPUT_PREFIX}{uuid.uuid4().hex}.json"
        execution = build_trusted_benchmark_command(
            command_override or command,
            contract,
            output_path,
        )
        try:
            result, execution_failure = self._execute(
                execution,
                timeout=self._timeout(self._plan.benchmark_timeout_seconds),
                label="benchmark",
                cancel=cancel,
            )
            decoded = _Decoded(output=execution_failure or result.output.strip(), passed=False)
            framed = decoded.output
            if execution_failure is None:
                if result.truncated:
                    # The sandbox keeps only the head of long output, which
                    # drops the framed result appended after the evaluator's
                    # own logs. Read the result file on its own instead.
                    framed = self._sandbox.execute(_framed_result_command(output_path)).output
                decoded = _decode_framed(
                    framed,
                    decoded.output,
                    contract,
                    required_metrics,
                    exited_cleanly=result.exit_code == 0,
                )
            output, passed, row = decoded.output, decoded.passed, decoded.row
            if changed := self._git.trusted_input_changes():
                mutation = "Evaluator-owned files changed during benchmark execution: " + ", ".join(
                    changed
                )
                output = f"{output}\n{mutation}".strip()
                passed = False
                row = None
            return TrustedBenchmarkResult(
                command=command,
                executed=True,
                passed=passed,
                output=output,
                failure=None if passed else output,
                failure_kind=(
                    None if passed else _benchmark_failure_kind(framed, result, execution_failure)
                ),
                failure_reason=decoded.reason,
                stdout=result.stdout,
                stderr=result.stderr,
                row=row,
                metrics=decoded.metrics,
                partial_measurement=decoded.partial,
                provisioned_volumes=volumes,
            )
        finally:
            with contextlib.suppress(Exception):
                self._sandbox.execute(f"rm -f -- {shlex.quote(output_path)}")

    def _execute(
        self,
        command: str,
        *,
        timeout: int | None,
        label: str,
        cancel: threading.Event,
    ) -> tuple[SandboxExecutionResult, str | None]:
        try:
            result = self._sandbox.execute(command, timeout=timeout, cancel=cancel)
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-837217 [BLE001]; trusted command failures are typed policy-visible outcomes rather than run-fatal exceptions.
            return SandboxExecutionResult(output="", exit_code=None), (
                f"{label} command could not be executed: {error}"
            )
        return result, None

    def _timeout(self, declared: int | None) -> int | None:
        if declared is None:
            return None
        return declared + self._plan.framework_setup_timeout_seconds


@dataclass(frozen=True, slots=True)
class _Decoded:
    """One benchmark run's verdict after its framed result was decoded."""

    output: str
    passed: bool
    row: Mapping[str, float] | None = None
    metrics: Mapping[str, TrustedMetricDeclaration] = field(default_factory=dict)
    partial: PartialMeasurement | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TrustedBenchmarkDecoding:
    """What one finished trusted benchmark run reported, by the result contract.

    `passed` holds only for a clean exit whose framed result is a valid row.
    An evaluator `error` record fails the run whatever its exit status, and
    carries the evaluator's `reason` and its `partial` measurement. A result
    that violates the contract fails the run with the violation in `violation`.
    """

    passed: bool
    row: Mapping[str, float] | None = None
    metrics: Mapping[str, TrustedMetricDeclaration] = field(default_factory=dict)
    partial: PartialMeasurement | None = None
    reason: str | None = None
    violation: str | None = None


def _benchmark_failure_kind(
    framed: str, result: SandboxExecutionResult, execution_failure: str | None
) -> BenchmarkFailureKind:
    """Only a completed framed command proves that the workload rejected the input.

    A shell launch error can also exit 1. The trusted wrapper emits both markers
    even when the evaluator writes nothing, so empty or absent results remain retryable.
    Negative or absent exit codes describe cancellation, timeout, or transport loss.
    """
    _, marker, framed_result = framed.rpartition(_BENCHMARK_MARKER)
    encoded, end_marker, _ = framed_result.partition(_BENCHMARK_END_MARKER)
    if (
        execution_failure is None
        and result.exit_code is not None
        and result.exit_code >= 0
        and not result.cancelled
        and marker
        and end_marker
        and encoded.strip()
    ):
        return BenchmarkFailureKind.WORKLOAD
    return BenchmarkFailureKind.INFRASTRUCTURE


def decode_trusted_benchmark_run(
    framed: str,
    contract: TrustedBenchmarkContract,
    required_metrics: frozenset[str],
    *,
    exited_cleanly: bool,
) -> TrustedBenchmarkDecoding:
    """Decode one finished run's framed output; every trusted executor uses this.

    The evaluator's own `error` record decides first, so a run that reported a
    failure keeps its partial measurement even if its exit status was lost.
    """
    try:
        failure = _decode_benchmark_failure(framed, contract)
    except ValueError as error:
        return TrustedBenchmarkDecoding(passed=False, violation=str(error))
    if failure is not None:
        return TrustedBenchmarkDecoding(
            passed=False, partial=failure.partial, reason=failure.failure
        )
    if not exited_cleanly:
        return TrustedBenchmarkDecoding(passed=False)
    try:
        row, metrics = decode_trusted_benchmark_output(framed, contract, required_metrics)
    except (ProtocolError, ValueError, TypeError, json.JSONDecodeError) as error:
        return TrustedBenchmarkDecoding(passed=False, violation=str(error))
    return TrustedBenchmarkDecoding(passed=True, row=row, metrics=metrics)


def _decode_framed(
    framed: str,
    output: str,
    contract: TrustedBenchmarkContract,
    required_metrics: frozenset[str],
    *,
    exited_cleanly: bool,
) -> _Decoded:
    """Decode a finished run and append any contract violation to its output."""
    decoded = decode_trusted_benchmark_run(
        framed, contract, required_metrics, exited_cleanly=exited_cleanly
    )
    if decoded.violation is not None:
        output = f"{output}\n{decoded.violation}".strip()
    return _Decoded(
        output=output,
        passed=decoded.passed,
        row=decoded.row,
        metrics=decoded.metrics,
        partial=decoded.partial,
        reason=decoded.reason,
    )


class _BenchmarkResultError(ValueError):
    """One malformed trusted benchmark result."""

    @classmethod
    def missing_markers(cls) -> _BenchmarkResultError:
        return cls("benchmark output did not include its result JSON")

    @classmethod
    def evaluator_failure(cls, failure: str | None) -> _BenchmarkResultError:
        return cls(f"benchmark evaluator reported a failure: {failure}")

    @classmethod
    def field_count(cls, metric: str, count: int) -> _BenchmarkResultError:
        return cls(f"expected exactly one {metric!r} field, found {count}")

    @classmethod
    def nonnumeric(cls, metric: str) -> _BenchmarkResultError:
        return cls(f"{metric!r} is not numeric")

    @classmethod
    def nonfinite(cls, metric: str) -> _BenchmarkResultError:
        return cls(f"{metric!r} is not finite")

    @classmethod
    def scalar(cls, error: Exception) -> _BenchmarkResultError:
        return cls(f"invalid benchmark result: {error}")

    @classmethod
    def protocol(cls, error: ProtocolError, hello: Hello | None) -> _BenchmarkResultError:
        declared = ", ".join(sorted(hello.metrics)) if hello is not None else "(none declared)"
        return cls(
            f"invalid benchmark result [{error.code}]: {error}; evaluator declares: {declared}"
        )


def decode_trusted_benchmark_output(
    output: str,
    contract: TrustedBenchmarkContract,
    required_metrics: frozenset[str],
) -> tuple[Mapping[str, float], Mapping[str, TrustedMetricDeclaration]]:
    """Decode the framed benchmark contract used by every trusted executor."""
    _, marker, framed = output.rpartition(_BENCHMARK_MARKER)
    encoded, end_marker, _ = framed.partition(_BENCHMARK_END_MARKER)
    if not marker or not end_marker:
        raise _BenchmarkResultError.missing_markers()
    if isinstance(contract, ScalarBenchmarkContract):
        try:
            value = _parse_scalar(encoded, contract.metric)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise _BenchmarkResultError.scalar(error) from error
        return {contract.metric: value}, {}
    hello: Hello | None = None
    try:
        records = parse_records(encoded)
        hello = next((record for record in records if isinstance(record, Hello)), None)
        measurement = read_measurement(records)
        if required_metrics and hello is not None:
            check_objectives(hello, set(required_metrics))
    except ProtocolError as error:
        raise _BenchmarkResultError.protocol(error, hello) from error
    if measurement.values is None:
        raise _BenchmarkResultError.evaluator_failure(measurement.failure)
    declarations = {
        name: TrustedMetricDeclaration(
            direction=spec.direction,
            unit=spec.unit,
            required=spec.required,
        )
        for name, spec in (measurement.metrics or {}).items()
    }
    return measurement.values, declarations


def _decode_benchmark_failure(
    output: str, contract: TrustedBenchmarkContract
) -> Measurement | None:
    """Return a validated evaluator failure, preserving its reason and partial row."""
    if not isinstance(contract, ProtocolBenchmarkContract):
        return None
    _, marker, framed = output.rpartition(_BENCHMARK_MARKER)
    encoded, end_marker, _ = framed.partition(_BENCHMARK_END_MARKER)
    if not marker or not end_marker:
        return None
    hello: Hello | None = None
    try:
        records = parse_records(encoded)
        hello = next((record for record in records if isinstance(record, Hello)), None)
        if not any(isinstance(record, ErrorRecord) for record in records):
            return None
        return read_measurement(records)
    except ProtocolError as error:
        raise _BenchmarkResultError.protocol(error, hello) from error


def _parse_scalar(encoded: str, metric: str) -> float:
    payload = json.loads(encoded.strip())
    values = (
        [payload[metric]]
        if isinstance(payload, dict) and metric in payload
        else _metric_values(payload, metric)
    )
    if len(values) != 1:
        raise _BenchmarkResultError.field_count(metric, len(values))
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _BenchmarkResultError.nonnumeric(metric)
    result = float(value)
    if not math.isfinite(result):
        raise _BenchmarkResultError.nonfinite(metric)
    return result


def _metric_values(value: object, metric: str) -> list[object]:
    if isinstance(value, dict):
        matches = [item for key, item in value.items() if key == metric]
        for item in value.values():
            matches.extend(_metric_values(item, metric))
        return matches
    if isinstance(value, list):
        matches: list[object] = []
        for item in value:
            matches.extend(_metric_values(item, metric))
        return matches
    return []


def create_trusted_evaluation_executor(
    plan: TrustedEvaluationPlan,
    *,
    workspace: Path,
    sandbox: Sandbox,
    git: GitTracker,
    model_requests: ModelRequestReconciler | None = None,
) -> TrustedEvaluationExecutor:
    """Bind trusted evaluation mechanism to one owned workspace."""
    return RuntimeTrustedEvaluation(plan, workspace, sandbox, git, model_requests)
