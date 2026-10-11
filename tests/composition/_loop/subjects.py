"""The strategies registered with the conformance suite.

A ported strategy adds one `Subject` here: its fresh-instance factory, its harness, its reply
schemas and a generator of scripted worlds. The properties themselves live in
`conformance.py` and are not edited per strategy.
"""

from __future__ import annotations

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

from vibesys.orchestration.dynamic.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.dynamic.strategy.api import (
    DynamicConfig,
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
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


SUBJECTS = {
    subject.name: subject
    for subject in (
        dynamic_subject("dynamic", _DYNAMIC_CONFIG, sweeps_crashes=False),
        dynamic_subject("dynamic-serial", _SERIAL_CONFIG, sweeps_crashes=True),
    )
}
