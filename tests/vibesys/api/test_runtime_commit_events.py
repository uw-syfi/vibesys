"""A confirmed core commit derives round and experiment events strategy-agnostically.

After each confirmed commit the run diffs the previous projected `RunView` against the new
one and emits `ROUND_FINISHED` for newly observed rounds and `EXPERIMENTS_CHANGED` when what
the projection shows changed. These tests never reference a specific strategy ID: the
projector is a synthetic one over a minimal state.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from vibesys.events import CoreEvent, CoreEventType
from vibesys.run.contracts import RoundSummary, RunStatus, RunView
from vibesys.run.integration import LocalRunIntegration


class _FakeAgentState(BaseModel):
    """Minimal typed state carrying only what the projector needs."""

    round_numbers: tuple[int, ...] = ()
    active: int = 0
    revision: int = 0
    profile_skipped: bool = False


class _FakeProjector:
    """Project `_FakeAgentState` into a view whose payload is what a frontend would show."""

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        if namespace != "commit-probe" or not isinstance(state, _FakeAgentState):
            return None
        return _agent_view(state, run_id=run_id)


def _agent_view(state: _FakeAgentState, *, run_id: str = "probe") -> RunView:
    return RunView(
        run_id=run_id,
        loop="commit-probe",
        status=RunStatus.ACTIVE,
        projection={"rounds": list(state.round_numbers), "active": state.active},
        rounds=tuple(
            RoundSummary(
                number=number,
                status="completed",
                attempts=1,
                judge_verdict="pass",
                profile_skipped=state.profile_skipped,
            )
            for number in state.round_numbers
        ),
        # A core run stamps every projection with its commit revision.
        experiment_revision=state.revision,
    )


def test_commit_projection_preserves_profile_skip_and_publication_order() -> None:
    integration = LocalRunIntegration()
    order: list[str] = []
    captured: list[CoreEvent] = []

    def capture_event(event: CoreEvent) -> None:
        order.append(event.type.value)
        captured.append(event)

    integration.add_committed_state_listener(
        lambda _namespace, _state, _changed: order.append("state_published")
    )
    integration.events.subscribe(capture_event)
    observer = integration.core_commit_observer("probe", _FakeProjector(), "commit-probe")

    observer.committed(None, _FakeAgentState(round_numbers=(1,), revision=1, profile_skipped=True))

    round_finished = next(event for event in captured if event.type is CoreEventType.ROUND_FINISHED)
    assert round_finished.data is not None
    assert round_finished.data.kind == "round_finished"
    assert round_finished.data.profile_skipped is True
    assert order[:2] == ["state_published", CoreEventType.ROUND_FINISHED.value]


@given(
    steps=st.lists(
        st.tuples(st.integers(min_value=0, max_value=3), st.integers(min_value=0, max_value=2)),
        min_size=0,
        max_size=12,
    ),
    resume_at=st.integers(min_value=0, max_value=12),
)
@settings(max_examples=100)
def test_commit_events_property_no_double_emission_and_experiments_iff_changed(
    steps: list[tuple[int, int]], resume_at: int
) -> None:
    """Across any sequence of commits, each round fires once and experiments fire iff changed.

    ``steps`` pairs the rounds a commit adds with how far it moves the active hypothesis.
    ``resume_at`` recomputes the previous state from its durable value, as a restarted
    process does, which must be indistinguishable from the in-memory object.
    """
    round_numbers: list[int] = []
    active = 0
    states: list[_FakeAgentState] = [_FakeAgentState()]
    for revision, (new_rounds, active_delta) in enumerate(steps, start=1):
        next_round = (round_numbers[-1] + 1) if round_numbers else 1
        round_numbers.extend(range(next_round, next_round + new_rounds))
        active += active_delta
        states.append(
            _FakeAgentState(round_numbers=tuple(round_numbers), active=active, revision=revision)
        )

    integration = LocalRunIntegration()
    observer = integration.core_commit_observer("probe", _FakeProjector(), "commit-probe")
    captured: list[CoreEvent] = []
    integration.events.subscribe(captured.append)
    for index in range(1, len(states)):
        before = states[index - 1]
        if index == resume_at:
            before = _FakeAgentState.model_validate(before.model_dump())
        observer.committed(before, states[index])

    announced = [
        int((event.round_label or "").removeprefix("round-"))
        for event in captured
        if event.type is CoreEventType.ROUND_FINISHED
    ]
    assert announced == list(round_numbers)

    changed_count = sum(1 for event in captured if event.type is CoreEventType.EXPERIMENTS_CHANGED)
    expected_changes = sum(
        1
        for index in range(1, len(states))
        if (states[index - 1].round_numbers, states[index - 1].active)
        != (states[index].round_numbers, states[index].active)
    )
    assert changed_count == expected_changes


def test_a_commit_of_another_namespace_projects_nothing() -> None:
    integration = LocalRunIntegration()
    captured: list[CoreEvent] = []
    integration.events.subscribe(captured.append)
    observer = integration.core_commit_observer("probe", _FakeProjector(), "other-namespace")

    observer.committed(None, _FakeAgentState(round_numbers=(1,), revision=1))

    assert not [event for event in captured if event.type is CoreEventType.ROUND_FINISHED]
