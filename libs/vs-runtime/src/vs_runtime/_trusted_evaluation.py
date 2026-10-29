"""Private trusted accuracy and benchmark execution for one workspace."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import math
import shlex
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

from vs_core.api import EvidenceKind
from vs_decode.api import Unreadable, decode_json
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
from vs_redaction.api import redact_diagnostic_text
from vs_runtime._failure_classification import (
    RecordState,
    TerminalSignal,
    check_startup_cause_patterns,
    classify,
    signal_of,
)
from vs_runtime._model_requests import ModelRequestError
from vs_runtime.contracts import BenchmarkFailureKind
from vs_sandbox.api import CommandResult
from vs_sim.api import DISCARD

# Parses untrusted JSON with nesting bounded, failing as a ValueError.
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_MAX_BENCHMARK_RESULT_BYTES = 16 * 1024 * 1024
"""The largest benchmark result block read out of evaluator output."""

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_project.api import GitTracker
    from vs_runtime.api.infrastructure import ModelRequestReconciler
    from vs_sandbox.api import CommandRunner
    from vs_sim.api import BlockingRunner, Event, OutputSink, Threads

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


# A result file larger than the sandbox's output cap is read back in chunks.
# Each chunk is base64 so a multibyte character never splits across reads; at
# 48,000 bytes it encodes to 64,000 characters, under the 100,000-character
# local and container caps. The total stays bounded for evaluator output.
_RESULT_CHUNK_BYTES = 48_000
_MAX_RESULT_BYTES = 64 * 1024 * 1024


def _framed(text: str) -> str:
    return f"\n{_BENCHMARK_MARKER}\n{text}\n{_BENCHMARK_END_MARKER}\n"


def _read_result_file(sandbox: CommandRunner, output_path: str) -> tuple[str, str | None]:
    """Return the framed result file and, when it cannot be read whole, why.

    The file is printed in one command when it fits the sandbox's output cap,
    and otherwise read back in bounded chunks. A file that cannot be read
    whole returns the truncated framing with a reason naming its size.
    """
    whole = sandbox.execute(_framed_result_command(output_path), output=DISCARD)
    if not whole.truncated:
        return whole.output, None
    path = shlex.quote(output_path)
    try:
        size = int(sandbox.execute(f"wc -c < {path}", output=DISCARD).output.strip())
    except ValueError:
        return whole.output, "benchmark result file size could not be read"
    if size > _MAX_RESULT_BYTES:
        return whole.output, (
            f"benchmark result file is {size} bytes, over the {_MAX_RESULT_BYTES}-byte "
            "limit for trusted results"
        )
    data = _read_chunks(sandbox, path, size)
    if data is None:
        return whole.output, (
            f"benchmark result file is {size} bytes and could not be read back "
            f"in {_RESULT_CHUNK_BYTES}-byte chunks"
        )
    return _framed(data.decode("utf-8", errors="replace")), None


def _read_chunks(sandbox: CommandRunner, path: str, size: int) -> bytes | None:
    """Read ``size`` bytes of a quoted path in base64 chunks, or ``None`` on any gap."""
    data = bytearray()
    for offset in range(0, size, _RESULT_CHUNK_BYTES):
        chunk = sandbox.execute(
            f"tail -c +{offset + 1} {path} | head -c {_RESULT_CHUNK_BYTES} | base64 | tr -d '\\n'",
            output=DISCARD,
        )
        if chunk.exit_code != 0 or chunk.truncated:
            return None
        try:
            data += base64.b64decode(chunk.output.strip(), validate=True)
        except binascii.Error:
            return None
    # A file that changed size while being read is not one consistent result.
    return bytes(data) if len(data) == size else None


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


class TrustedBuild(BaseModel):
    """The candidate's build: confined candidate code, then the outputs it must produce.

    ``command`` runs from the root of a fresh checkout of the measured commit under the run
    environment's agent confinement, never as a trusted command. ``outputs`` are the
    evaluator package's ``candidate_outputs``, verified after the command and passed to the
    trusted check and measure stages.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str = Field(min_length=1)
    timeout_seconds: int | None = Field(default=None, gt=0)
    outputs: tuple[str, ...] = ()


