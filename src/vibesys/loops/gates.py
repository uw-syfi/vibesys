"""Framework-owned correctness and measurement gates shared by optimization loops."""

from __future__ import annotations

import contextlib
import json
import math
import shlex
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from vibesys.events import (
    CoreEventType,
    EventStatus,
    GateFinishedData,
    GateKind,
    GateStartedData,
    SubprocessOutputData,
)
from vibesys.render.sink import output_sink
from vs_evaluator_protocol.api import (
    Hello,
    ProtocolError,
    check_objectives,
    parse_records,
    read_measurement,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vibesys.evaluators.input_manifest import BenchmarkResult
    from vibesys.loops.metrics import MetricSpace, Objective
    from vibesys.run import LoopContext
    from vs_sandbox.api import SandboxExecutionResult

# Truncation lengths for gate failure output. All three values are defined
# here so that the logged window, the agent-feedback window, and the record
# window stay consistent across every gate and loop call site.
GATE_LOG_TAIL_CHARS = 1000
GATE_FEEDBACK_TAIL_CHARS = 4000
GATE_RECORD_TAIL_CHARS = 8000


def emit_gate_started(
    gate: GateKind,
    *,
    recipe: str | None = None,
    command: str | None = None,
    round_label: str | None = None,
) -> None:
    """Publish that one framework gate began evaluating a candidate.

    Every emission must be balanced by exactly one :func:`emit_gate_finished`
    for the same gate (and recipe), including reused and early-failure paths.
    """
    output_sink().emit(
        CoreEventType.GATE_STARTED,
        data=GateStartedData(gate=gate, recipe=recipe, command=command),
        status=EventStatus.ACTIVE,
        round_label=round_label,
    )


def emit_gate_finished(
    data: GateFinishedData,
    *,
    passed: bool,
    round_label: str | None = None,
) -> None:
    """Publish one framework gate outcome; envelope status carries the verdict."""
    output_sink().emit(
        CoreEventType.GATE_FINISHED,
        data=data,
        status=EventStatus.COMPLETED if passed else EventStatus.FAILED,
        round_label=round_label,
    )


def framework_command_timeout(ctx: LoopContext, timeout_seconds: int | None) -> int | None:
    """Add environment-owned setup time without weakening the command's own budget.

    Environment-owned Modal/SkyPilot deployment and readiness happens before the
    trusted command runs, so charging that setup to the command's declared budget
    can time it out before it receives its configured execution window. Extend the
    budget by the run environment's ``framework_setup_timeout_seconds`` allowance.
    """
    if timeout_seconds is None:
        return None
    view = getattr(ctx, "run_environment_view", None)
    setup_timeout = getattr(view, "framework_setup_timeout_seconds", 0)
    if not isinstance(setup_timeout, int):
        setup_timeout = 0
    return timeout_seconds + setup_timeout


@dataclass(frozen=True)
class AccuracyGateResult:
    """Outcome of running the immutable accuracy command for a candidate."""

    command: str | None
    passed: bool
    output: str
    feedback: str | None
    executed: bool


def run_accuracy_gate(
    ctx: LoopContext,
    *,
    process_id: str,
    timeout_seconds: int | None = None,
    execution_command: str | None = None,
    round_label: str | None = None,
) -> AccuracyGateResult:
    """Run the trusted accuracy command without delegating acceptance to an agent."""
    changed = ctx.trusted_input_changes()
    command = ctx.judge_accuracy_command
    if changed:
        output = "Evaluator-owned files were modified: " + ", ".join(changed)
        emit_gate_started(GateKind.ACCURACY, command=command or None, round_label=round_label)
        emit_gate_finished(
            GateFinishedData(
                gate=GateKind.ACCURACY,
                output_tail=output[-GATE_LOG_TAIL_CHARS:],
            ),
            passed=False,
            round_label=round_label,
        )
        return AccuracyGateResult(
            command=command,
            passed=False,
            output=output,
            feedback=output,
            executed=False,
        )
    if not command:
        return AccuracyGateResult(
            command=None,
            passed=True,
            output="",
            feedback=None,
            executed=False,
        )

    emit_gate_started(GateKind.ACCURACY, command=command, round_label=round_label)
    command_to_execute = execution_command or command
    try:
        if timeout_seconds is None:
            result = ctx.judge_backend.execute(command_to_execute)
        else:
            result = ctx.judge_backend.execute(command_to_execute, timeout=timeout_seconds)
        output = result.output.strip()
        passed = result.exit_code == 0
        _publish_subprocess_output(ctx, process_id=process_id, result=result)
    except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010263 [BLE001]; backend execution errors become accuracy feedback rather than aborting the run.
        output = f"accuracy command could not be executed: {exc}"
        passed = False

    changed_after_execution = ctx.trusted_input_changes()
    if changed_after_execution:
        mutation = "Evaluator-owned files changed during accuracy execution: " + ", ".join(
            changed_after_execution
        )
        output = f"{output}\n{mutation}".strip()
        passed = False

    if passed:
        emit_gate_finished(
            GateFinishedData(gate=GateKind.ACCURACY),
            passed=True,
            round_label=round_label,
        )
        feedback = None
    else:
        emit_gate_finished(
            GateFinishedData(
                gate=GateKind.ACCURACY,
                output_tail=output[-GATE_LOG_TAIL_CHARS:],
            ),
            passed=False,
            round_label=round_label,
        )
        feedback = f"Framework accuracy gate failed.\n{output[-GATE_FEEDBACK_TAIL_CHARS:]}"

    return AccuracyGateResult(
        command=command,
        passed=passed,
        output=output,
        feedback=feedback,
        executed=True,
    )


def _publish_subprocess_output(
    ctx: LoopContext,
    *,
    process_id: str,
    result: SandboxExecutionResult,
    process_kind: str = "accuracy_checker",
) -> None:
    streams = (("stdout", result.stdout), ("stderr", result.stderr))
    for stream, content in streams:
        if content:
            ctx.events.emit(
                CoreEventType.SUBPROCESS_OUTPUT,
                data=SubprocessOutputData(
                    process_id=process_id,
                    process_kind=process_kind,
                    stream=stream,
                    content=content,
                ),
            )


FRAMEWORK_BENCHMARK_MARKER = "__VIBESYS_FRAMEWORK_BENCHMARK_JSON__"
FRAMEWORK_BENCHMARK_END_MARKER = "__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__"

# The flag every result-protocol evaluator registers for its output file; see
# ``OutputFlag`` in the evaluator SDK (``sdk/vs-evaluator/vseval/schema.go``).
PROTOCOL_OUTPUT_FLAG = "--vs-output"

# The SkyPilot bridge allowlists framework result artifacts by this path
# shape (``_FRAMEWORK_ARTIFACT`` in ``vibesys.skypilot.bridge``); the nonce
# appended per invocation must stay within its ``[a-zA-Z0-9._-]`` alphabet.
_BENCHMARK_OUTPUT_PREFIX = "/tmp/vibesys-framework-benchmark-"  # noqa: S108  # lint-waiver: LW-010207 [S108]; evaluator and remote bridge share this fixed artifact path protocol.


@dataclass(frozen=True, slots=True)
class FrameworkBenchmarkOutcome:
    """What one framework benchmark run reported.

    ``feedback`` is set exactly when the run failed and the round must retry.
    On success ``metric_name`` and ``metric_value`` carry the headline scalar
    both result contracts produce, and ``row`` carries the complete validated
    metric row, which only the evaluator result protocol reports.
    """

    feedback: str | None = None
    metric_name: str | None = None
    metric_value: float | None = None
    metric_direction: Literal["max", "min"] | None = None
    metric_unit: str | None = None
    row: Mapping[str, float] | None = None


@dataclass(frozen=True)
class BenchmarkContract:
    """The manifest-declared trusted benchmark result contract, if any.

    Bundles what a loop must thread from the input bundle down to each
    candidate evaluation: at most one of the two contract forms plus the
    benchmark command's own time budget.
    """

    result_spec: BenchmarkResult | None = None
    result_protocol: Literal[2] | None = None
    timeout_seconds: int | None = None

    @property
    def declared(self) -> bool:
        """True when either contract form is configured."""
        return self.result_spec is not None or self.result_protocol is not None

    @property
    def output_argument(self) -> str | None:
        """The flag the benchmark takes its result path under, if any.

        The scalar contract declares its own flag; the result protocol fixes
        one for every evaluator. The gate appends it to the benchmark command,
        and the sandbox is told the same value so it can allowlist the result
        artifact, so both must read it from here rather than restating it.
        """
        if self.result_spec is not None:
            return self.result_spec.json_argument
        if self.result_protocol is not None:
            return PROTOCOL_OUTPUT_FLAG
        return None


@dataclass(frozen=True)
class BenchmarkGateResult:
    """Outcome of running the benchmark result contract for a candidate.

    ``executed`` is False when the gate never reached a recordable conclusion:
    no contract is declared, or the contract is declared without a benchmark
    command. Loop-side bookkeeping (progress notes, snapshots) applies only to
    executed gates.
    """

    command: str | None
    output: str
    executed: bool
    outcome: FrameworkBenchmarkOutcome

    @property
    def passed(self) -> bool:
        """Whether the round may keep this benchmark reading.

        Derived rather than stored: ``FrameworkBenchmarkOutcome.feedback`` is
        set exactly when the run failed, so a stored boolean beside it would be
        a second writer of the same fact.
        """
        return self.outcome.feedback is None


@dataclass(frozen=True)
class _ParsedBenchmarkOutput:
    output: str
    passed: bool
    metric_name: str | None
    metric_value: float | None
    metric_direction: Literal["max", "min"] | None
    metric_unit: str | None
    row: Mapping[str, float] | None


def read_protocol_benchmark(
    text: str, *, objectives: Sequence[Objective]
) -> FrameworkBenchmarkOutcome:
    """Turn a recovered evaluator record stream into a benchmark outcome.

    Never raises for a bad stream: an invalid stream, a structured evaluator
    failure, and an undecidable headline metric all become round feedback.
    Objectives belong to the task rather than to the evaluator, so the check
    that the evaluator declares every optimized metric happens here and not in
    the protocol reader.
    """
    hello: Hello | None = None
    try:
        records = parse_records(text)
        hello = next((record for record in records if isinstance(record, Hello)), None)
        measurement = read_measurement(records)
        if objectives and hello is not None:
            check_objectives(hello, {objective.name for objective in objectives})
    except ProtocolError as error:
        return FrameworkBenchmarkOutcome(feedback=_protocol_feedback(error, hello))
    if measurement.values is None:
        return FrameworkBenchmarkOutcome(
            feedback=f"benchmark evaluator reported a failure: {measurement.failure}"
        )
    outcome = _select_headline_metric(measurement.values, objectives)
    if outcome.metric_name is not None:
        configured = next(
            (item.direction for item in objectives if item.name == outcome.metric_name),
            None,
        )
        spec = (
            hello.metrics[outcome.metric_name]
            if hello is not None and outcome.metric_name in hello.metrics
            else None
        )
        # The unit is the evaluator's alone: ``objectives.toml`` names axes,
        # it does not say what they are measured in.
        outcome = replace(
            outcome,
            metric_direction=configured or (spec.direction if spec is not None else None),
            metric_unit=spec.unit if spec is not None else None,
        )
    return outcome


def _protocol_feedback(error: ProtocolError, hello: Hello | None) -> str:
    """Render a rejected record stream, naming its reason code and metrics."""
    declared = ", ".join(sorted(hello.metrics)) if hello is not None else "(none declared)"
    return f"invalid benchmark result [{error.code}]: {error}; evaluator declares: {declared}"


def _select_headline_metric(
    values: Mapping[str, float], objectives: Sequence[Objective]
) -> FrameworkBenchmarkOutcome:
    """Select the back-compat headline scalar out of a complete metric row.

    The first configured objective names it. With no objectives configured a
    single-metric evaluator is unambiguous; anything else is a task
    configuration error to report rather than a row to guess through.
    """
    if objectives:
        name = objectives[0].name
        return FrameworkBenchmarkOutcome(metric_name=name, metric_value=values[name], row=values)
    if len(values) == 1:
        name, value = next(iter(values.items()))
        return FrameworkBenchmarkOutcome(metric_name=name, metric_value=value, row=values)
    return FrameworkBenchmarkOutcome(
        feedback=(
            f"benchmark evaluator reported metrics {', '.join(sorted(values))} but the task "
            "configures no objectives, so no headline metric is defined; declare the optimized "
            "metrics in objectives.toml"
        )
    )


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


def _parse_json_metric(encoded: str, metric: str) -> float:
    """Parse one finite numeric metric from a JSON result payload."""
    payload = json.loads(encoded.strip())
    # A result object owns its top-level metric. Rich benchmark reports may
    # repeat that name in per-trial diagnostics, which must not make the
    # declared aggregate ambiguous. Preserve recursive lookup for legacy
    # list-shaped result payloads.
    if isinstance(payload, dict) and metric in payload:
        values = [payload[metric]]
    else:
        values = _metric_values(payload, metric)
    if len(values) != 1:
        message = f"expected exactly one {metric!r} field, found {len(values)}"
        raise ValueError(message)
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, int | float):
        message = f"{metric!r} is not numeric"
        raise TypeError(message)
    metric_value = float(value)
    if not math.isfinite(metric_value):
        message = f"{metric!r} is not finite"
        raise ValueError(message)
    return metric_value


