"""Evolve control flow over typed effects, independent of run infrastructure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from vibesys.loops.evolve.population import Individual

if TYPE_CHECKING:
    import random

    from vibesys.loops.evolve.population import Population
    from vibesys.loops.evolve.search_policy import SearchPolicy, SearchSelection
    from vibesys.loops.metrics import MetricSpace


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    """Search relationships retained with an evaluated candidate."""

    parent_id: int
    inspiration_ids: list[int]
    policy_parent_id: str | None = None
    target_island: int | None = None


@dataclass(frozen=True, slots=True)
class CandidateJudgement:
    """The judge's decision and feedback, before framework gates run."""

    passed: bool
    feedback: str | None


@dataclass(frozen=True, slots=True)
class CandidateFitness:
    """Fitness measured after all mandatory gates pass."""

    metric: float | None
    unit: str | None
    metrics: dict[str, float]


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


class CandidateEffects(Protocol):
    """Side effects needed to evaluate one candidate in its own environment."""

    def mutate(self) -> str:
        """Run the mutator and return its summary."""
        ...

    def judge(self) -> CandidateJudgement:
        """Review the current candidate."""
        ...

    def run_gates(self) -> str | None:
        """Run mandatory gates and return failure feedback, if any."""
        ...

    def measure(self) -> CandidateFitness:
        """Measure fitness after all gates pass."""
        ...

    def snapshot(self) -> str | None:
        """Commit the passing candidate and return its revision."""
        ...

    def close(self) -> None:
        """Release candidate resources on every exit path."""
        ...


def evaluate_candidate(effects: CandidateEffects, identity: CandidateIdentity) -> CandidateOutcome:
    """Apply judge and framework gates before measuring or retaining a candidate."""
    try:
        summary = effects.mutate()
        judgement = effects.judge()
        if not judgement.passed:
            return _failed(identity, summary, judgement.feedback)
        feedback = effects.run_gates()
        if feedback is not None:
            return _failed(identity, summary, feedback)
        fitness = effects.measure()
        commit = effects.snapshot()
        return CandidateOutcome(
            passed=True,
            parent_id=identity.parent_id,
            inspiration_ids=identity.inspiration_ids,
            summary=summary,
            feedback=judgement.feedback,
            commit=commit,
            perf_metric=fitness.metric,
            perf_unit=fitness.unit,
            metrics=fitness.metrics,
            policy_parent_id=identity.policy_parent_id,
            target_island=identity.target_island,
        )
    finally:
        effects.close()


def _failed(identity: CandidateIdentity, summary: str, feedback: str | None) -> CandidateOutcome:
    return CandidateOutcome(
        passed=False,
        parent_id=identity.parent_id,
        inspiration_ids=identity.inspiration_ids,
        summary=summary,
        feedback=feedback,
        policy_parent_id=identity.policy_parent_id,
        target_island=identity.target_island,
    )


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

    def complete_generation(self, generation: int, effects: SearchEffects) -> None:
        """Persist generation-level search state after all offspring are recorded."""
        self.search_policy.finish_generation(generation)
        effects.checkpoint(f"evolve: complete generation {generation}")


def parallel_enabled(max_parallelism: int, *, supported: bool) -> bool:
    """Allow concurrent evaluation only when the environment supports it."""
    return max_parallelism > 1 and supported


@dataclass(frozen=True, slots=True)
class BootstrapAttemptResult:
    """Recorded attempt whose checkpoint and report are still pending."""

    seed: Individual | None
    message: str


class BootstrapEffects(Protocol):
    """One bootstrap attempt and its run-scoped reporting effects."""

    def begin(self, max_attempts: int) -> None:
        """Open bootstrap logs before the first attempt."""
        ...

    def attempt(self, number: int, max_attempts: int) -> BootstrapAttemptResult:
        """Evaluate and save one attempt without checkpointing it."""
        ...

    def checkpoint(self, label: str) -> None:
        """Commit the just-recorded attempt's durable state."""
        ...

    def report(self, message: str) -> None:
        """Report an attempt after its checkpoint succeeds."""
        ...

    def exhausted(self, max_attempts: int) -> None:
        """Report that the budget produced no passing seed."""
        ...


def retry_bootstrap(max_attempts: int, effects: BootstrapEffects) -> Individual | None:
    """Retry bootstrap until one recorded seed passes or the budget is exhausted."""
    effects.begin(max_attempts)
    for number in range(1, max_attempts + 1):
        result = effects.attempt(number, max_attempts)
        label = (
            f"evolve: record bootstrap seed {result.seed.id}"
            if result.seed is not None
            else f"evolve: record failed bootstrap {number}"
        )
        effects.checkpoint(label)
        effects.report(result.message)
        if result.seed is not None:
            return result.seed
    effects.exhausted(max_attempts)
    return None


class EvolveRunEffects(Protocol):
    """Concrete work and reporting required by the run-level scheduler."""

    def bootstrap(self) -> Individual | None:
        """Establish and record the first passing seed in the shared population."""
        ...

    def bootstrap_failed(self) -> None:
        """Report exhausted bootstrap attempts."""
        ...

    def parallel_unsupported(self, max_parallelism: int) -> None:
        """Report a requested concurrency level that cannot be used."""
        ...

    def begin_generation(
        self, generation: int, max_generations: int, population_size: int, passed_count: int
    ) -> None:
        """Open generation-scoped logs and progress."""
        ...

    def run_serial(self, generation: int) -> None:
        """Evaluate one generation on the shared context."""
        ...

    def run_parallel(self, generation: int) -> None:
        """Evaluate one generation in isolated contexts."""
        ...

    def finalize(self, frontier: list[Individual] | None, best: Individual | None) -> None:
        """Report the final frontier and materialize the selected candidate."""
        ...


@dataclass
class EvolveRunScheduler:
    """Schedule resume/bootstrap, generations, checkpoints, and final selection."""

    search: EvolveSearch
    search_effects: SearchEffects
    effects: EvolveRunEffects
    max_generations: int
    max_parallelism: int
    supports_parallel: bool

    def run(self) -> bool:
        """Complete the configured budget, or stop if bootstrap finds no seed."""
        if self.search.needs_bootstrap() and self.effects.bootstrap() is None:
            self.effects.bootstrap_failed()
            return False
        parallel = parallel_enabled(self.max_parallelism, supported=self.supports_parallel)
        if self.max_parallelism > 1 and not parallel:
            self.effects.parallel_unsupported(self.max_parallelism)
        for generation in range(1, self.max_generations + 1):
            self.effects.begin_generation(
                generation,
                self.max_generations,
                len(self.search.population),
                len(self.search.population.passed),
            )
            if parallel:
                self.effects.run_parallel(generation)
            else:
                self.effects.run_serial(generation)
            self.search.complete_generation(generation, self.search_effects)
        frontier = (
            self.search.population.frontier(self.search.space)
            if self.search.space.objectives
            else None
        )
        self.effects.finalize(frontier, self.search.final_choice())
        return True