_PATTERN_CHARACTERS = frozenset("*?[]\\:!")


def check_unread_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """The paths, each checked to name a workspace file or directory plainly; raises naming one.

    A path is relative, has no empty, ``.`` or ``..`` part, and holds no pattern or pathspec
    character, so it excludes exactly itself (and, for a directory, what lies below it).
    """
    for path in paths:
        parts = path.removesuffix("/").split("/")
        if (
            not path
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or _PATTERN_CHARACTERS & set(path)
        ):
            message = (
                f"unread path {path!r} must be a plain relative workspace path: no empty, '.'"
                " or '..' part, and none of * ? [ ] \\ : !"
            )
            raise ValueError(message)
    return paths


class TrustedEvaluationPlan(BaseModel):
    """Resolved commands and transport contract for one workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    build: TrustedBuild | None = None
    """The candidate's confined build, when the task declares one."""

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
    # Regular expressions (case-insensitive) that a server's log tail matches when its
    # candidate caused a startup failure. The task's domain supplies them: the runtime
    # holds only the facts true of any process. They describe why a candidate failed;
    # they do not decide whether an infrastructure failure is measured again.
    candidate_startup_causes: tuple[str, ...] = ()

    _check_startup_causes = field_validator("candidate_startup_causes")(
        check_startup_cause_patterns
    )

    unread_paths: tuple[str, ...] = ()
    """Workspace paths (files or directories) the task's evaluation never reads.

    Declared by the task, for example an agent's notes. A commit that changes only these is
    the same measured input (``MeasurementPlan.measured_input``), so its official measurement
    may reuse an earlier run of that input instead of running again.
    """

    _check_unread_paths = field_validator("unread_paths")(check_unread_paths)

    accuracy_gated: tuple[EvidenceKind, ...] = ()
    """The task's trust rule: kinds of evidence that count only if the accuracy stage passed.

    Declared by the task's evaluation definition and carried into every measurement plan;
    core holds no such rule of its own.
    """

    def execution_budget_seconds(
        self,
        stages: tuple[Literal["build", "accuracy", "benchmark", "profile", "framework_setup"], ...],
    ) -> int:
        """Sum declared timeouts for exactly the stages the evaluation will run.

        Missing stage bounds are an error. Callers include framework_setup
        when their execution plan runs it; no runtime default is guessed.
        """
        if not stages or len(stages) != len(set(stages)):
            message = "stages must be nonempty and unique"
            raise ValueError(message)
        timeouts = {
            "build": None if self.build is None else self.build.timeout_seconds,
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
        stages: tuple[Literal["build", "accuracy", "benchmark", "profile", "framework_setup"], ...],
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
    operator_output: str = ""
    operator_failure: str | None = None
    # Whose fault a failed command is: present exactly when it failed, with no default.
    failure_kind: BenchmarkFailureKind | None = None
    stdout: str = ""
    stderr: str = ""
    operator_stdout: str = ""
    operator_stderr: str = ""
    provisioned_volumes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _failure_kind_exactly_when_failed(self) -> TrustedAccuracyResult:
        if self.passed != (self.failure_kind is None):
            message = "a trusted accuracy result carries a failure kind exactly when it failed"
            raise ValueError(message)
        if self.passed != (self.operator_failure is None):
            message = "a failed accuracy result requires a separate operator-safe failure"
            raise ValueError(message)
        return self


class TrustedBuildResult(BaseModel):
    """Policy-neutral result of one confined candidate build command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str | None = None
    executed: bool
    passed: bool
    output: str = ""
    failure: str | None = None
    operator_output: str = ""
    operator_failure: str | None = None
    # Whose fault a failed build is: present exactly when it failed, with no default.
    failure_kind: BenchmarkFailureKind | None = None

    @model_validator(mode="after")
    def _failure_kind_exactly_when_failed(self) -> TrustedBuildResult:
        if self.passed != (self.failure_kind is None):
            message = "a build result carries a failure kind exactly when it failed"
            raise ValueError(message)
        if self.passed != (self.operator_failure is None):
            message = "a failed build result requires a separate operator-safe failure"
            raise ValueError(message)
        return self

    @classmethod
    def failed(
        cls, failure: str, kind: BenchmarkFailureKind, *, command: str | None, executed: bool
    ) -> TrustedBuildResult:
        """A failed build whose failure text is all its output."""
        operator_failure = redact_diagnostic_text(failure)
        return cls(
            command=command,
            executed=executed,
            passed=False,
            output=failure,
            failure=failure,
            operator_output=operator_failure,
            operator_failure=operator_failure,
            failure_kind=kind,
        )


_TIMEOUT_EXIT = 124
"""The exit status a ``CommandRunner`` reports for a command it stopped at its timeout."""

NO_BUILD_CONFINEMENT = (
    "this run environment has no agent confinement for the candidate's build, so the build "
    "does not run; use a run environment that runs the agent in a container (docker)"
)


class TrustedBenchmarkResult(BaseModel):
    """Policy-neutral result of one trusted benchmark command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str | None = None
    executed: bool
    passed: bool
    output: str = ""
    failure: str | None = None
    operator_output: str = ""
    operator_failure: str | None = None
    # Whose fault a failed run is: present exactly when the run failed, with no default.
    failure_kind: BenchmarkFailureKind | None = None
    # The validated evaluator error record's message, without transport framing.
    failure_reason: str | None = None
    # The capability the evaluator named as unmet on a failed run, when it did.
    unmet_requirement: str | None = None
    operator_failure_reason: str | None = None
    stdout: str = ""
    stderr: str = ""
    operator_stdout: str = ""
    operator_stderr: str = ""
    row: Mapping[str, FiniteFloat] | None = None
    metrics: Mapping[str, TrustedMetricDeclaration] = Field(default_factory=dict)
    # What a failed run measured before it stopped, as its evaluator reported it.
    partial_measurement: PartialMeasurement | None = None
    provisioned_volumes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _failure_kind_exactly_when_failed(self) -> TrustedBenchmarkResult:
        if self.passed != (self.failure_kind is None):
            message = "a trusted benchmark result carries a failure kind exactly when it failed"
            raise ValueError(message)
        if self.passed != (self.operator_failure is None):
            message = "a failed benchmark result requires a separate operator-safe failure"
            raise ValueError(message)
        if (self.failure_reason is None) != (self.operator_failure_reason is None):
            message = "a benchmark reason requires a separate operator-safe reason"
            raise ValueError(message)
        if self.passed and self.unmet_requirement is not None:
            message = "only a failed benchmark result names an unmet requirement"
            raise ValueError(message)
        return self


class TrustedEvaluationExecutor(Protocol):
    """Serialize trusted evaluation effects for one workspace."""

    async def build(
        self, *, confinement: CommandRunner | None, output: OutputSink
    ) -> TrustedBuildResult:
        """Run the candidate's build confined; with no confinement it fails closed.

        The command's stdout and stderr are written to *output* as it runs.
        """
        ...

    async def accuracy(
        self, *, command_override: str | None = None, output: OutputSink
    ) -> TrustedAccuracyResult:
        """Run the configured accuracy command and enforce input integrity.

        The command's stdout and stderr are written to *output* as it runs.
        """
        ...

    async def benchmark(
        self,
        *,
        command_override: str | None = None,
        required_metrics: frozenset[str] = frozenset(),
        output: OutputSink,
    ) -> TrustedBenchmarkResult:
        """Run and decode the configured benchmark result contract.

        The benchmark command's stdout and stderr, framed result included, are
        written to *output* as it runs; reading a truncated result back is not.
        """
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

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-260143 [PLR0913]; the plan, the workspace it owns, and the threads and runner its cancellable worker uses.
        # > A parameter object for workspace, sandbox and Git would have one user and only
        # > rename arguments; bundling threads and blocking would be a second ``Platform``,
        # > which only entrypoints and the server runtime take.
        self,
        plan: TrustedEvaluationPlan,
        workspace: Path,
        sandbox: CommandRunner,
        git: GitTracker,
        model_requests: ModelRequestReconciler | None,
        threads: Threads,
        blocking: BlockingRunner,
    ) -> None:
        self._plan = plan
        self._threads = threads
        self._blocking = blocking
        self._workspace = workspace
        self._sandbox = sandbox
        self._git = git
        self._model_requests = model_requests
        self._lock = asyncio.Lock()

    async def accuracy(
        self, *, command_override: str | None = None, output: OutputSink
    ) -> TrustedAccuracyResult:
        """Run one accuracy operation without overlapping workspace mutation."""
        async with self._lock:
            return await self._run_sync(partial(self._accuracy, command_override, output))

    async def build(
        self, *, confinement: CommandRunner | None, output: OutputSink
    ) -> TrustedBuildResult:
        """Run the candidate's build under ``confinement`` without overlapping workspace mutation.

        ``confinement`` is the run environment's agent confinement for this workspace; with
        none the build fails closed and runs nothing. The command's output is written to
        *output* as it runs.
        """
        async with self._lock:
            return await self._run_sync(partial(self._build, confinement, output))

    async def benchmark(
        self,
        *,
        command_override: str | None = None,
        required_metrics: frozenset[str] = frozenset(),
        output: OutputSink,
    ) -> TrustedBenchmarkResult:
        """Run one benchmark operation without overlapping workspace mutation."""
        async with self._lock:
            return await self._run_sync(
                partial(self._benchmark, command_override, required_metrics, output)
            )

    async def _run_sync[Result](
        self,
        operation: Callable[[Event], Result],
    ) -> Result:
        """Run *operation* in a worker; cancelling the caller stops its command.

        On cancellation the operation's cancel event stops the sandbox command
        (and, on Slurm, cancels its job); the worker is drained before the
        cancellation propagates, so no command outlives this call.
        """
        cancel = self._threads.event()
        task = asyncio.create_task(self._blocking.run(operation, cancel))
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

    def _build(
        self, confinement: CommandRunner | None, sink: OutputSink, cancel: Event
    ) -> TrustedBuildResult:
        build = self._plan.build
        if build is None:
            return TrustedBuildResult(executed=False, passed=True)
        refusal = self._build_refusal(build, confinement)
        if refusal is not None or confinement is None:
            return refusal or TrustedBuildResult.failed(
                NO_BUILD_CONFINEMENT,
                BenchmarkFailureKind.WORKLOAD,
                command=build.command,
                executed=False,
            )
        # A runner that cannot start the command raises; the executor records that as the
        # evaluation's failure, as it does for any stage that raises.
        result = confinement.execute(
            build.command, timeout=self._timeout(build.timeout_seconds), cancel=cancel, output=sink
        )
        if changed := self._git.trusted_input_changes():
            return TrustedBuildResult.failed(
                "Evaluator-owned files changed during the build: " + ", ".join(changed),
                BenchmarkFailureKind.WORKLOAD,
                command=build.command,
                executed=True,
            )
        if result.exit_code == 0 and not result.cancelled:
            return TrustedBuildResult(
                command=build.command,
                executed=True,
                passed=True,
                output=result.output.strip(),
                operator_output=result.diagnostic_output.strip(),
            )
        status = (
            "timed out" if result.exit_code == _TIMEOUT_EXIT else f"exited with {result.exit_code}"
        )
        failure = f"Candidate build {status}.\n{result.output.strip()}".strip()
        operator_failure = f"Candidate build {status}.\n{result.diagnostic_output.strip()}".strip()
        return TrustedBuildResult(
            command=build.command,
            executed=True,
            passed=False,
            output=failure,
            failure=failure,
            operator_output=operator_failure,
            operator_failure=operator_failure,
            failure_kind=_command_failure_kind(RecordState.OUTCOME, result, None),
        )

    def _build_refusal(
        self, build: TrustedBuild, confinement: CommandRunner | None
    ) -> TrustedBuildResult | None:
        """Why the build must not run at all, or None: no confinement, or tampered inputs."""
        if confinement is None:
            return TrustedBuildResult.failed(
                NO_BUILD_CONFINEMENT,
                BenchmarkFailureKind.WORKLOAD,
                command=build.command,
                executed=False,
            )
        if changed := self._git.trusted_input_changes():
            return TrustedBuildResult.failed(
                "Evaluator-owned files were modified: " + ", ".join(changed),
                BenchmarkFailureKind.WORKLOAD,
                command=build.command,
                executed=False,
            )
        return None

    def _accuracy(
        self, command_override: str | None, sink: OutputSink, cancel: Event
    ) -> TrustedAccuracyResult:
        command = self._plan.accuracy_command
        volumes, failure = self._provision()
        if failure is not None:
            operator_failure = redact_diagnostic_text(failure)
            return TrustedAccuracyResult(
                command=command,
                executed=False,
                passed=False,
                output=failure,
                failure=failure,
                operator_output=operator_failure,
                operator_failure=operator_failure,
                failure_kind=BenchmarkFailureKind.INFRASTRUCTURE,
            )
        if changed := self._git.trusted_input_changes():
            failure = "Evaluator-owned files were modified: " + ", ".join(changed)
            operator_failure = redact_diagnostic_text(failure)
            return TrustedAccuracyResult(
                command=command,
                executed=False,
                passed=False,
                output=failure,
                failure=failure,
                operator_output=operator_failure,
                operator_failure=operator_failure,
                failure_kind=BenchmarkFailureKind.WORKLOAD,
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
            sink=sink,
        )
        output = execution_failure or result.output.strip()
        operator_output = (
            redact_diagnostic_text(execution_failure)
            if execution_failure is not None
            else result.diagnostic_output.strip()
        )
        passed = execution_failure is None and result.exit_code == 0
        mutated = False
        if changed := self._git.trusted_input_changes():
            mutation = "Evaluator-owned files changed during accuracy execution: " + ", ".join(
                changed
            )
            output = f"{output}\n{mutation}".strip()
            operator_output = f"{operator_output}\n{redact_diagnostic_text(mutation)}".strip()
            passed = False
            mutated = True
        return TrustedAccuracyResult(
            command=command,
            executed=True,
            passed=passed,
            output=output,
            failure=None if passed else output,
            operator_output=operator_output,
            operator_failure=None if passed else operator_output,
            # The exit status is the accuracy command's own verdict, so it leaves an outcome
            # whenever it reported one; a candidate that changed evaluator-owned files owns it.
            failure_kind=(
                None
                if passed
                else BenchmarkFailureKind.WORKLOAD
                if mutated
                else _command_failure_kind(RecordState.OUTCOME, result, execution_failure)
            ),
            stdout=result.stdout,
            stderr=result.stderr,
            operator_stdout=result.diagnostic_stdout,
            operator_stderr=result.diagnostic_stderr,
            provisioned_volumes=volumes,
        )

    def _benchmark(
        self,
        command_override: str | None,
        required_metrics: frozenset[str],
        sink: OutputSink,
        cancel: Event,
    ) -> TrustedBenchmarkResult:
        contract = self._plan.benchmark_contract
        command = self._plan.benchmark_command
        volumes, failure = self._provision()
        if failure is not None:
            operator_failure = redact_diagnostic_text(failure)
            return TrustedBenchmarkResult(
                command=command,
                executed=False,
                passed=False,
                output=failure,
                failure=failure,
                operator_output=operator_failure,
                operator_failure=operator_failure,
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
            operator_failure = redact_diagnostic_text(failure)
            return TrustedBenchmarkResult(
                executed=False,
                passed=False,
                output=failure,
                failure=failure,
                operator_output=operator_failure,
                operator_failure=operator_failure,
                failure_kind=BenchmarkFailureKind.WORKLOAD,
            )
        if changed := self._git.trusted_input_changes():
            failure = "Evaluator-owned files were modified: " + ", ".join(changed)
            operator_failure = redact_diagnostic_text(failure)
            return TrustedBenchmarkResult(
                command=command,
                executed=False,
                passed=False,
                output=failure,
                failure=failure,
                operator_output=operator_failure,
                operator_failure=operator_failure,
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
                sink=sink,
            )
            raw_output = execution_failure or result.output.strip()
            diagnostic_output = (
                redact_diagnostic_text(execution_failure)
                if execution_failure is not None
                else result.diagnostic_output.strip()
            )
            decoded = _Decoded(output=raw_output, passed=False)
            framed = raw_output
            problem = None
            if execution_failure is None:
                if result.truncated:
                    # The sandbox keeps only the head of long output, which
                    # drops the framed result appended after the evaluator's
                    # own logs. Read the result file on its own instead.
                    framed, problem = _read_result_file(self._sandbox, output_path)
                decoded = _decode_framed(
                    framed,
                    decoded.output if problem is None else f"{decoded.output}\n{problem}",
                    contract,
                    required_metrics,
                    exited_cleanly=result.exit_code == 0,
                )
            output, passed, row = decoded.output, decoded.passed, decoded.row
            operator_output = diagnostic_output
            if problem is not None:
                operator_output = f"{operator_output}\n{problem}".strip()
            if decoded.violation is not None:
                safe_violation = redact_diagnostic_text(decoded.violation)
                operator_output = f"{operator_output}\n{safe_violation}".strip()
            if changed := self._git.trusted_input_changes():
                mutation = "Evaluator-owned files changed during benchmark execution: " + ", ".join(
                    changed
                )
                output = f"{output}\n{mutation}".strip()
                operator_output = f"{operator_output}\n{redact_diagnostic_text(mutation)}".strip()
                passed = False
                row = None
            return TrustedBenchmarkResult(
                command=command,
                executed=True,
                passed=passed,
                output=output,
                failure=None if passed else output,
                operator_output=operator_output,
                operator_failure=None if passed else operator_output,
                failure_kind=(
                    None
                    if passed
                    else _command_failure_kind(record_state(framed), result, execution_failure)
                ),
                failure_reason=decoded.reason,
                unmet_requirement=None if passed else decoded.requirement,
                operator_failure_reason=(
                    None if decoded.reason is None else redact_diagnostic_text(decoded.reason)
                ),
                stdout=result.stdout,
                stderr=result.stderr,
                operator_stdout=result.diagnostic_stdout,
                operator_stderr=result.diagnostic_stderr,
                row=row,
                metrics=decoded.metrics,
                partial_measurement=decoded.partial,
                provisioned_volumes=volumes,
            )
        finally:
            with contextlib.suppress(Exception):
                self._sandbox.execute(f"rm -f -- {shlex.quote(output_path)}", output=DISCARD)

    def _execute(
        self,
        command: str,
        *,
        timeout: int | None,
        label: str,
        cancel: Event,
        sink: OutputSink,
    ) -> tuple[CommandResult, str | None]:
        try:
            result = self._sandbox.execute(command, timeout=timeout, cancel=cancel, output=sink)
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-837217 [BLE001]; trusted command failures are typed policy-visible outcomes rather than run-fatal exceptions.
            return CommandResult(output="", exit_code=None), (
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
    violation: str | None = None
    requirement: str | None = None


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
    # What the evaluator left in its result file; with the exit status it decides whose
    # fault a failed run is (`classify`).
    record: RecordState = RecordState.ABSENT
    # The capability the evaluator's `error` record named as unmet, when it did.
    requirement: str | None = None


def record_state(framed: str) -> RecordState:
    """What the evaluator wrote between the trusted wrapper's markers.

    The wrapper prints both markers even when the evaluator dies first, so a run whose
    evaluator never wrote its file frames nothing. An evaluator that only declared its
    schema (a ``hello`` record, written before anything is measured) started but left no
    outcome. Any other content is an outcome, even a malformed one: it is the evaluator's own.
    """
    _, marker, framed_result = framed.rpartition(_BENCHMARK_MARKER)
    encoded, end_marker, _ = framed_result.partition(_BENCHMARK_END_MARKER)
    if not (marker and end_marker and encoded.strip()):
        return RecordState.ABSENT
    try:
        records = parse_records(encoded)
    except (ProtocolError, ValueError):
        return RecordState.OUTCOME
    if records and all(isinstance(item, Hello) for item in records):
        return RecordState.DECLARED
    return RecordState.OUTCOME


def _command_failure_kind(
    record: RecordState, result: CommandResult, execution_failure: str | None
) -> BenchmarkFailureKind:
    """Whose fault a failed trusted command is, by its exit status and its record.

    The one reading of a sandbox result for every trusted stage (accuracy and benchmark).
    A command that could not run, was cancelled, or timed out in the sandbox reported
    no exit status (negative or absent), which says nothing about the candidate.
    """
    signal = (
        TerminalSignal.NO_EXIT_STATUS
        if execution_failure is not None or result.cancelled
        else signal_of(result.exit_code)
    )
    return classify(signal, record)


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
    kind = record_state(framed)
    try:
        failure = _decode_benchmark_failure(framed, contract)
    except ValueError as error:
        return TrustedBenchmarkDecoding(passed=False, violation=str(error), record=kind)
    if failure is not None:
        return TrustedBenchmarkDecoding(
            passed=False,
            partial=failure.partial,
            reason=failure.failure,
            record=kind,
            requirement=failure.requirement,
        )
    if not exited_cleanly:
        return TrustedBenchmarkDecoding(passed=False, record=kind)
    try:
        row, metrics = decode_trusted_benchmark_output(framed, contract, required_metrics)
    except (ProtocolError, ValueError, TypeError) as error:
        return TrustedBenchmarkDecoding(passed=False, violation=str(error), record=kind)
    return TrustedBenchmarkDecoding(passed=True, row=row, metrics=metrics, record=kind)


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
        violation=decoded.violation,
        requirement=decoded.requirement,
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
        except (ValueError, TypeError) as error:
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
    payload = decode_json(
        encoded.strip(), _JSON, source="benchmark result", max_bytes=_MAX_BENCHMARK_RESULT_BYTES
    )
    if isinstance(payload, Unreadable):
        raise _BenchmarkResultError(str(payload))
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


def create_trusted_evaluation_executor(  # noqa: PLR0913  # lint-waiver: LW-260144 [PLR0913]; mirrors the executor's constructor, whose waiver LW-260143 explains the arguments.
    # > A parameter object would have this one factory as its only user and only rename
    # > the constructor's arguments.
    plan: TrustedEvaluationPlan,
    *,
    workspace: Path,
    sandbox: CommandRunner,
    git: GitTracker,
    model_requests: ModelRequestReconciler | None = None,
    threads: Threads,
    blocking: BlockingRunner,
) -> TrustedEvaluationExecutor:
    """Bind trusted evaluation mechanism to one owned workspace.

    Each operation runs on ``blocking``; ``threads`` makes the event that cancels it.
    """
    return RuntimeTrustedEvaluation(
        plan, workspace, sandbox, git, model_requests, threads, blocking
    )