def _check_output_slug(output_slug: str) -> None:
    """Reject a slug that would move the result file out of its directory.

    The slug reaches the shell inside a quoted path, so this is not an
    injection guard: it keeps a caller from silently writing (and removing)
    a file outside ``_BENCHMARK_OUTPUT_PREFIX``'s directory, which the
    SkyPilot artifact allowlist and the cleanup both assume.
    """
    if not output_slug:
        message = "output_slug must not be empty"
        raise ValueError(message)
    if "/" in output_slug or ".." in output_slug:
        _exception_message = f"output_slug must be a single path segment, got {output_slug!r}"
        raise ValueError(_exception_message)


def _parse_benchmark_output(
    output: str,
    *,
    result_spec: BenchmarkResult | None,
    objectives: Sequence[Objective],
) -> _ParsedBenchmarkOutput:
    """Parse the framed result after a successful benchmark command."""
    metric_name = result_spec.metric if result_spec is not None else None
    metric_value: float | None = None
    metric_direction: Literal["max", "min"] | None = None
    metric_unit: str | None = None
    row: Mapping[str, float] | None = None
    _, marker, framed = output.rpartition(FRAMEWORK_BENCHMARK_MARKER)
    encoded, end_marker, _ = framed.partition(FRAMEWORK_BENCHMARK_END_MARKER)
    passed = True
    if not marker or not end_marker:
        output = f"{output}\nbenchmark output did not include its result JSON".strip()
        passed = False
    elif result_spec is None:
        protocol_outcome = read_protocol_benchmark(encoded, objectives=objectives)
        if protocol_outcome.feedback is not None:
            output = f"{output}\n{protocol_outcome.feedback}".strip()
            passed = False
        else:
            metric_name = protocol_outcome.metric_name
            metric_value = protocol_outcome.metric_value
            metric_direction = protocol_outcome.metric_direction
            metric_unit = protocol_outcome.metric_unit
            row = protocol_outcome.row
    else:
        try:
            metric_value = _parse_json_metric(encoded, result_spec.metric)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            output = f"{output}\ninvalid benchmark result: {exc}".strip()
            passed = False
    return _ParsedBenchmarkOutput(
        output=output,
        passed=passed,
        metric_name=metric_name,
        metric_value=metric_value,
        metric_direction=metric_direction,
        metric_unit=metric_unit,
        row=row,
    )


