"""The run's events carry what core compared and warn of a runaway count of evaluations, once each.

Core logs a comparison of a candidate with its base, and a runaway count of an agent's
evaluations, as the run goes. The commit observer of the production host turns each one core
newly logged into one event, in the order core logged them, however the commits cut the log.
The comparisons and counts here are generated; the verdicts are core's (the comparison tests of
`vs-core`), and nothing here decides one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.real_git import NO_GIT, NO_GITHUB
from tests.system.agent_tools.world import AgentToolWorld, Turn

from vibesys.events import CoreEvent, CoreEventType, FrameworkWarningData, MeasurementComparedData
from vibesys.run.integration import LocalRunIntegration
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    Comparison,
    ComparisonVerdict,
    CoreState,
    EventCursor,
    FlowState,
    HostFence,
    HostId,
    RevisionRef,
    Runaway,
    RunEnvelope,
    Scope,
)
from vs_dst.api import StepLoopHosting, simulate
from vs_runtime.api.core import RuntimeRecord
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

RUN_ID = "events"
SCOPE = Scope.model_validate({"owner": {"root": "attempt-0", "kind": "attempt"}, "generation": 0})
_FLOATS = st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False)
_COMPARISONS = st.builds(
    Comparison,
    scope=st.just(SCOPE),
    metric=st.just("throughput"),
    unit=st.sampled_from((None, "tokens/s")),
    direction=st.sampled_from(("max", "min")),
    candidate=st.integers(0, 15).map(lambda n: RevisionRef.of_git_commit(f"{n:x}" * 40)),
    candidate_input=st.just("candidate-input"),
    base=st.just(RevisionRef.of_git_commit("ab" * 20)),
    base_input=st.just("base-input"),
    candidate_value=_FLOATS,
    base_value=_FLOATS,
    ratio=_FLOATS,
    noise=_FLOATS,
    verdict=st.sampled_from(ComparisonVerdict),
    candidate_samples=st.integers(0, 6),
    base_samples=st.integers(0, 6),
)
_RUNAWAYS = st.builds(
    Runaway, scope=st.just(SCOPE), evaluations=st.integers(1, 50), limit=st.integers(1, 50)
)


@pytest.fixture(scope="module")
def finished(tmp_path_factory: pytest.TempPathFactory) -> tuple[CoreState, FlowState]:
    """A run on the step loop that ended, whose core state and search state the records are made of."""
    directory: Path = tmp_path_factory.mktemp("events")
    run = simulate(AgentToolWorld(directory, (Turn(),), total=1), StepLoopHosting())
    assert run.finished
    assert isinstance(run.state, FlowState)
    return run.core, run.state


def _record(state: CoreState, search: FlowState) -> RuntimeRecord[FlowState]:
    declaration = state.run.declaration
    assert declaration is not None
    return RuntimeRecord[FlowState].fresh(
        RunEnvelope[FlowState](
            schema_version=ENVELOPE_SCHEMA_VERSION,
            fence=HostFence(host_id=HostId(root="events"), epoch=1),
            strategy_id=declaration.strategy_id,
            state_schema=declaration.state_schema,
            core=state,
            strategy=search,
            event_cursor=EventCursor(sequence=0),
        )
    )


def _heard(
    finished: tuple[CoreState, FlowState],
    commits: list[tuple[tuple[Comparison, ...], tuple[Runaway, ...]]],
) -> list[CoreEvent]:
    """The events the observer publishes as the log grows by each of ``commits`` in turn."""
    core, search = finished
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=NO_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )
    heard: list[CoreEvent] = []
    integration.events.subscribe(heard.append)
    observer = integration.core_commit_observer(RUN_ID, None, "events")
    previous: RuntimeRecord[FlowState] | None = None
    for comparisons, runaways in commits:
        evaluation = core.evaluation.model_copy(
            update={"comparisons": comparisons, "runaways": runaways}
        )
        current = _record(core.model_copy(update={"evaluation": evaluation}), search)
        observer.committed(previous, current)
        previous = current
    return heard


def _cuts(items: tuple[object, ...], cuts: list[int]) -> list[int]:
    """The sizes the log has at each commit: growing, ending at all of ``items``."""
    return sorted({min(cut, len(items)) for cut in cuts} | {len(items)})


@given(
    comparisons=st.lists(_COMPARISONS, max_size=5).map(tuple),
    runaways=st.lists(_RUNAWAYS, max_size=3).map(tuple),
    cuts=st.lists(st.integers(0, 6), max_size=4),
)
def test_each_comparison_and_runaway_core_logs_is_one_event_in_order_whatever_the_commits(
    finished: tuple[CoreState, FlowState],
    comparisons: tuple[Comparison, ...],
    runaways: tuple[Runaway, ...],
    cuts: list[int],
) -> None:
    sizes = _cuts(comparisons, cuts)
    commits = [(comparisons[:size], runaways[: min(size, len(runaways))]) for size in sizes]
    commits[-1] = (comparisons, runaways)

    heard = _heard(finished, commits)

    emitted = [
        event.data
        for event in heard
        if event.type is CoreEventType.MEASUREMENT_COMPARED
        and isinstance(event.data, MeasurementComparedData)
    ]
    assert len(emitted) == len(comparisons)
    for data, comparison in zip(emitted, comparisons, strict=True):
        assert data.verdict is comparison.verdict
        assert data.ratio == comparison.ratio
        assert data.noise == comparison.noise
        assert data.direction == comparison.direction
        assert data.metric == comparison.metric
        assert data.base_commit == comparison.base.revision_id.root
        assert data.candidate_samples == comparison.candidate_samples
        assert data.base_samples == comparison.base_samples
    warned = [
        event.data
        for event in heard
        if event.type is CoreEventType.FRAMEWORK_WARNING
        and isinstance(event.data, FrameworkWarningData)
        and event.data.source_label == "evaluations"
    ]
    assert len(warned) == len(runaways)
    for data, runaway in zip(warned, runaways, strict=True):
        assert f"{runaway.evaluations} evaluations" in (data.detail or "")
        assert f"warning at {runaway.limit}" in (data.detail or "")
