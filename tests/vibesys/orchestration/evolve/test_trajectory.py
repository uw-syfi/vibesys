"""The population a given ``--seed`` produces is the population policy's own, whatever the order.

A scripted world decides what every candidate does from its name alone (see ``world.py``). The
flow runs against it with the far side answering open waits in generated orders, and the
population it ends with must equal the one the population policy builds when it is used serially
by hand: cold starts until one passes, then generations whose proposals are chosen from the
population as the generation began, and whose children are admitted in slot order. Concurrency and
completion order change nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve import world
from tests.vibesys.orchestration.evolve._host import Host, begin, config

from vibesys.metrics import EvidenceReading, MetricRow, MetricSpace, Objective
from vibesys.orchestration.evolve.models import JudgeResponse, MutatorResponse
from vibesys.orchestration.evolve.population import (
    CandidateOutcome,
    PopulationConfig,
    PopulationSearch,
    PopulationState,
    Proposal,
)
from vibesys.orchestration.evolve.search.api import ExportPatch, PatchExport
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.review import Verdict
from vs_core.api import EvidenceKind, EvidenceRef, Retained, RevisionRef
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vs_core.api.testing import Asked

GENERATIONS, CHILDREN, ATTEMPTS = 2, 3, 3
SEEDS = [(1, 3), (2, 7), (5, 11)]
Selector = Literal["vibesys", "openevolve"]


def commit_of(name: str) -> str:
    """The revision a candidate leaves."""
    return f"commit-{name}"


def name_of(commit: str) -> str:
    return commit.removeprefix("commit-")


def space_of(*, two: bool) -> MetricSpace:
    objectives = (Objective("throughput", "max"),)
    return MetricSpace(objectives=(*objectives, Objective("ttft_ms", "min")) if two else objectives)


class ScriptedHost(Host):
    """A host whose candidates do what the scripted world says they do."""

    seed_world: int = 0

    def key(self, asked: Asked) -> str:
        return asked.path.rstrip("/")

    def _mutator(self, asked: Asked, variant: int) -> None:
        del variant
        name = self.key(asked)
        reply = MutatorResponse(
            summary=world.child(self.seed_world, name).summary,
            hypothesis="faster",
            expected_behavior="up",
        )
        self.produced.append(name)
        self.run.reply(
            asked, reply, checkpoint=Retained(revision=RevisionRef.of_git_commit(commit_of(name)))
        )

    def _judge(self, asked: Asked, variant: int) -> None:
        del variant
        name = self.key(asked)
        passes = world.child(self.seed_world, name).judge_passes
        verdict = JudgeResponse(
            analysis="a",
            feedback="" if passes else f"fails {name}",
            verdict=Verdict.PASS if passes else Verdict.FAIL,
        )
        self.run.reply(asked, verdict)

    def _profiler(self, asked: Asked, variant: int) -> None:
        del variant
        cache = world.child(self.seed_world, self.key(asked)).cache_hit
        summary = ProfilerSummary(
            analysis="a", bottlenecks="b", suggestions="s", metrics={"cache_hit": cache}
        )
        self.run.reply(asked, summary)

    def _reading_of(self, ref: EvidenceRef) -> EvidenceReading:
        reading = super()._reading_of(ref)
        if ref.kind is not EvidenceKind.BENCHMARK:
            return reading
        child = world.child(self.seed_world, name_of(ref.candidate.revision_id.root))
        rows = (
            MetricRow(name="throughput", value=child.throughput, direction="max", unit="tokens/s"),
            MetricRow(name="ttft_ms", value=child.ttft_ms, direction="min", unit="ms"),
        )
        return reading.model_copy(update={"metrics": rows})

    def _export(self, request: ExportPatch) -> PatchExport:
        return PatchExport(
            status="succeeded", patch=f"patch for {request.revision.revision_id.root}"
        )


def scripted(
    seed_world: int, seed_rng: int, *, two: bool, selector: Selector, parallel: int
) -> Host:
    host = begin(
        config(
            max_generations=GENERATIONS,
            children_per_generation=CHILDREN,
            bootstrap_max_attempts=ATTEMPTS,
            k_top_inspirations=1,
            k_random_inspirations=1,
            seed=seed_rng,
            search_policy=selector,
            max_parallelism=parallel,
            profiling=True,
        ),
        restarting=False,
        host_type=ScriptedHost,
        metric_space=space_of(two=two),
    )
    assert isinstance(host, ScriptedHost)
    host.seed_world = seed_world
    return host


def reference(
    seed_world: int, seed_rng: int, space: MetricSpace, selector: Selector
) -> PopulationState:
    """The population policy used serially by hand, with no agent and no core."""
    search = PopulationSearch(
        PopulationConfig(
            space=space,
            selection_temperature=1.0,
            frontier_bias=0.7,
            k_top_inspirations=1,
            k_random_inspirations=1,
            selector=selector,
            seed=seed_rng,
        )
    )

    def outcome(name: str, proposal: Proposal | None) -> CandidateOutcome:
        child = world.child(seed_world, name)
        parent_id = None if proposal is None else proposal.parent.id
        inspiration_ids = (
            () if proposal is None else tuple(item.id for item in proposal.inspirations)
        )
        policy_parent_id = None if proposal is None else proposal.policy_parent_id
        target_island = None if proposal is None else proposal.target_island
        if not child.judge_passes:
            return CandidateOutcome(
                passed=False,
                feedback=f"fails {name}",
                commit=commit_of(name) if proposal is None else None,
                parent_id=parent_id,
                inspiration_ids=inspiration_ids,
                summary=child.summary,
                policy_parent_id=policy_parent_id,
                target_island=target_island,
            )
        return CandidateOutcome(
            passed=True,
            commit=commit_of(name),
            perf_metric=child.throughput,
            perf_unit="tokens/s",
            metrics=(
                ("cache_hit", child.cache_hit),
                ("throughput", child.throughput),
                ("ttft_ms", child.ttft_ms),
            ),
            code=f"patch for {commit_of(name)}",
            parent_id=parent_id,
            inspiration_ids=inspiration_ids,
            summary=child.summary,
            policy_parent_id=policy_parent_id,
            target_island=target_island,
        )

    state = search.initial()
    for name in world.bootstrap_names(seed_world, ATTEMPTS):
        _, state = search.admit(state, outcome(name, None))
    if search.needs_bootstrap(state):
        return state
    for generation in range(1, GENERATIONS + 1):
        state = search.end_generation(state)
        proposals, walk = [], state
        for _ in range(CHILDREN):
            proposal, walk = search.propose(walk)
            proposals.append(proposal)
        for slot, proposal in enumerate(proposals, start=1):
            if proposal is not None:
                _, state = search.admit(
                    state, outcome(world.child_name(generation, slot), proposal)
                )
    return state


def population(host: Host) -> PopulationState:
    assert host.record is not None
    return host.record.population


@pytest.mark.parametrize("selector", ["vibesys", "openevolve"])
@pytest.mark.parametrize(("seed_world", "seed_rng"), SEEDS)
def test_the_serial_run_builds_the_population_the_policy_builds_by_hand(
    seed_world: int, seed_rng: int, selector: Selector
) -> None:
    host = scripted(seed_world, seed_rng, two=seed_world == 2, selector=selector, parallel=1)
    host.drain()

    expected = reference(seed_world, seed_rng, space_of(two=seed_world == 2), selector)
    assert population(host) == expected


@settings(
    max_examples=examples(pr=10, nightly=12),
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    seeds=st.sampled_from(SEEDS),
    parallel=st.integers(1, 4),
    picks=st.lists(st.integers(0, 7), min_size=1, max_size=40),
    selector=st.sampled_from(["vibesys", "openevolve"]),
)
def test_the_population_does_not_depend_on_completion_order(
    seeds: tuple[int, int], parallel: int, picks: list[int], selector: Selector
) -> None:
    seed_world, seed_rng = seeds
    host = scripted(seed_world, seed_rng, two=False, selector=selector, parallel=parallel)
    for pick in picks:
        if not host.step(pick):
            break
    host.drain()

    assert population(host) == reference(seed_world, seed_rng, space_of(two=False), selector)
