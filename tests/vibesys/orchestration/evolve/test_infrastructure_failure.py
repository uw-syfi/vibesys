"""An infrastructure benchmark failure is the machinery's, not the candidate's.

Evolve admits every measured candidate to the population, passed or failed, and failed
ones become lessons for later mutations. A candidate whose benchmark only ever failed in
infrastructure was never measured, so it must not take a population slot.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.orchestration.evolve.models import EvolveOptions, EvolveState
from vibesys.orchestration.evolve.plugin import PLUGIN
from vs_core.api import DEFAULT_MAX_MEASUREMENT_SUBMISSIONS
from vs_runtime.api import AccuracyEvaluation, BenchmarkEvaluation, BenchmarkFailureKind, RunFacts
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

_FACTS = RunFacts(
    domain_id="generic",
    objective="Increase throughput.",
    benchmark_configured=True,
)
_OPTIONS = EvolveOptions.model_validate(
    {
        "max_generations": 1,
        "children_per_generation": 1,
        "k_top_inspirations": 0,
        "k_random_inspirations": 0,
        "selection_temperature": 1.0,
        "frontier_bias": 0.7,
        "bootstrap_max_attempts": 1,
        "keep_deployments": False,
        "max_parallelism": 1,
    }
)
_MEASURED = BenchmarkEvaluation(executed=True, metric_name="throughput", metric_value=80.0)
_SEED_AND_CHILD = 2


def _respond(
    role: AgentRole,
    _history: tuple[str, ...],
    _message: str,
    _response: type[BaseModel] | None,
) -> object:
    if role.id == "implementer":
        return {
            "summary": "reduced allocation overhead",
            "hypothesis": "reuse avoids repeated allocation",
            "expected_behavior": "lower latency",
        }
    if role.id == "judge":
        return {"analysis": "candidate is sound", "feedback": "", "verdict": "pass"}
    return {
        "analysis": "measured steady state",
        "bottlenecks": "allocation",
        "suggestions": "reuse buffers",
        "perf_metric": 100.0,
        "perf_unit": "tokens/s",
    }


def _failed(kind: BenchmarkFailureKind) -> BenchmarkEvaluation:
    return BenchmarkEvaluation(executed=False, feedback="node lost", failure_kind=kind)


def _evolve(root: Path, child: list[BenchmarkEvaluation]) -> tuple[FakeRun, EvolveState]:
    """Run one generation whose seed measures cleanly and whose child reads as scripted."""

    async def scenario() -> tuple[FakeRun, EvolveState]:
        run = FakeRun(PLUGIN, project_root=root, facts=_FACTS, responder=_respond)
        run.evaluation.script_benchmark(_MEASURED, *child)
        try:
            await PLUGIN.orchestrate(run, _OPTIONS)
            state = await run.state.load(EvolveState)
            assert state is not None
            return run, state
        finally:
            await run.close()

    return asyncio.run(scenario())


@settings(max_examples=6)
@given(
    kind=st.sampled_from((BenchmarkFailureKind.INFRASTRUCTURE, BenchmarkFailureKind.AMBIGUOUS)),
    lost=st.integers(min_value=1, max_value=DEFAULT_MAX_MEASUREMENT_SUBMISSIONS - 1),
)
def test_a_candidate_whose_measurement_is_lost_is_measured_again(
    tmp_path_factory: pytest.TempPathFactory, kind: BenchmarkFailureKind, lost: int
) -> None:
    root = tmp_path_factory.mktemp("evolve")
    # An ambiguous failure is measured at most twice in all.
    lost = 1 if kind is BenchmarkFailureKind.AMBIGUOUS else lost

    run, state = _evolve(root, [*[_failed(kind)] * lost, _MEASURED])

    assert len(run.evaluation.benchmark_calls) == 1 + lost + 1
    assert [individual.passed for individual in state.population.individuals] == [True, True]


def test_a_candidate_never_measured_takes_no_population_slot(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("evolve")
    lost = [_failed(BenchmarkFailureKind.INFRASTRUCTURE)] * DEFAULT_MAX_MEASUREMENT_SUBMISSIONS

    run, state = _evolve(root, lost)

    assert len(run.evaluation.benchmark_calls) == 1 + DEFAULT_MAX_MEASUREMENT_SUBMISSIONS
    assert [individual.passed for individual in state.population.individuals] == [True]


def test_a_candidate_that_failed_on_its_own_account_is_admitted_as_failed(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("evolve")

    run, state = _evolve(root, [_failed(BenchmarkFailureKind.WORKLOAD)])

    assert len(run.evaluation.benchmark_calls) == _SEED_AND_CHILD
    assert [individual.passed for individual in state.population.individuals] == [True, False]


def test_a_candidate_whose_accuracy_is_lost_beyond_the_bound_takes_no_population_slot(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("evolve")

    async def scenario() -> tuple[FakeRun, EvolveState]:
        run = FakeRun(PLUGIN, project_root=root, facts=_FACTS, responder=_respond)
        lost = AccuracyEvaluation(
            executed=False,
            feedback="node lost",
            failure_kind=BenchmarkFailureKind.INFRASTRUCTURE,
        )
        # The seed's accuracy passes; the child's is lost until the bound.
        run.evaluation.script_accuracy(
            AccuracyEvaluation(executed=True), *[lost] * DEFAULT_MAX_MEASUREMENT_SUBMISSIONS
        )
        run.evaluation.script_benchmark(_MEASURED)
        try:
            await PLUGIN.orchestrate(run, _OPTIONS)
            state = await run.state.load(EvolveState)
            assert state is not None
            return run, state
        finally:
            await run.close()

    run, state = asyncio.run(scenario())

    assert len(run.evaluation.accuracy_calls) == 1 + DEFAULT_MAX_MEASUREMENT_SUBMISSIONS
    assert [individual.passed for individual in state.population.individuals] == [True]
