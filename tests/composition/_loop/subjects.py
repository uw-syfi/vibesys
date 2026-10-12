"""The strategies registered with the conformance suite.

A ported strategy adds one `Subject` here: its fresh-instance factory, its harness, its reply
schemas and a generator of scripted worlds. The properties themselves live in
`conformance.py` and are not edited per strategy.
"""

from __future__ import annotations

import json
from collections import deque
from typing import TYPE_CHECKING, Any

from hypothesis import strategies as st
from tests.composition._loop.conformance import Subject, World
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import FACTS, LIMITS, config
from tests.vibesys.orchestration.evolve.strategy._executors import Executors as EvolveExecutors
from tests.vibesys.orchestration.evolve.strategy._replies import mutated as evolve_mutated
from tests.vibesys.orchestration.evolve.strategy._replies import profiled as evolve_profiled
from tests.vibesys.orchestration.evolve.strategy._replies import reviewed as evolve_reviewed
from tests.vibesys.orchestration.evolve.strategy._run import config as evolve_config
from tests.vibesys.orchestration.evolve.strategy._run import harness as evolve_harness
from tests.vibesys.orchestration.issue_queue.strategy._executors import Executors as PlainExecutors
from tests.vibesys.orchestration.issue_queue.strategy._replies import (
    evaluated as plain_evaluated,
)
from tests.vibesys.orchestration.issue_queue.strategy._replies import (
    implemented as plain_implemented,
)
from tests.vibesys.orchestration.issue_queue.strategy._replies import judged as plain_judged
from tests.vibesys.orchestration.issue_queue.strategy._run import config as plain_config
from tests.vibesys.orchestration.issue_queue.strategy._run import harness as plain_harness
from tests.vibesys.orchestration.multi.strategy._executors import Executors as MultiExecutors
from tests.vibesys.orchestration.multi.strategy._replies import (
    implemented as multi_implemented,
)
from tests.vibesys.orchestration.multi.strategy._replies import (
    plan_reply as multi_plan_reply,
)
from tests.vibesys.orchestration.multi.strategy._replies import (
    pre_round_reply as multi_pre_round_reply,
)
from tests.vibesys.orchestration.multi.strategy._replies import (
    profiler_reply as multi_profiler_reply,
)
from tests.vibesys.orchestration.multi.strategy._replies import reviewed as multi_reviewed
from tests.vibesys.orchestration.multi.strategy._run import config as multi_config
from tests.vibesys.orchestration.multi.strategy._run import harness as multi_harness
from tests.vibesys.orchestration.single.strategy.world import SingleWorld

