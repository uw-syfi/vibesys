"""The host benchmarks a fresh run's input tree before policy starts."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from vibesys.run.host import measure_input
from vs_runtime.api import (
    BenchmarkEvaluation,
    BenchmarkObjective,
    MetricDirection,
    OrchestrationPlugin,
    RunFacts,
    RunStatus,
)
from vs_runtime.api.testing import FakeRun

_OBJECTIVES = (BenchmarkObjective(name="throughput", direction=MetricDirection.MAXIMIZE),)
_READING = BenchmarkEvaluation(
    executed=True, metric_name="throughput", metric_value=800_000.0, row={"throughput": 800_000.0}
)


class _Options(BaseModel):
    pass


async def _orchestrate(_run: object, _options: BaseModel) -> RunStatus:
    return RunStatus.SUCCEEDED


def _plugin(*, measures: bool) -> OrchestrationPlugin:
    return OrchestrationPlugin(
        id="measuring" if measures else "plain",
        agents=(),
        options=_Options,
        orchestrate=_orchestrate,
        input_objectives=(lambda _options: _OBJECTIVES) if measures else None,
    )


def _fake(*, benchmark_configured: bool = True) -> FakeRun:
    run = FakeRun(
        _plugin(measures=True),
        facts=RunFacts(
            domain_id="generic", objective="Improve.", benchmark_configured=benchmark_configured
        ),
    )
    run.evaluation.script_benchmark(_READING)
    return run


def test_fresh_run_input_is_benchmarked_with_the_plugin_objectives() -> None:
    run = _fake()

    measured = asyncio.run(measure_input(run, _plugin(measures=True), _Options(), fresh=True))

    assert measured.facts.input_benchmark == _READING
    (call,) = run.evaluation.benchmark_calls
    assert call.workspace is run.workspaces.root
    assert call.objectives == _OBJECTIVES
    assert measured.workspaces is run.workspaces
    assert measured.evaluation is run.evaluation


@pytest.mark.parametrize(
    ("measures", "fresh", "benchmark_configured"),
    [
        pytest.param(True, False, True, id="resumed"),
        pytest.param(False, True, True, id="plugin-does-not-ask"),
        pytest.param(True, True, False, id="no-benchmark"),
    ],
)
def test_input_is_not_benchmarked_otherwise(
    *, measures: bool, fresh: bool, benchmark_configured: bool
) -> None:
    run = _fake(benchmark_configured=benchmark_configured)

    measured = asyncio.run(measure_input(run, _plugin(measures=measures), _Options(), fresh=fresh))

    assert measured is run
    assert measured.facts.input_benchmark is None
    assert run.evaluation.benchmark_calls == []
