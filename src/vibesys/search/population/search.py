"""PopulationSearch: the pure evolutionary-search state machine.

Orchestration owns everything ``PopulationSearch`` is not: which agent runs,
prompt rendering, filesystem/git access, and persistence. This module only
answers "who's the parent/inspirations" (``propose``) and "what did this
candidate become" (``admit``), and returns the next :class:`PopulationState`
for the caller to persist. Given the same input state, every method is
deterministic.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, cast

from vibesys.search.population import openevolve_selector, vibesys_selector
from vibesys.search.population.models import (
    CandidateOutcome,
    Individual,
    OpenEvolveSelectorConfig,
    OpenEvolveSelectorState,
    PopulationConfig,
    PopulationState,
    Proposal,
    RandomState,
)

if TYPE_CHECKING:
    from vibesys.evaluators.gates import FrameworkBenchmarkOutcome

    # TODO(stack PR 06): import from vibesys.evaluators.perf_reply once  # noqa: FIX002  # tracked: #288
    # schemas.py dissolves; at BASE, ProfilerSummary still lives in
    # vibesys.schemas.
    from vibesys.schemas import ProfilerSummary

__all__ = ["PopulationSearch", "candidate_fitness"]


def candidate_fitness(
    summary: ProfilerSummary | None,
    benchmark: FrameworkBenchmarkOutcome | None,
) -> tuple[float | None, str | None, dict[str, float]]:
    """Resolve a candidate's recorded fitness: trusted benchmark over profiler.

    A declared benchmark result contract owns the axes it measures; the
    profiler's self-report owns the rest. The trusted row is merged *over*
    the profiler's row rather than replacing it: the scalar contract reports
    one number, so on a two-axis task replacing the row would leave every
    individual incomplete on the second axis, and ``frontier`` (which keeps
    only individuals carrying a value for every configured axis) would return
    nothing.

    The unit comes from the evaluator's own declaration when the result
    protocol supplies one; ``objectives.toml`` names axes but does not say
    what they are measured in, so the profiler's unit is the fallback.
    """
    metrics = dict(summary.metrics) if summary and summary.metrics else {}
    if (
        benchmark is not None
        and benchmark.metric_name is not None
        and benchmark.metric_value is not None
    ):
        trusted = (
            dict(benchmark.row)
            if benchmark.row
            else {benchmark.metric_name: benchmark.metric_value}
        )
        return (
            benchmark.metric_value,
            benchmark.metric_unit or (summary.perf_unit if summary else None),
            metrics | trusted,
        )
    return (
        summary.perf_metric if summary else None,
        summary.perf_unit if summary else None,
        metrics,
    )


class PopulationSearch:
    """Bind one run's deterministic selection policy to its config."""

    def __init__(self, config: PopulationConfig) -> None:  # noqa: D107  # tracked: #288
        self.config = config

    def initial(self) -> PopulationState:
        """Return the state a fresh run starts from."""
        rng = random.Random(self.config.seed)  # noqa: S311  # sampling, not security
        selector_state: OpenEvolveSelectorState | None = None
        if self.config.selector == "openevolve":
            oe_rng = random.Random(self.config.seed)  # noqa: S311
            selector_state = OpenEvolveSelectorState(
                config=self.config.openevolve or OpenEvolveSelectorConfig(),
                objective_signature=openevolve_selector.objective_signature(self.config.space),
                rng_state=cast("RandomState", oe_rng.getstate()),
            )
        return PopulationState(
            rng_state=cast("RandomState", rng.getstate()), selector_state=selector_state
        )

    @property
    def needs_code(self) -> bool:
        """Whether ``admit`` needs ``CandidateOutcome.code`` populated."""
        return self.config.selector == "openevolve"

    def needs_bootstrap(self, state: PopulationState) -> bool:
        """Whether the population has no passing parent yet."""
        return not vibesys_selector.passed_individuals(state.individuals)

    def propose(self, state: PopulationState) -> tuple[Proposal | None, PopulationState]:
        """Select a parent and inspirations; ``None`` when nothing has passed."""
        if self.config.selector == "openevolve":
            return self._propose_openevolve(state)
        rng = random.Random()  # noqa: S311
        rng.setstate(state.rng_state)
        proposal = vibesys_selector.select(
            state.individuals,
            rng=rng,
            k_top_inspirations=self.config.k_top_inspirations,
            k_random_inspirations=self.config.k_random_inspirations,
            selection_temperature=self.config.selection_temperature,
            space=self.config.space,
            frontier_bias=self.config.frontier_bias,
        )
        new_state = state.model_copy(update={"rng_state": cast("RandomState", rng.getstate())})
        return proposal, new_state

    def _propose_openevolve(
        self, state: PopulationState
    ) -> tuple[Proposal | None, PopulationState]:
        if state.selector_state is None:
            raise ValueError("openevolve selector requires PopulationState.selector_state")  # noqa: TRY003
        proposal, selector_state = openevolve_selector.select(
            state.selector_state,
            state.individuals,
            k_top_inspirations=self.config.k_top_inspirations,
            k_random_inspirations=self.config.k_random_inspirations,
            space=self.config.space,
        )
        new_state = state.model_copy(update={"selector_state": selector_state})
        if proposal is None:
            passers = vibesys_selector.passed_individuals(state.individuals)
            if not passers:
                return None, new_state
            proposal = Proposal(parent=passers[-1])
        return proposal, new_state

    def admit(
        self, state: PopulationState, outcome: CandidateOutcome
    ) -> tuple[Individual, PopulationState]:
        """Assign a stable id, append to the population, and update selector state."""
        individual = Individual(
            id=state.next_id,
            generation=state.generation,
            parent_id=outcome.parent_id,
            inspiration_ids=outcome.inspiration_ids,
            commit=outcome.commit,
            perf_metric=outcome.perf_metric,
            perf_unit=outcome.perf_unit,
            metrics=dict(outcome.metrics),
            passed=outcome.passed,
            summary=outcome.summary,
            feedback=outcome.feedback or "",
            policy_parent_id=outcome.policy_parent_id,
            policy_target_island=outcome.target_island,
        )
        selector_state = state.selector_state
        if outcome.passed and self.config.selector == "openevolve" and individual.commit:
            if selector_state is None:
                raise ValueError("openevolve selector requires PopulationState.selector_state")  # noqa: TRY003
            selector_state = openevolve_selector.admit(
                selector_state,
                individual,
                code=outcome.code or "",
                policy_parent_id=outcome.policy_parent_id,
                target_island=outcome.target_island,
                space=self.config.space,
            )
        new_state = state.model_copy(
            update={
                "individuals": (*state.individuals, individual),
                "next_id": state.next_id + 1,
                "selector_state": selector_state,
            }
        )
        return individual, new_state

    def end_generation(self, state: PopulationState) -> PopulationState:
        """Advance the generation counter once every child slot is accounted for."""
        return state.model_copy(update={"generation": state.generation + 1})

    def best(self, state: PopulationState) -> Individual | None:
        """Return the passed individual leading on the run's headline axis."""
        return vibesys_selector.best(state.individuals, self.config.space)

    def frontier(self, state: PopulationState) -> list[Individual]:
        """Return the Pareto-non-dominated subset of passed individuals."""
        return vibesys_selector.frontier(state.individuals, self.config.space)

    def failure_lessons(
        self, state: PopulationState, *, limit: int = 3, max_chars: int = 700
    ) -> list[str]:
        """Distinct feedback from the most-recent failed individuals.

        While the population has no passing parent, every child is a cold
        start that re-writes the server from scratch; this surfaces recent
        distinct failure feedback so a new attempt avoids traps earlier ones
        hit. De-duplicates on a normalized prefix and truncates each lesson.
        """
        seen: set[str] = set()
        lessons: list[str] = []
        for individual in reversed(state.individuals):  # most recent first
            if individual.passed:
                continue
            feedback = individual.feedback.strip()
            if not feedback:
                continue
            key = " ".join(feedback[:160].lower().split())
            if key in seen:
                continue
            seen.add(key)
            lessons.append(
                feedback if len(feedback) <= max_chars else feedback[:max_chars].rstrip() + " …"
            )
            if len(lessons) >= limit:
                break
        return lessons

    def wip_seed(self, state: PopulationState) -> Individual | None:
        """Most-recent failed cold-start seed whose work was snapshotted.

        A WIP seed is a failed individual (``passed=False``) with
        ``parent_id is None`` that still carries a ``commit`` (its
        snapshotted tree), letting the next cold start repair it in place
        instead of restarting from scratch.
        """
        for individual in reversed(state.individuals):  # most recent first
            if not individual.passed and individual.parent_id is None and individual.commit:
                return individual
        return None
