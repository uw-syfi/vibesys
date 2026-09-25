"""Behavioral and property tests for ``vibesys.search.population``.

These exercise the pure ``PopulationSearch`` state machine directly (no
``RunContext``, no filesystem, no agents): determinism, resume-equivalence
after a simulated crash, and hypothesis property tests covering frontier
non-domination, id uniqueness/monotonicity, and OpenEvolve state boundedness
(the R4 regression: upstream snapshots must not grow without bound).
"""

from __future__ import annotations

import copy

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.search.population import (
    CandidateOutcome,
    OpenEvolveSelectorConfig,
    PopulationConfig,
    PopulationSearch,
)


def _outcome(
    *,
    passed: bool = True,
    parent_id: int | None = None,
    perf_metric: float | None = 1.0,
    commit: str | None = "c",
    code: str | None = None,
) -> CandidateOutcome:
    return CandidateOutcome(
        passed=passed,
        parent_id=parent_id,
        commit=commit if passed else None,
        perf_metric=perf_metric if passed else None,
        summary="s",
        feedback="f" if not passed else "",
        code=code,
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_propose_is_pure_same_state_same_proposal() -> None:
    """Calling propose twice on the *same* state gives the same proposal."""
    search = PopulationSearch(PopulationConfig(seed=7))
    state = search.initial()
    ind, state = search.admit(state, _outcome(perf_metric=1.0))
    state = search.end_generation(state)

    proposal_a, _ = search.propose(state)
    proposal_b, _ = search.propose(state)

    assert proposal_a == proposal_b
    assert proposal_a is not None
    assert proposal_a.parent.id == ind.id


def test_two_independent_runs_with_same_seed_and_outcomes_match() -> None:
    """Two independently driven runs with the same seed/outcomes diverge never."""

    def drive(search: PopulationSearch) -> list[int | None]:
        state = search.initial()
        parent_ids: list[int | None] = []
        _, state = search.admit(state, _outcome(perf_metric=1.0))
        state = search.end_generation(state)
        for perf in (2.0, 3.0, 4.0):
            proposal, state = search.propose(state)
            parent_id = proposal.parent.id if proposal else None
            parent_ids.append(parent_id)
            _, state = search.admit(state, _outcome(parent_id=parent_id, perf_metric=perf))
            state = search.end_generation(state)
        return parent_ids

    a = drive(PopulationSearch(PopulationConfig(seed=42, selection_temperature=0.5)))
    b = drive(PopulationSearch(PopulationConfig(seed=42, selection_temperature=0.5)))
    assert a == b


# ---------------------------------------------------------------------------
# Resume: persist after any admit, reload -> same next proposal
# ---------------------------------------------------------------------------


def test_resume_after_admit_gives_same_next_proposal() -> None:
    search = PopulationSearch(PopulationConfig(seed=3))
    state = search.initial()
    _, state = search.admit(state, _outcome(perf_metric=1.0))
    state = search.end_generation(state)
    _, state = search.admit(state, _outcome(parent_id=1, perf_metric=2.0))
    state = search.end_generation(state)

    # Simulate a checkpoint/reload boundary: serialize and rebuild.
    reloaded = type(state).model_validate_json(state.model_dump_json())
    assert reloaded == state

    proposal_direct, _ = search.propose(state)
    proposal_reloaded, _ = search.propose(reloaded)
    assert proposal_direct == proposal_reloaded


# ---------------------------------------------------------------------------
# Crash/resume at the commit boundary for paid candidates (R2)
# ---------------------------------------------------------------------------


def test_crash_between_propose_and_admit_replays_identically() -> None:
    """A crash after propose() but before the caller checkpoints the admitted
    state is safe: PopulationSearch never persists mid-selection, so resuming
    from the last *admitted* state and calling propose() again reproduces the
    exact same proposal -- there is nothing to "recover," only paid work
    (the candidate evaluation) to redo. This is the crash-safety story that
    replaces the deleted generation-journal/cursor machinery.
    """
    search = PopulationSearch(PopulationConfig(seed=11, selection_temperature=0.3))
    state = search.initial()
    _, state = search.admit(state, _outcome(perf_metric=1.0))
    state = search.end_generation(state)
    _, state = search.admit(state, _outcome(parent_id=1, perf_metric=5.0))
    state = search.end_generation(state)

    # This is the last state a real orchestrator would have checkpointed.
    committed = copy.deepcopy(state)

    # "Before the crash": propose, but the candidate evaluation (paid work)
    # never reaches admit()/a checkpoint.
    proposal_before_crash, _ = search.propose(state)

    # "Resume": reload the last committed state and propose again.
    resumed_proposal, _ = search.propose(committed)

    assert resumed_proposal == proposal_before_crash


def test_crash_after_admit_but_before_checkpoint_state_is_immutable() -> None:
    """admit() never mutates its input state in place, so a caller that
    crashes before persisting the returned state still has an untouched,
    valid "pre-admit" state to resume from (redoing just that one candidate).
    """
    search = PopulationSearch(PopulationConfig(seed=5))
    state = search.initial()
    pre_admit = copy.deepcopy(state)
    _, post_admit = search.admit(state, _outcome(perf_metric=1.0))

    assert state == pre_admit
    assert post_admit != state
    assert len(state.individuals) == 0
    assert len(post_admit.individuals) == 1


# ---------------------------------------------------------------------------
# failure_lessons / wip_seed: used by evolve's bootstrap to steer cold starts
# ---------------------------------------------------------------------------


def test_failure_lessons_empty_before_any_failure() -> None:
    search = PopulationSearch(PopulationConfig(seed=1))
    state = search.initial()
    assert search.failure_lessons(state) == []


def _failed_outcome(feedback: str) -> CandidateOutcome:
    return CandidateOutcome(
        passed=False, parent_id=None, commit=None, perf_metric=None, summary="s", feedback=feedback
    )


def test_failure_lessons_most_recent_first_deduplicated_and_truncated() -> None:
    search = PopulationSearch(PopulationConfig(seed=1))
    state = search.initial()
    _, state = search.admit(state, _failed_outcome("first failure"))
    _, state = search.admit(state, _failed_outcome("First Failure"))
    _, state = search.admit(state, _failed_outcome("x" * 900))

    lessons = search.failure_lessons(state, limit=3, max_chars=700)

    # The near-duplicate ("First Failure" vs "first failure", normalized on a
    # lowercased prefix) collapses to a single lesson, and the most recent
    # failure (the long one, truncated) sorts first.
    assert len(lessons) == 2
    assert lessons[0].endswith("…")
    assert len(lessons[0]) <= 700 + len(" …")
    assert lessons[1] == "First Failure"


def test_failure_lessons_ignores_passed_individuals_and_blank_feedback() -> None:
    search = PopulationSearch(PopulationConfig(seed=1))
    state = search.initial()
    _, state = search.admit(state, _outcome(passed=True, perf_metric=1.0))
    _, state = search.admit(state, _failed_outcome("   "))
    assert search.failure_lessons(state) == []


def test_failure_lessons_respects_limit() -> None:
    search = PopulationSearch(PopulationConfig(seed=1))
    state = search.initial()
    for i in range(5):
        _, state = search.admit(state, _failed_outcome(f"failure {i}"))
    lessons = search.failure_lessons(state, limit=2)
    assert lessons == ["failure 4", "failure 3"]


def test_wip_seed_none_when_no_snapshotted_cold_start_failure() -> None:
    search = PopulationSearch(PopulationConfig(seed=1))
    state = search.initial()
    assert search.wip_seed(state) is None
    # A failed child of a parent (not a cold start) doesn't count.
    _, state = search.admit(
        state,
        CandidateOutcome(passed=True, parent_id=None, commit="c0", perf_metric=1.0, summary="s"),
    )
    _, state = search.admit(
        state,
        CandidateOutcome(
            passed=False, parent_id=1, commit=None, perf_metric=None, summary="s", feedback="f"
        ),
    )
    assert search.wip_seed(state) is None


def _wip_outcome(commit: str | None, feedback: str) -> CandidateOutcome:
    return CandidateOutcome(
        passed=False,
        parent_id=None,
        commit=commit,
        perf_metric=None,
        summary="s",
        feedback=feedback,
    )


def test_wip_seed_returns_most_recent_failed_snapshotted_cold_start() -> None:
    search = PopulationSearch(PopulationConfig(seed=1))
    state = search.initial()
    # Failed cold start with no commit: not a usable WIP seed.
    _, state = search.admit(state, _wip_outcome(None, "f0"))
    assert search.wip_seed(state) is None

    # Failed cold start with a snapshotted commit: usable.
    _, state = search.admit(state, _wip_outcome("wip-1", "f1"))
    seed = search.wip_seed(state)
    assert seed is not None
    assert seed.commit == "wip-1"

    # A later, more recent failed cold start with its own commit supersedes
    # the earlier one as the WIP seed.
    _, state = search.admit(state, _wip_outcome("wip-2", "f2"))
    seed = search.wip_seed(state)
    assert seed is not None
    assert seed.commit == "wip-2"


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------

_outcome_strategy = st.builds(
    _outcome,
    passed=st.booleans(),
    perf_metric=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
)


@given(outcomes=st.lists(_outcome_strategy, min_size=0, max_size=12))
@settings(max_examples=50)
def test_admitted_ids_unique_and_increasing(outcomes: list[CandidateOutcome]) -> None:
    search = PopulationSearch(PopulationConfig(seed=1))
    state = search.initial()
    seen_ids: list[int] = []
    for outcome in outcomes:
        individual, state = search.admit(state, outcome)
        seen_ids.append(individual.id)
    assert seen_ids == sorted(set(seen_ids))
    assert len(set(seen_ids)) == len(seen_ids)


@given(
    perf_metrics=st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=10,
    )
)
@settings(max_examples=50)
def test_frontier_never_dominated(perf_metrics: list[float]) -> None:
    """Every member of the returned frontier is Pareto-non-dominated by any
    other passed individual (single-axis space: this reduces to "the
    frontier is exactly the set of maxima").
    """
    space = MetricSpace(objectives=(Objective(name="throughput", direction="max"),))
    search = PopulationSearch(PopulationConfig(space=space, seed=2))
    state = search.initial()
    for value in perf_metrics:
        outcome = CandidateOutcome(
            passed=True,
            parent_id=None,
            commit="c",
            perf_metric=value,
            metrics={"throughput": value},
            summary="s",
        )
        _, state = search.admit(state, outcome)

    front = search.frontier(state)
    front_values = {individual.metrics["throughput"] for individual in front}
    best_value = max(perf_metrics)
    # Nothing on the frontier is beaten by another admitted individual.
    for individual in front:
        value = individual.metrics["throughput"]
        assert not any(other > value for other in perf_metrics)
    # The maximum is always on the frontier.
    assert best_value in front_values


