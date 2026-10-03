"""The run's input measurement and the gate it imposes on candidates."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from vibesys.metrics import Measurement, MetricComparison
from vibesys.orchestration.dynamic.models import (
    DynamicOptions,
    DynamicState,
    EvaluationResult,
    InputMeasurementAttempts,
)
from vs_runtime.api import BenchmarkFailureKind, BenchmarkObjective, MetricDirection, Run

_MAX_INPUT_MEASUREMENT_ATTEMPTS = 3

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def benchmark_objectives(options: DynamicOptions) -> tuple[BenchmarkObjective, ...]:
    """Return the configured objectives every trusted benchmark reports."""
    return tuple(
        BenchmarkObjective(name=item.name, direction=MetricDirection(item.direction))
        for item in options.metric_space.objectives
    )


class InputGate:
    """Measures the input (root) revision once per run and judges candidates against it.

    The measurement runs in the background beside the first workstreams; only
    candidate decisions and adoption wait for it (:meth:`measured`). The
    reading is stored in ``state.baseline`` and committed through ``commit``
    under ``lock``, the same lock that guards every other state change.
    """

    def __init__(
        self,
        run: Run,
        options: DynamicOptions,
        state: DynamicState,
        *,
        lock: asyncio.Lock,
        commit: Callable[[str], Awaitable[None]],
    ) -> None:
        """Bind the gate to one run's state and its commit path."""
        self.run = run
        self.options = options
        self.state = state
        self._lock = lock
        self._commit_state = commit
        self._input_measurement: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Measure the input in the background unless a measurement is running.

        Candidates take far longer to reach a decision than the input takes to
        measure, so the first planning call need not wait for it. A failed
        infrastructure measurement starts again within its durable submission budget.
        """
        if self._input_measurement is None or self._input_measurement.done():
            self._input_measurement = asyncio.create_task(self._measure())

    async def measured(self) -> None:
        """Wait for an input reading before judging a candidate against it.

        Infrastructure failures may retry within the durable submission budget.
        A recorded workload rejection is reused without another submission.
        """
        self.start()
        if self._input_measurement is not None:
            await asyncio.shield(self._input_measurement)

    async def stop(self) -> None:
        """Cancel a running measurement; the run is ending."""
        task = self._input_measurement
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _measure(self) -> None:
        """Benchmark the input revision once per run; resume reuses the stored reading.

        Without it, the first accepted candidate has nothing to beat, so a
        regression could be adopted or built on. A failed measurement is
        retried within a durable bound only when it is not a workload rejection.
        """
        if self.state.winner_revision is not None or not self.run.facts.benchmark_configured:
            return
        revision = self.run.workspaces.root.trusted_input_baseline
        if revision is None:
            revision = self.run.workspaces.root.revision
        if revision is None:
            message = "dynamic orchestration requires a recorded root revision"
            raise RuntimeError(message)
        if not await self._claim_attempt(revision):
            return
        try:
            benchmark = await self.run.evaluation.benchmark(
                self.run.workspaces.root,
                objectives=benchmark_objectives(self.options),
            )
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-031002 [BLE001]; evaluator exceptions consume the durable bounded retry budget.
            # > The runtime does not normalize evaluator transport failures to one
            # > exception type, so a narrower catch would let a transient Slurm or
            # > provider error end the search; propagating instead fails the run
            # > before any candidate work, for a measurement that can be retried.
            self.run.observations.note(f"dynamic input baseline measurement failed: {error}")
            return
        if not benchmark.passed and benchmark.failure_kind is not BenchmarkFailureKind.WORKLOAD:
            # Missing classification, including old cached evidence, is not a
            # verdict on the input. Retry conservatively within the same bound.
            self.run.observations.note(
                f"dynamic input baseline benchmark did not run or failed in infrastructure: "
                f"{benchmark.feedback}"
            )
            return
        async with self._lock:
            self.state.baseline = EvaluationResult(
                revision=revision,
                benchmark_passed=benchmark.passed,
                benchmark_feedback=benchmark.feedback,
                metric_name=benchmark.metric_name,
                metric_value=benchmark.metric_value,
                metric_direction=benchmark.metric_direction,
                metric_unit=benchmark.metric_unit,
                metrics=dict(benchmark.row or {}),
                partial_measurement=benchmark.partial_measurement,
            )
            await self._commit_state("dynamic: measure input baseline")
        if not benchmark.passed:
            # A typed workload rejection is a property of this input revision,
            # so the failed reading is durable and never re-measured.
            self.run.observations.note(
                "dynamic input does not satisfy the benchmark; candidates need only a "
                "passing trusted benchmark"
            )

    async def _claim_attempt(self, revision: str) -> bool:
        """Durably reserve one submission before executing any external work."""
        async with self._lock:
            measurement = self.state.input_measurement
            if measurement is not None and measurement.revision != revision:
                self.state.baseline = None
                measurement = None
            if self.state.baseline is not None:
                return False
            if measurement is None:
                measurement = InputMeasurementAttempts(revision=revision)
                self.state.input_measurement = measurement
            if measurement.attempts >= _MAX_INPUT_MEASUREMENT_ATTEMPTS:
                return False
            measurement.attempts += 1
            # Commit before submitting: a crash cannot reset the retry budget.
            await self._commit_state("dynamic: submit input measurement")
        return True

    def admits(self, metrics: dict[str, float], headline: Measurement | None) -> bool:
        """Return whether a candidate materially beats the measured input.

        With configured objectives the candidate must dominate the input row
        (no worse within noise on any axis, better on one). Without them the
        headline must be better than the input's beyond noise. A missing or
        failed input reading gates nothing.
        """
        baseline = self.state.baseline
        if baseline is None or baseline.benchmark_passed is not True:
            return True
        space = self.options.metric_space
        if space.objectives:
            if not space.complete(baseline.metrics):
                return True
            return space.dominates(metrics, baseline.metrics)
        if baseline.metric_name is None or baseline.metric_value is None or headline is None:
            return True
        comparison = space.compare(
            headline,
            Measurement(
                metric=baseline.metric_name,
                value=baseline.metric_value,
                direction=(
                    baseline.metric_direction.value
                    if baseline.metric_direction is not None
                    else None
                ),
            ),
        )
        return comparison in {MetricComparison.BETTER, MetricComparison.INCOMPARABLE}


__all__ = ["InputGate", "benchmark_objectives"]