def _execute_benchmark_command(
    ctx: LoopContext,
    command: str,
    *,
    output_path: str,
    process_id: str,
    timeout_seconds: int | None,
) -> tuple[str, bool, list[str]]:
    """Run one benchmark command, capture output, and clean its result file."""
    changed_before_execution = ctx.trusted_input_changes()
    if changed_before_execution:
        output = "Evaluator-owned files were modified: " + ", ".join(changed_before_execution)
        return output, False, changed_before_execution

    try:
        if timeout_seconds is None:
            result = ctx.judge_backend.execute(command)
        else:
            result = ctx.judge_backend.execute(command, timeout=timeout_seconds)
        output = result.output.strip()
        passed = result.exit_code == 0
        _publish_subprocess_output(
            ctx,
            process_id=process_id,
            result=result,
            process_kind="benchmark",
        )
    except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010264 [BLE001]; backend execution errors become benchmark feedback rather than aborting the run.
        output = f"benchmark command could not be executed: {exc}"
        passed = False
    finally:
        # Remove the per-invocation transport artifact on every exit path
        # (success, nonzero exit, timeout, malformed output). The result is
        # already recovered from stdout, so the file is dead weight; leaving
        # it leaks one JSON per benchmark on hosts where /tmp persists for
        # weeks. Best-effort: a cleanup failure must not mask the result.
        #
        # Limitation on the timeout path: the Docker and Modal backends
        # report a timeout as exit code -1 rather than raising, so this
        # cleanup runs while the timed-out benchmark may still be alive in
        # the sandbox and can write its result file afterwards. The nonce
        # keeps that orphan from being read by any later invocation -- the
        # correctness property this gate owns -- but it can survive as a
        # leaked file until the sandbox is torn down.
        with contextlib.suppress(Exception):
            ctx.judge_backend.execute(f"rm -f -- {shlex.quote(output_path)}")
    return output, passed, []


