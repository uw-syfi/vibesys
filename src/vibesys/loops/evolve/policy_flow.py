"""Evolve control flow over typed effects, independent of run infrastructure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from vibesys.loops.evolve.population import Individual

if TYPE_CHECKING:
    import random

    from vibesys.evaluators.metrics import MetricSpace
    from vibesys.loops.evolve.population import Population
    from vibesys.loops.evolve.search_policy import SearchPolicy, SearchSelection


@dataclass
class CandidateOutcome:
    """Evaluated candidate, before the orchestrator assigns a population ID."""

    passed: bool
    parent_id: int | None
    inspiration_ids: list[int]
    summary: str
    feedback: str | None
    commit: str | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    policy_parent_id: str | None = None
    target_island: int | None = None


@dataclass(frozen=True, slots=True)
class SelectionSettings:
    """Policy-owned sampling controls for one evolve run."""

    top_inspirations: int
    random_inspirations: int
    temperature: float
    frontier_bias: float


class SearchEffects(Protocol):
    """Persistence, code lookup, and reporting needed by the search policy."""

    def checkpoint(self, label: str) -> None:
        """Commit durable policy state."""
        ...

    def save_population(self, population: Population) -> None:
        """Write the current population."""
        ...

    def retain_candidate(self, label: str, commit: str) -> None:
        """Keep a candidate commit reachable."""
        ...

    def candidate_code(self, commit: str) -> str:
        """Read the candidate patch when the search policy needs it."""
        ...

    def log(self, message: str) -> None:
        """Report a policy outcome."""
        ...

    def warn(self, message: str) -> None:
        """Report a recoverable selection problem."""
        ...


@dataclass
class EvolveSearch:
    """Single-threaded candidate selection, recording, and final choice."""

    population: Population
    search_policy: SearchPolicy
    space: MetricSpace

    def plan(
        self, effects: SearchEffects, *, rng: random.Random, settings: SelectionSettings
    ) -> SearchSelection | None:
        """Select from current population and checkpoint the policy's sampler state."""
        selection = self.search_policy.select(
            self.population,
            rng=rng,
            k_top_inspirations=settings.top_inspirations,
            k_random_inspirations=settings.random_inspirations,
            selection_temperature=settings.temperature,
            space=self.space,
            frontier_bias=settings.frontier_bias,
        )
        effects.checkpoint("evolve: record search selection")
        if selection is None:
            effects.warn("no passing parent available; skipping candidate")
        return selection

    def record(
        self, outcome: CandidateOutcome, *, generation: int, effects: SearchEffects
    ) -> Individual:
        """Assign a stable ID, persist, and register a passing candidate."""
        individual = Individual(
            id=self.population.next_id(),
            generation=generation,
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
        if individual.commit:
            effects.retain_candidate(f"individual-{individual.id}", individual.commit)
        self.population.add(individual)
        effects.save_population(self.population)
        if outcome.passed:
            if individual.commit:
                self.search_policy.record(
                    individual,
                    code=(
                        effects.candidate_code(individual.commit)
                        if self.search_policy.requires_code
                        else ""
                    ),
                    policy_parent_id=outcome.policy_parent_id,
                    target_island=outcome.target_island,
                    space=self.space,
                )
            metrics_repr = (
                " ".join(f"{key}={value:g}" for key, value in individual.metrics.items())
                if individual.metrics
                else f"{individual.perf_metric} {individual.perf_unit or ''}"
            )
            effects.log(
                f"[Gen {generation}] Cand {individual.id} PASSED — "
                f"{metrics_repr} (parent={outcome.parent_id})"
            )
        else:
            feedback = (outcome.feedback or "").splitlines()
            effects.log(
                f"[Gen {generation}] Cand {individual.id} FAILED — "
                f"feedback: {feedback[0][:120] if feedback else ''}"
            )
        return individual

    def final_choice(self) -> Individual | None:
        """Choose a scalar champion, falling back to the latest passer."""
        best = self.population.best(self.space)
        if best is None and self.population.passed:
            best = max(self.population.passed, key=lambda individual: individual.id)
        return best

    def needs_bootstrap(self) -> bool:
        """Resume only when a passing seed already exists."""
        return not self.population.passed


def parallel_enabled(max_parallelism: int, *, supported: bool) -> bool:
    """Allow concurrent evaluation only when the environment supports it."""
    return max_parallelism > 1 and supported


@dataclass(frozen=True, slots=True)
class BootstrapAttemptResult:
    """Recorded attempt whose checkpoint and report are still pending."""

    seed: Individual | None
    message: str
