"""The strategies registered with the conformance suite.

A ported strategy adds one `Subject` here: its fresh-instance factory, its harness, its reply
schemas and a generator of scripted worlds. The properties themselves live in
`conformance.py` and are not edited per strategy.
"""

from __future__ import annotations

import json
from collections import deque
from typing import TYPE_CHECKING

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
from tests.vibesys.orchestration.single.strategy._executors import Executors as SingleExecutors
from tests.vibesys.orchestration.single.strategy._replies import combined as single_combined
from tests.vibesys.orchestration.single.strategy._replies import plan_reply as single_plan_reply
from tests.vibesys.orchestration.single.strategy._run import config as single_config
from tests.vibesys.orchestration.single.strategy._run import harness as single_harness

from vibesys.orchestration.dynamic.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.dynamic.strategy.api import (
    DynamicConfig,
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vibesys.orchestration.multi.core_policy.api import reply_schemas as multi_reply_schemas
from vibesys.orchestration.multi.strategy.api import MultiConfig, MultiStrategy
from vibesys.orchestration.single.core_policy.api import reply_schemas as single_reply_schemas
from vibesys.orchestration.single.strategy.api import SingleConfig, SingleStrategy
from vs_core.api import RunEnvelope
from vs_core.testing.drive import Harness

if TYPE_CHECKING:
    from collections.abc import Callable

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


class SingleWorld:
    """The single search's scripted executors, with each candidate's throughput drawn."""

    def __init__(self, throughputs: tuple[float | None, ...], passes: tuple[bool, ...]) -> None:
        self._throughputs = throughputs
        self.executors = SingleExecutors(
            planner=deque(single_plan_reply(f"h{n}") for n in range(8)),
            implementer=deque(single_combined(passed=passes[n % len(passes)]) for n in range(8)),
            benchmark=self.headline,
        )

    def headline(self, commit: str) -> float | None:
        """The throughput the drawn table gives the candidate (a pure function of its commit)."""
        return self._throughputs[int(commit, 16) % len(self._throughputs)]

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """Answer as the scripted single executors do."""
        return self.executors(request, core)


@st.composite
def _single_world(draw: st.DrawFn) -> Callable[[], World]:
    throughputs = tuple(draw(st.lists(st.sampled_from(_THROUGHPUTS), min_size=1, max_size=4)))
    passes = tuple(draw(st.lists(st.booleans(), min_size=1, max_size=3)))
    return lambda: SingleWorld(throughputs, passes)


def single_subject(name: str, settings: SingleConfig, *, sweeps_crashes: bool) -> Subject:
    """The single search under ``settings``."""
    return Subject(
        name=name,
        new_strategy=lambda: SingleStrategy(config=settings),
        harness=single_harness(settings),
        schemas=single_reply_schemas(),
        worlds=_single_world(),
        sweeps_crashes=sweeps_crashes,
        default_world=lambda: SingleWorld((120.0, 80.0), (True,)),
        smallest_world=lambda: SingleWorld((120.0,), (True,)),
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
        single_subject("single", single_config(max_rounds=1), sweeps_crashes=True),
        single_subject(
            "single-cadence",
            single_config(max_rounds=3, official_eval_every=2),
            sweeps_crashes=False,
        ),
        multi_subject("multi", multi_config(max_rounds=1), sweeps_crashes=True),
        multi_subject(
            "multi-cadence",
            multi_config(max_rounds=3, official_eval_every=2, judge_every=2),
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