from vibesys.orchestration.dynamic.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.dynamic.strategy.api import (
    DynamicConfig,
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vibesys.orchestration.evolve.core_policy.api import reply_schemas as evolve_reply_schemas
from vibesys.orchestration.evolve.strategy.api import EvolveConfig, EvolveStrategy
from vibesys.orchestration.issue_queue.core_policy.api import reply_schemas as plain_reply_schemas
from vibesys.orchestration.issue_queue.strategy.api import IssueQueueConfig, IssueQueueStrategy
from vibesys.orchestration.multi.core_policy.api import reply_schemas as multi_reply_schemas
from vibesys.orchestration.multi.strategy.api import MultiConfig, MultiStrategy
from vs_core.api import RunEnvelope
from vs_core.testing.drive import Harness

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_core.api import CoreState, Request
    from vs_core.testing.drive import Answer

_DYNAMIC_CONFIG = config(max_in_flight=2)
_SERIAL_CONFIG = config(max_in_flight=1)
_THROUGHPUTS = (None, 30.0, 80.0, 120.0, 160.0)


class DynamicWorld:
    """The dynamic search's scripted executors, with each candidate's throughput drawn."""

    def __init__(
        self, hypotheses: int, throughputs: tuple[float | None, ...], passes: tuple[bool, ...]
    ) -> None:
        self._throughputs = throughputs
        self.executors = Executors(
            planner=deque([plan_reply(*(implement(f"h{n}") for n in range(hypotheses)))]),
            implementer=deque(implemented() for _ in range(8)),
            judge=deque(reviewed(passed=passes[n % len(passes)]) for n in range(8)),
            benchmark=self.headline,
        )

    def headline(self, commit: str) -> float | None:
        """The throughput the drawn table gives the candidate (a pure function of its commit)."""
        return self._throughputs[int(commit, 16) % len(self._throughputs)]

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """Answer as the scripted dynamic executors do."""
        return self.executors(request, core)


@st.composite
def _dynamic_world(draw: st.DrawFn) -> Callable[[], World]:
    hypotheses = draw(st.integers(min_value=1, max_value=2))
    throughputs = tuple(draw(st.lists(st.sampled_from(_THROUGHPUTS), min_size=1, max_size=4)))
    passes = tuple(draw(st.lists(st.booleans(), min_size=1, max_size=3)))
    return lambda: DynamicWorld(hypotheses, throughputs, passes)


def dynamic_subject(name: str, settings: DynamicConfig, *, sweeps_crashes: bool) -> Subject:
    """The dynamic search under ``settings``."""
    return Subject(
        name=name,
        new_strategy=lambda: DynamicStrategy(config=settings),
        harness=Harness(
            registry=dynamic_operation_registry(),
            facts=FACTS,
            limits=LIMITS,
            envelope_type=RunEnvelope[DynamicStrategyState],
            requirements=requirements_for(settings),
        ),
        schemas=reply_schemas(settings),
        worlds=_dynamic_world(),
        default_world=lambda: DynamicWorld(2, (120.0, 80.0), (True,)),
        smallest_world=lambda: DynamicWorld(1, (120.0,), (True,)),
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


_ATTRIBUTION = json.dumps(
    {
        "version": 1,
        "cost_unit": "ms",
        "components": [
            {"name": "gemm", "cost": 8.0, "share": 0.8, "evidence": ["a"]},
            {"name": "io", "cost": 2.0, "share": 0.2, "evidence": []},
        ],
    }
)


class MultiWorld:
    """The multi search's scripted executors, with each candidate's throughput drawn."""

    def __init__(
        self,
        throughputs: tuple[float | None, ...],
        passes: tuple[bool, ...],
        *,
        profiling: bool = False,
    ) -> None:
        self._throughputs = throughputs
        self.executors = MultiExecutors(
            pre_round=deque(multi_pre_round_reply(need_profile=profiling) for _ in range(8)),
            profiler=deque(multi_profiler_reply() for _ in range(8)),
            planner=deque(multi_plan_reply(f"h{n}") for n in range(8)),
            implementer=deque(
                multi_implemented(validation_recipe_artifact="validation/recipes.json")
                for _ in range(8)
            ),
            judge=deque(multi_reviewed(passed=passes[n % len(passes)]) for n in range(8)),
            benchmark=self.headline,
            attribution=_ATTRIBUTION if profiling else "",
        )

    def headline(self, commit: str) -> float | None:
        """The throughput the drawn table gives the candidate (a pure function of its commit)."""
        return self._throughputs[int(commit, 16) % len(self._throughputs)]

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """Answer as the scripted multi executors do."""
        return self.executors(request, core)


@st.composite
def _multi_world(draw: st.DrawFn, *, profiling: bool) -> Callable[[], World]:
    throughputs = tuple(draw(st.lists(st.sampled_from(_THROUGHPUTS), min_size=1, max_size=4)))
    passes = tuple(draw(st.lists(st.booleans(), min_size=1, max_size=3)))
    return lambda: MultiWorld(throughputs, passes, profiling=profiling)


def multi_subject(
    name: str, settings: MultiConfig, *, sweeps_crashes: bool, profiling: bool = False
) -> Subject:
    """The multi search under ``settings``."""
    return Subject(
        name=name,
        new_strategy=lambda: MultiStrategy(config=settings),
        harness=multi_harness(settings),
        schemas=multi_reply_schemas(),
        worlds=_multi_world(profiling=profiling),
        sweeps_crashes=sweeps_crashes,
        default_world=lambda: MultiWorld((120.0, 80.0), (True,), profiling=profiling),
        smallest_world=lambda: MultiWorld((120.0,), (True,), profiling=profiling),
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


class PlainWorld(PlainExecutors):
    """The issue queue's scripted roles and its board, which is a real owner over a fake tracker.

    The board operations are answered by `IssueBoardOwner.perform`, so a run reads and writes a
    board that keeps its state across the suite's restarts; the judge and the performance
    evaluator also file issues the run then works.
    """

    def __init__(self, passes: tuple[bool, ...], filings: tuple[tuple[str, ...], ...]) -> None:
        super().__init__(
            implementer=deque(plain_implemented(f"turn {n}") for n in range(24)),
            judge=deque(
                plain_judged(passed=passes[n % len(passes)], bugs=filings[n % len(filings)][:1])
                for n in range(24)
            ),
            perf=deque(plain_evaluated(*filings[n % len(filings)]) for n in range(8)),
        )


@st.composite
def _plain_world(draw: st.DrawFn) -> Callable[[], World]:
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
    return lambda: PlainWorld(passes, filings)


def plain_subject(name: str, settings: IssueQueueConfig, *, sweeps_crashes: bool) -> Subject:
    """The issue queue (`plain`) under ``settings``."""
    return Subject(
        name=name,
        new_strategy=lambda: IssueQueueStrategy(config=settings),
        harness=plain_harness(settings),
        schemas=plain_reply_schemas(settings),
        worlds=_plain_world(),
        default_world=lambda: PlainWorld((True,), (("a",),)),
        smallest_world=lambda: PlainWorld((True,), ((),)),
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


class EvolveWorld:
    """The evolve search's scripted executors: each child's verdict and throughput are drawn."""

    def __init__(self, throughputs: tuple[float | None, ...], passes: tuple[bool, ...]) -> None:
        self._throughputs = throughputs

        def replies(role: str, key: str, serial: int) -> str:
            del serial
            # A child's verdict is a pure function of its name, so it does not depend on the
            # order its siblings are asked in.
            passing = passes[sum(map(ord, key)) % len(passes)]
            if role == "mutator":
                return evolve_mutated(f"change of {key}")
            if role == "judge":
                return evolve_reviewed(passed=passing)
            return evolve_profiled()

        self.executors = EvolveExecutors(replies=replies, benchmark=self.headline)

    def headline(self, commit: str) -> float | None:
        """The throughput the drawn table gives the candidate (a pure function of its commit)."""
        return self._throughputs[int(commit, 16) % len(self._throughputs)]

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """Answer as the scripted evolve executors do."""
        return self.executors(request, core)


@st.composite
def _evolve_world(draw: st.DrawFn) -> Callable[[], World]:
    throughputs = tuple(draw(st.lists(st.sampled_from(_THROUGHPUTS), min_size=1, max_size=4)))
    passes = tuple(draw(st.lists(st.booleans(), min_size=1, max_size=3)))
    return lambda: EvolveWorld(throughputs, passes)


def evolve_subject(name: str, settings: EvolveConfig, *, sweeps_crashes: bool) -> Subject:
    """The evolve search under ``settings``."""
    return Subject(
        name=name,
        new_strategy=lambda: EvolveStrategy(config=settings),
        harness=evolve_harness(settings),
        schemas=evolve_reply_schemas(),
        worlds=_evolve_world(),
        sweeps_crashes=sweeps_crashes,
        default_world=lambda: EvolveWorld((120.0, 80.0), (True,)),
        smallest_world=lambda: EvolveWorld((120.0,), (True,)),
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
    "profiler_id": "linux_cpu",
    "profiler_support_name": "linux_cpu_profiler",
    "profiler_mcp_name": "vibesys-linux-cpu-profiler",
    "attribution": {"command": ("profile",), "timeout_seconds": 10},
}

SUBJECTS = {
    subject.name: subject
    for subject in (
        dynamic_subject("dynamic", _DYNAMIC_CONFIG, sweeps_crashes=False),
        dynamic_subject("dynamic-serial", _SERIAL_CONFIG, sweeps_crashes=True),
        single_subject("single", {"max_rounds": 1}, sweeps_crashes=True),
        single_subject(
            "single-cadence", {"max_rounds": 3, "official_eval_every": 2}, sweeps_crashes=False
        ),
        plain_subject("plain", plain_config(max_rounds=1), sweeps_crashes=True),
        plain_subject(
            "plain-rounds", plain_config(max_rounds=2, max_issues_per_round=2), sweeps_crashes=False
        ),
        multi_subject("multi", multi_config(max_rounds=1), sweeps_crashes=True),
        multi_subject(
            "multi-cadence",
            multi_config(max_rounds=3, official_eval_every=2, judge_every=2),
            sweeps_crashes=False,
        ),
        evolve_subject(
            "evolve",
            evolve_config(max_generations=1, children_per_generation=1, bootstrap_max_attempts=2),
            sweeps_crashes=True,
        ),
        # Concurrent children: the crash sweep covers a run whose slots overlap.
        evolve_subject(
            "evolve-parallel",
            evolve_config(
                max_generations=1,
                children_per_generation=2,
                bootstrap_max_attempts=2,
                max_parallelism=2,
                profiler_id="linux_cpu",
                profiler_support_name="linux_cpu_profiler",
                profiler_mcp_name="vibesys-linux-cpu-profiler",
            ),
            sweeps_crashes=True,
        ),
        evolve_subject(
            "evolve-generations",
            evolve_config(max_generations=3, children_per_generation=2, bootstrap_max_attempts=2),
            sweeps_crashes=False,
        ),
        multi_subject(
            "multi-guided",
            multi_config(max_rounds=1, **_GUIDED),
            sweeps_crashes=True,
            profiling=True,
        ),
    )
}
