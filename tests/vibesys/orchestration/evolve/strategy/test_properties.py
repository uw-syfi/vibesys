"""Properties of the evolve strategy beyond the shared conformance suite (design note 9.6)."""

from __future__ import annotations

import json
from collections import deque
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve.strategy._executors import Executors
from tests.vibesys.orchestration.evolve.strategy._replies import mutated, profiled, reviewed
from tests.vibesys.orchestration.evolve.strategy._run import config, run_shell

from vibesys.orchestration.evolve.strategy.api import EvolveStrategyState
from vs_core.api import (
    DispatchTurn,
    Measure,
    ProposeWinner,
    RequestTurn,
    RevisionRef,
    Settle,
    StartAttempt,
    Stop,
    Withdraw,
)

if TYPE_CHECKING:
    from vs_core.testing.drive import Answer
    from vs_runtime.api.testing import Run

RUNS = settings(max_examples=3, deadline=None, derandomize=True, database=None)


def _state(trace: Run) -> EvolveStrategyState:
    assert isinstance(trace.state, EvolveStrategyState)
    return trace.state


def _passing_world(*, passes: tuple[bool, ...]) -> Executors:
    return Executors(
        replies=lambda role, key, _serial: (
            mutated(f"change of {key}")
            if role == "mutator"
            else reviewed(passed=passes[sum(map(ord, key)) % len(passes)])
            if role == "judge"
            else profiled()
        ),
        benchmark=lambda commit: float(int(commit[:4], 16) % 90 + 10),
    )


@RUNS
@given(
    passes=st.lists(st.booleans(), min_size=1, max_size=4).map(tuple),
    parallelism=st.integers(1, 3),
    children=st.integers(1, 3),
)
def test_e4_individual_ids_are_unique_and_dense(
    passes: tuple[bool, ...], parallelism: int, children: int
) -> None:
    trace = run_shell(
        _passing_world(passes=(True, *passes)),
        max_parallelism=parallelism,
        children_per_generation=children,
        bootstrap_max_attempts=3,
    )
    population = _state(trace).population
    assert population is not None
    assert [item.id for item in population.individuals] == list(
        range(1, len(population.individuals) + 1)
    )
    assert population.next_id == len(population.individuals) + 1


def test_e6_a_cold_start_builds_on_the_previous_attempts_work_in_progress() -> None:
    executors = Executors(
        mutator=deque(mutated(f"seed {n}") for n in range(3)),
        # The first two cold starts are rejected, the third passes.
        judge=deque(
            [reviewed(passed=False), reviewed(passed=False), reviewed(passed=True)]
            + [reviewed()] * 8
        ),
    )
    trace = run_shell(
        executors, bootstrap_max_attempts=3, max_generations=1, children_per_generation=1
    )
    starts = [item for item in trace.decisions if isinstance(item, StartAttempt)][:3]
    settles = [
        item.disposition
        for item in trace.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ][:2]
    assert [item.retention for item in settles] == ["wip", "wip"]
    assert starts[1].workspace.base == settles[0].candidate
    assert starts[2].workspace.base == settles[1].candidate


def test_e6_a_cold_start_that_never_passes_ends_without_a_selection() -> None:
    executors = Executors(
        mutator=deque(mutated() for _ in range(2)),
        judge=deque(reviewed(passed=False) for _ in range(2)),
    )
    trace = run_shell(executors, bootstrap_max_attempts=2)
    final = trace.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "failure"
    assert not any(isinstance(item, ProposeWinner) for item in trace.decisions)
    assert sum(isinstance(item, StartAttempt) for item in trace.decisions) == 2


def test_a_run_without_a_profiler_asks_no_profiler_turn() -> None:
    trace = run_shell(_passing_world(passes=(True,)), max_generations=1)
    roles = {
        item.turn.session.role_id.root for item in trace.decisions if isinstance(item, RequestTurn)
    }
    assert roles == {"evolve-mutator", "evolve-judge"}


def test_a_profiler_turn_is_asked_once_per_passing_child_when_the_run_has_one() -> None:
    trace = run_shell(
        _passing_world(passes=(True,)),
        max_generations=1,
        children_per_generation=2,
        profiler_id="linux_cpu",
        profiler_support_name="linux_cpu_profiler",
        profiler_mcp_name="vibesys-linux-cpu-profiler",
    )
    profiles = [
        item
        for item in trace.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root == "evolve-profiler"
    ]
    assert len(profiles) == 3  # the cold start and two children


