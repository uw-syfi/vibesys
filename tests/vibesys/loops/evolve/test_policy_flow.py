"""Evolve policy tests use only in-memory population and fake effects."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import pytest

from vibesys.loops.evolve.policy_flow import (
    BootstrapAttemptResult,
    CandidateFitness,
    CandidateIdentity,
    CandidateJudgement,
    CandidateOutcome,
    EvolveRunScheduler,
    EvolveSearch,
    SelectionSettings,
    evaluate_candidate,
    parallel_enabled,
    retry_bootstrap,
)
from vibesys.loops.evolve.population import Individual, Population
from vibesys.loops.evolve.search_policy import SearchSelection
from vibesys.loops.metrics import MetricSpace, Objective


@dataclass
class FakeCandidateEffects:
    judgement: CandidateJudgement
    gate_feedback: str | None = None
    events: list[str] = field(default_factory=list)

    def mutate(self) -> str:
        self.events.append("mutate")
        return "change summary"

    def judge(self) -> CandidateJudgement:
        self.events.append("judge")
        return self.judgement

    def run_gates(self) -> str | None:
        self.events.append("gates")
        return self.gate_feedback

    def measure(self) -> CandidateFitness:
        self.events.append("measure")
        return CandidateFitness(11.0, "ops/s", {"throughput": 11.0})

    def snapshot(self) -> str:
        self.events.append("snapshot")
        return "candidate-sha"

    def close(self) -> None:
        self.events.append("close")


@pytest.mark.parametrize(
    ("judgement", "gate_feedback", "expected_events"),
    [
        (CandidateJudgement(passed=False, feedback=None), None, ["mutate", "judge", "close"]),
        (
            CandidateJudgement(passed=True, feedback="judge note"),
            "accuracy failed",
            ["mutate", "judge", "gates", "close"],
        ),
        (
            CandidateJudgement(passed=True, feedback="judge note"),
            None,
            ["mutate", "judge", "gates", "measure", "snapshot", "close"],
        ),
    ],
)
def test_candidate_policy_short_circuits_gates_and_snapshots_only_passers(
    judgement: CandidateJudgement, gate_feedback: str | None, expected_events: list[str]
) -> None:
    effects = FakeCandidateEffects(judgement, gate_feedback)
    outcome = evaluate_candidate(effects, CandidateIdentity(4, [1, 2], "policy-parent", 3))

    assert effects.events == expected_events
    assert outcome.passed is ("snapshot" in expected_events)
    assert (outcome.parent_id, outcome.inspiration_ids) == (4, [1, 2])
    assert (outcome.policy_parent_id, outcome.target_island) == ("policy-parent", 3)
    assert outcome.commit == ("candidate-sha" if outcome.passed else None)
    if outcome.passed:
        assert outcome.metrics == {"throughput": 11.0}
    elif gate_feedback is not None:
        assert outcome.feedback == gate_feedback


def test_candidate_policy_releases_effects_when_measurement_raises() -> None:
    class FailingMeasurement(FakeCandidateEffects):
        def measure(self) -> CandidateFitness:
            self.events.append("measure")
            message = "measurement failed"
            raise RuntimeError(message)

    effects = FailingMeasurement(CandidateJudgement(passed=True, feedback=None))
    with pytest.raises(RuntimeError, match="measurement failed"):
        evaluate_candidate(effects, CandidateIdentity(1, []))

    assert effects.events == ["mutate", "judge", "gates", "measure", "close"]


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

    candidate_effects = FakeCandidateEffects(CandidateJudgement(passed=True, feedback="approved"))
    outcome = evaluate_candidate(candidate_effects, CandidateIdentity(seed.id, []))
    recorded = search.record(outcome, generation=1, effects=effects)
    search.complete_generation(1, effects)

    assert recorded.id == 2
    assert recorded.commit == "candidate-sha"
    assert search.final_choice() is recorded
    assert policy.events == ["select:1", "record:2:patch text", "finish:1"]
    assert effects.events[:4] == [
        "checkpoint:evolve: record search selection",
        "retain:individual-2:candidate-sha",
        "save:2",
        "code:candidate-sha",
    ]
    assert effects.events[-1] == "checkpoint:evolve: complete generation 1"


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
    assert parallel_enabled(2, supported=True)
    assert not parallel_enabled(2, supported=False)


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


@dataclass
class FakeRunEffects:
    population: Population
    timeline: list[str]
    bootstrap_succeeds: bool = True
    final_best: Individual | None = None
    final_frontier: list[Individual] | None = None

    def bootstrap(self) -> Individual | None:
        self.timeline.append("bootstrap")
        if not self.bootstrap_succeeds:
            return None
        seed = Individual(
            id=self.population.next_id(),
            generation=0,
            parent_id=None,
            passed=True,
            commit="seed",
            perf_metric=0.0,
        )
        self.population.add(seed)
        return seed

    def bootstrap_failed(self) -> None:
        self.timeline.append("bootstrap_failed")

    def parallel_unsupported(self, max_parallelism: int) -> None:
        self.timeline.append(f"parallel_unsupported:{max_parallelism}")

    def begin_generation(
        self, generation: int, max_generations: int, population_size: int, passed_count: int
    ) -> None:
        self.timeline.append(
            f"begin:{generation}/{max_generations}:population={population_size}:passed={passed_count}"
        )

    def run_serial(self, generation: int) -> None:
        self.timeline.append(f"serial:{generation}")
        self._record_child(generation)

    def run_parallel(self, generation: int) -> None:
        self.timeline.append(f"parallel:{generation}")
        self._record_child(generation)

    def _record_child(self, generation: int) -> None:
        self.population.add(
            Individual(
                id=self.population.next_id(),
                generation=generation,
                parent_id=1,
                passed=True,
                commit=f"child-{generation}",
                perf_metric=float(generation),
            )
        )

    def finalize(self, frontier: list[Individual] | None, best: Individual | None) -> None:
        self.timeline.append("finalize")
        self.final_frontier = frontier
        self.final_best = best


def test_run_scheduler_bootstraps_then_completes_all_serial_generations() -> None:
    timeline: list[str] = []
    population = Population()
    policy = FakeSearchPolicy(None, timeline)
    effects = FakeRunEffects(population, timeline)
    scheduler = EvolveRunScheduler(
        EvolveSearch(population, policy, MetricSpace()),
        FakeSearchEffects(timeline),
        effects,
        max_generations=2,
        max_parallelism=1,
        supports_parallel=False,
    )

    assert scheduler.run()
    assert effects.final_best is not None
    assert effects.final_best.commit == "child-2"
    assert timeline == [
        "bootstrap",
        "begin:1/2:population=1:passed=1",
        "serial:1",
        "finish:1",
        "checkpoint:evolve: complete generation 1",
        "begin:2/2:population=2:passed=2",
        "serial:2",
        "finish:2",
        "checkpoint:evolve: complete generation 2",
        "finalize",
    ]


def test_run_scheduler_resumes_with_parallel_dispatch() -> None:
    timeline: list[str] = []
    seed = Individual(id=1, generation=0, parent_id=None, passed=True, commit="seed")
    population = Population([seed])
    effects = FakeRunEffects(population, timeline)
    scheduler = EvolveRunScheduler(
        EvolveSearch(population, FakeSearchPolicy(None, timeline), MetricSpace()),
        FakeSearchEffects(timeline),
        effects,
        max_generations=1,
        max_parallelism=3,
        supports_parallel=True,
    )

    assert scheduler.run()
    assert timeline == [
        "begin:1/1:population=1:passed=1",
        "parallel:1",
        "finish:1",
        "checkpoint:evolve: complete generation 1",
        "finalize",
    ]


def test_run_scheduler_stops_before_generation_when_bootstrap_fails() -> None:
    timeline: list[str] = []
    population = Population()
    scheduler = EvolveRunScheduler(
        EvolveSearch(population, FakeSearchPolicy(None, timeline), MetricSpace()),
        FakeSearchEffects(timeline),
        FakeRunEffects(population, timeline, bootstrap_succeeds=False),
        max_generations=3,
        max_parallelism=2,
        supports_parallel=True,
    )

    assert scheduler.run() is False
    assert timeline == ["bootstrap", "bootstrap_failed"]


def test_run_scheduler_downgrades_unsupported_parallel_mode() -> None:
    timeline: list[str] = []
    seed = Individual(id=1, generation=0, parent_id=None, passed=True, commit="seed")
    population = Population([seed])
    scheduler = EvolveRunScheduler(
        EvolveSearch(population, FakeSearchPolicy(None, timeline), MetricSpace()),
        FakeSearchEffects(timeline),
        FakeRunEffects(population, timeline),
        max_generations=1,
        max_parallelism=4,
        supports_parallel=False,
    )

    assert scheduler.run()
    assert timeline[:3] == [
        "parallel_unsupported:4",
        "begin:1/1:population=1:passed=1",
        "serial:1",
    ]


def test_run_scheduler_sends_frontier_and_scalar_choice_to_finalizer() -> None:
    timeline: list[str] = []
    low = Individual(
        id=1,
        generation=0,
        parent_id=None,
        passed=True,
        commit="low",
        perf_metric=1.0,
        metrics={"throughput": 1.0},
    )
    high = Individual(
        id=2,
        generation=0,
        parent_id=None,
        passed=True,
        commit="high",
        perf_metric=2.0,
        metrics={"throughput": 2.0},
    )
    population = Population([low, high])
    effects = FakeRunEffects(population, timeline)
    scheduler = EvolveRunScheduler(
        EvolveSearch(
            population,
            FakeSearchPolicy(None, timeline),
            MetricSpace(objectives=(Objective(name="throughput", direction="max"),)),
        ),
        FakeSearchEffects(timeline),
        effects,
        max_generations=0,
        max_parallelism=1,
        supports_parallel=False,
    )

    assert scheduler.run()
    assert effects.final_frontier == [high]
    assert effects.final_best is high
    assert timeline == ["finalize"]


@dataclass
class FakeBootstrapEffects:
    results: list[Individual | None]
    events: list[str] = field(default_factory=list)

    def begin(self, max_attempts: int) -> None:
        self.events.append(f"begin:{max_attempts}")

    def attempt(self, number: int, max_attempts: int) -> BootstrapAttemptResult:
        self.events.append(f"attempt:{number}/{max_attempts}")
        return BootstrapAttemptResult(self.results[number - 1], f"report:{number}")

    def checkpoint(self, label: str) -> None:
        self.events.append(f"checkpoint:{label}")

    def report(self, message: str) -> None:
        self.events.append(message)

    def exhausted(self, max_attempts: int) -> None:
        self.events.append(f"exhausted:{max_attempts}")


def test_bootstrap_retry_stops_on_first_passing_seed() -> None:
    seed = Individual(id=1, generation=0, parent_id=None, passed=True, commit="seed")
    effects = FakeBootstrapEffects([seed])

    assert retry_bootstrap(3, effects) is seed
    assert effects.events == [
        "begin:3",
        "attempt:1/3",
        "checkpoint:evolve: record bootstrap seed 1",
        "report:1",
    ]


def test_bootstrap_retry_continues_after_failure_then_stops() -> None:
    seed = Individual(id=2, generation=0, parent_id=None, passed=True, commit="seed")
    effects = FakeBootstrapEffects([None, seed])

    assert retry_bootstrap(3, effects) is seed
    assert effects.events == [
        "begin:3",
        "attempt:1/3",
        "checkpoint:evolve: record failed bootstrap 1",
        "report:1",
        "attempt:2/3",
        "checkpoint:evolve: record bootstrap seed 2",
        "report:2",
    ]


def test_bootstrap_retry_reports_exhaustion_without_extra_attempt() -> None:
    effects = FakeBootstrapEffects([None, None])

    assert retry_bootstrap(2, effects) is None
    assert effects.events == [
        "begin:2",
        "attempt:1/2",
        "checkpoint:evolve: record failed bootstrap 1",
        "report:1",
        "attempt:2/2",
        "checkpoint:evolve: record failed bootstrap 2",
        "report:2",
        "exhausted:2",
    ]


def test_bootstrap_retry_only_profiles_after_fake_gates_pass() -> None:
    class GateEffects(FakeBootstrapEffects):
        def attempt(self, number: int, max_attempts: int) -> BootstrapAttemptResult:
            super().attempt(number, max_attempts)
            candidate = FakeCandidateEffects(
                CandidateJudgement(passed=True, feedback="reviewed"),
                gate_feedback="accuracy failed" if number == 1 else None,
                events=self.events,
            )
            outcome = evaluate_candidate(candidate, CandidateIdentity(1, []))
            if not outcome.passed:
                return BootstrapAttemptResult(None, f"report:{number}")
            return BootstrapAttemptResult(
                Individual(
                    id=number,
                    generation=0,
                    parent_id=None,
                    passed=True,
                    commit=outcome.commit,
                ),
                f"report:{number}",
            )

    effects = GateEffects([None, None])
    seed = retry_bootstrap(3, effects)

    assert seed is not None
    assert seed.id == 2
    assert effects.events == [
        "begin:3",
        "attempt:1/3",
        "mutate",
        "judge",
        "gates",
        "close",
        "checkpoint:evolve: record failed bootstrap 1",
        "report:1",
        "attempt:2/3",
        "mutate",
        "judge",
        "gates",
        "measure",
        "snapshot",
        "close",
        "checkpoint:evolve: record bootstrap seed 2",
        "report:2",
    ]
