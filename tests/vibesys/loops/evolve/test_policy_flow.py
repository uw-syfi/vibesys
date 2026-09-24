"""Evolve policy tests use only in-memory population and fake effects."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.evolve.policy_flow import (
    CandidateOutcome,
    EvolveSearch,
    SelectionSettings,
)
from vibesys.loops.evolve.population import Individual, Population
from vibesys.loops.evolve.search_policy import SearchSelection


@dataclass
class FakeSearchPolicy:
    selection: SearchSelection | None
    events: list[str] = field(default_factory=list)
    requires_code: bool = True

    def select(self, population: Population, **_kwargs: object) -> SearchSelection | None:
        self.events.append(f"select:{len(population.all)}")
        return self.selection

    def record(self, individual: Individual, **kwargs: object) -> None:
        self.events.append(f"record:{individual.id}:{kwargs['code']}")

    def finish_generation(self, generation: int) -> None:
        self.events.append(f"finish:{generation}")


@dataclass
class FakeSearchEffects:
    events: list[str] = field(default_factory=list)

    def checkpoint(self, label: str) -> None:
        self.events.append(f"checkpoint:{label}")

    def save_population(self, population: Population) -> None:
        self.events.append(f"save:{len(population.all)}")

    def retain_candidate(self, label: str, commit: str) -> None:
        self.events.append(f"retain:{label}:{commit}")

    def candidate_code(self, commit: str) -> str:
        self.events.append(f"code:{commit}")
        return "patch text"

    def log(self, message: str) -> None:
        self.events.append(f"log:{message}")

    def warn(self, message: str) -> None:
        self.events.append(f"warn:{message}")


def test_search_policy_selects_records_and_checkpoints_without_run_environment() -> None:
    seed = Individual(id=1, generation=0, parent_id=None, passed=True, commit="seed-sha")
    population = Population([seed])
    policy = FakeSearchPolicy(SearchSelection(parent=seed, inspirations=[]))
    effects = FakeSearchEffects()
    search = EvolveSearch(population, policy, MetricSpace())

    assert not search.needs_bootstrap()
    rng = random.Random(7)  # noqa: S311  # Deterministic selection fixture.
    selection = search.plan(
        effects,
        rng=rng,
        settings=SelectionSettings(2, 1, 0.5, 0.7),
    )
    assert selection is not None
    assert selection.parent is seed
    assert effects.events == ["checkpoint:evolve: record search selection"]

    outcome = CandidateOutcome(
        passed=True,
        parent_id=seed.id,
        inspiration_ids=[],
        summary="approved",
        feedback="approved",
        commit="candidate-sha",
    )
    recorded = search.record(outcome, generation=1, effects=effects)

    assert recorded.id == 2
    assert recorded.commit == "candidate-sha"
    assert search.final_choice() is recorded
    assert policy.events == ["select:1", "record:2:patch text"]
    assert effects.events[:4] == [
        "checkpoint:evolve: record search selection",
        "retain:individual-2:candidate-sha",
        "save:2",
        "code:candidate-sha",
    ]


def test_search_policy_handles_no_parent_and_no_fitness_resume() -> None:
    policy = FakeSearchPolicy(None)
    effects = FakeSearchEffects()
    empty = EvolveSearch(Population(), policy, MetricSpace())
    assert empty.needs_bootstrap()
    assert empty.final_choice() is None
    rng = random.Random(1)  # noqa: S311  # Deterministic selection fixture.
    assert empty.plan(effects, rng=rng, settings=SelectionSettings(0, 0, 0.0, 0.0)) is None
    assert effects.events == [
        "checkpoint:evolve: record search selection",
        "warn:no passing parent available; skipping candidate",
    ]

    first = Individual(id=2, generation=0, parent_id=None, passed=True, commit="first")
    latest = Individual(id=9, generation=1, parent_id=2, passed=True, commit="latest")
    resumed = EvolveSearch(Population([first, latest]), policy, MetricSpace())
    assert resumed.final_choice() is latest


def test_failed_candidate_is_persisted_without_registering_search_code() -> None:
    policy = FakeSearchPolicy(None)
    effects = FakeSearchEffects()
    search = EvolveSearch(Population(), policy, MetricSpace())
    failed = CandidateOutcome(
        passed=False,
        parent_id=1,
        inspiration_ids=[],
        summary="bad change",
        feedback="accuracy failed",
    )

    recorded = search.record(failed, generation=2, effects=effects)

    assert recorded.id == 1
    assert recorded.passed is False
    assert policy.events == []
    assert effects.events[0] == "save:1"
    assert "FAILED" in effects.events[1]