def run_benchmark_gate(  # noqa: PLR0913  # lint-waiver: LW-011116 [PLR0913]; Process ID feeds subprocess events, slug names the nonce file, execution_base overrides the trusted command, and round_label scopes gate events; no caller object contains all four.
    ctx: LoopContext,
    *,
    contract: BenchmarkContract,
    space: MetricSpace,
    process_id: str,
    output_slug: str,
    execution_base: str | None = None,
    round_label: str | None = None,
) -> BenchmarkGateResult:
    """Run and parse an opt-in trusted benchmark result contract.

    ``contract`` describes the declared scalar or protocol result format and
    timeout; ``space`` supplies the configured metric axes.

    The result file path carries ``output_slug`` plus a per-invocation nonce,
    and is removed before the benchmark runs, so a concurrent or earlier run
    (including another user on a shared ``/tmp``) can never satisfy this
    invocation's ``cat``: a benchmark that does not write its own fresh result
    fails instead of silently reporting a stale one.

    ``output_slug`` names the caller's invocation (round and retry, or a
    candidate id) and is interpolated into that path, so it must stay a single
    path segment.

    Raises:
        ValueError: when ``output_slug`` is empty or would escape the result
            directory.
    """
    _check_output_slug(output_slug)
    if not contract.declared:
        return BenchmarkGateResult(
            command=None,
            output="",
            executed=False,
            outcome=FrameworkBenchmarkOutcome(),
        )

    base_command = ctx.judge_benchmark_command
    if not base_command:
        feedback = "Benchmark result contract is configured without a benchmark command."
        return BenchmarkGateResult(
            command=None,
            output=feedback,
            executed=False,
            outcome=FrameworkBenchmarkOutcome(feedback=feedback),
        )

    output_path = f"{_BENCHMARK_OUTPUT_PREFIX}{output_slug}-{uuid.uuid4().hex[:12]}.json"
    # Not None: `contract.declared` is true, so one of the two forms set it.
    output_argument = contract.output_argument
    if output_argument is None:
        message = "declared benchmark result contract has no output argument"
        raise RuntimeError(message)
    # The markers recover the result file through stdout, which is what makes
    # the contract work for remote execution. Both contracts share that
    # transport; only the recovered text is parsed differently.
    command = (
        f"rm -f -- {shlex.quote(output_path)}"
        f" && {execution_base or base_command}"
        f" {shlex.quote(output_argument)} {shlex.quote(output_path)}"
        f" && printf '\\n{FRAMEWORK_BENCHMARK_MARKER}\\n'"
        f" && cat {shlex.quote(output_path)}"
        f" && printf '\\n{FRAMEWORK_BENCHMARK_END_MARKER}\\n'"
    )
    emit_gate_started(GateKind.BENCHMARK, command=base_command, round_label=round_label)
    output, passed, changed_before_execution = _execute_benchmark_command(
        ctx,
        command,
        output_path=output_path,
        process_id=process_id,
        timeout_seconds=contract.timeout_seconds,
    )

    metric_name: str | None = (
        contract.result_spec.metric if contract.result_spec is not None else None
    )
    metric_value: float | None = None
    metric_direction: Literal["max", "min"] | None = None
    metric_unit: str | None = None
    row: Mapping[str, float] | None = None
    if passed:
        parsed = _parse_benchmark_output(
            output,
            result_spec=contract.result_spec,
            objectives=space.objectives,
        )
        output = parsed.output
        passed = parsed.passed
        metric_name = parsed.metric_name
        metric_value = parsed.metric_value
        metric_direction = parsed.metric_direction
        metric_unit = parsed.metric_unit
        row = parsed.row

    changed = [] if changed_before_execution else ctx.trusted_input_changes()
    if changed:
        output = (
            f"{output}\nEvaluator-owned files changed during benchmark execution: "
            + ", ".join(changed)
        ).strip()
        passed = False
        metric_value = None
        row = None

    if passed:
        has_metric = metric_name is not None and metric_value is not None
        emit_gate_finished(
            GateFinishedData(
                gate=GateKind.BENCHMARK,
                metric=metric_name if has_metric else None,
                value=metric_value if has_metric else None,
                # Preserve the historical fallback: the scalar contract declares
                # no unit, so the metric name stands in for it.
                unit=(metric_unit or metric_name) if has_metric else None,
            ),
            passed=True,
            round_label=round_label,
        )
        outcome = FrameworkBenchmarkOutcome(
            metric_name=metric_name,
            metric_value=metric_value,
            # The protocol path resolves the direction while reading the row
            # (configured objective first, then the evaluator's declaration);
            # the legacy scalar contract keeps its historical maximize default.
            metric_direction=(
                metric_direction
                or next(
                    (item.direction for item in space.objectives if item.name == metric_name),
                    None,
                )
                or ("max" if contract.result_spec is not None else None)
            ),
            # Only the result protocol declares a unit; the scalar contract
            # names a metric and nothing else, so its unit stays unknown.
            metric_unit=metric_unit,
            row=row,
        )
    else:
        emit_gate_finished(
            GateFinishedData(
                gate=GateKind.BENCHMARK,
                output_tail=output[-GATE_LOG_TAIL_CHARS:],
            ),
            passed=False,
            round_label=round_label,
        )
        outcome = FrameworkBenchmarkOutcome(
            feedback=f"Framework benchmark failed.\n{output[-GATE_FEEDBACK_TAIL_CHARS:]}"
        )
    return BenchmarkGateResult(
        command=base_command,
        output=output,
        executed=True,
        outcome=outcome,
    )
