"""In-memory :class:`~vibesys.orchestration.gates.GateExecutor` test double.

Scripts ``ctx.gates``' two trusted commands (accuracy check, benchmark
measure) without a sandbox or subprocess: a caller queues outcomes with
:meth:`FakeGateExecutor.script_accuracy`/:meth:`script_benchmark`; anything
unscripted returns a configurable default (an executed pass, by default) and
every call is recorded for direct assertion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vibesys.evaluators.gates import (
    AccuracyGateResult,
    BenchmarkContract,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
)

#: Outcome an unscripted accuracy check receives when no other default was set.
DEFAULT_ACCURACY_RESULT = AccuracyGateResult(
    command=None, passed=True, output="", feedback=None, executed=False
)

#: Outcome an unscripted benchmark measurement receives when no other default was set.
DEFAULT_BENCHMARK_RESULT = BenchmarkGateResult(
    command=None, output="", executed=False, outcome=FrameworkBenchmarkOutcome()
)


@dataclass(frozen=True, slots=True)
class FakeAccuracyCall:
    """One recorded call to :meth:`FakeGateExecutor.run_accuracy`."""

    process_id: str
    timeout_seconds: int | None
    execution_command: str | None
    round_label: str | None


@dataclass(frozen=True, slots=True)
class FakeBenchmarkCall:
    """One recorded call to :meth:`FakeGateExecutor.run_benchmark`."""

    process_id: str
    output_slug: str
    timeout_seconds: int | None
    execution_base: str | None
    round_label: str | None


@dataclass(slots=True)
class FakeGateExecutor:
    """Configurable in-memory double for `vibesys.orchestration.gates.GateExecutor`.

    Each of :attr:`accuracy_results`/:attr:`benchmark_results` is a queue:
    ``run_accuracy``/``run_benchmark`` pop one entry per call, in order,
    falling back to :attr:`default_accuracy_result`/
    :attr:`default_benchmark_result` once exhausted (or always, if the queue
    was never populated).
    """

    default_accuracy_result: AccuracyGateResult = DEFAULT_ACCURACY_RESULT
    default_benchmark_result: BenchmarkGateResult = DEFAULT_BENCHMARK_RESULT
    accuracy_results: list[AccuracyGateResult] = field(default_factory=list)
    benchmark_results: list[BenchmarkGateResult] = field(default_factory=list)
    accuracy_calls: list[FakeAccuracyCall] = field(default_factory=list)
    benchmark_calls: list[FakeBenchmarkCall] = field(default_factory=list)

    def script_accuracy(self, *results: AccuracyGateResult) -> None:
        """Queue one or more accuracy outcomes for successive calls."""
        self.accuracy_results.extend(results)

    def script_benchmark(self, *results: BenchmarkGateResult) -> None:
        """Queue one or more benchmark outcomes for successive calls."""
        self.benchmark_results.extend(results)

    def run_accuracy(
        self,
        ctx: object,
        *,
        process_id: str,
        timeout_seconds: int | None = None,
        execution_command: str | None = None,
        round_label: str | None = None,
    ) -> AccuracyGateResult:
        """Return the next queued accuracy outcome, or the default."""
        del ctx
        self.accuracy_calls.append(
            FakeAccuracyCall(
                process_id=process_id,
                timeout_seconds=timeout_seconds,
                execution_command=execution_command,
                round_label=round_label,
            )
        )
        if self.accuracy_results:
            return self.accuracy_results.pop(0)
        return self.default_accuracy_result

    def run_benchmark(  # noqa: PLR0913  # LW-040100 [PLR0913]; mirrors GateExecutor.run_benchmark's own field count.
        self,
        ctx: object,
        *,
        contract: BenchmarkContract,
        space: object,
        process_id: str,
        output_slug: str,
        execution_base: str | None = None,
        round_label: str | None = None,
    ) -> BenchmarkGateResult:
        """Return the next queued benchmark outcome, or the default."""
        del ctx, space
        self.benchmark_calls.append(
            FakeBenchmarkCall(
                process_id=process_id,
                output_slug=output_slug,
                timeout_seconds=contract.timeout_seconds,
                execution_base=execution_base,
                round_label=round_label,
            )
        )
        if self.benchmark_results:
            return self.benchmark_results.pop(0)
        return self.default_benchmark_result
