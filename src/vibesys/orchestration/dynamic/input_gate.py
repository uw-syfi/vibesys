"""The run's input measurement and the gate it imposes on candidates."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.models import DynamicOptions, DynamicState, EvaluationResult
from vibesys.orchestration.metrics import Measurement, MetricComparison
from vs_runtime.api import BenchmarkObjective, MetricDirection, Run

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
        # Executed input-benchmark failures seen by this process. The first is
        # measured again before it is recorded as a property of the input.
        self._input_failures = 0

    def start(self) -> None:
        """Measure the input in the background unless a measurement is running.

        Candidates take far longer to reach a decision than the input takes to
        measure, so the first planning call need not wait for it. A failed
        measurement starts again at the next planning call or candidate decision.
        """
        if self._input_measurement is None or self._input_measurement.done():
            self._input_measurement = asyncio.create_task(self._measure())

    async def measured(self) -> None:
        """Wait for an input reading before judging a candidate against it.

        A measurement that ended without a reading starts again, so a decision
        made after a failed measurement still gets one more chance to be gated.
        """
        if self.state.baseline is None:
            self.start()
        if self._input_measurement is not None:
            await asyncio.shield(self._input_measurement)

    async def stop(self) -> None:
        """Cancel a running measurement; the run is ending.

        A measurement that already ended is still collected, so a stop that
        ended it is not reported as an unretrieved task exception.
        """
        task = self._input_measurement
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _measure(self) -> None:
        """Benchmark the input revision once per run; resume reuses the stored reading.

        Without it, the first accepted candidate has nothing to beat, so a
        regression could be adopted or built on. A failed measurement is
        retried before the next planning call rather than failing the run.
        """
        if (
            self.state.baseline is not None
            or self.state.winner_revision is not None
            or not self.run.facts.benchmark_configured
        ):
            return
        revision = self.run.workspaces.root.revision
        if revision is None:
            message = "dynamic orchestration requires a recorded root revision"
            raise RuntimeError(message)
        try:
            benchmark = await self.run.evaluation.benchmark(
                self.run.workspaces.root,
                objectives=benchmark_objectives(self.options),
            )
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-031002 [BLE001]; an input-measurement failure is retried before the next planning call.
            # > The runtime does not normalize evaluator transport failures to one
            # > exception type, so a narrower catch would let a transient Slurm or
            # > provider error end the search; propagating instead fails the run
            # > before any candidate work, for a measurement that can be retried.
            self.run.observations.note(f"dynamic input baseline measurement failed: {error}")
            return
        if not benchmark.passed and not benchmark.executed:
            # The benchmark never ran (provisioning or infrastructure), which
            # says nothing about the input; measure again before the next decision.
            self.run.observations.note(
                f"dynamic input baseline benchmark did not run: {benchmark.feedback}"
            )
            return
        if not benchmark.passed:
            self._input_failures += 1
            if self._input_failures == 1:
                # It ran beside agent work, so an OOM or a server start timeout
                # may be contention, not the input; measure again before
                # recording a verdict that disables the input gate.
                self.run.observations.note(
                    f"dynamic input baseline benchmark failed; measuring again: "
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
            # The benchmark ran and rejected the input twice (for example, it
            # lacks a capability the benchmark requires). That is a property
            # of the input, so it is recorded and never re-measured.
            self.run.observations.note(
                "dynamic input does not satisfy the benchmark; candidates need only a "
                "passing trusted benchmark"
            )

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
