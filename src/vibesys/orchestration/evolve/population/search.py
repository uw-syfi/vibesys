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
from typing import cast

from vibesys.orchestration.evolve.population import vibesys_selector
from vibesys.orchestration.evolve.population.models import (
    CandidateOutcome,
    Individual,
    OpenEvolveSelectorConfig,
    OpenEvolveSelectorState,
    PopulationConfig,
    PopulationState,
    Proposal,
    RandomState,
)

__all__ = ["PopulationSearch"]


class PopulationSearch:
    """Bind one run's deterministic selection policy to its config."""

    def __init__(self, config: PopulationConfig) -> None:
        """Store the run's population search configuration."""
        self.config = config

    def initial(self) -> PopulationState:
        """Return the state a fresh run starts from."""
        rng = random.Random(self.config.seed)  # noqa: S311  # sampling, not security  # LW-040056 [S311]; the generator only seeds deterministic search sampling and is not used for security.
        selector_state: OpenEvolveSelectorState | None = None
        if self.config.selector == "openevolve":
            from vibesys.orchestration.evolve.population import (  # noqa: PLC0415  # lint-waiver: LW-209111 [PLC0415]; the OpenEvolve selector loads OpenEvolve, its LLM clients and numpy (about a third of a second), and only runs with selector="openevolve" use it.
                openevolve_selector,
            )

            oe_rng = random.Random(self.config.seed)  # noqa: S311  # LW-040057 [S311]; the generator only seeds deterministic search sampling and is not used for security.
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
        rng = random.Random()  # noqa: S311  # LW-040058 [S311]; the generator only seeds deterministic search sampling and is not used for security.
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
            message = "openevolve selector requires PopulationState.selector_state"
            raise ValueError(message)
        from vibesys.orchestration.evolve.population import (  # noqa: PLC0415  # lint-waiver: LW-209112 [PLC0415]; the OpenEvolve selector loads OpenEvolve, its LLM clients and numpy (about a third of a second), and only runs with selector="openevolve" use it.
            openevolve_selector,
        )

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
            metrics=outcome.metrics,
            passed=outcome.passed,
            summary=outcome.summary,
            feedback=outcome.feedback or "",
            policy_parent_id=outcome.policy_parent_id,
            policy_target_island=outcome.target_island,
        )
        selector_state = state.selector_state
        if outcome.passed and self.config.selector == "openevolve" and individual.commit:
            if selector_state is None:
                message = "openevolve selector requires PopulationState.selector_state"
                raise ValueError(message)
            from vibesys.orchestration.evolve.population import (  # noqa: PLC0415  # lint-waiver: LW-209113 [PLC0415]; the OpenEvolve selector loads OpenEvolve, its LLM clients and numpy (about a third of a second), and only runs with selector="openevolve" use it.
                openevolve_selector,
            )

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
