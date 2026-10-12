"""The strategies registered with the conformance suite.

A ported strategy adds one `Subject` here: its fresh-instance factory, its harness, its reply
schemas and a generator of scripted worlds. The properties themselves live in
`conformance.py` and are not edited per strategy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypothesis import strategies as st
from tests.composition._loop.conformance import Subject, World
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy.world import DynamicWorld
from tests.vibesys.orchestration.evolve.strategy.world import EvolveWorld
from tests.vibesys.orchestration.issue_queue.strategy.world import (
    IssueQueueWorld,
)
from tests.vibesys.orchestration.issue_queue.strategy.world import (
    evaluated as plain_evaluated,
)
from tests.vibesys.orchestration.issue_queue.strategy.world import (
    implemented as plain_implemented,
)
from tests.vibesys.orchestration.issue_queue.strategy.world import judged as plain_judged
from tests.vibesys.orchestration.multi.strategy.world import (
    PROFILER,
    RECIPE,
    MultiWorld,
)
from tests.vibesys.orchestration.multi.strategy.world import implemented as multi_implemented
from tests.vibesys.orchestration.single.strategy.world import SingleWorld

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


_THROUGHPUTS = (None, 30.0, 80.0, 120.0, 160.0)


def _dynamic(
    hypotheses: int,
    throughputs: tuple[float | None, ...],
    passes: tuple[bool, ...],
    settings: Mapping[str, Any],
) -> DynamicWorld:
    """The dynamic search's world, with each candidate's throughput and verdict drawn."""
    return DynamicWorld(
        planner=[plan_reply(*(implement(f"h{n}") for n in range(hypotheses)))],
        implementer=[implemented() for _ in range(8)],
        judge=[reviewed(passed=passes[n % len(passes)]) for n in range(8)],
        throughputs=throughputs,
        **settings,
    )


@st.composite
def _dynamic_world(draw: st.DrawFn, settings: Mapping[str, Any]) -> Callable[[], World]:
    hypotheses = draw(st.integers(min_value=1, max_value=2))
    throughputs = tuple(draw(st.lists(st.sampled_from(_THROUGHPUTS), min_size=1, max_size=4)))
    passes = tuple(draw(st.lists(st.booleans(), min_size=1, max_size=3)))
    return lambda: _dynamic(hypotheses, throughputs, passes, settings)


def dynamic_subject(name: str, settings: Mapping[str, Any], *, sweeps_crashes: bool) -> Subject:
    """The dynamic search under the strategy config ``settings``."""
    setup = DynamicWorld(**settings).setup
    return Subject(
        name=name,
        new_strategy=setup.new_strategy,
        harness=setup.harness,
        schemas=setup.schemas,
        worlds=_dynamic_world(settings),
        default_world=lambda: _dynamic(2, (120.0, 80.0), (True,), settings),
        smallest_world=lambda: _dynamic(1, (120.0,), (True,), settings),
        sweeps_crashes=sweeps_crashes,
        issued=frozenset(
            {
                "adopt_revision",
                "close_attempt_scope",
                "close_session",
                "discard_workspace",
                "dispatch_turn",
                "ensure_session",
                "ensure_workspace",
                "execute_registered_operation",
                "observe_owned_job",
                "retain_revision",
                "snapshot_and_retain",
                "submit_measurement",
                "verify_adoption",
            }
        ),
    )


@st.composite
def _single_world(draw: st.DrawFn, settings: Mapping[str, Any]) -> Callable[[], World]:
    throughputs = tuple(draw(st.lists(st.sampled_from(_THROUGHPUTS), min_size=1, max_size=4)))
    passes = tuple(draw(st.lists(st.booleans(), min_size=1, max_size=3)))
    return lambda: SingleWorld(throughputs=throughputs, passes=passes, **settings)


def single_subject(name: str, settings: Mapping[str, Any], *, sweeps_crashes: bool) -> Subject:
    """The single search under the strategy config ``settings``."""
    setup = SingleWorld(**settings).setup
    return Subject(
        name=name,
        new_strategy=setup.new_strategy,
        harness=setup.harness,
        schemas=setup.schemas,
        worlds=_single_world(settings),
        sweeps_crashes=sweeps_crashes,
        default_world=lambda: SingleWorld(throughputs=(120.0, 80.0), **settings),
        smallest_world=lambda: SingleWorld(throughputs=(120.0,), **settings),
        issued=frozenset(
            {
                "adopt_revision",
                "close_attempt_scope",
                "close_session",
                "discard_workspace",
                "dispatch_turn",
                "ensure_session",
                "ensure_workspace",
                "execute_registered_operation",
                "observe_owned_job",
                "retain_revision",
                "snapshot_and_retain",
                "submit_measurement",
                "verify_adoption",
            }
        ),
    )


def _multi_world(
    settings: Mapping[str, Any],
    throughputs: tuple[float | None, ...],
    passes: tuple[bool, ...],
    *,
    profiling: bool,
) -> MultiWorld:
    """A multi world whose implementer names a validation recipe, so every run validates."""
    return MultiWorld(
        throughputs=throughputs,
        passes=passes,
        profiling=profiling,
        implementations=[multi_implemented(validation_recipe_artifact=RECIPE)] * 12,
        **settings,
    )


@st.composite
def _multi_worlds(
    draw: st.DrawFn, settings: Mapping[str, Any], *, profiling: bool
) -> Callable[[], World]:
    throughputs = tuple(draw(st.lists(st.sampled_from(_THROUGHPUTS), min_size=1, max_size=4)))
    passes = tuple(draw(st.lists(st.booleans(), min_size=1, max_size=3)))
    return lambda: _multi_world(settings, throughputs, passes, profiling=profiling)


def multi_subject(
    name: str, settings: Mapping[str, Any], *, sweeps_crashes: bool, profiling: bool = False
) -> Subject:
    """The multi search under the strategy config ``settings``."""
    setup = MultiWorld(**settings).setup
    return Subject(
        name=name,
        new_strategy=setup.new_strategy,
        harness=setup.harness,
        schemas=setup.schemas,
        worlds=_multi_worlds(settings, profiling=profiling),
        sweeps_crashes=sweeps_crashes,
        default_world=lambda: _multi_world(settings, (120.0, 80.0), (True,), profiling=profiling),
        smallest_world=lambda: _multi_world(settings, (120.0,), (True,), profiling=profiling),
        issued=frozenset(
            {
                "adopt_revision",
                "close_attempt_scope",
                "close_session",
                "discard_workspace",
                "dispatch_turn",
                "ensure_session",
                "ensure_workspace",
                "execute_registered_operation",
                "observe_owned_job",
                "retain_revision",
                "snapshot_and_retain",
                "submit_measurement",
                "verify_adoption",
            }
        ),
    )


def _plain_world(
    passes: tuple[bool, ...], filings: tuple[tuple[str, ...], ...], settings: Mapping[str, Any]
) -> IssueQueueWorld:
    """The issue queue's scripted roles and its board, which is a real owner over a fake tracker.

    The board operations are answered by `IssueBoardOwner.perform`, so a run reads and writes a
    board that keeps its state across the suite's restarts; the judge and the performance
    evaluator also file issues the run then works.
    """
    return IssueQueueWorld(
        implementer=[plain_implemented(f"turn {n}") for n in range(24)],
        judge=[
            plain_judged(passed=passes[n % len(passes)], bugs=filings[n % len(filings)][:1])
            for n in range(24)
        ],
        perf=[plain_evaluated(*filings[n % len(filings)]) for n in range(8)],
        **settings,
    )


@st.composite
def _plain_worlds(draw: st.DrawFn, settings: Mapping[str, Any]) -> Callable[[], World]:
    passes = tuple(draw(st.lists(st.booleans(), min_size=1, max_size=3)))
    filings = tuple(
        draw(
            st.lists(
                st.lists(st.sampled_from(("a", "b", "c")), max_size=2, unique=True).map(tuple),
                min_size=1,
                max_size=3,
            )
        )
    )
    return lambda: _plain_world(passes, filings, settings)


def plain_subject(name: str, settings: Mapping[str, Any], *, sweeps_crashes: bool) -> Subject:
    """The issue queue (`plain`) under the strategy config ``settings``."""
    setup = IssueQueueWorld(**settings).setup
    return Subject(
        name=name,
        new_strategy=setup.new_strategy,
        harness=setup.harness,
        schemas=setup.schemas,
        worlds=_plain_worlds(settings),
        default_world=lambda: _plain_world((True,), (("a",),), settings),
        smallest_world=lambda: _plain_world((True,), ((),), settings),
        sweeps_crashes=sweeps_crashes,
        issued=frozenset(
            {
                "adopt_revision",
                "close_session",
                "dispatch_turn",
                "ensure_session",
                "ensure_workspace",
                "execute_registered_operation",
                "observe_owned_job",
                "snapshot_and_retain",
                "submit_measurement",
                "verify_adoption",
            }
        ),
    )


@st.composite
def _evolve_world(draw: st.DrawFn, settings: Mapping[str, Any]) -> Callable[[], World]:
    throughputs = tuple(draw(st.lists(st.sampled_from(_THROUGHPUTS), min_size=1, max_size=4)))
    passes = tuple(draw(st.lists(st.booleans(), min_size=1, max_size=3)))
    return lambda: EvolveWorld(throughputs=throughputs, passes=passes, **settings)


def evolve_subject(name: str, settings: Mapping[str, Any], *, sweeps_crashes: bool) -> Subject:
    """The evolve search under the strategy config ``settings``."""
    setup = EvolveWorld(**settings).setup
    return Subject(
        name=name,
        new_strategy=setup.new_strategy,
        harness=setup.harness,
        schemas=setup.schemas,
        worlds=_evolve_world(settings),
        sweeps_crashes=sweeps_crashes,
        default_world=lambda: EvolveWorld(throughputs=(120.0, 80.0), **settings),
        smallest_world=lambda: EvolveWorld(throughputs=(120.0,), **settings),
        issued=frozenset(
            {
                "adopt_revision",
                "close_attempt_scope",
                "close_session",
                "discard_workspace",
                "dispatch_turn",
                "ensure_session",
                "ensure_workspace",
                "execute_registered_operation",
                "observe_owned_job",
                "retain_revision",
                "snapshot_and_retain",
                "submit_measurement",
                "verify_adoption",
            }
        ),
    )


_GUIDED = {
    **PROFILER,
    "attribution": {"command": ("profile",), "timeout_seconds": 10},
}

SUBJECTS = {
    subject.name: subject
    for subject in (
        dynamic_subject("dynamic", {"max_in_flight": 2}, sweeps_crashes=False),
        dynamic_subject("dynamic-serial", {"max_in_flight": 1}, sweeps_crashes=True),
        single_subject("single", {"max_rounds": 1}, sweeps_crashes=True),
        single_subject(
            "single-cadence", {"max_rounds": 3, "official_eval_every": 2}, sweeps_crashes=False
        ),
        plain_subject("plain", {"max_rounds": 1}, sweeps_crashes=True),
        plain_subject(
            "plain-rounds", {"max_rounds": 2, "max_issues_per_round": 2}, sweeps_crashes=False
        ),
        multi_subject("multi", {"max_rounds": 1}, sweeps_crashes=True),
        multi_subject(
            "multi-cadence",
            {"max_rounds": 3, "official_eval_every": 2, "judge_every": 2},
            sweeps_crashes=False,
        ),
        evolve_subject(
            "evolve",
            {"max_generations": 1, "children_per_generation": 1, "bootstrap_max_attempts": 2},
            sweeps_crashes=True,
        ),
        # Concurrent children: the crash sweep covers a run whose slots overlap.
        evolve_subject(
            "evolve-parallel",
            {
                "max_generations": 1,
                "children_per_generation": 2,
                "bootstrap_max_attempts": 2,
                "max_parallelism": 2,
                "profiler_id": "linux_cpu",
                "profiler_support_name": "linux_cpu_profiler",
                "profiler_mcp_name": "vibesys-linux-cpu-profiler",
            },
            sweeps_crashes=True,
        ),
        evolve_subject(
            "evolve-generations",
            {"max_generations": 3, "children_per_generation": 2, "bootstrap_max_attempts": 2},
            sweeps_crashes=False,
        ),
        multi_subject(
            "multi-guided",
            {"max_rounds": 1, **_GUIDED},
            sweeps_crashes=True,
            profiling=True,
        ),
    )
}