@given(admit_count=st.integers(min_value=6, max_value=30))
@settings(max_examples=15, deadline=None)
def test_openevolve_state_size_bounded_across_many_admits(admit_count: int) -> None:
    """R4 regression: the OpenEvolve selector state must not grow without
    bound as more candidates are admitted. On the old (pre-refactor)
    directory-snapshot implementation, every admit wrote a *new* immutable
    snapshot directory under ``snapshots/`` that was never pruned, so state
    on disk grew monotonically with the number of admits forever -- this
    test fails against that implementation once ``admit_count`` exceeds a
    few. Here state is data (``OpenEvolveSelectorState.files`` is replaced
    wholesale on every admit, not appended to), so once the tiny configured
    ``population_size``/``archive_size`` caps are reached, later admits must
    not grow the serialized state further: the database evicts old programs
    instead of accumulating history.
    """
    openevolve_config = OpenEvolveSelectorConfig(
        population_size=3, archive_size=3, num_islands=1, migration_interval=1000
    )
    config = PopulationConfig(selector="openevolve", seed=1, openevolve=openevolve_config)
    search = PopulationSearch(config)
    state = search.initial()

    sizes: list[int] = []
    for i in range(admit_count):
        outcome = CandidateOutcome(
            passed=True,
            parent_id=None,
            commit=f"c{i}",
            perf_metric=float(i),
            summary="s",
            code=f"code-{i}",
        )
        _, state = search.admit(state, outcome)
        assert state.selector_state is not None
        sizes.append(len(state.selector_state.model_dump_json()))

    # Once past the tiny cap, size must plateau rather than keep growing: the
    # last few admits' state sizes must not exceed roughly the size at the
    # cap boundary by more than a small constant factor.
    at_cap = sizes[min(len(sizes) - 1, openevolve_config.population_size + 2)]
    tail = sizes[-3:]
    assert all(size <= at_cap * 2 for size in tail), (at_cap, tail, sizes)