def test_m2_a_failing_profiler_never_fails_the_child() -> None:
    executors = Executors(
        mutator=deque(mutated() for _ in range(2)),
        judge=deque(reviewed() for _ in range(2)),
        # An unparsable reply and its unparsable correction, for each of the two children.
        profiler=deque(['{"analysis": 1}'] * 4),
    )
    trace = run_shell(
        executors,
        max_generations=1,
        children_per_generation=1,
        profiler_id="linux_cpu",
        profiler_support_name="linux_cpu_profiler",
        profiler_mcp_name="vibesys-linux-cpu-profiler",
    )
    state = _state(trace)
    assert state.population is not None
    assert all(item.passed for item in state.population.individuals)
    assert any(note.startswith("profiler:") for note in state.notes)
    assert isinstance(trace.decisions[-1], Stop)
    final = trace.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"


def test_a_failed_review_skips_the_trusted_gates_and_keeps_the_feedback() -> None:
    executors = Executors(
        mutator=deque(mutated() for _ in range(3)),
        judge=deque([reviewed(), reviewed(passed=False, feedback="tests fail"), reviewed()]),
    )
    trace = run_shell(executors, max_generations=1, children_per_generation=2)
    state = _state(trace)
    assert state.population is not None
    failed = [item for item in state.population.individuals if not item.passed]
    assert [item.feedback for item in failed] == ["tests fail"]
    measured = {
        item.plan.candidate.revision_id.root
        for item in trace.decisions
        if isinstance(item, Measure) and isinstance(item.plan.candidate, RevisionRef)
    }
    assert len(measured) == 2  # the failed child was never measured


def test_a_candidate_the_machinery_could_not_measure_earns_no_individual() -> None:
    executors = Executors(
        mutator=deque(mutated() for _ in range(4)),
        judge=deque(reviewed() for _ in range(4)),
        # Every official measurement ends without evidence, so no cold start can be admitted.
        infrastructure_failure=lambda plan: plan.purpose == "official",
    )
    trace = run_shell(executors, bootstrap_max_attempts=2, max_generations=1)
    state = _state(trace)
    assert state.population is not None
    assert state.population.individuals == ()
    assert state.bootstrap_spent == 2
    assert isinstance(trace.decisions[-1], Stop)
    assert trace.decisions[-1].result.outcome == "failure"


def test_the_state_grows_linearly_in_individuals() -> None:
    sizes = []
    for generations in (1, 2, 3):
        trace = run_shell(
            _passing_world(passes=(True,)),
            max_generations=generations,
            children_per_generation=2,
        )
        population = _state(trace).population
        assert population is not None
        sizes.append(
            (len(population.individuals), len(json.dumps(population.model_dump(mode="json"))))
        )
    (n1, s1), (n2, s2), (n3, s3) = sizes
    per_individual = (s2 - s1) / (n2 - n1)
    assert abs((s3 - s2) / (n3 - n2) - per_individual) < 0.2 * per_individual


def test_a_dispatch_is_never_repeated_for_one_invocation() -> None:
    trace = run_shell(_passing_world(passes=(True,)), max_generations=2)
    dispatched = [
        item.turn.invocation_id.root for item in trace.decisions if isinstance(item, RequestTurn)
    ]
    assert len(dispatched) == len(set(dispatched))
    assert DispatchTurn is not None
    assert config().total_attempts >= 1


@RUNS
@given(
    unit=st.none() | st.sampled_from(("tokens/s", "calls/s")),
    profiler_replies=st.sampled_from(("unparsable", "no_metric")),
)
def test_every_trusted_individual_records_a_unit_label_whatever_the_profiler_says(
    unit: str | None, profiler_replies: str
) -> None:
    """A benchmark headline without a declared unit is labeled by its metric name.

    The profiler's own self-report cannot supply the label: it fails, or reports no number.
    """
    profiler: deque[str | Answer] = deque(
        ['{"analysis": 1}'] * 4 if profiler_replies == "unparsable" else [profiled()] * 4
    )
    executors = Executors(
        mutator=deque(mutated() for _ in range(2)),
        judge=deque(reviewed() for _ in range(2)),
        profiler=profiler,
        unit=unit,
    )
    trace = run_shell(
        executors,
        max_generations=1,
        children_per_generation=1,
        profiler_id="linux_cpu",
        profiler_support_name="linux_cpu_profiler",
        profiler_mcp_name="vibesys-linux-cpu-profiler",
    )
    state = _state(trace)
    assert state.population is not None
    measured = [item for item in state.population.individuals if item.perf_metric is not None]
    assert measured
    assert all(item.perf_unit == (unit or "throughput") for item in measured)
